#!/usr/bin/env python3
"""
Dar Global (darglobal.co.uk) scraper.

Pipeline: robots.txt -> sitemap.xml -> clean/dedupe URLs -> fetch (cached) ->
extract main text -> JSONL records.

Usage:
    pip install requests beautifulsoup4 lxml trafilatura
    pip install playwright && playwright install chromium   # JS-rendered pages
    python scrape_darglobal.py                 # full run
    python scrape_darglobal.py --limit 20      # quick test
    python scrape_darglobal.py --no-playwright # static HTML only

Pages whose static HTML yields < MIN_CHARS of text are re-rendered in headless
Chromium (scrolling to trigger lazy content). Pages still < DROP_CHARS are dropped.

Output:
    data/raw_html/<hash>.html           cached raw pages (re-runs are free)
    data/raw_html_rendered/<hash>.html  cached browser-rendered pages
    data/raw/darglobal.jsonl            one record per page
    data/raw/darglobal_pdf_links.txt    PDF links found (brochures etc.)
"""
import argparse
import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

import requests
import trafilatura
from bs4 import BeautifulSoup

BASE = "https://darglobal.co.uk"
UA = "DarGlobalChatbotResearchBot/1.0 (assignment; contact: your-email@example.com)"
MIN_CHARS = 600  # below this, the static HTML is "thin" -> render with a real browser
DROP_CHARS = 100  # below this even after rendering, the record is dropped

# UI/form/cookie/payment noise that pollutes page text
BOILERPLATE = re.compile(
    r"(please enter|please provide|invalid phone|doesn.t match the selected|we use cookies|"
    r"file is too large|unsupported file format|upload failed|form submitted|form was sent|"
    r"your payment|payment has been|payment session|minimum length|i.ve read and agree|"
    r"i acknowledge that i have read|how did you hear about us|are you a licensed|"
    r"i.d like to hear about|your submission was successful|all rights reserved|"
    r"^thank you for your (interest|patience)|team will reach out|"
    r"register your interest|kindly enter your credentials)",
    re.I,
)

RAW_HTML = Path("data/raw_html")
RENDERED = Path("data/raw_html_rendered")
OUT_DIR = Path("data/raw")
OUT_FILE = OUT_DIR / "darglobal.jsonl"
PDF_FILE = OUT_DIR / "darglobal_pdf_links.txt"

# Pages with no value for a Q&A chatbot (payments, thank-you, duplicates, legal boilerplate)
SKIP_PATHS = {
    "/pay-online", "/pay-online/status", "/success", "/thank-you", "/bookYourUnit",
    "/landing-page", "/landingpage/newStaticLandingPageHtml", "/new-home-page",
    "/internship-application", "/privacy-policy", "/terms-conditions",
    "/terms-of-uses", "/broker-contest-terms-conditions", "/win-a-trip-to-dubai",
    "/exclusive-membership",  # test run: only form errors, payment T&Cs and a project-name list
}
COMPANY_PATHS = {
    "/", "/about", "/faq", "/investor", "/why-invest", "/careers", "/internships",
    "/get-in-touch", "/become-a-broker", "/become-an-agent", "/dg-circle",
    "/exclusive-membership", "/insignia", "/tokenization", "/development-management",
    "/commercial", "/hospitality", "/live-all-in", "/campaigns", "/luxury-golf-communities",
}


def classify(path: str) -> str:
    p = path.rstrip("/") or "/"
    if p in COMPANY_PATHS:
        return "company"
    first = p.strip("/").split("/")[0]
    return {
        "press": "press", "blog": "blog", "insights": "insight", "partners": "partner",
        "projects": "project_listing", "one-of-one": "project", "rayana": "project",
        "amaya": "project", "trump-plaza": "project", "landing-page": "project",
    }.get(first, "project" if p.count("/") == 1 else "page")


# ---------------------------------------------------------------- robots / sitemap
def load_robots(session):
    rp = RobotFileParser()
    r = session.get(f"{BASE}/robots.txt", timeout=20)
    rp.parse(r.text.splitlines() if r.ok else [])
    return rp


def clean_url(raw: str) -> str:
    """Fix sitemap oddities: stray spaces, unicode dashes/apostrophes."""
    u = raw.strip()
    parts = urlparse(u)
    path = quote(parts.path, safe="/%:@,-._~")
    return f"{parts.scheme}://{parts.netloc}{path}"


def get_sitemap_urls(session):
    r = session.get(f"{BASE}/sitemap.xml", timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    seen, urls = set(), []
    for loc in root.findall(".//s:loc", ns):
        u = clean_url(loc.text or "")
        key = u.lower().rstrip("/")
        if u and key not in seen:
            seen.add(key)
            urls.append(u)
    return urls


# ---------------------------------------------------------------- fetching
def fetch(session, url, delay, retries=3):
    cache = RAW_HTML / (hashlib.md5(url.encode()).hexdigest() + ".html")
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="ignore"), True
    variants = [url]
    if "%20" in url:  # sitemap had stray spaces; try without them too
        variants.append(url.replace("%20", ""))
    for v in variants:
        for attempt in range(retries):
            try:
                r = session.get(v, timeout=30)
                time.sleep(delay)
                if r.status_code == 200:
                    cache.write_text(r.text, encoding="utf-8")
                    return r.text, False
                if r.status_code == 404:
                    break
                time.sleep(2 ** attempt)
            except requests.RequestException:
                time.sleep(2 ** attempt)
    return None, False


class Renderer:
    """One shared headless Chromium for all JS-rendered pages (cached on disk)."""

    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._ctx = self._browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
        RENDERED.mkdir(parents=True, exist_ok=True)

    def render(self, url, delay):
        cache = RENDERED / (hashlib.md5(url.encode()).hexdigest() + ".html")
        if cache.exists():
            return cache.read_text(encoding="utf-8", errors="ignore")
        page = self._ctx.new_page()
        # skip heavy assets we don't need; they are the usual cause of load timeouts
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        try:
            try:
                page.goto(url, wait_until="commit", timeout=90000)
            except Exception as e:
                print(f"    goto slow/failed ({type(e).__name__}); using whatever loaded")
            for state, ms in (("domcontentloaded", 45000), ("networkidle", 15000)):
                try:
                    page.wait_for_load_state(state, timeout=ms)
                except Exception:
                    pass
            # scroll to trigger lazy-loaded sections, then let them settle
            for _ in range(8):
                page.mouse.wheel(0, 1800)
                page.wait_for_timeout(400)
            page.wait_for_timeout(1000)
            html = page.content()
        finally:
            page.close()
        if len(html) > 5000:  # don't cache failed/blank renders
            cache.write_text(html, encoding="utf-8")
        time.sleep(delay)
        return html

    def close(self):
        self._browser.close()
        self._pw.stop()


# ---------------------------------------------------------------- extraction
def next_data_text(html: str) -> str:
    """If the site is Next.js, pull human-readable strings out of __NEXT_DATA__."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return ""
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return ""
    out = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and len(o) > 25 and " " in o and not o.startswith(("http", "/_next")):
            out.append(re.sub(r"<[^>]+>", " ", o).strip())

    walk(data.get("props", {}))
    return "\n".join(dict.fromkeys(out))


def extract(html: str, url: str):
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.string or "").strip() if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"})
    description = (meta.get("content") or "").strip() if meta else ""

    text, date = "", None
    j = trafilatura.extract(
        html, url=url, output_format="json", with_metadata=True,
        include_tables=True, favor_recall=True,
    )
    if j:
        d = json.loads(j)
        text, date = d.get("text", "") or "", d.get("date")
        title = d.get("title") or title

    if len(text) < MIN_CHARS:  # fallback: raw body text + Next.js data
        for t in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            t.decompose()
        body = soup.get_text("\n", strip=True)
        text = max([text, body, next_data_text(html)], key=len)

    pdfs = sorted({urljoin(url, a["href"]) for a in BeautifulSoup(html, "lxml").find_all("a", href=True)
                   if a["href"].lower().split("?")[0].endswith(".pdf")})
    # drop form/cookie/payment UI lines, and exact duplicate lines
    seen, lines = set(), []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or BOILERPLATE.search(ln) or ln in seen:
            continue
        seen.add(ln)
        lines.append(ln)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, description, text, date, pdfs


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only process N URLs (testing)")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    ap.add_argument("--no-playwright", action="store_true",
                    help="disable browser rendering of thin pages (on by default)")
    args = ap.parse_args()
    use_browser = not args.no_playwright
    renderer = None

    RAW_HTML.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers["User-Agent"] = UA

    rp = load_robots(s)
    urls = get_sitemap_urls(s)
    print(f"Sitemap: {len(urls)} unique URLs")

    todo = []
    for u in urls:
        path = urlparse(u).path or "/"
        if path.rstrip("/") in SKIP_PATHS or "?" in u:
            continue
        if not rp.can_fetch(UA, u):
            print(f"  robots.txt blocks: {u}")
            continue
        todo.append(u)
    if args.limit:
        todo = todo[: args.limit]
    print(f"Fetching {len(todo)} pages (delay {args.delay}s)...\n")

    stats, thin, failed, all_pdfs = Counter(), [], [], set()
    now = datetime.now(timezone.utc).isoformat()

    with OUT_FILE.open("w", encoding="utf-8") as f:
        for i, url in enumerate(todo, 1):
            html, cached = fetch(s, url, args.delay)
            if html is None:
                failed.append(url)
                print(f"[{i}/{len(todo)}] FAIL {url}")
                continue
            title, desc, text, date, pdfs = extract(html, url)

            if len(text) < MIN_CHARS and use_browser:
                try:
                    if renderer is None:
                        renderer = Renderer()
                    rhtml = renderer.render(url, args.delay)
                    r_title, r_desc, r_text, r_date, r_pdfs = extract(rhtml, url)
                    if len(r_text) > len(text):  # keep whichever extraction is richer
                        title, desc, text, date, pdfs = r_title or title, r_desc or desc, r_text, r_date or date, r_pdfs
                except Exception as e:
                    print(f"    browser render failed: {e}")

            if len(text) < DROP_CHARS:
                thin.append(url)
                print(f"[{i}/{len(todo)}] DROP (empty after rendering) {url}")
                continue
            if len(text) < MIN_CHARS:
                thin.append(url)
            all_pdfs.update(pdfs)

            path = urlparse(url).path
            rec = {
                "id": hashlib.md5(url.encode()).hexdigest()[:16],
                "source": "darglobal",
                "type": classify(path),
                "url": url,
                "title": title,
                "content": text,
                "metadata": {"description": desc, "published": date, "language": "en",
                             "pdf_links": pdfs},
                "scraped_at": now,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats[rec["type"]] += 1
            print(f"[{i}/{len(todo)}] {rec['type']:<15} {len(text):>6} chars  {url}")

    if renderer is not None:
        renderer.close()
    PDF_FILE.write_text("\n".join(sorted(all_pdfs)), encoding="utf-8")

    print("\n=== SUMMARY ===")
    print("Records by type:", dict(stats), "| total:", sum(stats.values()))
    print(f"Failed: {len(failed)} | Thin (<{MIN_CHARS} chars): {len(thin)} | PDFs found: {len(all_pdfs)}")
    if thin:
        print("Thin pages (re-run with --playwright if many):")
        for u in thin[:15]:
            print("  ", u)
    if failed:
        print("Failed URLs:")
        for u in failed[:15]:
            print("  ", u)
    print(f"\nWrote {OUT_FILE}")


if __name__ == "__main__":
    main()