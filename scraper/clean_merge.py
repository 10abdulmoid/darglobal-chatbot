#!/usr/bin/env python3
"""
Clean + merge the scraped data into one chatbot knowledge file.

    data/raw/darglobal.jsonl  +  data/raw/wasalt.jsonl  (+ data/curated/*.md, optional)
        -> data/processed/knowledge.jsonl   (one chunk per line, ready for search)

Run from the repo root (venv active):
    python scraper\\clean_merge.py

Steps:
  1. drop empty/error pages and exact-duplicate pages (same content)
  2. remove site-wide boilerplate lines (nav/footer text repeated on many pages)
  3. cap oversized pages (e.g. Wasalt category grids of 60k+ chars)
  4. split pages into ~1,200-char chunks with a small overlap, on paragraph boundaries
  5. drop duplicate chunks, write knowledge.jsonl, print stats
"""
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

RAW = [Path("data/raw/darglobal.jsonl"), Path("data/raw/wasalt.jsonl")]
CURATED = Path("data/curated")
OUT = Path("data/processed/knowledge.jsonl")

CHUNK = 1200          # target chars per chunk
OVERLAP = 150         # chars carried over between chunks
MIN_CHUNK = 120       # drop chunks smaller than this
BOILER_SHARE = 0.25   # a short line on >25% of a source's pages is boilerplate
CAP = {               # max chars kept per page, by record type
    "category": 6000,
    "project_listing": 12000,
    "listing": 4000,
    "default": 30000,
}
ERROR_MARK = "application error: a client-side exception"


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def load():
    recs = []
    for p in RAW:
        if not p.exists():
            print(f"! missing {p}, skipping")
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                recs.append(json.loads(line))
    if CURATED.exists():  # hand-checked facts files rank as their own type
        for md in sorted(CURATED.glob("*.md")):
            text = md.read_text(encoding="utf-8")
            src = "wasalt" if "wasalt" in md.stem.lower() else "darglobal"
            recs.append({"source": src, "type": "curated", "url": f"curated/{md.name}",
                         "title": md.stem.replace("_", " ").title(), "content": text,
                         "metadata": {}})
    return recs


# template-heavy pages: lines like "3", "Riyadh" or "price: 900000" repeat across
# many of them but are real data, so the repeated-line filter must not touch them
NO_BOILER_TYPES = {"curated", "listing", "category"}
FACT_LINE = re.compile(r"^[A-Za-z]+: \S|^Key facts:$")


def boilerplate_lines(recs):
    """Short lines that repeat across a large share of one source's pages."""
    per_source = defaultdict(Counter)
    pages = Counter()
    for r in recs:
        if r["type"] in NO_BOILER_TYPES:
            continue
        pages[r["source"]] += 1
        for ln in set(l.strip() for l in r["content"].splitlines()):
            if 0 < len(ln) < 200:
                per_source[r["source"]][ln] += 1
    bad = {}
    for src, counts in per_source.items():
        limit = max(5, BOILER_SHARE * pages[src])
        bad[src] = {ln for ln, c in counts.items() if c >= limit}
    return bad


def split(text):
    """Chunk on paragraph/line boundaries, ~CHUNK chars with OVERLAP."""
    parts = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks, cur = [], ""
    for p in parts:
        while len(p) > CHUNK:  # very long paragraph: cut on sentence ends
            cut = p.rfind(". ", 0, CHUNK)
            cut = cut + 1 if cut > CHUNK // 2 else CHUNK
            parts_head, p = p[:cut].strip(), p[cut:].strip()
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(parts_head)
        if len(cur) + len(p) + 1 > CHUNK and cur:
            chunks.append(cur)
            cur = cur[-OVERLAP:].split(" ", 1)[-1] + "\n" + p if OVERLAP else p
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return [c.strip() for c in chunks if len(c.strip()) >= MIN_CHUNK]


def main():
    recs = load()
    print(f"Loaded {len(recs)} pages")

    # 1. drop empty / error pages and exact duplicate pages
    seen, kept, dup_pages = set(), [], 0
    for r in recs:
        c = r.get("content") or ""
        if len(c) < 100 or ERROR_MARK in c.lower():
            continue
        h = hashlib.md5(norm(c).encode()).hexdigest()
        if h in seen:
            dup_pages += 1
            continue
        seen.add(h)
        kept.append(r)
    print(f"Dropped {len(recs) - len(kept) - dup_pages} empty/error pages, {dup_pages} duplicate pages")

    # 2. strip boilerplate lines
    bad = boilerplate_lines(kept)
    for src, lines in bad.items():
        print(f"Boilerplate lines removed for {src}: {len(lines)}")
    for r in kept:
        lines = [l.strip() for l in r["content"].splitlines()]
        if r["type"] in NO_BOILER_TYPES:
            r["content"] = "\n".join(l for l in lines if l)
            continue
        b = bad.get(r["source"], set())
        r["content"] = "\n".join(l for l in lines if l and (l not in b or FACT_LINE.match(l)))

    # 3-5. cap, chunk, dedupe chunks
    OUT.parent.mkdir(parents=True, exist_ok=True)
    seen_chunks, n_chunks, by = set(), 0, Counter()
    with OUT.open("w", encoding="utf-8") as f:
        for r in kept:
            text = r["content"][: CAP.get(r["type"], CAP["default"])]
            title = (r.get("title") or "").strip()
            for i, ch in enumerate(split(text)):
                h = hashlib.md5(norm(ch).encode()).hexdigest()
                if h in seen_chunks:
                    continue
                seen_chunks.add(h)
                meta = r.get("metadata") or {}
                f.write(json.dumps({
                    "id": f"{hashlib.md5(r['url'].encode()).hexdigest()[:12]}-{i}",
                    "source": r["source"],
                    "type": r["type"],
                    "url": r["url"],
                    "title": title,
                    "chunk": i,
                    "published": meta.get("published"),
                    "text": ch,
                }, ensure_ascii=False) + "\n")
                n_chunks += 1
                by[(r["source"], r["type"])] += 1

    print(f"\n=== DONE === {len(kept)} pages -> {n_chunks} chunks  ->  {OUT}")
    for (src, typ), n in sorted(by.items()):
        print(f"  {src:<10} {typ:<16} {n:>5} chunks")


if __name__ == "__main__":
    main()
