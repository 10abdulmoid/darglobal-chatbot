#!/usr/bin/env python3
"""
Wasalt (wasalt.sa) scraper: sitemaps -> sample pages -> text + structured facts -> JSONL.

Save as  scraper\\scrape_wasalt.py  (needs scraper.py in the same folder).
Run from the repo root with the venv active:

    python scraper\\scrape_wasalt.py --limit 10        # quick test
    python scraper\\scrape_wasalt.py                   # full run (~450 pages)
    python scraper\\scrape_wasalt.py --browser-fallback  # re-render thin pages in Edge/Chrome

Respects robots.txt (everything allowed except /search and /cdn-cgi/rum).
Sitemaps (from robots.txt): static, product (property pages), rental, category.
Search-result pages are NOT scraped.

Output:
    data/raw/wasalt.jsonl           one record per page (same schema as darglobal.jsonl)
    data/raw_html_wasalt/<hash>.html  cached pages (re-runs are free)
"""
import argparse
import gzip
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from scraper import extract  # shared text extraction (trafilatura + fallbacks)

SITE = "https://wasalt.sa"
UA = "ChatbotResearchBot/1.0 (assignment; contact: your-email@example.com)"
CDN = "https://cdn.wasalt.sa/sitemap/"
SITEMAPS = {  # kind -> (sitemap file, record type)
    "static": ("static_sitemap_en_sa.xml.gz", "page"),
    "listing": ("product_sitemap_en_sa.xml.gz", "listing"),
    "rental": ("rental_pdp_sitemap_en_sa.xml.gz", "listing"),
    "category": ("category_sitemap_en_sa.xml.gz", "category"),
}
CACHE = Path("data/raw_html_wasalt")
OUT_DIR = Path("data/raw")
OUT_FILE = OUT_DIR / "wasalt.jsonl"
THIN = 300  # chars; below this a page is considered thin

LD_KEYS = {
    "name", "description", "price", "priceCurrency", "lowPrice", "highPrice",
    "addressLocality", "addressRegion", "streetAddress", "addressCountry",
    "numberOfRooms", "numberOfBedrooms", "numberOfBathroomsTotal", "numberOfBathrooms",
    "value", "unitText", "unitCode", "datePosted", "availability", "category", "propertyType",
}


# ------------------------------------------------------------------ sitemaps
def read_sitemap(session, url, depth=0):
    r = session.get(url, timeout=60)
    r.raise_for_status()
    data = r.content
    try:
        data = gzip.decompress(data)
    except OSError:
        pass  # already decompressed by the server/requests
    root = ET.fromstring(data)
    locs = [e.text.strip() for e in root.iter() if e.tag.split("}")[-1] == "loc" and e.text]
    if root.tag.split("}")[-1] == "sitemapindex" and depth < 2:
        out = []
        for child in locs[:3]:  # an index: follow only the first few children
            out += read_sitemap(session, child, depth + 1)
        return out
    return locs


def sample_evenly(urls, n):
    if n <= 0 or not urls:
        return []
    if len(urls) <= n:
        return urls
    step = len(urls) / n
    return [urls[int(i * step)] for i in range(n)]


# ------------------------------------------------------------------ structured facts
def jsonld_facts(soup):
    """Readable 'key: value' facts and FAQ pairs from JSON-LD blocks."""
    lines = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("@type") == "Question" and o.get("name"):
                ans = o.get("acceptedAnswer") or {}
                a = re.sub(r"<[^>]+>", " ", str(ans.get("text", ""))).strip()
                lines.append(f"Q: {o['name']} A: {a}")
                return
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                elif k in LD_KEYS and v not in (None, ""):
                    lines.append(f"{k}: {re.sub(chr(10), ' ', str(v)).strip()}")
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or ""))
        except Exception:
            continue
    return list(dict.fromkeys(lines))[:60]


def facts_to_meta(lines):
    meta = {}
    for ln in lines:
        if ": " in ln and not ln.startswith("Q: "):
            k, v = ln.split(": ", 1)
            if k in ("price", "priceCurrency", "addressLocality", "addressRegion",
                     "numberOfBedrooms", "numberOfBathroomsTotal", "propertyType") and k not in meta:
                meta[k] = v
    return meta


# ------------------------------------------------------------------ fetch
def fetch(session, url, delay):
    cache = CACHE / (hashlib.md5(url.encode()).hexdigest() + ".html")
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="ignore")
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30)
            time.sleep(delay)
            if r.status_code == 200:
                cache.write_text(r.text, encoding="utf-8")
                return r.text
            if r.status_code in (404, 410):
                return None
            time.sleep(2 ** attempt)
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return None


def build_record(url, kind, rtype, html):
    soup = BeautifulSoup(html, "lxml")
    title, desc, text, date, _ = extract(html, url)
    facts = jsonld_facts(soup)
    if facts:
        text = (text + "\n\nKey facts:\n" + "\n".join(facts)).strip()
    meta = {"description": desc, "published": date, "language": "en", "kind": kind}
    meta.update(facts_to_meta(facts))
    return {
        "id": hashlib.md5(url.encode()).hexdigest()[:16],
        "source": "wasalt",
        "type": rtype,
        "url": url,
        "title": title,
        "content": text,
        "metadata": meta,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only process N URLs (testing)")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--static", type=int, default=200, help="max static/info pages")
    ap.add_argument("--listings", type=int, default=250, help="sampled sale listings")
    ap.add_argument("--rentals", type=int, default=100, help="sampled rental listings")
    ap.add_argument("--categories", type=int, default=60, help="sampled category pages")
    ap.add_argument("--browser-fallback", action="store_true",
                    help="re-render thin pages in Edge/Chrome (needs retry_missing.py)")
    args = ap.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = UA

    rp = RobotFileParser()
    rr = s.get(f"{SITE}/robots.txt", timeout=20)
    rp.parse(rr.text.splitlines() if rr.ok else [])

    caps = {"static": args.static, "listing": args.listings,
            "rental": args.rentals, "category": args.categories}
    todo = []  # (url, kind, type)
    for kind, (fname, rtype) in SITEMAPS.items():
        try:
            urls = read_sitemap(s, CDN + fname)
        except Exception as e:
            print(f"! could not read {fname}: {e}")
            continue
        urls = [u for u in dict.fromkeys(urls)
                if "?" not in u and not urlparse(u).path.startswith(("/ar/", "/search"))]
        picked = sample_evenly(urls, caps[kind])
        print(f"{kind:<9} {len(urls):>7} URLs in sitemap -> using {len(picked)}")
        todo += [(u, kind, rtype) for u in picked if rp.can_fetch(UA, u)]
    if args.limit:  # test mode: take a few of each kind, not just the first kind
        per = max(1, args.limit // 4)
        seen, test = Counter(), []
        for item in todo:
            if seen[item[1]] < per:
                seen[item[1]] += 1
                test.append(item)
        todo = test[: args.limit]
    print(f"\nFetching {len(todo)} pages (delay {args.delay}s)...\n")

    browser = None
    stats, thin, failed = Counter(), [], []
    with OUT_FILE.open("w", encoding="utf-8") as f:
        for i, (url, kind, rtype) in enumerate(todo, 1):
            html = fetch(s, url, args.delay)
            if html is None:
                failed.append(url)
                print(f"[{i}/{len(todo)}] FAIL {url}")
                continue
            rec = build_record(url, kind, rtype, html)

            if len(rec["content"]) < THIN and args.browser_fallback:
                try:
                    if browser is None:
                        from retry_missing import RealBrowser
                        browser = RealBrowser()
                    rhtml, status, api = browser.render(url)
                    r2 = build_record(url, kind, rtype, rhtml)
                    if len(r2["content"]) < THIN and len(api) > len(r2["content"]):
                        r2["content"] = api
                    if len(r2["content"]) > len(rec["content"]):
                        rec = r2
                except Exception as e:
                    print(f"    browser fallback failed: {type(e).__name__}")

            if len(rec["content"]) < 100:
                thin.append(url)
                print(f"[{i}/{len(todo)}] DROP (empty) {url}")
                continue
            if len(rec["content"]) < THIN:
                thin.append(url)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats[kind] += 1
            print(f"[{i}/{len(todo)}] {kind:<9} {len(rec['content']):>6} chars  {url}")

    if browser is not None:
        browser.close()
    print("\n=== SUMMARY ===")
    print("Records by kind:", dict(stats), "| total:", sum(stats.values()))
    print(f"Failed: {len(failed)} | Thin/dropped (<{THIN} chars): {len(thin)}")
    for u in (thin + failed)[:15]:
        print("  ", u)
    print(f"\nWrote {OUT_FILE}")


if __name__ == "__main__":
    main()