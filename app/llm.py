"""
OpenRouter client with automatic free-model selection and fallback.

Free models on OpenRouter come and go and are rate-limited, so instead of hard-coding one:
  1. on startup, ask OpenRouter which ':free' models exist right now
  2. rank them by a preference list (strong instruction-following models first)
  3. on each request, try them in order; on 429 / 5xx / timeout / empty reply, move to the next
Set OPENROUTER_MODELS="model1,model2" to override the automatic choice.
"""
import logging
import os
import time

import httpx

log = logging.getLogger("llm")
API = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
KEY = os.getenv("OPENROUTER_API_KEY", "")

PREFERENCE = [  # substrings, best first; anything else free is used after these
    "llama-3.3-70b", "deepseek-chat", "deepseek-v3", "qwen3", "qwen-2.5-72b", "gpt-oss",
    "gemini", "mistral-small", "llama-4", "gemma-3-27b", "mistral",
]
FALLBACK_MODELS = [  # used only if the model list cannot be fetched
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "mistralai/mistral-small-3.2-24b-instruct:free",
]

_models: list[str] = []
_cooldown: dict[str, float] = {}  # model -> time until which we skip it


def discover_models(limit=5):
    env = [m.strip() for m in os.getenv("OPENROUTER_MODELS", "").split(",") if m.strip()]
    if env:
        return env
    try:
        r = httpx.get(f"{API}/models", timeout=15)
        r.raise_for_status()
        free = []
        for m in r.json().get("data", []):
            mid = m.get("id", "")
            p = m.get("pricing") or {}
            is_free = mid.endswith(":free") or (str(p.get("prompt")) == "0" and str(p.get("completion")) == "0")
            modality = (m.get("architecture") or {}).get("output_modalities") or ["text"]
            if is_free and "text" in modality and (m.get("context_length") or 0) >= 16000:
                free.append(mid)

        def rank(mid):
            for i, pref in enumerate(PREFERENCE):
                if pref in mid:
                    return i
            return len(PREFERENCE)

        free.sort(key=rank)
        if free:
            return free[:limit]
    except Exception as e:
        log.warning("could not fetch model list (%s); using built-in fallback list", e)
    return FALLBACK_MODELS


def models():
    global _models
    if not _models:
        _models = discover_models()
        log.info("free models in use: %s", _models)
    return _models


class LLMError(Exception):
    pass


def _merge_system(messages):
    """Some free models reject the 'system' role: fold it into the first user message."""
    sys = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [dict(m) for m in messages if m["role"] != "system"]
    if sys and rest:
        rest[0]["content"] = f"{sys}\n\n---\n\n{rest[0]['content']}"
    return rest


def chat(messages, max_tokens=700, temperature=0.2):
    """Returns (answer_text, model_used). Raises LLMError if every model fails."""
    if not KEY:
        raise LLMError("OPENROUTER_API_KEY is not set on the server.")
    headers = {
        "Authorization": f"Bearer {KEY}",
        "HTTP-Referer": os.getenv("APP_URL", "https://huggingface.co/spaces"),
        "X-Title": "DarGlobal & Wasalt Property Assistant",
    }
    errors = []
    now = time.time()
    candidates = [m for m in models() if _cooldown.get(m, 0) < now] or models()
    for model in candidates:
        for msgs in (messages, _merge_system(messages)):
            try:
                r = httpx.post(f"{API}/chat/completions", headers=headers, timeout=60, json={
                    "model": model, "messages": msgs,
                    "max_tokens": max_tokens, "temperature": temperature,
                })
            except httpx.HTTPError as e:
                errors.append(f"{model}: {type(e).__name__}")
                break
            if r.status_code == 400 and msgs is messages:
                continue  # retry same model with the system prompt merged in
            if r.status_code in (402, 429) or r.status_code >= 500:
                _cooldown[model] = time.time() + (120 if r.status_code == 429 else 600)
                errors.append(f"{model}: HTTP {r.status_code}")
                break
            if r.status_code != 200:
                errors.append(f"{model}: HTTP {r.status_code} {r.text[:120]}")
                break
            data = r.json()
            if data.get("error"):
                errors.append(f"{model}: {str(data['error'])[:120]}")
                _cooldown[model] = time.time() + 120
                break
            try:
                text = (data["choices"][0]["message"].get("content") or "").strip()
            except (KeyError, IndexError):
                text = ""
            if text:
                return text, data.get("model", model)
            errors.append(f"{model}: empty reply")
            break
    log.warning("all models failed: %s", errors)
    raise LLMError("; ".join(errors))
