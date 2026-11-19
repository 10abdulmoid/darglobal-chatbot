"""
Retrieval over data/processed/knowledge.jsonl.

BM25 keyword search (fast, tiny, no GPU/model download) plus routing rules:
  - which company the question is about (Dar Global / Wasalt / both)
  - whether it is a property-search question (then listings are allowed to rank high)
    or a general/company question (then company, FAQ, press and project pages win)
  - at most 2 chunks per page, so answers draw on several sources
"""
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from rank_bm25 import BM25Okapi

DATA = Path(os.getenv("KNOWLEDGE_FILE", Path(__file__).resolve().parent.parent / "data/processed/knowledge.jsonl"))

STOP = set("""a an the and or of to in on for with at by from is are was were be been being it its this that
these those as about into over under than then there here what which who whom whose when where why how do does
did can could should would will shall may might must i me my we our you your they them their he she his her
please tell give show list any some all more most much many also just only not no yes""".split())

# small synonym map: bridges common question words to the words the sites actually use
SYNONYMS = {
    "ceo": ["chief", "executive", "founder"], "founder": ["ceo", "founded"],
    "boss": ["ceo", "chief", "executive"], "leader": ["ceo", "chief", "executive"],
    "listed": ["london", "stock", "exchange", "lse"], "stock": ["listed", "exchange"],
    "price": ["sar", "aed", "cost", "priced"], "cost": ["price", "sar"],
    "cheap": ["price", "sar"], "expensive": ["price", "sar"],
    "flat": ["apartment"], "apartments": ["apartment"], "villas": ["villa"],
    "bhk": ["bedroom", "bedrooms"], "bed": ["bedroom", "bedrooms"], "rooms": ["bedroom"],
    "rent": ["rental", "lease"], "rental": ["rent"], "buy": ["sale", "purchase"],
    "office": ["contact", "address", "touch", "floor", "tower"],
    "located": ["contact", "address", "office"], "address": ["contact", "office"],
    "contact": ["address", "phone", "office", "touch"],
    "phone": ["contact"], "partners": ["partner", "brand", "partnership"],
    "brands": ["brand", "partner", "collaboration"], "revenue": ["results", "sales", "interim"],
    "results": ["revenue", "interim", "profit"], "visa": ["residency", "golden"],
    "auction": ["auctions", "mazadat"], "ksa": ["saudi"], "uae": ["dubai", "emirates"],
    # Arabic question words -> English page words (fallback when the LLM translation of an Arabic question fails)
    **{w: en.split() for en, ws in {
        "office contact address": "مكتب مكاتب مكاتبها المكاتب مقر عنوان",
        "project projects": "مشروع مشاريع المشاريع مشاريعها",
        "partner brand partnership": "شركاء شريك شراكات الشراكات العلامات علامات العلامة",
        "apartment": "شقة شقق الشقق", "villa": "فيلا فلل الفلل", "sale": "للبيع بيع البيع",
        "rent rental": "للإيجار إيجار الإيجار", "bedroom": "غرف غرفة", "price": "سعر أسعار الأسعار",
        "services": "خدمات الخدمات", "auction": "مزاد مزادات المزادات",
        "riyadh": "الرياض", "jeddah": "جدة", "dubai": "دبي", "oman": "عمان عُمان", "london": "لندن",
        "spain": "إسبانيا اسبانيا", "qatar": "قطر", "saudi": "السعودية",
        "darglobal": "جلوبال غلوبال", "ceo chief executive": "الرئيس التنفيذي",
    }.items() for w in ws.split()},
    **{w: ["revenue", "results", "profit", "sales", "interim"] for w in (
        "earn", "earning", "money", "income", "profit", "turnover", "financial", "financials", "make")},
}

TYPE_WEIGHT_GENERAL = {
    "curated": 1.7, "company": 1.45, "faq": 1.45, "page": 1.3, "press": 1.25, "project": 1.3,
    "project_listing": 1.1, "partner": 1.05, "insight": 0.95, "blog": 0.8,
    "category": 0.7, "listing": 0.55,
}
TYPE_WEIGHT_PROPERTY = dict(TYPE_WEIGHT_GENERAL, listing=1.35, category=1.0, blog=0.7)

PROPERTY_HINTS = re.compile(
    r"\b(bed(room)?s?|bhk|sqm|sq\.?\s?m|square|for (rent|sale)|to rent|apartment|villa|flat|studio|"
    r"duplex|land|plot|chalet|floor|budget|under \d|below \d|cheapest|listing|listings|daily rental)\b",
    re.I,
)


def tokenize(text):
    toks = re.findall(r"[a-z0-9؀-ۿ]+", text.lower())
    out = []
    for t in toks:
        if t in STOP or (len(t) < 2 and not t.isdigit()):
            continue
        if len(t) > 4 and t.endswith("ies"):
            t = t[:-3] + "y"  # companies -> company
        elif len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]  # crude plural folding: villas -> villa
        out.append(t)
    return out


def expand(tokens):
    extra = []
    for t in tokens:
        extra += SYNONYMS.get(t, [])
    return tokens + [tokenize(e)[0] for e in extra if tokenize(e)]


class Index:
    def __init__(self, path=DATA):
        self.docs = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
        corpus = []
        for d in self.docs:
            # title counted twice so a page about "AIDA" ranks for "aida"
            title = d.get("title") or ""
            corpus.append(tokenize(f"{title} {title} {d['text']}"))
        self.bm25 = BM25Okapi(corpus)
        self.sources = sorted({d["source"] for d in self.docs})

    @staticmethod
    def route(question):
        q = question.lower()
        dg = bool(re.search(r"dar\s?global|darglobal|dar global|دار\s?(جلوبال|غلوبال)", q))
        ws = bool(re.search(r"wasalt|وصلت", q))
        if dg and not ws:
            company = "darglobal"
        elif ws and not dg:
            company = "wasalt"
        else:
            company = "both"
        return company, bool(PROPERTY_HINTS.search(q))

    def search(self, question, k=6, history_hint="", company=None):
        """company: None/"all" = auto-route; "darglobal"/"wasalt" = only that source's chunks."""
        forced = company if company in ("darglobal", "wasalt") else None
        company, is_property = self.route(question + " " + history_hint)
        if forced:
            company = forced
        tokens = expand(tokenize(question + " " + history_hint))
        if not tokens:
            return [], company
        scores = self.bm25.get_scores(tokens)
        weights = TYPE_WEIGHT_PROPERTY if is_property else TYPE_WEIGHT_GENERAL

        ranked = []
        for i, s in enumerate(scores):
            if s <= 0:
                continue
            d = self.docs[i]
            s *= weights.get(d["type"], 1.0)
            if company != "both" and d["source"] != company:
                if forced:
                    continue
                s *= 0.25
            ranked.append((s, i))
        ranked.sort(reverse=True)
        if company != "both":
            # question names one company: use only its pages, unless it has almost nothing
            own = [(s, i) for s, i in ranked if self.docs[i]["source"] == company]
            if len(own) >= 3:
                ranked = own

        per_url, picked = defaultdict(int), []
        if company == "both" and not is_property and len(self.sources) > 1:
            # general question about both/neither: make sure each company is represented
            quota = {src: (k + 1) // 2 for src in self.sources}
        else:
            quota = None
        for s, i in ranked:
            d = self.docs[i]
            if per_url[d["url"]] >= 2:
                continue
            if quota is not None and quota.get(d["source"], 0) <= 0:
                continue
            per_url[d["url"]] += 1
            if quota is not None:
                quota[d["source"]] -= 1
            picked.append((s, d))
            if len(picked) >= k:
                break
        if not picked:
            return [], company
        top = picked[0][0]
        # drop weak tail results (< 25% of best score) unless that leaves fewer than 3
        strong = [d for s, d in picked if s >= 0.25 * top]
        return (strong if len(strong) >= 3 else [d for _, d in picked[:3]]), company
