#!/usr/bin/env python3
"""
Retry the Dar Global pages the first scrape missed (failed, dropped, or thin),
using a REAL browser (Edge / Chrome / Brave) instead of the bundled Chromium.

Run from the repo root, with the venv active, AFTER the main scrape:
    python scraper\\retry_missing.py                   # tries Edge, then Chrome
    python scraper\\retry_missing.py --limit 3         # quick test on 3 pages
    python scraper\\retry_missing.py --browser-path "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"

It only touches pages that are missing from data/raw/darglobal.jsonl or have
< MIN_CHARS of text, and merges richer results back into that file. A backup
is written to data/raw/darglobal.jsonl.bak first. A browser window opens and
runs by itself: leave it alone (use --headless to hide it).

How it copes with pages that crash ("Application error: a client-side exception"):
  1. ad/analytics trackers are always blocked (they trigger many of the crashes)
  2. if the page still shows the error, it is reloaded with nothing else blocked
  3. whatever happens, JSON API responses the page fetched are captured, and the
     text inside them is used when the rendered page is empty or crashed

Needs scraper.py (the main scraper) in the same folder.
"""
import argparse
import hashlib
import json
import re
import shutil
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from scraper import (  # noqa: E402  (main scraper module, same folder)
    BOILERPLATE, DROP_CHARS, MIN_CHARS, OUT_FILE, RENDERED, SKIP_PATHS, UA,
    classify, extract, get_sitemap_urls, load_robots,
)

ERROR_MARK = "application error: a client-side exception"

# third-party ad/analytics hosts: never needed for page text, often cause crashes/loops
TRACKERS = (
    "google-analytics", "googletagmanager", "doubleclick", "adservice.google",
    "googleadservices", "googlesyndication", "facebook.net", "facebook.com/tr",
    "connect.facebook", "hotjar", "clarity.ms", "snapchat", "tiktok", "analytics.",
    "linkedin.com/px", "licdn.com", "ads-twitter", "bing.com/bat", "taboola",
    "outbrain", "cookiebot", "onetrust", "segment.", "mixpanel", "sentry.io",
    "intercom", "hubspot", "zdassets", "tawk.to", "clickcease",
)


def is_error_page(text):
    return ERROR_MARK in (text or "").lower()


def json_text(bodies):
    """Pull human-readable strings out of captured JSON API responses."""
    out = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and len(o) > 25 and " " in o and not o.startswith(("http", "/", "data:")):
            s = re.sub(r"<[^>]+>", " ", o)
            s = re.sub(r"\s+", " ", s).strip()
            if len(s) > 25 and not BOILERPLATE.search(s):
                out.append(s)

    for b in bodies:
        try:
            walk(json.loads(b))
        except Exception:
            pass
    return "\n".join(dict.fromkeys(out))


def load_existing():
    recs, purged = {}, 0
    if OUT_FILE.exists():
        for line in OUT_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if is_error_page(r.get("content")):  # junk saved by an earlier run
                    purged += 1
                    continue
                recs[r["url"]] = r
    if purged:
        print(f"Purged {purged} saved record(s) that were only the site's error page")
    return recs


def save(recs):
    tmp = OUT_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in recs.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(OUT_FILE)


class RealBrowser:
    def __init__(self, browser_path=None, headless=False):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser, self.name = None, None
        attempts = [("path", browser_path)] if browser_path else [("msedge", None), ("chrome", None)]
        for kind, val in attempts:
            try:
                if kind == "path":
                    self._browser = self._pw.chromium.launch(executable_path=val, headless=headless)
                else:
                    self._browser = self._pw.chromium.launch(channel=kind, headless=headless)
                self.name = val or kind
                break
            except Exception as e:
                print(f"  could not launch {val or kind}: {str(e).splitlines()[0]}")
        if self._browser is None:
            self._pw.stop()
            raise SystemExit("No usable browser found. Pass --browser-path to your Chrome/Edge/Brave .exe")
        self._ctx = self._browser.new_context(viewport={"width": 1366, "height": 900})
        RENDERED.mkdir(parents=True, exist_ok=True)

    def render(self, url, block_assets=True):
        """Returns (html, status, api_text). Hard-capped at roughly 60-75s per page."""
        page = self._ctx.new_page()

        def router(route):
            req = route.request
            if any(t in req.url for t in TRACKERS):
                return route.abort()
            if block_assets and req.resource_type in ("image", "media", "font"):
                return route.abort()
            return route.continue_()

        page.route("**/*", router)

        bodies = []

        def on_response(resp):
            try:
                ct = resp.headers.get("content-type") or ""
                if "json" in ct and resp.status == 200 and not any(t in resp.url for t in TRACKERS):
                    body = resp.text()
                    if 200 < len(body) < 3_000_000:
                        bodies.append(body)
            except Exception:
                pass

        page.on("response", on_response)

        status, html = None, ""
        try:
            try:
                resp = page.goto(url, wait_until="commit", timeout=40000)
                status = resp.status if resp else None
            except Exception as e:
                print(f"    goto slow ({type(e).__name__}); using whatever loaded")
            for state, ms in (("domcontentloaded", 20000), ("networkidle", 8000)):
                try:
                    page.wait_for_load_state(state, timeout=ms)
                except Exception:
                    pass
            for _ in range(8):  # trigger lazy-loaded sections
                page.mouse.wheel(0, 1800)
                page.wait_for_timeout(350)
            page.wait_for_timeout(800)
            try:
                html = page.content()
            except Exception:
                html = ""
        finally:
            page.close()
        if len(html) > 5000 and not is_error_page(html[:200000]):
            (RENDERED / (hashlib.md5(url.encode()).hexdigest() + ".html")).write_text(html, encoding="utf-8")
        return html, status, json_text(bodies)

    def close(self):
        self._browser.close()
        self._pw.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--browser-path", default=None)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    s = requests.Session()
    s.headers["User-Agent"] = UA
    rp = load_robots(s)
    existing = load_existing()

    targets = []
    for u in get_sitemap_urls(s):
        path = urlparse(u).path or "/"
        if path.rstrip("/") in SKIP_PATHS or "?" in u or not rp.can_fetch(UA, u):
            continue
        if u not in existing or len(existing[u]["content"]) < MIN_CHARS:
            targets.append(u)
    if args.limit:
        targets = targets[: args.limit]
    print(f"{len(existing)} records on disk; {len(targets)} pages to retry\n")
    if not targets:
        return

    if OUT_FILE.exists():
        shutil.copy(OUT_FILE, OUT_FILE.with_suffix(".jsonl.bak"))

    br = RealBrowser(args.browser_path, args.headless)
    print(f"Using browser: {br.name}\n")
    now = datetime.now(timezone.utc).isoformat()
    improved, still_missing = 0, []
    try:
        for i, url in enumerate(targets, 1):
            tag = f"[{i}/{len(targets)}]"
            try:
                html, status, api = br.render(url)
                title, desc, text, date, pdfs = extract(html, url)
                crashed = is_error_page(text) or (is_error_page(html[:200000]) and len(text) < MIN_CHARS)
                if crashed:
                    print("    page crashed (site error); reloading with only trackers blocked")
                    html2, status2, api2 = br.render(url, block_assets=False)
                    title, desc, text, date, pdfs = extract(html2, url)
                    status = status2 or status
                    if len(api2) > len(api):
                        api = api2
            except Exception as e:
                print(f"{tag} ERROR {type(e).__name__}: {str(e).splitlines()[0]}  {url}")
                still_missing.append(url)
                continue

            if status and status >= 400:
                print(f"{tag} HTTP {status} (dead link, skipping)  {url}")
                still_missing.append(url)
                continue

            if is_error_page(text):
                text = ""  # never keep the crash message as content
            via = "rendered-page"
            if len(text) < MIN_CHARS and len(api) > max(300, len(text)):
                text, via = api, "api-json"  # use the data the page fetched

            old_len = len(existing[url]["content"]) if url in existing else 0
            if len(text) >= DROP_CHARS and len(text) > old_len:
                existing[url] = {
                    "id": hashlib.md5(url.encode()).hexdigest()[:16],
                    "source": "darglobal",
                    "type": classify(urlparse(url).path),
                    "url": url,
                    "title": title,
                    "content": text,
                    "metadata": {"description": desc, "published": date, "language": "en",
                                 "pdf_links": pdfs, "extracted_via": via},
                    "scraped_at": now,
                }
                improved += 1
                save(existing)  # save after every good page so nothing is lost
                print(f"{tag} OK ({via})  {old_len:>5} -> {len(text):>6} chars  {url}")
            else:
                still_missing.append(url)
                print(f"{tag} still empty ({len(text)} chars)  {url}")
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nInterrupted; progress so far is saved.")
    finally:
        br.close()
        save(existing)

    print(f"\n=== DONE === improved/added: {improved} | still missing: {len(still_missing)} | total records: {len(existing)}")
    for u in still_missing:
        print("  ", u)


if __name__ == "__main__":
    main()