"""News headlines + sentiment.

Sources (merged and de-duplicated per ticker):
  * Google News RSS search  - free, no key, covers every ticker (primary)
  * Yahoo Finance news      - free, no key
  * NewsAPI.org (optional)  - needs NEWSAPI_KEY; free plan has a small daily budget, so it is
                              spent on the most interesting names (top/bottom of the ranking)

Sentiment model: VADER (fast, default) or FinBERT (finance-tuned transformer, more accurate on
headlines; `pip install transformers torch`, set news.model: finbert).

Per ticker per day we keep the raw headlines (data/news/raw/YYYY-MM-DD/TICKER.json) and a
recency-weighted average sentiment in data/news/sentiment.parquet. That file grows daily and is
what lets the news feature be backtested later on genuinely point-in-time data.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, Iterable, List, Optional
from urllib.parse import quote_plus

import pandas as pd
import requests

from .config import Paths, ns

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) call-signal-scanner/2.0"}
_CORP = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|plc|holdings|group|"
                   r"the|class [a-z]|n\.?v\.?|s\.?a\.?|&)\b\.?", re.I)


def clean_company(name: str) -> str:
    n = _CORP.sub(" ", str(name or ""))
    n = re.sub(r"[^\w\s\-'.]", " ", n)
    return re.sub(r"\s+", " ", n).strip(" .,-")


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower())[:90]


# --------------------------------------------------------------------------- finance lexicon
# Words that describe a company's field rather than good or bad news (drug, disease, product topics).
FIN_NEUTRAL = {
    "cancer", "cancers", "tumor", "tumors", "tumour", "tumours", "disease", "diseases", "obesity",
    "diabetes", "infection", "infections", "virus", "viruses", "pain", "stroke", "heart", "death",
    "deaths", "fatal", "addiction", "depression", "alzheimer", "alzheimer's", "hiv", "illness",
    "sick", "injury", "injuries", "weapon", "weapons", "defense", "defence", "war", "warfare",
    "gun", "guns", "attack", "attacks", "battle", "fight", "fighting", "killer", "crash",
}
# Market language VADER lacks (values on VADER's -4..+4 scale).
FIN_LEXICON = {
    "upgrade": 2.2, "upgrades": 2.2, "upgraded": 2.2, "downgrade": -2.2, "downgrades": -2.2,
    "downgraded": -2.2, "outperform": 1.8, "outperforms": 1.8, "overweight": 1.2,
    "underperform": -1.8, "underperforms": -1.8, "underweight": -1.2, "beats": 1.8, "tops": 1.2,
    "misses": -1.8, "missed": -1.6, "surge": 2.0, "surges": 2.0, "surged": 2.0, "soar": 2.2,
    "soars": 2.2, "soared": 2.2, "jumps": 1.6, "jumped": 1.6, "rallies": 1.6, "rally": 1.4,
    "climbs": 1.2, "gains": 1.2, "plunge": -2.4, "plunges": -2.4, "plunged": -2.4,
    "plummets": -2.6, "plummeted": -2.6, "tumbles": -2.2, "tumbled": -2.2, "slumps": -2.0,
    "slumped": -2.0, "sinks": -1.8, "sank": -1.8, "drops": -1.4, "dropped": -1.4, "falls": -1.2,
    "fell": -1.2, "slides": -1.4, "record": 1.0, "approval": 1.8, "approved": 1.8,
    "approves": 1.8, "buyback": 1.4, "layoffs": -1.6, "recall": -1.8, "recalls": -1.8,
    "probe": -1.4, "investigation": -1.4, "subpoena": -1.8, "bankruptcy": -3.0, "default": -2.0,
    "delisting": -2.4, "halted": -1.6, "fraud": -3.0, "resigns": -1.2, "breakthrough": 2.2,
}
_PHRASES = [
    # (pattern, token appended so VADER scores the direction)
    (re.compile(r"\b(raise[sd]?|raising|lift(s|ed)?|boost(s|ed)?|hike[sd]?|increase[sd]?|up(s|ped)?)\b"
                r".{0,40}\b(price target|target|guidance|outlook|forecast|dividend|estimates?)\b", re.I), "upgrade"),
    (re.compile(r"\b(cut[s]?|lower(s|ed)?|slash(es|ed)?|reduce[sd]?|trim(s|med)?)\b"
                r".{0,40}\b(price target|target|guidance|outlook|forecast|dividend|estimates?)\b", re.I), "downgrade"),
    (re.compile(r"\bbeat(s)?\b.{0,25}\b(estimates?|expectations|forecasts?|consensus)\b", re.I), "beats"),
    (re.compile(r"\bmiss(es|ed)?\b.{0,25}\b(estimates?|expectations|forecasts?|consensus)\b", re.I), "misses"),
]


def finance_rewrite(text: str) -> str:
    """Append direction words for common market phrases ("raises price target" -> upgrade)."""
    extra = [tok for pat, tok in _PHRASES if pat.search(text)]
    return text + (" . " + " ".join(extra) if extra else "")


# --------------------------------------------------------------------------- sentiment models
class Sentiment:
    _inst = None
    _lock = threading.Lock()

    def __init__(self, model: str = "vader"):
        self.model = model
        self._vader = None
        self._finbert = None
        if model == "finbert":
            try:
                from transformers import pipeline  # type: ignore
                self._finbert = pipeline("text-classification", model="ProsusAI/finbert",
                                         top_k=None, truncation=True)
            except Exception as e:
                print(f"[news] FinBERT unavailable ({e}); falling back to VADER")
                self.model = "vader"
        if self.model == "vader":
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self._vader = SentimentIntensityAnalyzer()
            # VADER is a general social-media lexicon: it reads topic words such as "cancer" as
            # strongly negative and does not know market language. Neutralise the topic words and
            # add finance terms so headlines are scored on what they say about the stock.
            for w in FIN_NEUTRAL:
                self._vader.lexicon.pop(w, None)
            self._vader.lexicon.update(FIN_LEXICON)

    @classmethod
    def get(cls, model: str) -> "Sentiment":
        with cls._lock:
            if cls._inst is None or cls._inst.model != model:
                cls._inst = Sentiment(model)
            return cls._inst

    def score(self, texts: List[str]) -> List[float]:
        """Each headline -> sentiment in [-1, 1]."""
        if not texts:
            return []
        if self._finbert is not None:
            res = self._finbert(texts, batch_size=32)
            out = []
            for r in res:
                d = {x["label"].lower(): x["score"] for x in r}
                out.append(float(d.get("positive", 0) - d.get("negative", 0)))
            return out
        return [float(self._vader.polarity_scores(finance_rewrite(t))["compound"]) for t in texts]


# --------------------------------------------------------------------------- sources
def _get(url: str, params: Optional[dict] = None, tries: int = 3) -> Optional[requests.Response]:
    for k in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=20)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 503):
                time.sleep(2.0 * (k + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1.0 * (k + 1))
    return None


def google_news(ticker: str, company: str, days: int = 7) -> List[Dict]:
    name = clean_company(company)
    q = f'("{name}" OR "{ticker}") (stock OR shares OR earnings) when:{days}d'
    url = ("https://news.google.com/rss/search?q=" + quote_plus(q) +
           "&hl=en-US&gl=US&ceid=US:en")
    r = _get(url)
    if r is None:
        return []
    out = []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3].strip()
        pub = it.findtext("pubDate")
        try:
            ts = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
        except Exception:
            ts = None
        if title:
            out.append({"title": title, "source": src or "Google News", "url": it.findtext("link"),
                        "published": ts.isoformat() if ts else None, "via": "google"})
    return out


def yahoo_news(ticker: str) -> List[Dict]:
    try:
        import yfinance as yf
        items = yf.Ticker(ticker).get_news(count=20) or []
    except Exception:
        return []
    out = []
    for a in items:
        c = a.get("content", a) or {}
        title = (c.get("title") or "").strip()
        if not title:
            continue
        pub = c.get("pubDate") or c.get("displayTime")
        if pub is None and a.get("providerPublishTime"):
            pub = datetime.fromtimestamp(a["providerPublishTime"], tz=timezone.utc).isoformat()
        prov = c.get("provider") or {}
        url = (c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) \
            else c.get("link")
        out.append({"title": title, "summary": (c.get("summary") or "")[:300],
                    "source": prov.get("displayName") if isinstance(prov, dict) else a.get("publisher"),
                    "url": url, "published": pub, "via": "yahoo"})
    return out


def newsapi_news(ticker: str, company: str, days: int = 7) -> List[Dict]:
    key = os.getenv("NEWSAPI_KEY", "").strip()
    if not key:
        return []
    name = clean_company(company)
    params = {"q": f'"{name}" OR "{ticker}"', "language": "en", "sortBy": "publishedAt",
              "pageSize": 30, "from": (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d"),
              "apiKey": key}
    r = _get("https://newsapi.org/v2/everything", params, tries=2)
    if r is None:
        return []
    out = []
    for a in r.json().get("articles", []):
        t = (a.get("title") or "").strip()
        if t and t != "[Removed]":
            out.append({"title": t, "summary": (a.get("description") or "")[:300],
                        "source": (a.get("source") or {}).get("name"), "url": a.get("url"),
                        "published": a.get("publishedAt"), "via": "newsapi"})
    return out


# --------------------------------------------------------------------------- aggregation
def _relevant(h: Dict, ticker: str, company: str) -> bool:
    if h.get("via") == "yahoo":  # Yahoo's feed is already ticker-specific
        return True
    text = (h.get("title", "") + " " + h.get("summary", "")).lower()
    name = clean_company(company).lower()
    first = name.split(" ")[0] if name else ""
    return (re.search(rf"\b{re.escape(ticker.lower())}\b", text) is not None
            or (name and name in text) or (len(first) >= 4 and first in text))


def _parse_ts(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        ts = pd.Timestamp(s)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.to_pydatetime()
    except Exception:
        return None


def collect(ticker: str, company: str, cfg: Dict, use_newsapi: bool) -> List[Dict]:
    days = int(cfg["news"].get("lookback_days", 7))
    heads = google_news(ticker, company, days)
    if cfg["news"].get("yahoo_news", True):
        heads += yahoo_news(ticker)
    if use_newsapi and cfg["news"].get("provider") == "newsapi":
        heads += newsapi_news(ticker, company, days)
    now = datetime.now(timezone.utc)
    seen, out = set(), []
    for h in heads:
        k = _norm_title(h["title"])
        if k in seen or not _relevant(h, ticker, company):
            continue
        ts = _parse_ts(h.get("published"))
        if ts and (now - ts).days > days:
            continue
        seen.add(k)
        h["published"] = ts.isoformat() if ts else None
        out.append(h)
    out.sort(key=lambda h: h.get("published") or "", reverse=True)
    return out[:40]


def aggregate(heads: List[Dict], half_life_days: float) -> Dict:
    """Recency-weighted sentiment: a headline loses half its weight every `half_life_days`
    (default 1 day), so today's news dominates and week-old news is nearly ignored."""
    now = datetime.now(timezone.utc)
    num = den = 0.0
    for h in heads:
        ts = _parse_ts(h.get("published"))
        # age in fractional days; undated headlines are treated as old so they barely count
        age = max(0.0, (now - ts).total_seconds() / 86400) if ts else 5.0
        w = 0.5 ** (age / half_life_days)
        num += w * h["sentiment"]
        den += w
    srcs = {h.get("source") for h in heads if h.get("source")}
    recent = sum(1 for h in heads if (_parse_ts(h.get("published")) or now - timedelta(days=9))
                 > now - timedelta(days=2))
    return {"sentiment": num / den if den else None, "n_headlines": len(heads),
            "n_sources": len(srcs), "n_48h": recent}


class NewsBudget:
    """Tracks NewsAPI requests spent today."""

    def __init__(self, paths: Paths, limit: int):
        self.p, self.limit = paths.news_budget, limit
        self.today = datetime.now().strftime("%Y-%m-%d")
        d = {}
        if self.p.exists():
            try:
                d = json.loads(self.p.read_text())
            except Exception:
                d = {}
        self.used = d.get("used", 0) if d.get("date") == self.today else 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if not os.getenv("NEWSAPI_KEY") or self.used >= self.limit:
                return False
            self.used += 1
            self.p.write_text(json.dumps({"date": self.today, "used": self.used}))
            return True


def _raw_path(paths: Paths, day: str, ticker: str):
    return paths.news_raw / day / f"{ticker}.json"


def fetch_for(ticker: str, company: str, cfg: Dict, paths: Paths, budget: NewsBudget,
              want_newsapi: bool, force: bool = False) -> Dict:
    day = datetime.now().strftime("%Y-%m-%d")
    p = _raw_path(paths, day, ticker)
    max_age_h = float(cfg["news"].get("refetch_hours", 3))
    fresh = p.exists() and (time.time() - p.stat().st_mtime) < max_age_h * 3600
    if fresh and not force:
        heads = json.loads(p.read_text(encoding="utf-8"))
    else:
        use_api = want_newsapi and budget.take()
        heads = collect(ticker, company, cfg, use_api)
        model = Sentiment.get(cfg["news"].get("model", "vader"))
        texts = [h["title"] + (". " + h["summary"] if h.get("summary") else "") for h in heads]
        for h, s in zip(heads, model.score(texts)):
            h["sentiment"] = round(s, 4)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(heads, ensure_ascii=False, indent=1), encoding="utf-8")
    agg = aggregate(heads, float(cfg["news"].get("half_life_days", 1.0)))
    if agg["n_headlines"] < int(cfg["news"].get("min_headlines", 2)):
        agg["sentiment"] = None
    return {"ticker": ticker, "date": pd.Timestamp(day), **agg}


def update_news(universe: pd.DataFrame, cfg: Dict, paths: Paths, priority: List[str],
                progress: Optional[Callable] = None, only: Optional[Iterable[str]] = None,
                force: bool = False) -> Dict:
    """Fetch today's news for every ticker (or `only`). NewsAPI goes to `priority` first."""
    if cfg["news"].get("provider", "newsapi") == "none" and not cfg["news"].get("yahoo_news", True):
        pass  # Google News still runs; it needs no key
    names = dict(zip(universe["ticker"], universe["name"]))
    tickers = list(only) if only else list(universe["ticker"])
    budget = NewsBudget(paths, int(cfg["news"].get("daily_request_budget", 90)))
    pri = set(priority[: budget.limit])
    Sentiment.get(cfg["news"].get("model", "vader"))  # load model once, before threads
    rows = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_for, t, names.get(t, t), cfg, paths, budget, t in pri, force): t
                for t in tickers}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                rows.append(f.result())
            except Exception as e:
                print(f"[news] {futs[f]}: {e}")
            if progress:
                progress("news", i, len(tickers))
    if rows:
        new = pd.DataFrame(rows)
        if paths.news_sent.exists():
            old = pd.read_parquet(paths.news_sent)
            new = pd.concat([old, new]).drop_duplicates(["date", "ticker"], keep="last")
        new.sort_values(["date", "ticker"]).to_parquet(paths.news_sent, index=False)
    return {"tickers": len(rows), "newsapi_used": budget.used,
            "with_sentiment": sum(1 for r in rows if r["sentiment"] is not None)}


def load_sentiment_panel(paths: Paths, index: pd.DatetimeIndex, columns) -> pd.DataFrame:
    """Daily sentiment panel aligned to trading days; values carry forward up to 3 days."""
    if not paths.news_sent.exists():
        return pd.DataFrame(index=index, columns=columns, dtype=float)
    s = pd.read_parquet(paths.news_sent)
    s["date"] = ns(s["date"])
    w = s.pivot_table(index="date", columns="ticker", values="sentiment", aggfunc="last")
    w = w.reindex(columns=columns)
    full = w.reindex(w.index.union(index)).sort_index().ffill(limit=3)
    return full.reindex(index).astype(float)


def headlines(paths: Paths, ticker: str, days: int = 7) -> List[Dict]:
    out = []
    for k in range(days):
        day = (datetime.now() - timedelta(days=k)).strftime("%Y-%m-%d")
        p = _raw_path(paths, day, ticker)
        if p.exists():
            try:
                out += json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    seen, uniq = set(), []
    for h in out:
        k = _norm_title(h["title"])
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    uniq.sort(key=lambda h: h.get("published") or "", reverse=True)
    return uniq[:30]
