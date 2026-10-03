#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
NEPSE NEWS HUB v1.2.1 — Nepal stock-market news aggregator (single-file build)
===============================================================================
Backend (FastAPI) + scheduler (APScheduler 3.x) + SQLite + source adapters +
EN/NE relevance engine + dashboard (served at /) + exports. Python 3.10+.

QUICK START (Windows PowerShell; macOS/Linux equivalents in brackets)
    py -3.11 -m venv .venv                       [python3 -m venv .venv]
    .venv\Scripts\Activate.ps1                   [source .venv/bin/activate]
    pip install fastapi "uvicorn[standard]" httpx feedparser beautifulsoup4 ^
                "apscheduler>=3.10,<4" openpyxl python-dateutil
    python nepse_news_hub.py selftest            # offline tests: filter, dedup, dates, DB
    python nepse_news_hub.py verify-sources      # LIVE probe of every source -> data/source_report.md/.csv
    python nepse_news_hub.py                     # server + scheduler -> http://127.0.0.1:8000

COMMANDS
    serve [--host H] [--port P] [--no-scheduler]   (default command)
    collect [--source ID ...] [--force] [--rediscover]
    verify-sources [--only ID ...] [--apply]        (--apply writes discovered feeds into sources.json)
    selftest
    init [--reset-sources] [--reset-keywords] [--reset-settings]

ENVIRONMENT (optional; a .env file next to this script is read automatically)
    NNH_HOST=127.0.0.1   NNH_PORT=8000   NNH_DATA_DIR=./data   NNH_DB=./data/nepse_news.db
    NNH_ADMIN_TOKEN=...  (REQUIRED if exposed beyond localhost: protects all write endpoints)
    NNH_CONTACT=you@firm.com   (appended to the honest User-Agent)
    ANTHROPIC_API_KEY=...      (optional; enables labelled 30-60 word AI summaries)
    NNH_AI_MODEL=claude-haiku-4-5-20251001

DATA FILES (created on first run in ./data)
    sources.json   source registry (edit here or in the dashboard)
    keywords.json  relevance rules (groups, weights, tiers, anchors, negatives)
    settings.json  collection settings
    nepse_news.db  SQLite: articles, seen URLs, source health, run log
    nnh.log        rotating log

COLLECTION POLICY
    RSS/Atom first (configured feed -> <link rel=alternate> autodiscovery -> /feed, /rss).
    robots.txt honoured (RFC 9309 semantics), honest User-Agent, conditional GET (ETag /
    Last-Modified), per-source minimum interval, retry with backoff on 429/5xx only.
    HTML listing adapter is OPT-IN per source ("permitted": true after reviewing terms).
    No paywall/login/CAPTCHA/anti-bot circumvention; 401/403 are recorded, never bypassed.
    Only headline, link, timestamps and a <=55-word publisher excerpt are stored.
"""
import argparse
import copy
import csv
import hmac
import html
import io
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

try:
    import feedparser
    import httpx
    from bs4 import BeautifulSoup
    from fastapi import Body, Depends, FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, Response
except ImportError as _e:  # pragma: no cover
    sys.stderr.write(f"Missing dependency: {_e}\nRun: pip install fastapi \"uvicorn[standard]\" httpx feedparser "
                     "beautifulsoup4 \"apscheduler>=3.10,<4\" openpyxl python-dateutil\n")
    sys.exit(1)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(Path(__file__).resolve().parent / ".env")

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
APP_NAME = "Binjal Halwai NewsPortal"
VERSION = "1.2.8"
HOST = os.getenv("NNH_HOST", "127.0.0.1")
PORT = int(os.getenv("NNH_PORT", "8000"))
ADMIN_TOKEN = os.getenv("NNH_ADMIN_TOKEN", "")
CONTACT = os.getenv("NNH_CONTACT", "")
UA_TOKEN = "NEPSENewsHub"
USER_AGENT = os.getenv("NNH_USER_AGENT") or (
    f"{UA_TOKEN}/{VERSION} (personal financial-news aggregator; respects robots.txt"
    + (f"; contact {CONTACT}" if CONTACT else "") + ")")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL = os.getenv("NNH_AI_MODEL", "claude-haiku-4-5-20251001")
UTC = timezone.utc
NPT = timezone(timedelta(hours=5, minutes=45), "NPT")
log = logging.getLogger("nnh")


class CFG:
    data_dir = Path(os.getenv("NNH_DATA_DIR") or (Path(__file__).resolve().parent / "data"))
    db_path = Path(os.getenv("NNH_DB") or (data_dir / "nepse_news.db"))


CATEGORIES = {
    "official": "Official Notices",
    "market_daily": "NEPSE Daily Market Updates",
    "market_trends": "Market Trends & Technical Analysis",
    "ipo": "IPO, FPO & Right Shares",
    "dividend": "Dividend & Bonus Shares",
    "company": "Listed Company Announcements",
    "results": "Quarterly & Annual Results",
    "banking": "Banking & Financial Institutions",
    "hydro": "Hydropower & Energy",
    "insurance": "Insurance & Microfinance",
    "sebon": "SEBON & Regulatory Updates",
    "nrb": "NRB & Monetary Policy",
    "funds": "Mutual Funds & PMS",
    "mna": "Mergers & Acquisitions",
    "investors": "Broker & Investor Developments",
    "economy": "Economy & Capital Market",
    "budget": "Government Policy & Budget",
    "education": "Investor Education",
    "other": "Other Relevant Financial News",
}
MODE_TIERS = {"strict": ["high"], "financial": ["high", "medium"], "broad": ["high", "medium", "low"]}
MODE_LABELS = {"strict": "Strict NEPSE-only", "financial": "NEPSE + financial market",
               "broad": "Broad business & economy"}
SOURCE_TYPES = ("market_portal", "business_news", "general_news", "regulatory")
METHODS = ("auto", "rss", "listing", "manual")

DEFAULT_SETTINGS = {
    "interval_min": 30, "mode": "financial",
    "thresholds": {"high": 10.0, "medium": 5.0, "low": 2.0},
    "languages": ["en", "ne"], "group_duplicates": True,
    "ai_summaries": False, "ai_max_per_run": 15,
    "retention_days": 730, "excluded_retention_days": 7, "max_workers": 6,
}

# --------------------------------------------------------------------------------------
# Source registry (seed). Evidence reflects desk research on 29 Sep 2026; every source is
# re-verified live by `verify-sources` and on first collection. Nothing here is assumed to work.
# --------------------------------------------------------------------------------------
EV_DIR = "Feed directory (Feedspot, updated Aug 2026) lists a native feed"
EV_NONE = "Feed directory lists NO native feed ('Generate RSS')"
EV_UNV = "Unverified candidate - resolved at runtime"
EV_NOFEED = "No public feed/API identified in research"


def _s(sid, name, url, section, stype, lang, method, feeds=(), notes="", evidence=EV_UNV):
    return {"id": sid, "name": name, "url": url, "section_url": section or url, "type": stype,
            "language": lang, "method": method, "feeds": list(feeds), "notes": notes,
            "evidence": evidence, "enabled": True, "permitted": False, "listing": {},
            "min_interval_min": 15, "country": "NP"}


DEFAULT_SOURCES = [
    # A. Dedicated stock-market & financial portals
    _s("sharesansar", "ShareSansar", "https://www.sharesansar.com/", "https://www.sharesansar.com/news-page",
       "market_portal", "en", "manual", notes="Highest NEPSE news density. Market-data portal; no public feed "
       "found. Manual link unless written permission is obtained.", evidence=EV_NOFEED),
    _s("merolagani", "MeroLagani", "https://merolagani.com/", "https://merolagani.com/NewsList.aspx",
       "market_portal", "both", "manual", notes="Market-data portal; no public feed found.", evidence=EV_NOFEED),
    _s("nepalipaisa", "Nepali Paisa", "https://www.nepalipaisa.com/", "", "market_portal", "both", "auto"),
    _s("arthasarokar", "Artha Sarokar", "https://arthasarokar.com/", "", "business_news", "ne", "rss",
       ["https://arthasarokar.com/feed"], evidence=EV_DIR),
    _s("arthasansar", "Arthasansar", "https://arthasansar.com/", "", "business_news", "ne", "auto",
       ["https://arthasansar.com/feed"]),
    _s("nepsealpha", "NepseAlpha", "https://nepsealpha.com/", "", "market_portal", "en", "manual",
       notes="Analytics platform behind bot protection; not collected automatically.", evidence=EV_NOFEED),
    _s("bajarkochirfar", "Bajarko Chirfar", "https://bajarkochirfar.com/", "", "market_portal", "ne", "auto",
       ["https://bajarkochirfar.com/feed"]),
    _s("chukul", "Chukul", "https://chukul.com/", "", "market_portal", "en", "manual",
       notes="Analytics application; no news feed identified.", evidence=EV_NOFEED),
    _s("nepalsharemarket", "Nepal Share Market", "https://www.nepalsharemarket.com/", "", "market_portal",
       "both", "auto", ["https://www.nepalsharemarket.com/feed"]),
    _s("investopaper", "Investopaper", "https://www.investopaper.com/", "", "market_portal", "en", "auto",
       ["https://www.investopaper.com/feed/"]),
    _s("nepse", "Nepal Stock Exchange (NEPSE)", "https://www.nepalstock.com/",
       "https://nepalstock.com/news-and-alerts",
       "regulatory", "en", "manual",
       notes="Official exchange. News section: https://nepalstock.com/news-and-alerts. Web API is "
       "undocumented and token-protected; not used because that would circumvent access controls. "
       "No public RSS/Atom feed identified (Phase-1 verified).", evidence=EV_NOFEED),
    # B. Business & economic publishers
    _s("bizmandu", "Bizmandu", "https://bizmandu.com/", "", "business_news", "ne", "auto", ["https://bizmandu.com/feed"]),
    _s("arthiknews", "Arthik News", "https://arthiknews.com/", "", "business_news", "ne", "auto",
       ["https://arthiknews.com/feed"], notes="Feed directory lists 'Aarthik News' at aarthiknews.com - confirm "
       "which domain you intended (both are registered here)."),
    _s("aarthiknews", "Aarthik News (aarthiknews.com)", "https://aarthiknews.com/", "", "business_news", "ne",
       "auto", evidence=EV_NONE),
    _s("newbusinessage", "New Business Age", "https://www.newbusinessage.com/", "", "business_news", "en", "auto",
       evidence=EV_NONE),
    _s("karobardaily", "Karobar Daily", "https://www.karobardaily.com/", "", "business_news", "ne", "auto",
       evidence=EV_NONE),
    _s("abhiyandaily", "Arthik Abhiyan", "https://abhiyandaily.com/", "", "business_news", "ne", "rss",
       ["https://abhiyandaily.com/abhiyanrss"], evidence=EV_DIR),
    _s("nepaleconomicforum", "Nepal Economic Forum", "https://nepaleconomicforum.org/", "", "business_news", "en",
       "auto", ["https://nepaleconomicforum.org/feed/"]),
    _s("nepaleconomicnews", "Nepal Economic News", "https://nepaleconomicnews.com/", "", "business_news", "en",
       "auto", ["https://nepaleconomicnews.com/feed"]),
    _s("fiscalnepal", "Fiscal Nepal", "https://www.fiscalnepal.com/", "", "business_news", "both", "auto",
       ["https://www.fiscalnepal.com/feed/"]),
    _s("bankingkhabar", "Banking Khabar", "https://bankingkhabar.com/", "", "business_news", "ne", "auto",
       ["https://bankingkhabar.com/feed"]),
    _s("bankingsamachar", "Banking Samachar", "https://bankingsamachar.com/", "", "business_news", "ne", "auto",
       ["https://bankingsamachar.com/feed"]),
    _s("capitalnepal", "Capital Nepal", "https://www.capitalnepal.com/", "", "business_news", "both", "auto",
       notes="Additional source discovered in research."),
    _s("bizshala", "Bizshala", "https://www.bizshala.com/", "", "business_news", "ne", "auto",
       notes="Additional source discovered in research."),
    _s("clickmandu", "Clickmandu", "https://clickmandu.com/", "", "business_news", "ne", "auto",
       notes="Additional source discovered in research."),
    _s("arthapath", "Arthapath", "https://arthapath.com/", "", "business_news", "ne", "auto",
       notes="Additional source discovered in research."),
    # C. General news portals
    _s("onlinekhabar", "Onlinekhabar", "https://www.onlinekhabar.com/", "https://www.onlinekhabar.com/business",
       "general_news", "ne", "rss", ["https://www.onlinekhabar.com/feed"], evidence=EV_DIR),
    _s("onlinekhabar_en", "Onlinekhabar English", "https://english.onlinekhabar.com/", "", "general_news", "en",
       "auto", ["https://english.onlinekhabar.com/feed"], notes="Additional source discovered in research."),
    _s("ratopati", "Ratopati", "https://www.ratopati.com/", "", "general_news", "ne", "rss",
       ["https://www.ratopati.com/feed"], evidence=EV_DIR),
    _s("nepallive", "Nepal Live", "https://nepallive.com/", "", "general_news", "ne", "auto",
       ["https://nepallive.com/feed"]),
    _s("setopati", "Setopati", "https://www.setopati.com/", "", "general_news", "ne", "rss",
       ["https://www.setopati.com/feed"], evidence=EV_DIR),
    _s("ekantipur", "Kantipur", "https://ekantipur.com/", "https://ekantipur.com/business", "general_news", "ne",
       "auto", evidence=EV_NONE),
    _s("kathmandupost", "The Kathmandu Post", "https://kathmandupost.com/", "https://kathmandupost.com/money",
       "general_news", "en", "auto", evidence=EV_NONE),
    _s("himalayantimes", "The Himalayan Times", "https://thehimalayantimes.com/",
       "https://thehimalayantimes.com/business", "general_news", "en", "auto", evidence=EV_NONE),
    _s("myrepublica", "Republica", "https://myrepublica.nagariknetwork.com/", "", "general_news", "en", "auto",
       evidence=EV_NONE),
    _s("nagariknews", "Nagarik News", "https://nagariknews.nagariknetwork.com/", "", "general_news", "ne", "rss",
       ["https://nagariknews.nagariknetwork.com/feed"], evidence=EV_DIR),
    _s("nepalnews", "Nepal News", "https://nepalnews.com/", "", "general_news", "en", "auto",
       ["https://nepalnews.com/feed"]),
    _s("gorkhapatra", "Gorkhapatra", "https://gorkhapatraonline.com/", "", "general_news", "ne", "auto",
       evidence=EV_NONE),
    _s("risingnepal", "The Rising Nepal", "https://risingnepaldaily.com/", "", "general_news", "en", "auto",
       evidence=EV_NONE, notes="Additional source discovered in research."),
    _s("annapurnaexpress", "The Annapurna Express", "https://theannapurnaexpress.com/", "", "general_news", "en",
       "auto", evidence=EV_NONE),
    _s("khabarhub_en", "Khabarhub English", "https://english.khabarhub.com/", "", "general_news", "en", "auto",
       ["https://english.khabarhub.com/feed/"]),
    _s("nepalpress", "Nepal Press", "https://www.nepalpress.com/", "", "general_news", "ne", "auto",
       ["https://www.nepalpress.com/feed"]),
    _s("ujyaalo", "Ujyaalo Online", "https://ujyaaloonline.com/", "", "general_news", "ne", "auto", evidence=EV_NONE),
    _s("rss_agency", "Rastriya Samachar Samiti", "https://rss.com.np/", "", "general_news", "both", "auto",
       notes="Brief lists rss.com.np; Wikipedia lists rssnepal.org.np as the agency site - confirm domain."),
    _s("newsofnepal", "News of Nepal", "https://newsofnepal.com/", "", "general_news", "ne", "rss",
       ["https://newsofnepal.com/feed/"], evidence=EV_DIR),
    _s("avenues", "Avenues TV", "https://avenues.tv/", "", "general_news", "ne", "auto", ["https://avenues.tv/feed"]),
    _s("imagekhabar", "Image Khabar", "https://www.imagekhabar.com/", "", "general_news", "ne", "auto",
       ["https://www.imagekhabar.com/feed"]),
    _s("nepalaaja", "Nepal Aaja", "https://nepalaaja.com/", "", "general_news", "ne", "auto",
       ["https://nepalaaja.com/feed"]),
    _s("nepalitimes", "Nepali Times", "https://www.nepalitimes.com/", "", "general_news", "en", "rss",
       ["https://www.nepalitimes.com/feed/"], evidence=EV_DIR, notes="Additional source discovered in research."),
    _s("rajdhanidaily", "Rajdhani Daily", "https://rajdhanidaily.com/", "", "general_news", "ne", "rss",
       ["https://rajdhanidaily.com/feed/"], evidence=EV_DIR, notes="Additional source discovered in research."),
    _s("kathmandutribune", "Kathmandu Tribune", "https://kathmandutribune.com/", "", "general_news", "en", "rss",
       ["https://kathmandutribune.com/feed/"], evidence=EV_DIR, notes="Additional source discovered in research."),
    _s("nayapatrika", "Naya Patrika", "https://nayapatrikadaily.com/", "", "general_news", "ne", "auto",
       evidence=EV_NONE, notes="Additional source discovered in research."),
    _s("baahrakhari", "Baahrakhari", "https://baahrakhari.com/", "", "general_news", "ne", "auto",
       evidence=EV_NONE, notes="Additional source discovered in research."),
    _s("onlinetvnepal", "Online TV Nepal", "https://onlinetvnepal.com/", "", "general_news", "ne", "rss",
       ["https://onlinetvnepal.com/feed/"], evidence=EV_DIR, notes="Additional source discovered in research."),
    _s("news24nepal", "News24 Nepal", "https://news24nepal.tv/", "", "general_news", "ne", "rss",
       ["https://news24nepal.tv/feed/"], evidence=EV_DIR, notes="Additional source discovered in research."),
    _s("osnepal", "OS Nepal", "https://www.osnepal.com/", "", "general_news", "ne", "rss",
       ["https://www.osnepal.com/feed"], evidence=EV_DIR, notes="Additional source discovered in research."),
    # D. Official market, regulatory & government sources
    _s("sebon", "Securities Board of Nepal (SEBON)", "https://www.sebon.gov.np/",
       "https://www.sebon.gov.np/news",
       "regulatory", "both", "manual",
       notes="Highest-value official source. News section: https://www.sebon.gov.np/news. "
       "No public RSS/Atom feed identified (Phase-1 verified). Enable listing adapter only after "
       "confirming the public notice page may be polled per its terms.", evidence=EV_NOFEED),
    _s("cdsc", "CDS and Clearing Ltd (CDSC)", "https://cdsc.com.np/",
       "https://cdsc.com.np/Home/news",
       "regulatory", "en", "manual",
       notes="Official CDS clearing house. News section: https://cdsc.com.np/Home/news. "
       "No public RSS/Atom feed identified (Phase-1 verified).", evidence=EV_NOFEED),
    _s("nrb", "Nepal Rastra Bank (NRB)", "https://www.nrb.org.np/", "", "regulatory", "both", "auto",
       notes="Feed availability resolved at runtime; otherwise manual."),
    _s("mof", "Ministry of Finance", "https://mof.gov.np/", "", "regulatory", "both", "manual", evidence=EV_NOFEED),
    _s("nia", "Nepal Insurance Authority", "https://nia.gov.np/", "", "regulatory", "both", "manual",
       evidence=EV_NOFEED),
    _s("ocr", "Office of the Company Registrar", "https://ocr.gov.np/", "", "regulatory", "ne", "manual",
       notes="Registry service, low news value.", evidence=EV_NOFEED),
    _s("doind", "Department of Industry", "https://doind.gov.np/", "", "regulatory", "ne", "manual",
       evidence=EV_NOFEED),
    _s("ibn", "Investment Board Nepal", "https://ibn.gov.np/", "", "regulatory", "en", "manual", evidence=EV_NOFEED),
    # E. General portal sources requested in Phase-1 — feed availability requires live verification
    _s("hamropatro", "Hamro Patro", "https://www.hamropatro.com/",
       "https://www.hamropatro.com/news",
       "general_news", "ne", "auto",
       notes="Phase-1 addition. General Nepali portal with a news section. "
       "RSS/Atom feed availability unverified — run 'verify-sources --only hamropatro' to check. "
       "If no feed is found, set method=manual.",
       evidence=EV_UNV),
    _s("suryapatro", "Suryapatro", "https://suryapatro.com/",
       "https://suryapatro.com/news",
       "general_news", "ne", "auto",
       notes="Phase-1 addition. Nepali calendar/news portal. "
       "RSS/Atom feed availability unverified — run 'verify-sources --only suryapatro' to check. "
       "If no feed is found, set method=manual.",
       evidence=EV_UNV),
    # F. Phase-2 additions
    _s("sharehubnepal", "ShareHub Nepal", "https://www.sharehubnepal.com/",
       "https://www.sharehubnepal.com/news",
       "market_portal", "en", "rss",
       ["https://www.sharehubnepal.com/feed"],
       notes="Phase-2 addition. Nepal stock market news portal covering NEPSE daily updates, IPO, "
       "dividend, company results, mutual funds, rights shares, AGM notices and SEBON regulatory updates. "
       "RSS feed confirmed at /feed (Feedspot listed). Run 'verify-sources --only sharehubnepal' to verify live.",
       evidence="RSS feed listed on Feedspot and confirmed at /feed (Oct 2026)"),
]

# --------------------------------------------------------------------------------------
# Relevance rules (seed). Score = sum over matched groups of
#   weight x min(2, (1.5 if in headline else 1.0) + 0.25 x (distinct terms - 1))
# 'requires_anchor' groups get x0.6 when the only Nepal anchor is the publisher's origin.
# Negative terms with no explicit Nepal anchor => excluded.
# --------------------------------------------------------------------------------------
def _g(gid, label, weight, tier, cats, en, ne, requires_anchor=True, anchor=False):
    return {"id": gid, "label": label, "weight": weight, "tier": tier, "categories": cats,
            "requires_anchor": requires_anchor, "anchor": anchor, "enabled": True, "en": en, "ne": ne}


DEFAULT_KEYWORDS = {
    "version": 1,
    "anchors": {
        "en": ["Nepal", "Nepali", "Nepalese", "Kathmandu", "Lalitpur", "Pokhara", "NEPSE", "Nepse", "SEBON",
               "NRB", "Nepal Rastra Bank", "Rastra Bank", "CDSC", "MeroShare"],
        "ne": ["नेपाल", "नेप्से", "राष्ट्र बैंक", "धितोपत्र बोर्ड", "काठमाडौं", "मेरोशेयर"],
    },
    "negative": {
        "en": ["Wall Street", "Dow Jones", "Nasdaq", "NYSE", "S&P 500", "Sensex", "Nifty", "FTSE", "Nikkei",
               "Hang Seng", "Shanghai Composite", "Bombay Stock Exchange", "Federal Reserve",
               "Reserve Bank of India", "RBI", "European Central Bank", "ECB", "Bank of Japan", "Bank of England",
               "cricket", "football", "World Cup", "box office", "Bollywood"],
        "ne": ["सेन्सेक्स", "निफ्टी", "क्रिकेट", "फुटबल", "चलचित्र", "बलिउड"],
    },
    "groups": [
        _g("market_specific", "NEPSE & exchange (specific)", 12, "high", ["market_daily"],
           ["NEPSE", "Nepse", "NEPSE index", "Nepal Stock Exchange", "Nepalese stock market", "sensitive index",
            "float index"],
           ["नेप्से", "नेपाल स्टक एक्सचेन्ज", "नेप्से परिसूचक", "संवेदनशील परिसूचक", "फ्लोट परिसूचक"],
           requires_anchor=False),
        _g("market_generic", "Stock market (generic)", 8, "high", ["market_daily"],
           ["share market", "stock market", "stock exchange", "secondary market", "share price", "stock price",
            "market turnover", "trading volume", "daily turnover", "market capitalization",
            "market capitalisation", "circuit breaker", "listed companies", "listed company",
            "securities market", "floorsheet", "floor sheet"],
           ["शेयर बजार", "सेयर बजार", "धितोपत्र बजार", "दोस्रो बजार", "शेयर मूल्य", "सेयर मूल्य",
            "शेयर कारोबार", "सेयर कारोबार", "शेयर खरिद बिक्री", "बजार पुँजीकरण", "कारोबार रकम",
            "कारोबार संख्या", "सर्किट ब्रेकर", "सूचीकृत कम्पनी", "परिसूचक", "अंकले बढ्यो", "अंकले घट्यो",
            "अंकले उकालो", "अंकले ओरालो"]),
        _g("market_trends", "Trends & technicals", 5, "medium", ["market_trends"],
           ["bull market", "bear market", "bullish", "bearish", "market correction", "market rally",
            "technical analysis", "support level", "resistance level", "moving average", "RSI",
            "investor sentiment", "profit booking", "profit-taking", "sell-off",
            "Pine Script", "Pine Strategy", "Pine AI", "technical indicator", "trading indicator", "chart pattern",
            "candlestick"],
           ["प्राविधिक विश्लेषण", "सपोर्ट लेभल", "रेजिस्टेन्स", "बुलिस", "बियरिस", "बजार करेक्सन", "नाफा बुकिङ",
            "पाइन स्क्रिप्ट", "टेक्निकल एनालिसिस", "चार्ट प्याटर्न", "क्यान्डलस्टिक"]),
        _g("primary_market", "IPO / FPO / rights", 10, "high", ["ipo"],
           ["IPO", "initial public offering", "FPO", "further public offering", "right share", "rights share",
            "rights issue", "right issue", "public issue", "share allotment", "issue manager", "underwriter",
            "book building", "ASBA", "C-ASBA", "MeroShare", "share registrar", "debenture", "primary market",
            "auction of ordinary shares"],
           ["आईपीओ", "एफपीओ", "हकप्रद", "सार्वजनिक निष्कासन", "प्राथमिक बजार", "बाँडफाँड", "मेरोशेयर",
            "डिबेन्चर", "ऋणपत्र", "बुक बिल्डिङ", "धितोपत्र निष्कासन"], requires_anchor=False),
        _g("dividend", "Dividend / bonus / book closure", 10, "high", ["dividend"],
           ["dividend", "cash dividend", "stock dividend", "bonus share", "book closure", "book close",
            "record date"],
           ["लाभांश", "नगद लाभांश", "बोनस शेयर", "बोनस सेयर", "बुक क्लोज", "बुक क्लोजर"], requires_anchor=False),
        _g("corporate", "Company announcements", 8, "high", ["company"],
           ["AGM", "annual general meeting", "SGM", "special general meeting", "board meeting",
            "promoter share", "lock-in period", "company announcement", "share swap"],
           ["साधारण सभा", "विशेष साधारण सभा", "सञ्चालक समितिको बैठक", "संस्थापक शेयर", "संस्थापक सेयर",
            "लक-इन"]),
        _g("results", "Financial results", 8, "high", ["results"],
           ["quarterly report", "quarterly financial", "quarterly results", "financial results",
            "financial statement", "annual report", "net profit", "net loss", "operating profit", "EPS",
            "earnings per share", "NAV", "net worth per share", "book value per share", "ROE", "P/E ratio",
            "price-to-earnings", "distributable profit", "unaudited"],
           ["त्रैमासिक", "वित्तीय विवरण", "खुद नाफा", "खुद मुनाफा", "खुद घाटा", "प्रतिशेयर आम्दानी",
            "प्रतिशेयर नेटवर्थ", "वितरणयोग्य नाफा", "नाफा", "मुनाफा"]),
        _g("mna", "Mergers & acquisitions", 9, "high", ["mna"],
           ["merger", "acquisition", "amalgamation", "merged", "letter of intent"],
           ["मर्जर", "गाभिन", "गाभ्ने", "गाभिए", "एक्विजिसन", "अधिग्रहण"]),
        _g("sebon", "SEBON & securities regulation", 10, "high", ["sebon"],
           ["SEBON", "Securities Board of Nepal", "securities regulation", "capital market reform",
            "securities act", "stock broker", "stockbroker", "broker license", "broker licence",
            "investor protection", "margin trading", "CDS and Clearing", "CDSC", "demat", "TMS",
            "online trading system", "credit rating"],
           ["धितोपत्र बोर्ड", "धितोपत्र ऐन", "पुँजी बजार सुधार", "लगानीकर्ता संरक्षण", "सिडिएससी",
            "डिम्याट", "टीएमएस", "क्रेडिट रेटिङ", "धितोपत्र दलाल"]),
        _g("margin_credit", "Margin lending & share loans", 9, "high", ["nrb", "investors"],
           ["margin lending", "margin loan", "share loan", "share-backed loan", "loan against shares"],
           ["मार्जिन कर्जा", "मार्जिन प्रकृतिको कर्जा", "शेयर कर्जा", "सेयर कर्जा", "शेयर धितो कर्जा"]),
        _g("capital_gains", "Capital gains & market taxation", 9, "high", ["budget"],
           ["capital gains tax", "capital gain tax", "CGT"], ["पुँजीगत लाभ कर"]),
        _g("nrb", "NRB & monetary policy", 6, "medium", ["nrb"],
           ["Nepal Rastra Bank", "NRB", "central bank", "monetary policy", "policy rate", "repo rate",
            "reverse repo", "standing deposit facility", "standing liquidity facility",
            "interest rate corridor", "CRR", "SLR", "CCD ratio", "CD ratio", "liquidity", "interest rate",
            "base rate", "interbank rate", "inter-bank rate", "treasury bill", "T-bill"],
           ["नेपाल राष्ट्र बैंक", "राष्ट्र बैंक", "केन्द्रीय बैंक", "मौद्रिक नीति", "नीतिगत दर", "रिपो",
            "अनिवार्य नगद अनुपात", "वैधानिक तरलता अनुपात", "तरलता", "ब्याजदर", "ब्याज दर", "आधार दर",
            "अन्तरबैंक", "ट्रेजरी बिल"]),
        _g("banking", "Banks & financial institutions", 5, "medium", ["banking"],
           ["commercial bank", "development bank", "finance company", "BFIs", "banks and financial institutions",
            "deposit", "loan growth", "credit growth", "credit flow", "private sector credit", "NPL",
            "non-performing loan", "bad loan", "capital adequacy", "core capital"],
           ["वाणिज्य बैंक", "विकास बैंक", "वित्त कम्पनी", "बैंक तथा वित्तीय संस्था", "निक्षेप", "कर्जा प्रवाह",
            "कर्जा", "खराब कर्जा", "निष्क्रिय कर्जा", "पुँजीकोष", "बैंकिङ प्रणाली"]),
        _g("hydro", "Hydropower & energy", 5, "medium", ["hydro"],
           ["hydropower", "hydroelectric", "hydro project", "power purchase agreement", "PPA", "megawatt",
            "IPPAN", "Nepal Electricity Authority", "NEA", "electricity export", "power export"],
           ["जलविद्युत", "विद्युत खरिद सम्झौता", "पीपीए", "मेगावाट", "विद्युत प्राधिकरण", "विद्युत निर्यात",
            "बिजुली निर्यात", "इप्पान", "ऊर्जा"]),
        _g("insurance", "Insurance & microfinance", 5, "medium", ["insurance"],
           ["insurance", "life insurance", "non-life insurance", "reinsurance", "insurer", "microfinance",
            "laghubitta", "Nepal Insurance Authority"],
           ["बीमा", "जीवन बीमा", "निर्जीवन बीमा", "पुनर्बीमा", "बीमक", "लघुवित्त", "लघुबित्त", "बीमा प्राधिकरण"]),
        _g("funds", "Mutual funds, PMS & asset management", 7, "high", ["funds"],
           ["mutual fund", "portfolio management", "PMS", "discretionary portfolio", "asset management",
            "fund manager", "investment company", "institutional investor", "SIP", "open-ended fund",
            "close-ended fund", "closed-end fund", "merchant banker"],
           ["म्युचुअल फण्ड", "म्युचुअल फन्ड", "सामूहिक लगानी कोष", "पोर्टफोलियो व्यवस्थापन",
            "लगानी व्यवस्थापन", "कोष व्यवस्थापक", "मर्चेन्ट बैंकर", "संस्थागत लगानीकर्ता", "खुलामुखी",
            "बन्दमुखी"]),
        _g("investors", "Brokers & investors", 5, "medium", ["investors"],
           ["investor", "shareholder", "retail investor", "small investors", "broker", "brokerage",
            "broker commission", "investor association"],
           ["लगानीकर्ता", "शेयरधनी", "सेयरधनी", "साना लगानीकर्ता", "ब्रोकर", "दलाल", "ब्रोकर कमिसन",
            "लगानीकर्ता संघ"]),
        _g("economy", "Economy & macro", 3, "low", ["economy"],
           ["economy", "economic growth", "GDP", "inflation", "remittance", "balance of payments",
            "foreign exchange reserve", "forex reserve", "trade deficit", "current account", "gold price",
            "foreign direct investment", "FDI", "economic survey"],
           ["अर्थतन्त्र", "आर्थिक वृद्धि", "कुल गार्हस्थ्य उत्पादन", "मुद्रास्फीति", "महँगी", "रेमिट्यान्स",
            "विप्रेषण", "शोधनान्तर", "विदेशी मुद्रा सञ्चिति", "व्यापार घाटा", "चालु खाता", "सुनको भाउ",
            "सुनको मूल्य", "वैदेशिक लगानी", "आर्थिक सर्वेक्षण"]),
        _g("budget", "Government policy & budget", 4, "medium", ["budget"],
           ["budget", "fiscal year", "Ministry of Finance", "finance minister", "tax policy",
            "revenue collection", "public debt", "fiscal policy", "income tax", "customs duty"],
           ["बजेट", "आर्थिक वर्ष", "अर्थ मन्त्रालय", "अर्थमन्त्री", "कर नीति", "राजस्व", "सार्वजनिक ऋण",
            "वित्त नीति", "आयकर", "भन्सार"]),
        _g("sectors", "Other listed sectors", 2, "low", ["other"],
           ["cement", "pharmaceutical", "telecom", "hotel industry", "manufacturing", "Ncell"],
           ["सिमेन्ट", "औषधि उद्योग", "दूरसञ्चार", "होटल व्यवसाय", "उत्पादनमूलक"]),
        _g("education", "Investor education", 3, "low", ["education"],
           ["investor education", "investor awareness", "financial literacy", "how to invest"],
           ["लगानीकर्ता सचेतना", "वित्तीय साक्षरता", "लगानी गर्ने तरिका", "लगानी सचेतना"]),
        _g("companies", "Watchlist companies & symbols (SAMPLE - edit to your portfolio)", 10, "high", ["company"],
           ["Nabil Bank", "NABIL", "NIC Asia", "NICA", "Nepal Telecom", "NTC", "Himalayan Bank",
            "Global IME Bank", "GBIME", "Everest Bank", "Upper Tamakoshi", "Chilime Hydropower",
            "Nepal Life Insurance", "Citizen Investment Trust", "HIDCL"],
           ["नबिल बैंक", "नेपाल टेलिकम", "ग्लोबल आईएमई बैंक", "अपर तामाकोसी", "चिलिमे"],
           requires_anchor=False, anchor=True),
    ],
}

# --------------------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------------------
def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_iso(s):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def npt_str(s, fmt="%d %b %Y %H:%M"):
    d = parse_iso(s)
    return d.astimezone(NPT).strftime(fmt) if d else ""


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def is_http_url(u) -> bool:
    try:
        p = urlparse(str(u))
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc) and " " not in str(u)


_TRACK = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$|ref_src$|amp$|s$)")


def canonical_url(u: str) -> str:
    """Return a normalised, deduplicated URL: strip www., tracking params, AMP suffix, trailing slash.
    NOTE: URLs are intentionally NOT Unicode-normalised here — percent-encoding is preserved as-is."""
    p = urlparse(u.strip())
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACK.match(k.lower())]
    path = p.path or "/"
    if path.endswith("/amp"):
        path = path[:-4] or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse(("https", host, path, "", urlencode(sorted(q)), ""))


def clean_text(s) -> str:
    """Strip HTML entities and tags, apply NFC normalisation for correct Unicode (including Devanagari)."""
    s = html.unescape(s or "")
    if "<" in s and ">" in s:
        s = BeautifulSoup(s, "html.parser").get_text(" ")
    s = unicodedata.normalize("NFC", s)   # Phase-1: canonical Unicode for Devanagari & mixed text
    return re.sub(r"\s+", " ", s).strip()


_WP_TAIL = re.compile(r"\s*(The post .{0,300}? appeared first on .{0,160}?\.?|\[(?:…|\.\.\.)\]|Continue reading.*)$",
                      re.I | re.S)


def make_excerpt(raw, words=55) -> str:
    """Clean HTML/entities, strip WordPress boilerplate tail, return a publisher excerpt up to `words` words.
    Output is NFC-normalised (via clean_text) so Devanagari characters are stored in canonical form."""
    t = _WP_TAIL.sub("", clean_text(raw)).strip()
    w = t.split()
    return " ".join(w[:words]) + ("…" if len(w) > words else "")


def safe_cell(v):
    """Neutralise spreadsheet formula injection (=, +, -, @) in exported text."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


# --------------------------------------------------------------------------------------
# Text normalisation (EN/NE), language detection, matching
# --------------------------------------------------------------------------------------
_DEV = re.compile(r"[\u0900-\u097F]")
_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
# Orthographic variance in Nepali web text: शेयर/सेयर, पुँजी/पूँजी, ई/इ, ऊ/उ, nukta, ZWJ/ZWNJ.
_NE_MAP = str.maketrans({"ँ": "ं", "ी": "ि", "ू": "ु", "ई": "इ", "ऊ": "उ", "श": "स", "ष": "स",
                         "\u200c": None, "\u200d": None, "\u093c": None})


def norm_text(s) -> str:
    """NFC-normalise then apply Nepali orthographic equivalences (शेयर≈सेयर, ई≈इ, ँ≈ं, nukta, ZWJ).
    Safe for English text and mixed Nepali/English strings; does NOT alter URLs or numbers."""
    s = unicodedata.normalize("NFC", s or "")
    s = s.translate(_NE_MAP).translate(_DEV_DIGITS)
    return re.sub(r"\s+", " ", s).strip()


def detect_lang(text) -> str:
    letters = [c for c in (text or "") if c.isalpha() or _DEV.match(c)]
    if not letters:
        return "unknown"
    dev = sum(1 for c in letters if "\u0900" <= c <= "\u097F")
    return "ne" if dev / len(letters) >= 0.3 else "en"


_STOP_EN = set("a an the of in on at to for from by with as and or is are was were be been has have had will "
               "would its it this that into over after amid than per up down out new says said".split())
_NE_SUFFIXES = ("हरुको", "द्वारा", "लाई", "बाट", "सँग", "संग", "हरु", "को", "का", "कि", "मा", "ले")


def title_key(title) -> str:
    return re.sub(r"[^\w\u0900-\u097F]+", " ", norm_text(title).lower()).strip()


def title_tokens(title) -> set:
    toks = set()
    for w in title_key(title).split():
        if w in _STOP_EN:
            continue
        if _DEV.search(w):
            for suf in _NE_SUFFIXES:
                if w.endswith(suf) and len(w) > len(suf) + 1:
                    w = w[: -len(suf)]
                    break
        toks.add(w)
    return toks


class Matcher:
    """EN: whole-word, plural-tolerant, case-insensitive (CAPS acronyms <=6 chars are case-sensitive).
    NE: normalised, must start at a word boundary, suffixes allowed, spaces optional between words."""

    def __init__(self, term: str):
        self.term = term.strip()
        t = norm_text(self.term)
        if _DEV.search(t):
            body = r"\s*".join(re.escape(p) for p in t.split(" "))
            sfx = "|".join(re.escape(norm_text(s)) for s in _NE_SUFFIXES)
            # term must end at a word boundary, optionally followed by up to two grammatical suffixes
            # (fixes रिपो matching रिपोर्ट, अधिग्रहण matching unrelated compounds, etc.)
            self.rx = re.compile(r"(?<![\u0900-\u097F])" + body + r"(?:" + sfx + r"){0,2}(?![\u0900-\u097F])")
        else:
            acronym = t.isupper() and len(t.replace(" ", "")) <= 6
            body = r"[\s\-]+".join(re.escape(w) for w in t.split())
            if t[-1:].isalpha():
                body += "s?" if acronym else "(?:s|es)?"
            self.rx = re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", 0 if acronym else re.I)

    def search(self, text) -> bool:
        return bool(self.rx.search(text))


def _terms(block):
    if isinstance(block, dict):
        return [t for lang in ("en", "ne") for t in (block.get(lang) or []) if str(t).strip()]
    return [t for t in (block or []) if str(t).strip()]


class Classifier:
    def __init__(self, kw: dict, thresholds: dict):
        self.anchors = [Matcher(t) for t in _terms(kw.get("anchors"))]
        self.negative = [Matcher(t) for t in _terms(kw.get("negative"))]
        self.groups = []
        for g in kw.get("groups", []):
            if g.get("enabled", True):
                ms = [Matcher(t) for t in _terms({"en": g.get("en"), "ne": g.get("ne")})]
                if ms:
                    self.groups.append((g, ms))
        self.th = {k: float(v) for k, v in thresholds.items()}

    def classify(self, title: str, body: str, source: dict) -> dict:
        t, b = norm_text(title), norm_text(body)
        full = t + " \n " + b
        lang = detect_lang(f"{title} {body}")
        explicit_anchor = any(m.search(full) for m in self.anchors)
        negatives = [m.term for m in self.negative if m.search(full)]
        hits = []
        for g, ms in self.groups:
            in_t = [m.term for m in ms if m.search(t)]
            in_b = [m.term for m in ms if m.term not in in_t and m.search(b)]
            if in_t or in_b:
                hits.append((g, in_t + in_b, bool(in_t)))
                if g.get("anchor"):
                    explicit_anchor = True
        implicit_anchor = source.get("country", "NP") == "NP"
        score, cats, kws, high_hit = 0.0, defaultdict(float), [], False
        for g, terms, in_title in hits:
            s = float(g["weight"]) * min(2.0, (1.5 if in_title else 1.0) + 0.25 * (len(set(terms)) - 1))
            if g.get("requires_anchor") and not explicit_anchor:
                s *= 0.6 if implicit_anchor else 0.0
            if s <= 0:
                continue
            score += s
            high_hit = high_hit or g.get("tier") == "high"
            for c in g.get("categories", []):
                cats[c] += s
            kws.extend(terms)
        if negatives:
            score = max(0.0, score - 3) if explicit_anchor else 0.0
        if source.get("type") == "regulatory":
            score = max(score, self.th["medium"])
            cats["official"] += 1e6
        th = self.th
        if score >= th["high"] and high_hit:
            tier = "high"
        elif score >= th["medium"]:
            tier = "medium"
        elif score >= th["low"]:
            tier = "low"
        else:
            tier = "excluded"
        categories = [c for c, _ in sorted(cats.items(), key=lambda x: -x[1]) if c in CATEGORIES][:3]
        if tier != "excluded" and not categories:
            categories = ["other"]
        return {"language": lang, "relevance": tier, "score": round(score, 2),
                "categories": categories if tier != "excluded" else [],
                "keywords": list(dict.fromkeys(kws))[:12], "negatives": negatives}


# --------------------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------------------
def sanity_date(dt, now):
    """Reject future/implausible timestamps; repair the common 'NPT labelled as UTC' feed bug."""
    if dt is None:
        return None, "missing"
    if dt > now + timedelta(minutes=15):
        adj = dt - timedelta(hours=5, minutes=45)
        if now - timedelta(days=2) <= adj <= now + timedelta(minutes=15):
            return adj, "adjusted"
        return None, "suspect"
    if dt.year < 2000:
        return None, "suspect"
    return dt, "source"


def parse_feed_date(entry, now):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(key)
        if st:
            try:
                return sanity_date(datetime(*st[:6], tzinfo=UTC), now)
            except (TypeError, ValueError):
                continue
    return None, "missing"


_REL = re.compile(r"(\d+)\s*(मिनेट|minutes?|mins?|घण्टा|घन्टा|hours?|hrs?|दिन|days?)\s*(अगाडि|अघि|ago)?", re.I)


def parse_loose_date(raw, now):
    s = norm_text(raw)
    m = _REL.search(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith(("मिनेट", "min")):
            delta = timedelta(minutes=n)
        elif unit.startswith(("घण्टा", "घन्टा", "h")):
            delta = timedelta(hours=n)
        else:
            delta = timedelta(days=n)
        return now - delta, "relative"
    try:
        from dateutil import parser as dparser
        dt = dparser.parse(s, fuzzy=True, default=datetime(now.year, 1, 1))
    except (ValueError, OverflowError, ImportError):
        return None, "missing"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NPT)  # site-local times assumed to be NPT
    d, q = sanity_date(dt.astimezone(UTC), now)
    return d, ("parsed" if q == "source" else q)


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL, url TEXT NOT NULL, canonical_url TEXT NOT NULL UNIQUE, title_key TEXT NOT NULL,
  source_id TEXT NOT NULL, source_name TEXT NOT NULL, source_type TEXT,
  published_at TEXT, date_quality TEXT, retrieved_at TEXT NOT NULL, sort_at TEXT NOT NULL,
  language TEXT, categories TEXT NOT NULL DEFAULT '[]', primary_category TEXT,
  relevance TEXT NOT NULL, score REAL, keywords TEXT NOT NULL DEFAULT '[]',
  excerpt TEXT, summary_ai TEXT, group_id INTEGER, saved INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'classified', search_text TEXT);
CREATE INDEX IF NOT EXISTS ix_art_sort ON articles(sort_at);
CREATE INDEX IF NOT EXISTS ix_art_rel_sort ON articles(relevance, sort_at);
CREATE INDEX IF NOT EXISTS ix_art_src_sort ON articles(source_id, sort_at);
CREATE INDEX IF NOT EXISTS ix_art_lang_sort ON articles(language, sort_at);
CREATE INDEX IF NOT EXISTS ix_art_cat ON articles(primary_category);
CREATE INDEX IF NOT EXISTS ix_art_group ON articles(group_id);
CREATE INDEX IF NOT EXISTS ix_art_saved ON articles(saved);
CREATE INDEX IF NOT EXISTS ix_art_tkey ON articles(source_id, title_key);
CREATE TABLE IF NOT EXISTS seen(canonical_url TEXT PRIMARY KEY, first_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_health(
  source_id TEXT PRIMARY KEY, status TEXT, last_checked TEXT, last_success TEXT, last_discovery TEXT,
  resolved_feeds TEXT, cond TEXT, articles_total INTEGER DEFAULT 0, relevant_total INTEGER DEFAULT 0,
  last_items INTEGER DEFAULT 0, last_new INTEGER DEFAULT 0, last_error TEXT, error_count INTEGER DEFAULT 0,
  reachable INTEGER);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, trigger TEXT,
  sources INTEGER, new_articles INTEGER, relevant INTEGER, errors INTEGER);
"""
HEALTH_COLS = ("status", "last_checked", "last_success", "last_discovery", "resolved_feeds", "cond",
               "articles_total", "relevant_total", "last_items", "last_new", "last_error", "error_count",
               "reachable")


def connect():
    c = sqlite3.connect(CFG.db_path, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    return c


@contextmanager
def db():
    c = connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db():
    CFG.db_path.parent.mkdir(parents=True, exist_ok=True)
    c = connect()
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        c.commit()
    finally:
        c.close()


def get_health(c, sid) -> dict:
    r = c.execute("SELECT * FROM source_health WHERE source_id=?", (sid,)).fetchone()
    return dict(r) if r else {}


def upsert_health(c, sid, **kw):
    kw = {k: v for k, v in kw.items() if k in HEALTH_COLS}
    c.execute("INSERT OR IGNORE INTO source_health(source_id) VALUES(?)", (sid,))
    if kw:
        c.execute(f"UPDATE source_health SET {', '.join(k + '=?' for k in kw)} WHERE source_id=?",
                  (*kw.values(), sid))


def purge(settings):
    now = utcnow()
    with db() as c:
        c.execute("DELETE FROM articles WHERE relevance='excluded' AND saved=0 AND retrieved_at<?",
                  (iso(now - timedelta(days=int(settings["excluded_retention_days"]))),))
        cutoff = iso(now - timedelta(days=int(settings["retention_days"])))
        c.execute("DELETE FROM articles WHERE saved=0 AND retrieved_at<?", (cutoff,))
        c.execute("DELETE FROM seen WHERE first_seen<?", (cutoff,))
    log.info("Retention purge complete")


# --------------------------------------------------------------------------------------
# Config store (JSON files are the editable source of truth)
# --------------------------------------------------------------------------------------
_SOURCE_KEYS = ("id", "name", "url", "section_url", "type", "language", "method", "feeds", "notes", "evidence",
                "enabled", "permitted", "listing", "min_interval_min", "country")


def normalize_source(s: dict) -> dict:
    d = {"type": "general_news", "language": "both", "method": "auto", "feeds": [], "notes": "",
         "evidence": "User-added", "enabled": True, "permitted": False, "listing": {}, "min_interval_min": 15,
         "country": "NP"}
    d.update({k: v for k, v in s.items() if k in _SOURCE_KEYS and v is not None})
    d["section_url"] = d.get("section_url") or d.get("url")
    return d


class Store:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.p_sources, self.p_kw, self.p_settings = (self.dir / "sources.json", self.dir / "keywords.json",
                                                      self.dir / "settings.json")
        for p, default in ((self.p_sources, DEFAULT_SOURCES), (self.p_kw, DEFAULT_KEYWORDS),
                           (self.p_settings, DEFAULT_SETTINGS)):
            if not p.exists():
                self._write(p, default)

    def _read(self, p):
        with self.lock:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ValueError(f"{p.name} is not valid JSON (line {e.lineno}): {e.msg}") from e

    def _write(self, p, obj):
        with self.lock:
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, p)

    def sources(self):
        return [normalize_source(s) for s in self._read(self.p_sources)]

    def save_sources(self, lst):
        self._write(self.p_sources, lst)

    def keywords(self):
        return self._read(self.p_kw)

    def save_keywords(self, kw):
        self._write(self.p_kw, kw)

    def settings(self):
        s = copy.deepcopy(DEFAULT_SETTINGS)
        s.update(self._read(self.p_settings))
        th = dict(DEFAULT_SETTINGS["thresholds"])
        th.update(s.get("thresholds") or {})
        s["thresholds"] = th
        if s.get("mode") not in MODE_TIERS:
            s["mode"] = "financial"
        return s

    def save_settings(self, s):
        self._write(self.p_settings, s)


# --------------------------------------------------------------------------------------
# HTTP client with robots.txt, retries and honest identification
# --------------------------------------------------------------------------------------
class CollectError(Exception):
    pass


class Http:
    def __init__(self):
        self.client = httpx.Client(
            timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en,ne;q=0.9",
                     "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5"})
        self._robots, self._lock = {}, threading.Lock()

    def get(self, url, headers=None, retries=2):
        """HTTP GET with exponential back-off on 429/5xx and transport errors.
        One source's failure is caught by the caller and never propagates to other sources."""
        delay = 2.0
        for attempt in range(retries + 1):
            try:
                r = self.client.get(url, headers=headers or {})
            except httpx.TransportError:
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 3
                    continue
                raise
            except httpx.TimeoutException:
                # Treat a timeout like a transport error — retry with back-off
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 3
                    continue
                raise
            if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
                ra = r.headers.get("Retry-After", "")
                wait = min(float(ra), 60.0) if ra.isdigit() else delay
                time.sleep(wait)
                delay *= 3
                continue
            return r
        return r  # pragma: no cover

    def allowed(self, url) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self._lock:
            ent = self._robots.get(base)
        if not ent or time.time() - ent[0] > 43200:
            rp = robotparser.RobotFileParser()
            try:
                r = self.client.get(base + "/robots.txt", timeout=10)
                if r.status_code >= 500:
                    rp.disallow_all = True           # RFC 9309: unreachable -> assume disallow
                elif r.status_code >= 400:
                    rp.allow_all = True              # RFC 9309: unavailable (4xx) -> allowed
                else:
                    rp.parse(r.text.splitlines())
            except (httpx.HTTPError, httpx.TimeoutException):
                rp.disallow_all = True
            ent = (time.time(), rp)
            with self._lock:
                self._robots[base] = ent
        return ent[1].can_fetch(UA_TOKEN, url)


# --------------------------------------------------------------------------------------
# Source adapters
# --------------------------------------------------------------------------------------
def try_feed(http, url):
    try:
        if not http.allowed(url):
            return False, 0, "disallowed by robots.txt"
        r = http.get(url, retries=1)
    except httpx.HTTPError as e:
        return False, 0, type(e).__name__
    if r.status_code != 200:
        return False, 0, f"HTTP {r.status_code}"
    fp = feedparser.parse(r.content)
    if not fp.entries or not getattr(fp, "version", ""):
        return False, 0, "not an RSS/Atom feed"
    return True, len(fp.entries), ""


def discover_feeds(http, src) -> dict:
    """Configured feeds -> <link rel=alternate> autodiscovery -> common paths. Returns first working feed."""
    tried, notes, reachable = set(), [], None
    for u in [u for u in (src.get("feeds") or []) if is_http_url(u)]:
        tried.add(u)
        ok, n, err = try_feed(http, u)
        if ok:
            return {"feeds": [u], "reachable": True, "note": f"configured feed OK ({n} items)"}
        notes.append(f"configured feed {u} failed: {err}")
    found = []
    for page in dict.fromkeys([src.get("url"), src.get("section_url")]):
        if not page or not is_http_url(page):
            continue
        try:
            if not http.allowed(page):
                notes.append(f"{page} disallowed by robots.txt")
                reachable = True
                continue
            r = http.get(page, retries=1)
        except httpx.HTTPError as e:
            reachable = reachable or False
            notes.append(f"{page} unreachable ({type(e).__name__})")
            continue
        if r.status_code >= 500:
            reachable = reachable or False
            notes.append(f"{page} HTTP {r.status_code}")
            continue
        reachable = True
        if r.status_code >= 400:
            notes.append(f"{page} HTTP {r.status_code} (access restricted - not bypassed)")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for ln in soup.find_all("link"):
            rel = [x.lower() for x in (ln.get("rel") or [])]
            typ = (ln.get("type") or "").lower()
            if "alternate" in rel and ("rss" in typ or "atom" in typ):
                u = urljoin(str(r.url), ln.get("href") or "")
                if is_http_url(u) and "comment" not in u.lower() and u not in found:
                    found.append(u)
    if reachable:
        pu = urlparse(src["url"])
        for suffix in ("/feed", "/rss", "/rss.xml", "/feed.xml"):
            u = f"{pu.scheme}://{pu.netloc}{suffix}"
            if u not in found:
                found.append(u)
    for u in found:
        if u in tried or len(tried) >= 9:
            continue
        tried.add(u)
        ok, n, err = try_feed(http, u)
        if ok:
            return {"feeds": [u], "reachable": True, "note": f"discovered feed OK ({n} items)"}
    notes.append("no working RSS/Atom feed found")
    return {"feeds": [], "reachable": reachable, "note": "; ".join(notes)}


def fetch_feed(http, feed_url, cond: dict):
    if not http.allowed(feed_url):
        raise CollectError("disallowed by robots.txt")
    hdr = {}
    if cond.get("etag"):
        hdr["If-None-Match"] = cond["etag"]
    if cond.get("last_modified"):
        hdr["If-Modified-Since"] = cond["last_modified"]
    r = http.get(feed_url, headers=hdr)
    if r.status_code == 304:
        return [], cond
    if r.status_code != 200:
        raise CollectError(f"HTTP {r.status_code}" + (" (access restricted - not bypassed)"
                                                       if r.status_code in (401, 403) else ""))
    fp = feedparser.parse(r.content)
    if not fp.entries and fp.bozo:
        raise CollectError("response is not a valid RSS/Atom feed")
    now, items = utcnow(), []
    for e in fp.entries[:150]:
        link = urljoin(feed_url, (e.get("link") or "").strip())
        title = clean_text(e.get("title", ""))
        if not title or not is_http_url(link):
            continue
        dt, q = parse_feed_date(e, now)
        items.append({"title": title, "url": link, "published_at": dt, "date_quality": q,
                      "excerpt": make_excerpt(e.get("summary") or e.get("description") or "")})
    return items, {"etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified")}


def fetch_listing(http, src):
    """Opt-in HTML listing adapter. Runs only when the source is marked 'permitted'."""
    cfg = src.get("listing") or {}
    page = cfg.get("url") or src["section_url"]
    if not http.allowed(page):
        raise CollectError("listing page disallowed by robots.txt")
    r = http.get(page)
    if r.status_code != 200:
        raise CollectError(f"HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    host = urlparse(str(r.url)).netloc.lower().removeprefix("www.")
    now, items, seen = utcnow(), [], set()
    for a in soup.select(cfg.get("link_selector") or "article a[href], h2 a[href], h3 a[href]")[:150]:
        title = clean_text(a.get_text(" "))
        url = urljoin(str(r.url), a.get("href") or "")
        if len(title) < 12 or not is_http_url(url) or url in seen:
            continue
        if urlparse(url).netloc.lower().removeprefix("www.") != host:
            continue
        seen.add(url)
        dt, q = None, "missing"
        box = a.find_parent(["article", "li", "div"])
        if box is not None:
            tm = box.find("time")
            raw = (tm.get("datetime") or tm.get_text(" ")) if tm else ""
            if not raw and cfg.get("date_selector"):
                d = box.select_one(cfg["date_selector"])
                raw = d.get_text(" ") if d else ""
            if raw:
                dt, q = parse_loose_date(raw, now)
        items.append({"title": title, "url": url, "published_at": dt, "date_quality": q, "excerpt": ""})
    return items[:60]


# --------------------------------------------------------------------------------------
# Ingestion: dedup, classification, grouping, persistence
# --------------------------------------------------------------------------------------
def find_group(c, title, lang, when):
    toks = title_tokens(title)
    if len(toks) < 3:
        return None
    rows = c.execute("SELECT id, title, group_id FROM articles WHERE relevance!='excluded' AND language=? "
                     "AND sort_at BETWEEN ? AND ? ORDER BY id DESC LIMIT 400",
                     (lang, iso(when - timedelta(hours=48)), iso(when + timedelta(hours=48)))).fetchall()
    best, best_s = None, 0.0
    for r in rows:
        o = title_tokens(r["title"])
        if len(o) >= 3:
            s = len(toks & o) / len(toks | o)
            if s > best_s:
                best, best_s = r, s
    return (best["group_id"] or best["id"]) if best is not None and best_s >= 0.6 else None


def search_text_for(title, excerpt, keywords, source_name):
    return norm_text(f"{title} {excerpt} {' '.join(keywords)} {source_name}").lower()


def ingest(c, items, src, clf, settings, now=None):
    now = now or utcnow()
    tiers = set(MODE_TIERS[settings["mode"]])
    new = rel = 0
    for it in items:
        url, title = (it.get("url") or "").strip(), clean_text(it.get("title") or "")
        # Safety NFC pass: clean_text normalises, but listing/manual paths may bypass it
        title = unicodedata.normalize("NFC", title) if title else title
        if not title or not is_http_url(url):
            continue
        can = canonical_url(url)
        if c.execute("SELECT 1 FROM seen WHERE canonical_url=?", (can,)).fetchone():
            continue
        c.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (can, iso(now)))
        tkey = title_key(title)
        if c.execute("SELECT 1 FROM articles WHERE source_id=? AND title_key=? AND retrieved_at>=?",
                     (src["id"], tkey, iso(now - timedelta(days=3)))).fetchone():
            continue
        excerpt = it.get("excerpt") or ""
        res = clf.classify(title, excerpt, src)
        pub = it.get("published_at")
        gid = None
        if settings.get("group_duplicates", True) and res["relevance"] != "excluded":
            gid = find_group(c, title, res["language"], pub or now)
        cur = c.execute(
            "INSERT OR IGNORE INTO articles(title,url,canonical_url,title_key,source_id,source_name,source_type,"
            "published_at,date_quality,retrieved_at,sort_at,language,categories,primary_category,relevance,score,"
            "keywords,excerpt,group_id,search_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title, url, can, tkey, src["id"], src["name"], src.get("type"), iso(pub),
             it.get("date_quality") or ("source" if pub else "missing"), iso(now), iso(pub or now),
             res["language"], json.dumps(res["categories"]),
             res["categories"][0] if res["categories"] else None, res["relevance"], res["score"],
             json.dumps(res["keywords"], ensure_ascii=False), excerpt, gid,
             search_text_for(title, excerpt, res["keywords"], src["name"])))
        if cur.rowcount != 1:
            continue
        if gid is None:
            c.execute("UPDATE articles SET group_id=? WHERE id=?", (cur.lastrowid, cur.lastrowid))
        new += 1
        rel += res["relevance"] in tiers
    return new, rel


def reclassify_all(store) -> int:
    clf = Classifier(store.keywords(), store.settings()["thresholds"])
    srcmap = {s["id"]: s for s in store.sources()}
    n = 0
    with db() as c:
        for r in c.execute("SELECT id,title,excerpt,source_id,source_name,source_type FROM articles").fetchall():
            src = srcmap.get(r["source_id"]) or {"id": r["source_id"], "name": r["source_name"],
                                                 "type": r["source_type"], "country": "NP"}
            res = clf.classify(r["title"], r["excerpt"] or "", src)
            c.execute("UPDATE articles SET relevance=?,score=?,categories=?,primary_category=?,keywords=?,"
                      "language=?,search_text=? WHERE id=?",
                      (res["relevance"], res["score"], json.dumps(res["categories"]),
                       res["categories"][0] if res["categories"] else None,
                       json.dumps(res["keywords"], ensure_ascii=False), res["language"],
                       search_text_for(r["title"], r["excerpt"] or "", res["keywords"], r["source_name"]), r["id"]))
            n += 1
    return n


# --------------------------------------------------------------------------------------
# Optional AI summaries (labelled; source-grounded; never investment advice)
# --------------------------------------------------------------------------------------
def claude_summarize(http, title, text) -> str:
    prompt = ("Summarise this Nepali/English news article in 30-60 words of plain English. Use ONLY facts stated "
              "in the text; keep company names, dates and figures exact; attribute opinions or forecasts to whoever "
              "made them; give no investment advice and infer no market impact. Output only the summary.\n\n"
              f"HEADLINE: {title}\n\nARTICLE TEXT:\n{text[:6000]}")
    r = http.client.post("https://api.anthropic.com/v1/messages", timeout=60,
                         headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                                  "content-type": "application/json"},
                         json={"model": AI_MODEL, "max_tokens": 220,
                               "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return " ".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()


# --------------------------------------------------------------------------------------
# Collection engine & scheduler
# --------------------------------------------------------------------------------------
class Engine:
    def __init__(self, store: Store):
        self.store, self.http, self._lock = store, Http(), threading.Lock()
        self.state = {"running": False, "trigger": None, "started_at": None, "done": 0, "total": 0,
                      "last_result": None}

    def start_background(self, **kw) -> bool:
        if self.state["running"] or self._lock.locked():
            return False
        threading.Thread(target=self.run, kwargs=kw, daemon=True, name="nnh-collect").start()
        return True

    def run(self, trigger="scheduled", only=None, force=False, rediscover=False):
        if not self._lock.acquire(blocking=False):
            log.info("Collection already running; %s trigger skipped", trigger)
            return None
        started = utcnow()
        totals = {"sources": 0, "new": 0, "relevant": 0, "errors": 0}
        try:
            settings = self.store.settings()
            clf = Classifier(self.store.keywords(), settings["thresholds"])
            sources = [s for s in self.store.sources() if only is None or s["id"] in only]
            self.state.update(running=True, trigger=trigger, started_at=iso(started), done=0, total=len(sources))
            with ThreadPoolExecutor(max_workers=int(settings.get("max_workers", 6))) as ex:
                futs = {ex.submit(self._fetch_source, s, force, rediscover): s for s in sources}
                for f in as_completed(futs):
                    s = futs[f]
                    try:
                        res = f.result()
                    except Exception as e:  # adapter failure is isolated to its source
                        log.warning("Source %s failed: %s", s["id"], e)
                        res = {"status": "error", "error": f"{type(e).__name__}: {e}"[:300], "checked": True}
                    try:
                        self._record(s, res, clf, settings, totals)
                    except Exception:
                        log.exception("Recording failed for %s", s["id"])
                    self.state["done"] += 1
            if settings.get("ai_summaries") and ANTHROPIC_API_KEY:
                try:
                    self._summarize(settings)
                except Exception:
                    log.exception("AI summarisation failed")
        finally:
            fin = utcnow()
            try:
                with db() as c:
                    c.execute("INSERT INTO runs(started_at,finished_at,trigger,sources,new_articles,relevant,errors) "
                              "VALUES(?,?,?,?,?,?,?)", (iso(started), iso(fin), trigger, totals["sources"],
                                                        totals["new"], totals["relevant"], totals["errors"]))
            except Exception:
                log.exception("Could not write run log")
            self.state.update(running=False, last_result={**totals, "finished_at": iso(fin), "trigger": trigger})
            self._lock.release()
        log.info("Collection (%s) done: %s", trigger, totals)
        return totals

    def _fetch_source(self, src, force=False, rediscover=False) -> dict:
        if not src.get("enabled", True):
            return {"status": "disabled"}
        method = src.get("method", "auto")
        if method == "manual":
            return {"status": "manual-only"}
        with db() as c:
            hlt = get_health(c, src["id"])
        now = utcnow()
        min_iv = timedelta(minutes=2 if force else max(5, _int(src.get("min_interval_min"), 15)))
        last = parse_iso(hlt.get("last_checked"))
        if last and now - last < min_iv:
            return {"status": "skipped"}
        if method == "listing":
            if not src.get("permitted"):
                return {"status": "manual-only", "checked": True,
                        "error": "Listing adapter not enabled: set 'permitted' after reviewing the site's terms"}
            items = fetch_listing(self.http, src)
            return {"status": "active", "items": items, "checked": True}
        feeds = json.loads(hlt.get("resolved_feeds") or "[]")
        rediscover = rediscover or (hlt.get("error_count") or 0) >= 3
        upd = {}
        if rediscover or not feeds:
            ld = parse_iso(hlt.get("last_discovery"))
            if not (rediscover or force) and ld and now - ld < timedelta(hours=24):
                return {"status": hlt.get("status") or "manual-only", "unchanged": True}
            disc = discover_feeds(self.http, src)
            upd = {"last_discovery": iso(now), "reachable": disc["reachable"]}
            if not disc["feeds"]:
                upd["resolved_feeds"] = "[]"
                return {"status": "inactive" if disc["reachable"] is False else "manual-only",
                        "health": upd, "error": disc["note"], "checked": True}
            feeds = disc["feeds"]
            upd["resolved_feeds"] = json.dumps(feeds)
        cond = json.loads(hlt.get("cond") or "{}")
        items, errors, ok = [], [], 0
        for fu in feeds:
            try:
                got, meta = fetch_feed(self.http, fu, cond.get(fu, {}))
                items += got
                ok += 1
                cond[fu] = {k: v for k, v in (meta or {}).items() if v and k in ("etag", "last_modified")}
            except CollectError as e:
                errors.append(f"{fu}: {e}")
            except httpx.HTTPError as e:
                errors.append(f"{fu}: {type(e).__name__}")
        upd["cond"] = json.dumps(cond)
        status = "error" if ok == 0 else ("partial" if errors else "active")
        return {"status": status, "items": items, "health": upd, "error": "; ".join(errors) or None,
                "checked": True}

    def _record(self, src, res, clf, settings, totals):
        st = res.get("status")
        if st == "skipped" or res.get("unchanged"):
            return
        with db() as c:
            h = get_health(c, src["id"])
            upd = dict(res.get("health") or {})
            upd["status"] = st
            if not res.get("checked"):
                upsert_health(c, src["id"], **upd)
                return
            now = iso(utcnow())
            upd["last_checked"] = now
            if st in ("active", "partial"):
                items = res.get("items") or []
                new, rel = ingest(c, items, src, clf, settings)
                upd.update(last_success=now, last_items=len(items), last_new=new,
                           articles_total=(h.get("articles_total") or 0) + new,
                           relevant_total=(h.get("relevant_total") or 0) + rel, last_error=res.get("error"),
                           error_count=0 if st == "active" else (h.get("error_count") or 0))
                totals["new"] += new
                totals["relevant"] += rel
            else:
                bad = st in ("error", "inactive")
                upd.update(last_error=res.get("error"), error_count=(h.get("error_count") or 0) + (1 if bad else 0))
                totals["errors"] += 1 if bad else 0
            upsert_health(c, src["id"], **upd)
        totals["sources"] += 1

    def _article_text(self, url) -> str:
        if not self.http.allowed(url):
            return ""
        r = self.http.get(url, retries=1)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
            t.decompose()
        root = soup.find("article") or soup.body or soup
        paras = [clean_text(p.get_text(" ")) for p in root.find_all("p")]
        return "\n".join(p for p in paras if len(p) > 40)[:6000]

    def _summarize(self, settings):
        with db() as c:
            rows = c.execute("SELECT id,url,title FROM articles WHERE summary_ai IS NULL AND relevance IN "
                             "('high','medium') AND retrieved_at>=? ORDER BY sort_at DESC LIMIT ?",
                             (iso(utcnow() - timedelta(days=2)), int(settings["ai_max_per_run"]))).fetchall()
        for r in rows:
            summary = ""
            try:
                text = self._article_text(r["url"])
                if len(text) >= 200:
                    summary = claude_summarize(self.http, r["title"], text)
            except Exception as e:
                log.warning("Summary failed for %s: %s", r["url"], e)
            with db() as c:  # '' marks "attempted, content unavailable" so it is not retried every run
                c.execute("UPDATE articles SET summary_ai=? WHERE id=?", (summary[:600], r["id"]))


class Scheduler:
    def __init__(self, engine: Engine, store: Store):
        from apscheduler.schedulers.background import BackgroundScheduler
        self.engine, self.store = engine, store
        self.s = BackgroundScheduler(timezone=UTC, job_defaults={"coalesce": True, "max_instances": 1,
                                                                 "misfire_grace_time": 300})

    def start(self):
        self.s.start()
        self.apply(self.store.settings()["interval_min"], first_delay=10)
        self.s.add_job(lambda: purge(self.store.settings()), "interval", hours=24, id="purge",
                       replace_existing=True, next_run_time=datetime.now(UTC) + timedelta(minutes=5))

    def apply(self, minutes, first_delay=None):
        if self.s.get_job("collect"):
            self.s.remove_job("collect")
        if minutes and int(minutes) > 0:
            kw = {"next_run_time": datetime.now(UTC) + timedelta(seconds=first_delay)} if first_delay else {}
            self.s.add_job(self.engine.run, "interval", minutes=int(minutes), id="collect",
                           kwargs={"trigger": "scheduled"}, **kw)

    def next_run(self):
        j = self.s.get_job("collect")
        return iso(j.next_run_time) if j and j.next_run_time else None

# --------------------------------------------------------------------------------------
# Query layer
# --------------------------------------------------------------------------------------
def _like(tok):
    return "%" + tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _ymd(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=NPT)
    except (TypeError, ValueError):
        return None


def date_bounds(preset, dfrom=None, dto=None):
    now = datetime.now(NPT)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    lo = hi = None
    if preset == "today":
        lo = today
    elif preset == "yesterday":
        lo, hi = today - timedelta(days=1), today
    elif preset == "24h":
        lo = now - timedelta(hours=24)
    elif preset == "7d":
        lo = today - timedelta(days=6)
    elif preset == "30d":
        lo = today - timedelta(days=29)
    elif preset == "custom":
        lo = _ymd(dfrom)
        d2 = _ymd(dto)
        hi = d2 + timedelta(days=1) if d2 else None
    return iso(lo), iso(hi)


def build_filters(p: dict, settings: dict, skip_category=False):
    where, args = [], []
    rel = (p.get("relevance") or "").strip()
    if p.get("saved") in ("1", "true"):
        where.append("saved = 1")
    elif rel == "all":
        where.append("relevance != 'excluded'")
    elif rel in ("high", "medium", "low", "excluded"):
        where.append("relevance = ?")
        args.append(rel)
    else:
        tiers = MODE_TIERS[p.get("mode") if p.get("mode") in MODE_TIERS else settings["mode"]]
        where.append(f"relevance IN ({','.join('?' * len(tiers))})")
        args += tiers
    for tok in norm_text(p.get("q") or "").lower().split()[:8]:
        where.append("search_text LIKE ? ESCAPE '\\'")
        args.append(_like(tok))
    if p.get("source"):
        where.append("source_id = ?")
        args.append(p["source"])
    if p.get("lang") in ("en", "ne"):
        where.append("language = ?")
        args.append(p["lang"])
    else:
        langs = [x for x in settings.get("languages") or [] if x in ("en", "ne")]
        if langs and set(langs) != {"en", "ne"}:
            where.append(f"language IN ({','.join('?' * len(langs))},'unknown')")
            args += langs
    if p.get("category") and not skip_category:
        where.append("categories LIKE ?")
        args.append(f'%"{p["category"]}"%')
    lo, hi = date_bounds(p.get("preset"), p.get("date_from"), p.get("date_to"))
    if lo:
        where.append("sort_at >= ?")
        args.append(lo)
    if hi:
        where.append("sort_at < ?")
        args.append(hi)
    return (" WHERE " + " AND ".join(where)) if where else "", args


def related_counts(c, rows) -> dict:
    gids = sorted({r["group_id"] for r in rows if r["group_id"]})
    if not gids:
        return {}
    q = f"SELECT group_id, COUNT(*) AS n FROM articles WHERE group_id IN ({','.join('?' * len(gids))}) GROUP BY group_id"
    return {x["group_id"]: x["n"] - 1 for x in c.execute(q, gids)}


def serialize(r, number=None, related=0) -> dict:
    cats = json.loads(r["categories"] or "[]")
    return {"id": r["id"], "number": number, "title": r["title"], "url": r["url"], "source_id": r["source_id"],
            "source_name": r["source_name"], "official": r["source_type"] == "regulatory",
            "published_at": r["published_at"], "date_quality": r["date_quality"], "retrieved_at": r["retrieved_at"],
            "sort_at": r["sort_at"], "language": r["language"],
            "categories": [{"id": x, "label": CATEGORIES.get(x, x)} for x in cats], "relevance": r["relevance"],
            "score": r["score"], "keywords": json.loads(r["keywords"] or "[]"), "excerpt": r["excerpt"] or "",
            "summary_ai": r["summary_ai"] or "", "group_id": r["group_id"], "related_count": related,
            "saved": bool(r["saved"])}


def fetch_rows(p, settings, limit=20000):
    where, args = build_filters(p, settings)
    order = "ASC" if p.get("sort") == "oldest" else "DESC"
    with db() as c:
        return c.execute(f"SELECT * FROM articles{where} ORDER BY sort_at {order}, id {order} LIMIT {int(limit)}",
                         args).fetchall()


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------
def _slug(name, taken):
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "source"
    sid, i = base, 2
    while sid in taken:
        sid, i = f"{base}_{i}", i + 1
    return sid


def validate_source(d: dict, taken: set, existing_id=None) -> dict:
    name, url = str(d.get("name") or "").strip(), str(d.get("url") or "").strip()
    if not name or not is_http_url(url):
        raise ValueError("A name and a valid http(s) website URL are required")
    sec = str(d.get("section_url") or "").strip() or url
    if not is_http_url(sec):
        raise ValueError("Invalid news-section URL")
    feeds = [str(f).strip() for f in (d.get("feeds") or []) if str(f).strip()]
    bad = [f for f in feeds if not is_http_url(f)]
    if bad:
        raise ValueError(f"Invalid feed URL: {bad[0]}")
    if (d.get("method") or "auto") not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    if (d.get("type") or "general_news") not in SOURCE_TYPES:
        raise ValueError(f"type must be one of {SOURCE_TYPES}")
    if (d.get("language") or "both") not in ("en", "ne", "both"):
        raise ValueError("language must be en, ne or both")
    listing = {k: str(v).strip() for k, v in (d.get("listing") or {}).items()
               if k in ("url", "link_selector", "date_selector") and str(v).strip()}
    return normalize_source({
        **d, "id": existing_id or _slug(name, taken), "name": name, "url": url, "section_url": sec, "feeds": feeds,
        "method": d.get("method") or "auto", "type": d.get("type") or "general_news",
        "language": d.get("language") or "both", "listing": listing, "permitted": bool(d.get("permitted")),
        "enabled": bool(d.get("enabled", True)), "notes": str(d.get("notes") or "")[:500],
        "min_interval_min": max(5, min(1440, _int(d.get("min_interval_min"), 15)))})


def validate_settings(new: dict, cur: dict) -> dict:
    s = copy.deepcopy(cur)
    if "interval_min" in new:
        iv = _int(new["interval_min"], -1)
        if iv != 0 and not 5 <= iv <= 1440:
            raise ValueError("interval_min must be 0 (manual only) or 5-1440")
        s["interval_min"] = iv
    if "mode" in new:
        if new["mode"] not in MODE_TIERS:
            raise ValueError("mode must be strict, financial or broad")
        s["mode"] = new["mode"]
    if "thresholds" in new:
        th = {k: float(new["thresholds"][k]) for k in ("high", "medium", "low")}
        if not th["high"] > th["medium"] > th["low"] > 0:
            raise ValueError("thresholds must satisfy high > medium > low > 0")
        s["thresholds"] = th
    for b in ("ai_summaries", "group_duplicates"):
        if b in new:
            s[b] = bool(new[b])
    for k, lo, hi in (("ai_max_per_run", 0, 200), ("retention_days", 7, 3650), ("excluded_retention_days", 1, 90)):
        if k in new:
            v = _int(new[k], -1)
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            s[k] = v
    if "languages" in new:
        langs = [x for x in new["languages"] if x in ("en", "ne")]
        if not langs:
            raise ValueError("select at least one language")
        s["languages"] = langs
    return s


def validate_keywords(kw) -> dict:
    if not isinstance(kw, dict) or not isinstance(kw.get("groups"), list):
        raise ValueError("keywords must be an object with a 'groups' list")
    ids = set()
    for g in kw["groups"]:
        gid = g.get("id")
        if not gid or gid in ids:
            raise ValueError(f"group id missing or duplicated: {gid!r}")
        ids.add(gid)
        try:
            float(g.get("weight"))
        except (TypeError, ValueError):
            raise ValueError(f"group {gid}: weight must be numeric") from None
        if g.get("tier") not in ("high", "medium", "low"):
            raise ValueError(f"group {gid}: tier must be high, medium or low")
        bad = [c for c in g.get("categories", []) if c not in CATEGORIES]
        if bad:
            raise ValueError(f"group {gid}: unknown categories {bad}; valid: {list(CATEGORIES)}")
        for lang in ("en", "ne"):
            if not isinstance(g.get(lang, []), list):
                raise ValueError(f"group {gid}: '{lang}' must be a list of terms")
    try:
        Classifier(kw, DEFAULT_SETTINGS["thresholds"])
    except re.error as e:
        raise ValueError(f"a term could not be compiled: {e}") from None
    return kw


# --------------------------------------------------------------------------------------
# Exports: CSV / XLSX / digest / standalone snapshot
# --------------------------------------------------------------------------------------
EXPORT_HEADERS = ["S.N.", "Headline", "Publisher", "Category", "Publication date (NPT)", "Publication time (NPT)",
                  "Date quality", "Language", "Relevance", "Summary", "Summary type", "Original article URL",
                  "Retrieved (NPT)"]


def export_record(i, r):
    cats = "; ".join(CATEGORIES.get(x, x) for x in json.loads(r["categories"] or "[]"))
    summ, kind = (r["summary_ai"], "AI summary") if r["summary_ai"] else (
        (r["excerpt"], "Publisher excerpt") if r["excerpt"] else ("", "None"))
    return [i, r["title"], r["source_name"], cats, npt_str(r["published_at"], "%Y-%m-%d") or "n/a",
            npt_str(r["published_at"], "%H:%M") or "n/a", r["date_quality"], r["language"], r["relevance"], summ,
            kind, r["url"], npt_str(r["retrieved_at"], "%Y-%m-%d %H:%M")]


def build_csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_HEADERS)
    for i, r in enumerate(rows, 1):
        w.writerow([safe_cell(x) for x in export_record(i, r)])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM so Excel renders Devanagari correctly


def build_xlsx(rows, filters: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "News"
    ws.append(EXPORT_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F2A36")
    link = Font(color="0563C1", underline="single")
    for i, r in enumerate(rows, 1):
        ws.append([safe_cell(x) for x in export_record(i, r)])
        for col in (2, 12):
            c = ws.cell(row=ws.max_row, column=col)
            c.hyperlink = r["url"]
            c.font = link
    for i, wdt in enumerate([6, 72, 22, 30, 14, 10, 10, 8, 10, 60, 16, 50, 17], 1):
        ws.column_dimensions[get_column_letter(i)].width = wdt
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    info = wb.create_sheet("Export info")
    info.append(["Generated (NPT)", datetime.now(NPT).strftime("%Y-%m-%d %H:%M")])
    info.append(["Generator", f"{APP_NAME} v{VERSION}"])
    info.append(["Rows", len(rows)])
    for k, v in filters.items():
        info.append([f"Filter: {k}", safe_cell(str(v))])
    info.append(["Note", "Automated classification; verify material facts against primary disclosures."])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def build_digest(period: str) -> str:
    days = 1 if period == "daily" else 7
    now = datetime.now(NPT)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    with db() as c:
        rows = c.execute("SELECT * FROM articles WHERE relevance IN ('high','medium') AND sort_at>=? "
                         "ORDER BY sort_at DESC LIMIT 1500", (iso(start),)).fetchall()
    by = defaultdict(list)
    for r in rows:
        by[r["primary_category"] or "other"].append(r)
    kind = "Daily" if days == 1 else "Weekly"
    out = [f"# NEPSE News Digest - {kind} - {now:%d %b %Y}", "",
           f"_Window: {start:%d %b %Y} 00:00 to {now:%d %b %Y %H:%M} NPT | {len(rows)} articles (high + medium "
           f"relevance) | {APP_NAME} v{VERSION}_", "",
           "_Headlines link to the original publishers. Automated classification, not investment advice; verify "
           "material facts against primary disclosures (NEPSE, SEBON, company filings)._", ""]
    if rows:
        out += ["| Category | Articles |", "|---|---:|"]
        out += [f"| {CATEGORIES[k]} | {len(by[k])} |" for k in CATEGORIES if by.get(k)]
        out.append("")
    for k in CATEGORIES:
        items = by.get(k)
        if not items:
            continue
        out += [f"## {CATEGORIES[k]} ({len(items)})", ""]
        for i, r in enumerate(items, 1):
            when = (npt_str(r["published_at"], "%d %b %H:%M") + " NPT") if r["published_at"] else "time n/a"
            t = r["title"].replace("[", "(").replace("]", ")")
            out.append(f"{i}. **[{t}]({r['url']})** - {r['source_name']} | {when} | {r['relevance'].title()}")
        out.append("")
    if not rows:
        out.append("_No high/medium-relevance articles in this window._")
    return "\n".join(out)


SCREENER_BLOCK = r'''<style>
.vtabs{display:flex;gap:6px;align-items:flex-end;padding:8px 14px 0;background:var(--bg);overflow-x:auto;-webkit-overflow-scrolling:touch;white-space:nowrap;border-bottom:1px solid var(--ln);scrollbar-width:none}
.vtabs::-webkit-scrollbar{display:none}
.vtabs button{border:1px solid var(--ln);background:var(--p);color:var(--mu);border-radius:8px 8px 0 0;padding:8px 14px;font-weight:600;font-size:12px;cursor:pointer;min-height:38px;flex:0 0 auto;transition:all .15s ease}
.vtabs button.on{color:var(--onac);background:var(--ac);border-color:transparent;font-weight:700}
.vtabs button:not(.on):hover{border-color:var(--ac);color:var(--ac)}
.vtabs em{background:var(--ch);color:var(--ac);border:1px solid var(--ln);border-radius:6px;padding:1px 5px;margin-left:6px;font-size:9px;font-style:normal}
.vtabs[hidden]{display:none}
.vtabs.sub{padding:6px 14px;gap:6px;align-items:center;background:var(--p2);border-bottom:1px solid var(--ln)}
.vtabs.sub button{border-radius:16px;min-height:30px;padding:4px 12px;font-size:11.5px}
.vtabs.sub button.on{background:var(--ac);color:var(--onac)}
#scr{padding:12px 16px;max-width:1400px;margin:0 auto;min-width:0}
#scr [hidden]{display:none!important}
#scr .sdt{color:var(--mu);margin-bottom:8px;font-size:11px}
#scr .sst{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:12px}
#scr .sst div{background:var(--p);border:1px solid var(--ln);border-top:3px solid var(--ac2);border-radius:8px;padding:8px 12px;min-width:0}
#scr .sst b{display:block;font-size:17px}
#scr .sst small{color:var(--mu);font-size:10px;text-transform:uppercase;letter-spacing:.04em}
#scr .bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:10px}
#scr .bar input,#scr .bar select,#scr .bar button,#scr .bar summary{background:var(--p);color:var(--tx);border:1px solid var(--ln);border-radius:6px;padding:6px 10px;min-height:34px;font:inherit;cursor:pointer}
#scr .bar input{cursor:text;min-width:0;width:150px}
#scr .bar input.sm{width:80px}
#scr .bar button:hover,#scr .bar summary:hover{border-color:var(--ac)}
#scr .bar button.on{background:var(--ac);color:var(--onac);border-color:transparent}
#scr .chip{background:var(--ch);border:1px solid var(--ln);border-radius:12px;padding:2px 4px 2px 10px;font-size:11px;display:inline-flex;gap:4px;align-items:center}
#scr .chip i{cursor:pointer;font-style:normal;padding:0 5px;color:var(--bad)}
#scr details{position:relative}
#scr .scp{position:absolute;z-index:20;top:38px;left:0;width:min(520px,86vw);max-height:300px;overflow:auto;background:var(--p);border:1px solid var(--ln);border-radius:8px;padding:8px;display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:2px 10px;box-shadow:0 8px 24px rgba(0,0,0,.35)}
#scr .scp label{font-size:11px;white-space:nowrap}
#scr .tw{overflow:auto;max-height:70vh;border:1px solid var(--ln);border-radius:8px;background:var(--p);min-width:0;-webkit-overflow-scrolling:touch}
#scr .tw.fit{max-height:none}
#scr table{border-collapse:collapse;width:100%;font-size:12px}
#scr th{position:sticky;top:0;z-index:2;background:var(--ch);color:var(--tx);text-align:right;padding:7px 9px;white-space:nowrap;border-bottom:1px solid var(--ln);font-size:11px}
#scr th[data-s]{cursor:pointer}
#scr th[data-s]:hover{color:var(--ac)}
#scr td{padding:6px 9px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--ln);font-variant-numeric:tabular-nums}
#scr th:first-child,#scr td:first-child{text-align:left;position:sticky;left:0;background:var(--p);z-index:1}
#scr th:first-child{background:var(--ch);z-index:3}
#scr tbody tr:hover td{background:var(--p2)}
#scr .u{color:var(--ok)} #scr .d{color:var(--bad)}
#scr a[data-sym]{color:var(--lk);font-weight:700;text-decoration:none;cursor:pointer}
#scr a[data-sym]:hover{text-decoration:underline}
#scr .star{cursor:pointer;color:var(--lo);margin-right:4px;user-select:none}
#scr .star.on{color:var(--wa)}
#scr .note{color:var(--mu);font-size:11px;margin:4px 0 8px}
#scr .cgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:12px}
#scr .scard{background:var(--p);border:1px solid var(--ln);border-radius:8px;padding:10px 12px;min-width:0;overflow-x:auto}
#scr .scard h3{margin:0 0 8px;font-size:13px}
#scr .scard .tw{border:0;max-height:none}
#scr svg.chart{width:100%;height:auto;display:block}
#scr svg.chart text{fill:var(--tx);font-size:10px}
#scr svg.chart line{stroke:var(--ln)}
#scr .hmap{display:flex;flex-wrap:wrap;gap:3px}
#scr .tile{display:flex;flex-direction:column;justify-content:center;align-items:center;height:62px;border-radius:5px;color:var(--tx);cursor:pointer;font-size:11px;overflow:hidden;transition:transform .1s}
#scr .tile:hover{transform:scale(1.05);z-index:5;outline:2px solid var(--tx)}
#scr .tile b{font-size:12px}
#scr .kv{display:grid;grid-template-columns:1fr auto;gap:3px 12px;font-size:12px}
#scr .kv span:nth-child(odd){color:var(--mu)} #scr .kv span:nth-child(even){text-align:right;font-variant-numeric:tabular-nums}
#scr .rng{height:8px;border-radius:4px;background:var(--ch);position:relative;margin:8px 0 2px}
#scr .rng i{position:absolute;top:-3px;width:4px;height:14px;background:var(--ac);border-radius:2px}
#scr .cmp-t td:first-child,#scr .cmp-t th:first-child{min-width:120px}
@media(max-width:640px){#scr{padding:10px 8px}#scr .bar input{width:120px}#scr th,#scr td{padding:6px 7px}}
</style>
<section id="scr" hidden aria-label="Market Analytics">
  <div class="sdt" id="sdt"></div>
  <datalist id="syms"></datalist>

  <div id="v-mkt" class="subv" hidden>
    <div class="sst" id="m-sst"></div>
    <div class="cgrid" id="m-grid"></div>
  </div>

  <div id="v-s" class="subv" hidden>
    <div class="sst" id="s-sst"></div>
    <div class="bar" id="spre"></div>
    <div class="bar">
      <input id="sq" placeholder="Search symbol" list="syms" aria-label="Search symbol">
      <select id="fcol" aria-label="Filter column"></select>
      <input id="fmin" class="sm" placeholder="min" inputmode="decimal"><input id="fmax" class="sm" placeholder="max" inputmode="decimal">
      <button id="fadd">+ Filter</button><span id="sfl"></span>
      <button id="fclr">Clear</button>
      <details><summary>Columns</summary><div id="scp" class="scp"></div></details>
      <button id="scsv">Export CSV</button>
    </div>
    <div class="note" id="snote"></div>
    <div class="tw" id="t-s"></div>
  </div>

  <div id="v-map" class="subv" hidden>
    <div class="bar" id="map-bar"></div>
    <div class="note">Top 80 companies by market cap. Tile size follows market cap; colour follows the selected measure. Click a tile for Stock 360.</div>
    <div class="hmap" id="m-map"></div>
  </div>

  <div id="v-sig" class="subv" hidden>
    <div class="bar"><select id="sig-sel" aria-label="Signal">
      <option value="mom">Strong momentum (1M &gt; 10% and 3M &gt; 10%)</option>
      <option value="os">Oversold (RSI &lt; 30)</option>
      <option value="ob">Overbought (RSI &gt; 70)</option>
      <option value="nh">Near 52-week high (&lt; 5% below)</option>
      <option value="nl">Near 52-week low (&lt; 5% above)</option>
      <option value="ut">Uptrend (LTP &gt; SMA20 &gt; SMA50)</option>
      <option value="mb">MACD positive (MACD &gt; 0)</option>
    </select></div>
    <div class="note" id="sig-note"></div>
    <div class="tw" id="t-sig"></div>
  </div>

  <div id="v-val" class="subv" hidden>
    <div class="bar"><label>X</label><select id="val-x"><option>P/E (Annu.)</option><option>P/B</option><option>P/S</option><option>PEG</option></select><label>Y</label><select id="val-y"><option>ROE (Annu.)</option><option>ROA (Annu.)</option><option>1Y</option><option>Dividend Yield</option></select></div>
    <div class="scard"><svg class="chart" id="val-svg" viewBox="0 0 700 360" role="img" aria-label="Valuation scatter"></svg></div>
    <h3 style="margin:14px 0 6px;font-size:13px">Value screen: P/E (Annu.) 0&ndash;15 and P/B &lt; 2</h3>
    <div class="tw" id="t-val"></div>
  </div>

  <div id="v-qua" class="subv" hidden>
    <div class="bar"><label>X</label><select id="qua-x"><option>ROE (Annu.)</option><option>ROA (Annu.)</option></select><label>Y</label><select id="qua-y"><option>NPM</option><option>EPS (Annu.)</option><option>NIM</option></select></div>
    <div class="scard"><svg class="chart" id="qua-svg" viewBox="0 0 700 360" role="img" aria-label="Quality scatter"></svg></div>
    <h3 style="margin:14px 0 6px;font-size:13px">Quality ranking</h3>
    <div class="note">Score = average percentile rank (0&ndash;100) of ROE, ROA and NPM, plus lower NPL where reported. Relative ranking only, not a recommendation.</div>
    <div class="tw" id="t-qua"></div>
  </div>

  <div id="v-div" class="subv" hidden>
    <div class="bar"><input id="div-sq" placeholder="Search symbol" list="syms"><select id="div-f"><option value="all">All</option><option value="yes">Pays dividend</option><option value="high">Yield &ge; 5%</option></select></div>
    <div class="tw" id="t-div"></div>
  </div>

  <div id="v-cmp" class="subv" hidden>
    <div class="bar"><input id="cin" placeholder="Add symbol (max 4)" list="syms"><button id="cadd">Add</button><span id="cls"></span><button id="cclr">Clear</button></div>
    <div class="tw fit" id="t-cmp"></div>
  </div>

  <div id="v-360" class="subv" hidden>
    <div class="bar"><input id="sin" placeholder="Symbol e.g. NABIL" list="syms"><button id="sgo">Open</button></div>
    <div id="s360"></div>
  </div>

  <div id="v-bnk" class="subv" hidden>
    <div class="note">Companies reporting deposits (banks and similar institutions). Market cap in Arba (NPR billion).</div>
    <div class="tw" id="t-bnk"></div>
  </div>

  <div id="v-wch" class="subv" hidden>
    <div class="bar"><input id="win" placeholder="Add symbol" list="syms"><button id="wadd">Add</button><button id="wclr">Clear all</button></div>
    <div class="note">Saved in this browser only.</div>
    <div class="tw" id="t-wch"></div>
  </div>
</section>
<script>
(function(){
const D=__SDATA__,C=D.cols,R=D.rows;
const ci=n=>C.indexOf(n),num=v=>typeof v==='number'&&isFinite(v);
const $=s=>document.querySelector('#scr '+s);
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const CO=ci('Company');
const EX={};                                   // derived columns: name -> {SYMBOL: value}
const g=(r,n)=>{if(EX[n])return EX[n][r[CO]];const i=ci(n);return i<0?null:r[i]};
const fmt=(v,d)=>v==null||v===''?'–':num(v)?v.toLocaleString('en-IN',{maximumFractionDigits:d==null?2:d}):esc(v);
const arba=v=>num(v)?(v/1e6).toLocaleString('en-IN',{maximumFractionDigits:2}):'–';   // export is in NPR thousands
const PCT=new Set(['% Change','1M','3M','6M','1Y','YTD','% Below 52 Wk H','% Above 52 Wk L','ROE (Annu.)','ROA (Annu.)','ROE (TTM)','ROA (TTM)','NIM','NPM','NPL to Total Loan','Credit to Deposit','Int. Rate Spread','Dividend Yield','Cash','Bonus','Total Div','Graham Upside %']);
const CHG=new Set(['Change','% Change','1M','3M','6M','1Y','YTD','Graham Upside %']);
const LBL={'Market Cap':'Mkt Cap (Arba)','Deposits':'Deposits (Arba)','Total Assets':'Assets (Arba)'};
const ARB=new Set(['Market Cap','Deposits','Total Assets']);
const cell=(r,c)=>{
  const v=g(r,c);
  if(c==='Company'){const s=esc(v);return `<td><span class="star${wl.includes(v)?' on':''}" data-w="${s}" title="Watchlist">★</span><a href="#" data-sym="${s}">${s}</a></td>`}
  if(ARB.has(c))return `<td>${arba(v)}</td>`;
  const k=CHG.has(c)?(num(v)?(v>0?'u':v<0?'d':''):''):'';
  return `<td class="${k}">${fmt(v)}${PCT.has(c)&&num(v)?'%':''}</td>`;
};
const ST={};
function table(id,cols,rows,def){
  const s=id?(ST[id]||(ST[id]=Object.assign({k:null,asc:false},def||{}))):{k:null};
  const rs=rows.slice();
  if(s.k)rs.sort((a,b)=>{const x=g(a,s.k),y=g(b,s.k),nx=num(x),ny=num(y);
    if(nx&&ny)return s.asc?x-y:y-x; if(nx)return -1; if(ny)return 1;
    return s.asc?String(x||'').localeCompare(String(y||'')):String(y||'').localeCompare(String(x||''))});
  const h=cols.map(c=>`<th${id?` data-t="${id}" data-s="${esc(c)}"`:''}>${esc(LBL[c]||c)}${s.k===c?(s.asc?' ▲':' ▼'):''}</th>`).join('');
  const b=rs.map(r=>'<tr>'+cols.map(c=>cell(r,c)).join('')+'</tr>').join('')||`<tr><td colspan="${cols.length}" style="text-align:center;color:var(--mu)">No matching companies</td></tr>`;
  return {html:`<table><thead><tr>${h}</tr></thead><tbody>${b}</tbody></table>`,n:rs.length};
}
const put=(id,host,cols,rows,def)=>{const t=table(id,cols,rows,def);$(host).innerHTML=t.html;return t.n};

/* ---------- watchlist (browser storage, guarded) ---------- */
let wl=[];
try{wl=JSON.parse(localStorage.getItem('nnh_wch')||'[]').filter(x=>typeof x==='string')}catch(e){wl=[]}
const saveW=()=>{try{localStorage.setItem('nnh_wch',JSON.stringify(wl))}catch(e){}};
const toggleW=s=>{wl=wl.includes(s)?wl.filter(x=>x!==s):wl.concat(s);saveW();renderView()};
const find=q=>{q=String(q||'').trim().toUpperCase();return R.find(r=>String(r[CO]).toUpperCase()===q)||null};

/* ---------- derived columns ---------- */
(function(){
  const pr=(c)=>{const a=R.map(r=>g(r,c)).filter(num).sort((x,y)=>x-y);return v=>{if(!num(v)||a.length<2)return null;let lo=0;while(lo<a.length&&a[lo]<v)lo++;return 100*lo/(a.length-1)}};
  const pe=pr('ROE (Annu.)'),pa=pr('ROA (Annu.)'),pn=pr('NPM'),pl=pr('NPL to Total Loan');
  EX['Quality Score']={};EX['Graham Upside %']={};
  R.forEach(r=>{
    const p=[pe(g(r,'ROE (Annu.)')),pa(g(r,'ROA (Annu.)')),pn(g(r,'NPM'))];
    const l=pl(g(r,'NPL to Total Loan'));if(l!=null)p.push(100-l);
    const q=p.filter(x=>x!=null);
    EX['Quality Score'][r[CO]]=q.length>=2?Math.round(q.reduce((a,b)=>a+b,0)/q.length):null;
    const gr=g(r,"Graham\"s No."),px=g(r,'LTP');
    EX['Graham Upside %'][r[CO]]=num(gr)&&num(px)&&px>0&&gr>0?Math.round((gr/px-1)*1000)/10:null;
  });
})();

/* ---------- tabs ---------- */
const nav=document.createElement('nav');nav.className='vtabs';nav.setAttribute('aria-label','Sections');
nav.innerHTML='<button class="on" data-v="n">NEWS PORTAL<em>LATEST</em></button><button data-v="scr">STOCK SCREENER</button>';
const sub=document.createElement('nav');sub.className='vtabs sub';sub.setAttribute('aria-label','Stock screener tools');sub.hidden=true;
sub.innerHTML=[['v-mkt','MARKET'],['v-s','SCREENER'],['v-map','HEATMAP'],['v-sig','SIGNALS'],['v-val','VALUATION'],['v-qua','QUALITY'],['v-div','DIVIDENDS'],['v-cmp','COMPARE'],['v-360','STOCK 360'],['v-bnk','BANKING'],['v-wch','WATCHLIST']]
 .map(a=>`<button data-v="${a[0]}">${a[1]}</button>`).join('');
const top=document.querySelector('.top');
if(top){top.after(nav);nav.after(sub)}else{document.body.prepend(sub);document.body.prepend(nav)}
let curV='n',lastSub='v-mkt';
function go(v){
  if(v==='scr')v=lastSub;                       // STOCK SCREENER tab -> reopen the last tool used
  if(v!=='n'&&!sub.querySelector(`[data-v="${v}"]`))return;
  const news=(v==='n');
  if(!news)lastSub=v;
  nav.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x.dataset.v===(news?'n':'scr')));
  sub.hidden=news;
  sub.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x.dataset.v===v));
  ['.stats','.lay'].forEach(q=>{const el=document.querySelector(q);if(el)el.style.display=news?'':'none'});
  const sec=document.getElementById('scr');if(sec)sec.hidden=news;
  curV=v;
  if(!news){document.querySelectorAll('#scr .subv').forEach(el=>{el.hidden=(el.id!==v)});renderView()}
  window.scrollTo(0,0);
}
nav.onclick=sub.onclick=e=>{const b=e.target.closest('button');if(b)go(b.dataset.v)};

/* ---------- MARKET ---------- */
function r_mkt(){
  const ch=R.map(r=>g(r,'% Change')).filter(num),up=ch.filter(x=>x>0).length,dn=ch.filter(x=>x<0).length;
  const mc=R.reduce((a,r)=>a+(num(g(r,'Market Cap'))?g(r,'Market Cap'):0),0);
  const avg=ch.length?ch.reduce((a,b)=>a+b,0)/ch.length:null;
  const cnt=f=>R.filter(r=>{const v=f(r);return v}).length;
  const st=[['Companies',R.length],['Advancers',up],['Decliners',dn],['Unchanged',ch.length-up-dn],['Avg % change',avg==null?'–':(avg>0?'+':'')+avg.toFixed(2)+'%'],['Total mkt cap (Arba)',arba(mc)],
    ['RSI &lt; 30',cnt(r=>g(r,'RSI')<30&&num(g(r,'RSI')))],['RSI &gt; 70',cnt(r=>g(r,'RSI')>70)],['Within 5% of 52W high',cnt(r=>num(g(r,'% Below 52 Wk H'))&&g(r,'% Below 52 Wk H')<5)]];
  $('#m-sst').innerHTML=st.map(a=>`<div><small>${a[0]}</small><b>${a[1]}</b></div>`).join('');
  const top=(title,col,asc,extra)=>{
    const rs=R.filter(r=>num(g(r,col))).sort((a,b)=>asc?g(a,col)-g(b,col):g(b,col)-g(a,col)).slice(0,8);
    return `<div class="scard"><h3>${title}</h3><div class="tw">${table(null,['Company','LTP',col].concat(extra||[]),rs).html}</div></div>`};
  $('#m-grid').innerHTML=top('Top gainers today','% Change',false)+top('Top losers today','% Change',true)+top('Best 1-month','1M',false)+top('Worst 1-month','1M',true)+top('Most oversold (RSI)','RSI',true)+top('Highest dividend yield','Dividend Yield',false);
}

/* ---------- SCREENER ---------- */
const S_SHOW=['Company','LTP','% Change','1M','3M','6M','1Y','% Below 52 Wk H','RSI','MACD','P/E (Annu.)','P/B','EPS (Annu.)','ROE (Annu.)','Dividend Yield','Market Cap'];
let sCols=S_SHOW.filter(c=>ci(c)>=0),sFl=[];
const NUMC=C.filter(c=>c!=='#'&&R.some(r=>num(g(r,c))));
const PRE={'Value: P/E<15 & P/B<2':[['P/E (Annu.)',0.01,15],['P/B',0.01,2]],'Dividend yield ≥ 5%':[['Dividend Yield',5,null]],'RSI < 30 (oversold)':[['RSI',null,30]],'RSI > 70 (overbought)':[['RSI',70,null]],'Within 10% of 52-wk low':[['% Above 52 Wk L',null,10]],'3-month gain > 10%':[['3M',10,null]]};
let CUR=[];
function r_s(){
  const q=$('#sq').value.trim().toLowerCase();
  CUR=R.filter(r=>(!q||String(r[CO]).toLowerCase().includes(q))&&sFl.every(f=>{const v=g(r,f.c);return num(v)&&(f.min==null||v>=f.min)&&(f.max==null||v<=f.max)}));
  const n=put('s','#t-s',sCols,CUR,{k:'% Change',asc:false});
  $('#snote').textContent=`Showing ${n} of ${R.length} companies. Click a column title to sort; click a symbol for Stock 360; ★ adds to watchlist.`;
  $('#sfl').innerHTML=sFl.map((f,i)=>`<span class="chip">${esc(f.c)} ${f.min!=null?'≥ '+f.min:''} ${f.max!=null?'≤ '+f.max:''}<i data-fx="${i}" title="Remove">×</i></span>`).join(' ');
  const av=CUR.map(r=>g(r,'% Change')).filter(num);
  $('#s-sst').innerHTML=[['Matching',n],['Advancers',av.filter(x=>x>0).length],['Decliners',av.filter(x=>x<0).length],['Avg % change',av.length?(av.reduce((a,b)=>a+b,0)/av.length).toFixed(2)+'%':'–']].map(a=>`<div><small>${a[0]}</small><b>${a[1]}</b></div>`).join('');
}

/* ---------- HEATMAP ---------- */
let mapMet='% Change';
function r_map(){
  $('#map-bar').innerHTML='<label>Colour by</label>'+['% Change','1M','3M','1Y','RSI'].map(m=>`<button data-mm="${m}" class="${m===mapMet?'on':''}">${m}</button>`).join('');
  const rs=R.filter(r=>num(g(r,'Market Cap'))).sort((a,b)=>g(b,'Market Cap')-g(a,'Market Cap')).slice(0,80);
  $('#m-map').innerHTML=rs.map(r=>{
    let v=g(r,mapMet);const raw=v;
    if(mapMet==='RSI'&&num(v))v=(v-50)/5;
    const a=num(v)?Math.min(.8,.15+Math.abs(v)/10*.65):0;
    const bg=!num(v)||v===0?'var(--ch)':v>0?`rgba(22,163,74,${a})`:`rgba(225,29,72,${a})`;
    const w=Math.round(56+Math.sqrt(Math.max(0,g(r,'Market Cap'))/1e6)*7);
    return `<div class="tile" data-sym="${esc(r[CO])}" style="background:${bg};width:${w}px" title="${esc(r[CO])} · ${mapMet} ${fmt(raw)}"><b>${esc(r[CO])}</b><span>${fmt(raw,1)}</span></div>`}).join('');
}

/* ---------- SIGNALS ---------- */
const SIG={
  mom:r=>g(r,'1M')>10&&g(r,'3M')>10, os:r=>g(r,'RSI')<30, ob:r=>g(r,'RSI')>70,
  nh:r=>g(r,'% Below 52 Wk H')<5, nl:r=>g(r,'% Above 52 Wk L')<5,
  ut:r=>g(r,'LTP')>g(r,'SMA20')&&g(r,'SMA20')>g(r,'SMA50'), mb:r=>g(r,'MACD')>0};
function r_sig(){
  const f=SIG[$('#sig-sel').value];
  const rs=R.filter(r=>{try{return f(r)}catch(e){return false}});
  const n=put('sig','#t-sig',['Company','LTP','% Change','1M','3M','RSI','MACD','% Below 52 Wk H','% Above 52 Wk L'],rs,{k:'RSI',asc:true});
  $('#sig-note').textContent=`${n} companies match. Technical signals describe past price behaviour and are not buy/sell recommendations.`;
}

/* ---------- scatter ---------- */
function scatter(host,xc,yc){
  const el=$(host);
  const pts=R.filter(r=>num(g(r,xc))&&num(g(r,yc))).map(r=>({x:g(r,xc),y:g(r,yc),m:g(r,'Market Cap')||0,c:r[CO]}));
  if(pts.length<3){el.innerHTML='<text x="20" y="30">Not enough data</text>';return}
  const q=(a,p)=>{a=a.slice().sort((u,v)=>u-v);return a[Math.round(p*(a.length-1))]};
  let x0=q(pts.map(p=>p.x),.02),x1=q(pts.map(p=>p.x),.98),y0=q(pts.map(p=>p.y),.02),y1=q(pts.map(p=>p.y),.98);
  if(x0===x1){x0-=1;x1+=1} if(y0===y1){y0-=1;y1+=1}
  const W=700,H=360,L=50,B=40,T=14,Rt=14;
  const sx=v=>L+(v-x0)/(x1-x0)*(W-L-Rt),sy=v=>H-B-(v-y0)/(y1-y0)*(H-B-T);
  let h='';
  for(let i=0;i<=4;i++){
    const xv=x0+(x1-x0)*i/4,yv=y0+(y1-y0)*i/4;
    h+=`<line x1="${sx(xv)}" y1="${T}" x2="${sx(xv)}" y2="${H-B}"/><text x="${sx(xv)}" y="${H-B+14}" text-anchor="middle">${xv.toFixed(1)}</text>`;
    h+=`<line x1="${L}" y1="${sy(yv)}" x2="${W-Rt}" y2="${sy(yv)}"/><text x="${L-6}" y="${sy(yv)+3}" text-anchor="end">${yv.toFixed(1)}</text>`;
  }
  h+=`<text x="${W/2}" y="${H-4}" text-anchor="middle" font-weight="700">${esc(xc)}</text><text transform="rotate(-90 12 ${H/2})" x="12" y="${H/2}" text-anchor="middle" font-weight="700">${esc(yc)}</text>`;
  pts.filter(p=>p.x>=x0&&p.x<=x1&&p.y>=y0&&p.y<=y1).forEach(p=>{
    const rad=Math.max(3,Math.min(14,3+Math.sqrt(Math.max(0,p.m)/1e6)*.5));
    h+=`<circle data-sym="${esc(p.c)}" cx="${sx(p.x).toFixed(1)}" cy="${sy(p.y).toFixed(1)}" r="${rad.toFixed(1)}" fill="var(--ac)" fill-opacity=".55" stroke="var(--ac)" style="cursor:pointer"><title>${esc(p.c)}: ${xc} ${fmt(p.x)}, ${yc} ${fmt(p.y)}</title></circle>`;
  });
  el.innerHTML=h;
}
function r_val(){
  scatter('#val-svg',$('#val-x').value,$('#val-y').value);
  const rs=R.filter(r=>{const e=g(r,'P/E (Annu.)'),b=g(r,'P/B');return num(e)&&num(b)&&e>0.01&&e<=15&&b>0.01&&b<2});
  put('val','#t-val',['Company','LTP','P/E (Annu.)','P/B','PEG','EPS (Annu.)','BVPS','Graham"s No.','Graham Upside %','ROE (Annu.)','Dividend Yield'],rs,{k:'P/E (Annu.)',asc:true});
}
function r_qua(){
  scatter('#qua-svg',$('#qua-x').value,$('#qua-y').value);
  put('qua','#t-qua',['Company','LTP','Quality Score','ROE (Annu.)','ROA (Annu.)','NPM','NIM','NPL to Total Loan','EPS (Annu.)','P/E (Annu.)','P/B'],R.filter(r=>g(r,'Quality Score')!=null),{k:'Quality Score',asc:false});
}

/* ---------- DIVIDENDS ---------- */
function r_div(){
  const q=$('#div-sq').value.trim().toLowerCase(),f=$('#div-f').value;
  const rs=R.filter(r=>(!q||String(r[CO]).toLowerCase().includes(q))&&(f==='all'||(f==='yes'&&g(r,'Total Div')>0)||(f==='high'&&g(r,'Dividend Yield')>=5)));
  put('div','#t-div',['Company','LTP','Div Fiscal Year','Cash','Bonus','Total Div','Dividend Yield','EPS (Annu.)','P/E (Annu.)'],rs,{k:'Dividend Yield',asc:false});
}

/* ---------- COMPARE ---------- */
let cmp=[];
const CM=['LTP','% Change','1M','3M','1Y','Market Cap','RSI','P/E (Annu.)','P/B','EPS (Annu.)','BVPS','ROE (Annu.)','ROA (Annu.)','NIM','NPL to Total Loan','Dividend Yield','Quality Score'];
function r_cmp(){
  $('#cls').innerHTML=cmp.map(s=>`<span class="chip">${esc(s)}<i data-cx="${esc(s)}">×</i></span>`).join(' ');
  const rs=cmp.map(find).filter(Boolean);
  if(!rs.length){$('#t-cmp').innerHTML='<div class="note" style="padding:10px">Add up to 4 symbols to compare side by side.</div>';return}
  const head='<th>Metric</th>'+rs.map(r=>`<th><a href="#" data-sym="${esc(r[CO])}">${esc(r[CO])}</a></th>`).join('');
  const body=CM.filter(m=>m==='Quality Score'||ci(m)>=0).map(m=>`<tr><td>${esc(LBL[m]||m)}</td>`+rs.map(r=>{const v=g(r,m);
    return ARB.has(m)?`<td>${arba(v)}</td>`:`<td class="${CHG.has(m)&&num(v)?(v>0?'u':v<0?'d':''):''}">${fmt(v)}${PCT.has(m)&&num(v)?'%':''}</td>`}).join('')+'</tr>').join('');
  $('#t-cmp').innerHTML=`<table class="cmp-t"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
const addCmp=s=>{const r=find(s);if(r&&!cmp.includes(r[CO])&&cmp.length<4){cmp.push(r[CO])}};

/* ---------- STOCK 360 ---------- */
let cur360=null;
const G360=[['Price & momentum',['Close','LTP','Change','% Change','180D VWAP','Day High','Day Low','1M','3M','6M','1Y','YTD','52 Wk High','52 Wk Low','% Below 52 Wk H','% Above 52 Wk L','BETA']],
 ['Technicals',['RSI','MACD','Stochastic','ADX','RVI','SMA20','SMA50','EMA20','EMA50','Lower Band','Middle Band','Upper Band']],
 ['Valuation',['P/E (Annu.)','P/E (TTM)','P/S','P/B','PEG','PEVPB','Graham"s No.','Graham Upside %','EPS (Annu.)','EPS (TTM)','BVPS']],
 ['Profitability & quality',['ROE (Annu.)','ROA (Annu.)','ROE (TTM)','ROA (TTM)','NPM','NIM','Int. Rate Spread','NPL to Total Loan','Credit to Deposit','Quality Score']],
 ['Financials (Arba / as reported)',['Fiscal Year','Qtr','Paid Up Cap','Reserves & Surplus','Deposits','Loans & Advances','Total Assets','Total Liabilities','Revenue','Gross Profit','Operating Profit','Net Profit','Distributable Profit']],
 ['Dividend',['Div Fiscal Year','Cash','Bonus','Total Div','Dividend Yield']]];
function r_360(){
  const out=$('#s360'),r=cur360?find(cur360):null;
  if(!r){out.innerHTML='<div class="note">Enter a symbol, or click any symbol in the other tabs.</div>';return}
  const v=n=>g(r,n),px=v('LTP'),lo=v('52 Wk Low'),hi=v('52 Wk High');
  const pos=num(px)&&num(lo)&&num(hi)&&hi>lo?Math.max(0,Math.min(100,(px-lo)/(hi-lo)*100)):null;
  const pub=v('Public Share'),pro=v('Promotor Share'),tot=(num(pub)?pub:0)+(num(pro)?pro:0);
  const stat=(a,b)=>`<div><small>${a}</small><b>${b}</b></div>`;
  let h=`<div class="bar"><b style="font-size:18px">${esc(r[CO])}</b><button data-w="${esc(r[CO])}" class="${wl.includes(r[CO])?'on':''}">★ Watchlist</button><button data-cadd="${esc(r[CO])}">+ Compare</button></div>`;
  h+='<div class="sst">'+stat('LTP',fmt(px))+stat('% Change',`<span class="${num(v('% Change'))?(v('% Change')>0?'u':v('% Change')<0?'d':''):''}">${fmt(v('% Change'))}%</span>`)+stat('Market cap (Arba)',arba(v('Market Cap')))+stat('RSI',fmt(v('RSI'),1))+stat('P/E (Annu.)',fmt(v('P/E (Annu.)')))+stat('P/B',fmt(v('P/B')))+stat('Dividend yield',fmt(v('Dividend Yield'))+'%')+'</div>';
  if(pos!=null)h+=`<div class="scard" style="margin-bottom:12px"><h3>52-week range</h3><div class="rng"><i style="left:calc(${pos.toFixed(1)}% - 2px)"></i></div><div style="display:flex;justify-content:space-between;font-size:11px;color:var(--mu)"><span>${fmt(lo)}</span><span>${pos.toFixed(0)}% of range</span><span>${fmt(hi)}</span></div></div>`;
  h+='<div class="cgrid">';
  G360.forEach(gr=>{
    const rows=gr[1].filter(n=>(EX[n]||ci(n)>=0)&&g(r,n)!=null&&g(r,n)!=='').map(n=>{const val=g(r,n);
      const t=ARB.has(n)?arba(val):(['Paid Up Cap','Reserves & Surplus','Loans & Advances','Total Liabilities','Revenue','Gross Profit','Operating Profit','Net Profit','Distributable Profit'].includes(n)?arba(val):fmt(val)+(PCT.has(n)&&num(val)?'%':''));
      return `<span>${esc(LBL[n]||n)}</span><span class="${CHG.has(n)&&num(val)?(val>0?'u':val<0?'d':''):''}">${t}</span>`}).join('');
    if(rows)h+=`<div class="scard"><h3>${gr[0]}</h3><div class="kv">${rows}</div></div>`;
  });
  if(tot>0)h+=`<div class="scard"><h3>Shareholding</h3><div class="kv"><span>Public</span><span>${(pub/tot*100).toFixed(1)}%</span><span>Promoter</span><span>${(pro/tot*100).toFixed(1)}%</span></div></div>`;
  out.innerHTML=h+'</div>';
}
function open360(s){cur360=s;$('#sin').value=s;go('v-360')}

/* ---------- BANKING & WATCHLIST ---------- */
function r_bnk(){
  const rs=R.filter(r=>num(g(r,'Deposits'))&&g(r,'Deposits')>0);
  put('bnk','#t-bnk',['Company','LTP','% Change','Market Cap','Deposits','P/E (Annu.)','P/B','ROE (Annu.)','ROA (Annu.)','NIM','Int. Rate Spread','NPL to Total Loan','Credit to Deposit','Dividend Yield'],rs,{k:'Market Cap',asc:false});
}
function r_wch(){
  const rs=wl.map(find).filter(Boolean);
  if(!rs.length){$('#t-wch').innerHTML='<div class="note" style="padding:10px">Watchlist is empty. Click ★ next to any symbol.</div>';return}
  put('wch','#t-wch',['Company','LTP','% Change','1M','3M','1Y','RSI','P/E (Annu.)','P/B','Dividend Yield','Market Cap'],rs);
}

const VIEWS={'v-mkt':r_mkt,'v-s':r_s,'v-map':r_map,'v-sig':r_sig,'v-val':r_val,'v-qua':r_qua,'v-div':r_div,'v-cmp':r_cmp,'v-360':r_360,'v-bnk':r_bnk,'v-wch':r_wch};
function renderView(){const f=VIEWS[curV];if(f){try{f()}catch(e){console.error('screener view',curV,e)}}}

/* ---------- events ---------- */
const scr=document.getElementById('scr');
const pn=s=>{s=String(s).trim();return s===''||isNaN(+s)?null:+s};
scr.addEventListener('click',e=>{
  const t=e.target;let el;
  if((el=t.closest('[data-w]'))){toggleW(el.dataset.w);return}
  if((el=t.closest('[data-sym]'))){e.preventDefault();open360(el.dataset.sym);return}
  if((el=t.closest('th[data-s]'))){const s=ST[el.dataset.t]||(ST[el.dataset.t]={k:null,asc:false});if(s.k===el.dataset.s)s.asc=!s.asc;else{s.k=el.dataset.s;s.asc=false}renderView();return}
  if((el=t.closest('[data-p]'))){sFl=PRE[el.dataset.p].map(a=>({c:a[0],min:a[1],max:a[2]})).filter(f=>ci(f.c)>=0);renderView();return}
  if((el=t.closest('[data-fx]'))){sFl.splice(+el.dataset.fx,1);renderView();return}
  if((el=t.closest('[data-mm]'))){mapMet=el.dataset.mm;renderView();return}
  if((el=t.closest('[data-cx]'))){cmp=cmp.filter(x=>x!==el.dataset.cx);renderView();return}
  if((el=t.closest('[data-cadd]'))){addCmp(el.dataset.cadd);go('v-cmp');return}
  switch(t.id){
    case 'fadd':{const c=$('#fcol').value,mn=pn($('#fmin').value),mx=pn($('#fmax').value);if(c&&(mn!=null||mx!=null)){sFl.push({c:c,min:mn,max:mx});$('#fmin').value='';$('#fmax').value=''}renderView();break}
    case 'fclr':sFl=[];$('#sq').value='';renderView();break;
    case 'scsv':{
      const q=s=>{s=String(s==null?'':s);if(/^[=+\-@]/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"'};
      const txt=[sCols.map(q).join(',')].concat(CUR.map(r=>sCols.map(c=>q(g(r,c))).join(','))).join('\r\n');
      const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['\ufeff'+txt],{type:'text/csv'}));a.download='screener_'+(D.date||'view')+'.csv';a.click();break}
    case 'cadd':addCmp($('#cin').value);$('#cin').value='';renderView();break;
    case 'cclr':cmp=[];renderView();break;
    case 'sgo':cur360=($('#sin').value||'').trim().toUpperCase();renderView();break;
    case 'wadd':{const r=find($('#win').value);if(r&&!wl.includes(r[CO])){wl.push(r[CO]);saveW()}$('#win').value='';renderView();break}
    case 'wclr':wl=[];saveW();renderView();break;
  }
});
scr.addEventListener('input',e=>{if(['sq','div-sq'].includes(e.target.id))renderView()});
scr.addEventListener('change',e=>{
  if(e.target.closest('#scp')){const c=e.target.dataset.col;sCols=e.target.checked?(sCols.includes(c)?sCols:sCols.concat(c)):sCols.filter(x=>x!==c);if(!sCols.includes('Company'))sCols.unshift('Company');renderView();return}
  if(['sig-sel','val-x','val-y','qua-x','qua-y','div-f'].includes(e.target.id))renderView();
});
scr.addEventListener('keydown',e=>{
  if(e.key!=='Enter')return;
  if(e.target.id==='sin'){cur360=e.target.value.trim().toUpperCase();renderView()}
  else if(e.target.id==='cin'){addCmp(e.target.value);e.target.value='';renderView()}
  else if(e.target.id==='win'){const r=find(e.target.value);if(r&&!wl.includes(r[CO])){wl.push(r[CO]);saveW()}e.target.value='';renderView()}
});

/* ---------- init ---------- */
$('#sdt').textContent='Market data as of '+(D.date||'n/a')+' · '+R.length+' companies · values in NPR; market cap, deposits and assets shown in Arba (billion)';
$('#syms').innerHTML=R.map(r=>`<option value="${esc(r[CO])}">`).join('');
$('#fcol').innerHTML=NUMC.map(c=>`<option>${esc(c)}</option>`).join('');
$('#spre').innerHTML='<b>Presets:</b> '+Object.keys(PRE).map(k=>`<button data-p="${esc(k)}">${esc(k)}</button>`).join('');
$('#scp').innerHTML=C.filter(c=>c!=='#').map(c=>`<label><input type="checkbox" data-col="${esc(c)}" ${sCols.includes(c)?'checked':''}> ${esc(c)}</label>`).join('');
})();
</script>
'''


def clean_screener(d: dict) -> dict:
    """Normalise the exported screener data so the dashboard JS can rely on exact column names.

    - collapses doubled spaces in headers ("Day  High" -> "Day High"); "Close" -> "LTP"
    - the 2nd "Fiscal Year" -> "Div Fiscal Year"; the dividend "Total" -> "Total Div"
    - drops the "Average" row, the "Printed/Exported by ..." footer (it contains an e-mail) and rows with no price
    - NaN / inf -> null so the embedded JSON is always valid
    """
    cols, seen, out_cols = list(d.get("cols", [])), {}, []
    for c in cols:
        c = re.sub(r"\s+", " ", str(c)).strip()
        if c == "Close":
            c = "LTP"
        n = seen.get(c, 0)
        seen[c] = n + 1
        if n and c == "Fiscal Year":
            c = "Div Fiscal Year"
        elif n and c == "Total":
            c = "Total Div"
        out_cols.append(c)
    if "Company" not in out_cols or "LTP" not in out_cols:
        return {"date": d.get("date", ""), "cols": out_cols, "rows": []}
    ci, pi = out_cols.index("Company"), out_cols.index("LTP")
    rows = []
    for r in d.get("rows", []):
        r = [None if (isinstance(v, float) and not math.isfinite(v)) else v for v in r]
        name = r[ci] if ci < len(r) else None
        if not isinstance(name, str) or not name.strip():
            continue
        if name.strip().lower() in ("average", "total") or name.lower().startswith(("printed", "exported")):
            continue
        if pi >= len(r) or not isinstance(r[pi], (int, float)):
            continue
        r[ci] = name.strip()
        rows.append(r)
    return {"date": d.get("date", ""), "cols": out_cols, "rows": rows}


def inject_screener(html_doc: str) -> str:
    """PRIVATE use: if screener_data.json / screener_public.json (made by convert_screener.py) sits next to this
    script, add the Market / Screener / Heatmap / ... tabs. Nothing is added when the file is absent, so a
    public build stays news-only. A broken data file never breaks the news page."""
    d = Path(__file__).resolve().parent
    f = next((d / n for n in ("screener_data.json", "screener_public.json") if (d / n).is_file()), None)
    if f is None:
        return html_doc
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))      # Python's json accepts NaN
        data = json.dumps(clean_screener(raw), ensure_ascii=False, allow_nan=False)
    except Exception as exc:
        log.warning("Screener data %s ignored: %s", f.name, exc)
        return html_doc
    data = data.replace("</", "<\\/")
    return html_doc.replace("</body>", SCREENER_BLOCK.replace("__SDATA__", data) + "</body>", 1)


def build_snapshot(p: dict, settings: dict) -> str:
    p = dict(p)
    full = p.pop("full", None)          # ?full=1 keeps excerpts/AI summaries (private use only)
    rows = fetch_rows(p, settings, limit=3000)
    with db() as c:
        rel = related_counts(c, rows)
    arts = [serialize(r, None, rel.get(r["group_id"], 0)) for r in rows]
    if not full:                        # PUBLIC MODE: headline + source + time + link only
        for a in arts:
            a["excerpt"] = ""
            a["summary_ai"] = ""
    srcs = sorted({(a["source_id"], a["source_name"]) for a in arts}, key=lambda x: x[1].lower())
    lo_today, _ = date_bounds("today")
    lo_week, _ = date_bounds("7d")
    payload = {
        "generated_at": iso(utcnow()), "filters": p, "public": not full,
        "contact": os.getenv("NNH_CONTACT", ""),
        "meta": {"categories": [{"id": k, "label": v} for k, v in CATEGORIES.items()], "modes": MODE_LABELS,
                 "settings": {"mode": settings["mode"]}},
        "stats": {"total": len(arts), "today": sum(a["sort_at"] >= lo_today for a in arts),
                  "week": sum(a["sort_at"] >= lo_week for a in arts), "sources_active": len(srcs),
                  "sources_enabled": len(srcs), "sources_manual": 0, "sources_error": 0, "sources_unverified": 0,
                  "last_success": iso(utcnow()), "mode_label": "snapshot"},
        "sources": [{"id": i, "name": n} for i, n in srcs], "articles": arts}
    js = "window.__SNAPSHOT__ = " + json.dumps(payload, ensure_ascii=False).replace("</", "<\\/") + ";"
    return inject_screener(DASHBOARD_HTML.replace("/*__SNAPSHOT__*/", js))


def source_status(s, hl):
    if not s.get("enabled", True):
        return "disabled"
    return (hl or {}).get("status") or ("manual-only" if s["method"] == "manual" else "unverified")


def sources_with_health(store):
    with db() as c:
        health = {r["source_id"]: dict(r) for r in c.execute("SELECT * FROM source_health")}
    out = []
    for s in store.sources():
        hl = health.get(s["id"], {})
        h = {k: hl.get(k) for k in HEALTH_COLS if k not in ("cond", "resolved_feeds")}
        h["resolved_feeds"] = json.loads(hl.get("resolved_feeds") or "[]")
        h["status"] = source_status(s, hl)
        out.append({**s, "health": h})
    return out


# --------------------------------------------------------------------------------------
# Web application
# --------------------------------------------------------------------------------------
def create_app(store: Store, engine: Engine, sched=None) -> FastAPI:
    app = FastAPI(title=APP_NAME, version=VERSION, docs_url="/api/docs", redoc_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    def admin(request: Request):
        if ADMIN_TOKEN and not hmac.compare_digest(request.headers.get("X-Admin-Token", ""), ADMIN_TOKEN):
            raise HTTPException(401, "Admin token required (X-Admin-Token header)")

    def bad(e):
        raise HTTPException(400, str(e))

    def download(data, name, media):
        return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    stamp = lambda: datetime.now(NPT).strftime("%Y%m%d_%H%M")  # noqa: E731

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(inject_screener(DASHBOARD_HTML.replace("/*__SNAPSHOT__*/", "")))

    @app.get("/api/meta")
    def meta():
        s = store.settings()
        return {"app": APP_NAME, "version": VERSION,
                "categories": [{"id": k, "label": v} for k, v in CATEGORIES.items()], "modes": MODE_LABELS,
                "settings": {"mode": s["mode"], "interval_min": s["interval_min"], "languages": s["languages"]},
                "admin_required": bool(ADMIN_TOKEN), "ai_available": bool(ANTHROPIC_API_KEY)}

    @app.get("/api/articles")
    def articles(request: Request):
        p, s = dict(request.query_params), store.settings()
        per = min(200, max(10, _int(p.get("per_page"), 50)))
        with db() as c:
            where, args = build_filters(p, s)
            total = c.execute(f"SELECT COUNT(*) FROM articles{where}", args).fetchone()[0]
            pages = max(1, math.ceil(total / per))
            page = min(max(1, _int(p.get("page"), 1)), pages)
            order = "ASC" if p.get("sort") == "oldest" else "DESC"
            rows = c.execute(f"SELECT * FROM articles{where} ORDER BY sort_at {order}, id {order} LIMIT ? OFFSET ?",
                             [*args, per, (page - 1) * per]).fetchall()
            rel = related_counts(c, rows) if s["group_duplicates"] else {}
            fw, fa = build_filters(p, s, skip_category=True)
            facets, ft = Counter(), 0
            for r in c.execute(f"SELECT categories FROM articles{fw}", fa):
                ft += 1
                for cat in json.loads(r[0] or "[]"):
                    facets[cat] += 1
        return {"total": total, "page": page, "per_page": per, "pages": pages, "facets": dict(facets),
                "facet_total": ft,
                "items": [serialize(r, (page - 1) * per + i + 1, rel.get(r["group_id"], 0)) for i, r in enumerate(rows)]}

    @app.get("/api/articles/{aid}/related")
    def related(aid: int):
        with db() as c:
            r = c.execute("SELECT group_id FROM articles WHERE id=?", (aid,)).fetchone()
            if not r:
                raise HTTPException(404, "article not found")
            rows = c.execute("SELECT * FROM articles WHERE group_id=? AND id!=? ORDER BY sort_at DESC LIMIT 50",
                             (r["group_id"], aid)).fetchall()
        return [serialize(x) for x in rows]

    @app.post("/api/articles/{aid}/save", dependencies=[Depends(admin)])
    def save_article(aid: int, payload: dict = Body(default=None)):
        val = 1 if (payload or {}).get("saved", True) else 0
        with db() as c:
            if c.execute("UPDATE articles SET saved=? WHERE id=?", (val, aid)).rowcount != 1:
                raise HTTPException(404, "article not found")
        return {"id": aid, "saved": bool(val)}

    @app.get("/api/stats")
    def stats():
        s = store.settings()
        tiers = MODE_TIERS[s["mode"]]
        ph = ",".join("?" * len(tiers))
        lo_today, _ = date_bounds("today")
        lo_week, _ = date_bounds("7d")
        with db() as c:
            q = f"SELECT COUNT(*) FROM articles WHERE relevance IN ({ph})"
            total = c.execute(q, tiers).fetchone()[0]
            today = c.execute(q + " AND sort_at>=?", [*tiers, lo_today]).fetchone()[0]
            week = c.execute(q + " AND sort_at>=?", [*tiers, lo_week]).fetchone()[0]
            saved = c.execute("SELECT COUNT(*) FROM articles WHERE saved=1").fetchone()[0]
            last_run = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        srcs = sources_with_health(store)
        cnt = Counter(x["health"]["status"] for x in srcs)
        last_success = max((x["health"]["last_success"] for x in srcs if x["health"]["last_success"]), default=None)
        return {"total": total, "today": today, "week": week, "saved": saved, "mode": s["mode"],
                "mode_label": MODE_LABELS[s["mode"]], "sources_total": len(srcs),
                "sources_enabled": sum(1 for x in srcs if x["enabled"]),
                "sources_active": cnt["active"] + cnt["partial"], "sources_error": cnt["error"] + cnt["inactive"],
                "sources_manual": cnt["manual-only"], "sources_unverified": cnt["unverified"],
                "status_counts": dict(cnt), "last_success": last_success,
                "last_run": dict(last_run) if last_run else None,
                "next_run": sched.next_run() if sched else None, "running": engine.state["running"]}

    @app.get("/api/sources")
    def list_sources():
        return sources_with_health(store)

    @app.post("/api/sources", dependencies=[Depends(admin)])
    def add_source(payload: dict = Body(...)):
        lst = store.sources()
        try:
            src = validate_source(payload, {s["id"] for s in lst})
        except ValueError as e:
            bad(e)
        lst.append(src)
        store.save_sources(lst)
        return src

    @app.put("/api/sources/{sid}", dependencies=[Depends(admin)])
    def edit_source(sid: str, payload: dict = Body(...)):
        lst = store.sources()
        for i, s in enumerate(lst):
            if s["id"] != sid:
                continue
            try:
                new = validate_source({**s, **payload}, set(), existing_id=sid)
            except ValueError as e:
                bad(e)
            changed = any(new.get(k) != s.get(k) for k in ("url", "section_url", "feeds", "method", "listing"))
            lst[i] = new
            store.save_sources(lst)
            if changed:
                with db() as c:
                    upsert_health(c, sid, resolved_feeds="[]", cond="{}", last_discovery=None, status="unverified",
                                  error_count=0, last_error=None)
            return new
        raise HTTPException(404, "source not found")

    @app.delete("/api/sources/{sid}", dependencies=[Depends(admin)])
    def delete_source(sid: str):
        lst = store.sources()
        keep = [s for s in lst if s["id"] != sid]
        if len(keep) == len(lst):
            raise HTTPException(404, "source not found")
        store.save_sources(keep)
        with db() as c:
            c.execute("DELETE FROM source_health WHERE source_id=?", (sid,))
        return {"deleted": sid, "note": "Previously collected articles are retained."}

    @app.post("/api/sources/{sid}/retry", dependencies=[Depends(admin)])
    def retry_source(sid: str):
        if sid not in {s["id"] for s in store.sources()}:
            raise HTTPException(404, "source not found")
        if not engine.start_background(trigger="retry", only={sid}, force=True, rediscover=True):
            return JSONResponse({"started": False, "detail": "Collection already running"}, status_code=409)
        return {"started": True}

    @app.post("/api/refresh", dependencies=[Depends(admin)])
    def refresh(payload: dict = Body(default=None)):
        p = payload or {}
        if not engine.start_background(trigger="manual", force=True, rediscover=bool(p.get("rediscover"))):
            return JSONResponse({"started": False, "detail": "Collection already running"}, status_code=409)
        return {"started": True}

    @app.get("/api/refresh/status")
    def refresh_status():
        return engine.state

    @app.get("/api/settings")
    def get_settings():
        return {**store.settings(), "ai_available": bool(ANTHROPIC_API_KEY),
                "next_run": sched.next_run() if sched else None}

    @app.put("/api/settings", dependencies=[Depends(admin)])
    def put_settings(payload: dict = Body(...)):
        cur = store.settings()
        try:
            new = validate_settings(payload, cur)
        except (ValueError, KeyError, TypeError) as e:
            bad(e)
        store.save_settings(new)
        if sched and new["interval_min"] != cur["interval_min"]:
            sched.apply(new["interval_min"], first_delay=30)
        return new

    @app.get("/api/keywords")
    def get_keywords():
        return store.keywords()

    @app.put("/api/keywords", dependencies=[Depends(admin)])
    def put_keywords(payload: dict = Body(...)):
        try:
            store.save_keywords(validate_keywords(payload))
        except ValueError as e:
            bad(e)
        return {"saved": True}

    @app.post("/api/reclassify", dependencies=[Depends(admin)])
    def reclassify():
        return {"updated": reclassify_all(store)}

    @app.get("/api/export/articles.csv")
    def export_csv(request: Request):
        rows = fetch_rows(dict(request.query_params), store.settings())
        return download(build_csv(rows), f"nepse_news_{stamp()}.csv", "text/csv; charset=utf-8")

    @app.get("/api/export/articles.xlsx")
    def export_xlsx(request: Request):
        p = dict(request.query_params)
        return download(build_xlsx(fetch_rows(p, store.settings()), p), f"nepse_news_{stamp()}.xlsx",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/export/digest.md")
    def export_digest(period: str = "daily"):
        period = "weekly" if period == "weekly" else "daily"
        return download(build_digest(period).encode("utf-8"), f"nepse_{period}_digest_{stamp()}.md",
                        "text/markdown; charset=utf-8")

    @app.get("/api/export/snapshot.html")
    def export_snapshot(request: Request):
        html_doc = build_snapshot(dict(request.query_params), store.settings())
        return download(html_doc.encode("utf-8"), f"nepse_news_snapshot_{stamp()}.html", "text/html; charset=utf-8")

    @app.get("/api/export/sources.json")
    def export_sources_json():
        data = json.dumps(sources_with_health(store), ensure_ascii=False, indent=2)
        return download(data.encode("utf-8"), f"nepse_sources_{stamp()}.json", "application/json")

    @app.get("/api/export/sources.csv")
    def export_sources_csv():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Source ID", "Source Name", "Official URL", "News Section URL", "Source Type", "Language",
                    "Collection Method", "Integration Status", "Resolved Feed", "Last Successful Fetch (NPT)",
                    "Last Checked (NPT)", "Articles Retrieved", "Relevant Articles", "Error Status", "Evidence",
                    "Notes"])
        for s in sources_with_health(store):
            h = s["health"]
            w.writerow([safe_cell(x) for x in [
                s["id"], s["name"], s["url"], s["section_url"], s["type"], s["language"], s["method"], h["status"],
                " ".join(h["resolved_feeds"]), npt_str(h["last_success"]), npt_str(h["last_checked"]),
                h["articles_total"] or 0, h["relevant_total"] or 0, h["last_error"] or "", s["evidence"],
                s["notes"]]])
        return download(("\ufeff" + buf.getvalue()).encode("utf-8"), f"nepse_sources_{stamp()}.csv",
                        "text/csv; charset=utf-8")

    @app.get("/api/export/keywords.json")
    def export_keywords():
        return download(json.dumps(store.keywords(), ensure_ascii=False, indent=2).encode("utf-8"),
                        f"nepse_keywords_{stamp()}.json", "application/json")

    return app

# --------------------------------------------------------------------------------------
# CLI: verify-sources (live source audit)
# --------------------------------------------------------------------------------------
def probe_source(http, s) -> dict:
    r = {"id": s["id"], "name": s["name"], "url": s["url"], "section_url": s["section_url"], "type": s["type"],
         "language_configured": s["language"], "method_configured": s["method"], "evidence_seed": s["evidence"],
         "checked_npt": datetime.now(NPT).strftime("%Y-%m-%d %H:%M")}
    try:
        resp = http.get(s["url"], retries=1)
        r["site_http"], r["final_url"] = resp.status_code, str(resp.url)
    except httpx.HTTPError as e:
        r["site_http"], r["final_url"] = f"ERR {type(e).__name__}", ""
    try:
        r["robots_allows_home"] = http.allowed(s["url"])
    except Exception:
        r["robots_allows_home"] = "unknown"
    disc = discover_feeds(http, s)
    r["feed_url"] = disc["feeds"][0] if disc["feeds"] else ""
    r["discovery_note"] = disc["note"]
    r.update(feed_items=0, dated_pct="", languages_detected="", excerpts_pct="", sample_headline="", sample_url="")
    if r["feed_url"]:
        try:
            items, _ = fetch_feed(http, r["feed_url"], {})
            n = len(items)
            langs = Counter(detect_lang(i["title"]) for i in items)
            r.update(feed_items=n, dated_pct=round(100 * sum(1 for i in items if i["published_at"]) / n) if n else 0,
                     excerpts_pct=round(100 * sum(1 for i in items if i["excerpt"]) / n) if n else 0,
                     languages_detected=" ".join(f"{k}:{v}" for k, v in langs.items()),
                     sample_headline=items[0]["title"][:100] if items else "",
                     sample_url=items[0]["url"] if items else "")
            r["recommended_status"] = "active (RSS)"
        except (CollectError, httpx.HTTPError) as e:
            r["recommended_status"] = f"error ({e})"
    else:
        code = r["site_http"]
        if isinstance(code, int) and code in (401, 403, 429):
            r["recommended_status"] = f"manual-only (HTTP {code}: access restricted - not bypassed)"
        elif isinstance(code, int) and code < 400:
            r["recommended_status"] = "manual-only (site reachable; no permitted feed)"
        else:
            r["recommended_status"] = "inactive / unreachable"
    return r


def cmd_verify(args):
    store = Store(CFG.data_dir)
    init_db()
    http = Http()
    sources = [s for s in store.sources() if not args.only or s["id"] in args.only]
    print(f"Probing {len(sources)} sources live (robots.txt honoured; this can take a few minutes)...")
    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(lambda s: probe_source(http, s), sources))
    rows.sort(key=lambda r: (not r["feed_url"], r["name"].lower()))
    cols = list(rows[0].keys()) if rows else []
    with open(CFG.data_dir / "source_report.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: safe_cell(v) for k, v in r.items()})
    summary = Counter(r["recommended_status"].split(" (")[0] for r in rows)
    md = [f"# Binjal Halwai NewsPortal - Source Verification Report", "",
          f"Generated {datetime.now(NPT):%Y-%m-%d %H:%M} NPT from this machine's network. Method: homepage fetch, "
          "robots.txt check, configured feed -> `<link rel=alternate>` autodiscovery -> `/feed`, `/rss`, "
          "`/rss.xml`, `/feed.xml`; feed parsed and sampled. No login, paywall or bot-protection bypass attempted.",
          "", "**Summary:** " + ", ".join(f"{k}: {v}" for k, v in summary.most_common()), "",
          "| # | Source | Type | Site HTTP | robots | Feed | Items | Dated % | Langs | Recommended | Notes |",
          "|---:|---|---|---|---|---|---:|---:|---|---|---|"]
    for i, r in enumerate(rows, 1):
        note = (r["discovery_note"] or "").replace("|", "/")[:140]
        md.append(f"| {i} | {r['name']} | {r['type']} | {r['site_http']} | {r['robots_allows_home']} | "
                  f"{r['feed_url'] or '-'} | {r['feed_items']} | {r['dated_pct']} | {r['languages_detected']} | "
                  f"{r['recommended_status']} | {note} |")
    (CFG.data_dir / "source_report.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md[4:]))
    print(f"\nReports written: {CFG.data_dir / 'source_report.md'} and source_report.csv")
    if args.apply:
        found = {r["id"]: r["feed_url"] for r in rows if r["feed_url"]}
        lst = store.sources()
        for s in lst:
            if s["id"] in found:
                fu = found[s["id"]]
                s["feeds"] = [fu] + [x for x in s["feeds"] if x != fu]
                if s["method"] in ("auto", "manual"):
                    s["method"] = "rss"
        store.save_sources(lst)
        with db() as c:
            for sid, fu in found.items():
                upsert_health(c, sid, resolved_feeds=json.dumps([fu]), last_discovery=iso(utcnow()), error_count=0)
        print(f"--apply: {len(found)} sources set to RSS with verified feed URLs.")


# --------------------------------------------------------------------------------------
# CLI: selftest (offline). Headlines below are SYNTHETIC test fixtures, not news.
# --------------------------------------------------------------------------------------
SELFTEST_CASES = [
    ("NEPSE gains 23 points as turnover crosses Rs 5 billion", "", {"high"}, "market_daily"),
    ("नेप्से ३२ अंकले बढ्यो, कारोबार रकम ६ अर्ब नाघ्यो", "", {"high"}, "market_daily"),
    ("Himal Sample Hydropower to issue IPO to locals of project-affected areas", "", {"high"}, "ipo"),
    ("Sample Bikas Bank announces 12% cash dividend; book closure on Friday", "", {"high"}, "dividend"),
    ("Demo Laghubitta posts 40% rise in net profit in first quarter",
     "The company published its unaudited quarterly financial statement.", {"high", "medium"}, "results"),
    ("NRB unveils monetary policy for FY 2026/27, cuts policy rate", "", {"medium", "high"}, "nrb"),
    ("राष्ट्र बैंकले नीतिगत दर घटायो, तरलता बढ्ने", "", {"medium", "high"}, "nrb"),
    ("Commercial banks' interest rates on deposits to fall from next month", "", {"medium"}, "banking"),
    ("SEBON directs brokers to settle client funds within T+2", "", {"high"}, "sebon"),
    ("लगानीकर्ताले आईपीओमा आवेदन दिँदा मेरोशेयर प्रयोग गर्नुपर्ने", "", {"high"}, "ipo"),
    ("Demo Bank and Sample Bank sign agreement for merger", "", {"high", "medium"}, "mna"),
    ("Gold price hits record high in Kathmandu market", "", {"low"}, "economy"),
    ("Parliament passes bill on federal police", "", {"excluded"}, None),
    ("Wall Street closes higher as tech stocks rally", "", {"excluded"}, None),
    ("Sensex falls 500 points on foreign investor selling", "", {"excluded"}, None),
    ("Nepal cricket team wins T20 series against UAE", "", {"excluded"}, None),
]


def cmd_selftest(_args=None) -> int:
    results = []

    def check(ok, name, detail=""):
        results.append((bool(ok), name, detail))

    clf = Classifier(DEFAULT_KEYWORDS, DEFAULT_SETTINGS["thresholds"])
    src = {"id": "test", "name": "Test", "type": "general_news", "country": "NP"}
    for title, body, tiers, cat in SELFTEST_CASES:
        r = clf.classify(title, body, src)
        ok = r["relevance"] in tiers and (cat is None or cat in r["categories"])
        check(ok, f"classify: {title[:58]}", f"got {r['relevance']} ({r['score']}) {r['categories']}; "
                                             f"expected {sorted(tiers)} {cat or ''}")
    for mode, tier_expect in (("strict", {"high"}), ("broad", {"high", "medium", "low"})):
        check(set(MODE_TIERS[mode]) == tier_expect, f"mode tiers: {mode}")
    check(clf.classify("Nepse index", "", src)["language"] == "en", "language: English")
    check(clf.classify("नेप्से परिसूचक", "", src)["language"] == "ne", "language: Nepali")
    check(canonical_url("https://www.a.example/n/1/?utm_source=x&id=5#top") == "https://a.example/n/1?id=5",
          "canonical URL strips www, tracking params, slash, fragment")
    now = utcnow()
    d, q = sanity_date(now + timedelta(hours=5, minutes=35), now)
    check(q == "adjusted" and d is not None and abs((d - (now - timedelta(minutes=10))).total_seconds()) < 2,
          "date: NPT-labelled-as-UTC repaired")
    d, q = sanity_date(now + timedelta(days=3), now)
    check(d is None and q == "suspect", "date: implausible future timestamp rejected")
    d, q = parse_loose_date("२ घण्टा अगाडि", now)
    check(q == "relative" and d is not None and abs((now - d).total_seconds() - 7200) < 2, "date: Nepali relative")
    check(safe_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)", "export: formula injection neutralised")
    check(make_excerpt("<p>Hello <b>world</b></p> The post X appeared first on Y.") == "Hello world",
          "excerpt: HTML + WordPress tail stripped")
    a, b = title_tokens("NEPSE gains 23 points as turnover crosses Rs 5 billion"), \
        title_tokens("Nepse gains 23 points; turnover crosses Rs 5 bn")
    check(len(a & b) / len(a | b) >= 0.6, "dedup: near-duplicate similarity >= 0.6")
    # DB round-trip on a temporary database
    tmp = Path(tempfile.mkdtemp(prefix="nnh_test_"))
    old = CFG.db_path
    CFG.db_path = tmp / "t.db"
    try:
        init_db()
        st = copy.deepcopy(DEFAULT_SETTINGS)
        sa = normalize_source({"id": "ta", "name": "Test A", "url": "https://a.example/"})
        sb = normalize_source({"id": "tb", "name": "Test B", "url": "https://b.example/"})
        it = {"title": "NEPSE gains 23 points as turnover crosses Rs 5 billion", "excerpt": "",
              "url": "https://a.example/n/1?utm_source=x", "published_at": now - timedelta(hours=1),
              "date_quality": "source"}
        with db() as c:
            n1 = ingest(c, [it], sa, clf, st)[0]
            n2 = ingest(c, [dict(it, url="https://www.a.example/n/1/")], sa, clf, st)[0]
            n3 = ingest(c, [{"title": "Nepse gains 23 points; turnover crosses Rs 5 bn", "excerpt": "",
                             "url": "https://b.example/x", "published_at": now, "date_quality": "source"}],
                        sb, clf, st)[0]
            n4 = ingest(c, [{"title": "Parliament passes bill on federal police", "excerpt": "",
                             "url": "https://b.example/p", "published_at": None}], sb, clf, st)[0]
            groups = [r[0] for r in c.execute("SELECT group_id FROM articles WHERE relevance!='excluded' ORDER BY id")]
            w, ar = build_filters({"q": "turnover", "preset": "7d"}, st)
            found = c.execute(f"SELECT COUNT(*) FROM articles{w}", ar).fetchone()[0]
            w2, ar2 = build_filters({"preset": "7d"}, st)
            visible = c.execute(f"SELECT COUNT(*) FROM articles{w2}", ar2).fetchone()[0]
            miss = c.execute("SELECT date_quality FROM articles WHERE url='https://b.example/p'").fetchone()[0]
        check(n1 == 1 and n2 == 0 and n3 == 1 and n4 == 1, "db: canonical-URL dedup", f"{n1},{n2},{n3},{n4}")
        check(len(groups) == 2 and groups[0] == groups[1], "db: cross-source grouping keeps both links", str(groups))
        check(found == 2, "db: search + NPT date filter", str(found))
        check(visible == 2, "db: excluded articles hidden in default mode", str(visible))
        check(miss == "missing", "db: missing publication time flagged, not invented")
        check(len(build_csv(c_rows := fetch_rows({"preset": "7d"}, st))) > 0 and len(c_rows) == 2, "export: CSV")
        try:
            import openpyxl  # noqa: F401
            check(len(build_xlsx(c_rows, {})) > 1000, "export: XLSX")
        except ImportError:
            check(False, "export: XLSX", "openpyxl not installed")
        check("NEPSE News Digest" in build_digest("weekly"), "export: weekly digest")
    finally:
        CFG.db_path = old
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        import apscheduler
        check(apscheduler.__version__.split(".")[0] == "3", "dependency: APScheduler 3.x", apscheduler.__version__)
    except ImportError:
        check(False, "dependency: APScheduler 3.x", "not installed")
    width = max(len(n) for _, n, _ in results)
    for ok, name, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {'' if ok else detail}")
    failed = sum(1 for ok, _, _ in results if not ok)
    print(f"\n{len(results) - failed}/{len(results)} passed" + (f", {failed} FAILED" if failed else ""))
    return 1 if failed else 0


# --------------------------------------------------------------------------------------
# CLI: serve / collect / init
# --------------------------------------------------------------------------------------
def setup_logging():
    CFG.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.handlers.RotatingFileHandler(CFG.data_dir / "nnh.log", maxBytes=2_000_000, backupCount=3,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)
    for noisy in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_serve(args):
    store = Store(CFG.data_dir)
    init_db()
    engine = Engine(store)
    sched = None
    if not args.no_scheduler:
        sched = Scheduler(engine, store)
        sched.start()
    if args.host not in ("127.0.0.1", "localhost") and not ADMIN_TOKEN:
        log.warning("Listening on %s WITHOUT NNH_ADMIN_TOKEN - anyone on the network can change settings.", args.host)
    app = create_app(store, engine, sched)
    import uvicorn
    print(f"\n  {APP_NAME} v{VERSION}\n  Dashboard: http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"
          f"\n  Data dir : {CFG.data_dir}\n  Schedule : "
          f"{'every ' + str(store.settings()['interval_min']) + ' min' if sched and store.settings()['interval_min'] else 'manual only'}"
          "\n  Stop     : Ctrl+C\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_collect(args):
    store = Store(CFG.data_dir)
    init_db()
    engine = Engine(store)
    only = set(args.source) if args.source else None
    totals = engine.run(trigger="cli", only=only, force=args.force, rediscover=args.rediscover)
    print(json.dumps(totals, indent=2))
    for s in sources_with_health(store):
        if only is None or s["id"] in only:
            h = s["health"]
            print(f"{h['status']:<12} {s['name'][:34]:<34} new={h.get('last_new') or 0:<4} "
                  f"{(h.get('last_error') or '')[:90]}")


def cmd_publish(args):
    """Write a public, static site (headlines + links only) to ./public_site/index.html"""
    store = Store(CFG.data_dir)
    init_db()
    html_doc = build_snapshot({}, store.settings())
    out = Path(__file__).resolve().parent / "public_site"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(html_doc, encoding="utf-8")
    print(f"Public site written to: {out / 'index.html'}  ({len(html_doc) // 1024} KB)")


def cmd_init(args):
    CFG.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(CFG.data_dir)
    if args.reset_sources:
        store.save_sources(DEFAULT_SOURCES)
    if args.reset_keywords:
        store.save_keywords(DEFAULT_KEYWORDS)
    if args.reset_settings:
        store.save_settings(DEFAULT_SETTINGS)
    init_db()
    env = CFG.data_dir.parent / ".env.example"
    if not env.exists():
        env.write_text("# Copy to .env and edit. Never commit real secrets.\nNNH_HOST=127.0.0.1\nNNH_PORT=8000\n"
                       "NNH_ADMIN_TOKEN=\nNNH_CONTACT=\nANTHROPIC_API_KEY=\n"
                       "NNH_AI_MODEL=claude-haiku-4-5-20251001\n", encoding="utf-8")
    print(f"Initialised {CFG.data_dir} (sources.json, keywords.json, settings.json, nepse_news.db) and .env.example")


# --------------------------------------------------------------------------------------
# manual_headlines.txt  ->  hub  (for portals whose terms forbid automated access)
# --------------------------------------------------------------------------------------
MANUAL_FILE = Path(__file__).resolve().parent / "manual_headlines.txt"


def parse_manual_text(text, sources, today=None, max_age_days=3):
    """Parse the hand-typed headline file. Format:
        ## 2026-10-04            <- date header; applies to the lines below it
        sharesansar | Headline text copied from the page | https://www.sharesansar.com/newsdetail/...   (link optional)
    Returns (items_by_source_id, problems). Lines older than max_age_days are ignored."""
    import hashlib
    today = today or utcnow().date()
    by_key = {}
    for sc in sources:
        by_key[str(sc["id"]).lower()] = sc
        by_key[str(sc["name"]).lower()] = sc
    host = lambda u: (urlparse(u).hostname or "").lower().removeprefix("www.")  # noqa: E731
    out, problems, seen = {}, [], set()
    cur_date = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("﻿")
        if not line or (line.startswith("#") and not line.startswith("##")):
            continue
        if line.startswith("##"):
            try:
                cur_date = datetime.strptime(line.lstrip("#").strip()[:10], "%Y-%m-%d").date()
            except ValueError:
                cur_date = None
                problems.append(f"line {n}: bad date header (use ## YYYY-MM-DD)")
            continue
        if cur_date is None:
            problems.append(f"line {n}: no date header above this line")
            continue
        if (today - cur_date).days > max_age_days or cur_date > today + timedelta(days=1):
            continue
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 2:
            problems.append(f"line {n}: expected  source | headline | optional link")
            continue
        src = by_key.get(parts[0].lower())
        if src is None:
            problems.append(f"line {n}: unknown source '{parts[0]}'")
            continue
        title = clean_text(parts[1])
        if not (15 <= len(title) <= 300):
            problems.append(f"line {n}: headline must be 15-300 characters")
            continue
        sh, base = host(src["url"]), src["url"].rstrip("/")
        url = parts[2] if len(parts) > 2 else ""
        if url and not (is_http_url(url) and (host(url) == sh or host(url).endswith("." + sh))):
            problems.append(f"line {n}: link is not on {sh}; used the portal front page instead")
            url = ""
        if not url:
            url = base + "/?mh=" + hashlib.sha1(title.lower().encode("utf-8")).hexdigest()[:12]
        key = (src["id"], title.lower())
        if key in seen:
            continue
        seen.add(key)
        pub = min(datetime(cur_date.year, cur_date.month, cur_date.day, 6, 0, tzinfo=UTC), utcnow())
        out.setdefault(src["id"], []).append({"url": url, "title": title, "excerpt": "",
                                              "published_at": pub, "date_quality": "missing"})
    return out, problems


def cmd_manual_import(args):
    """Read manual_headlines.txt (typed by you) and add the headlines to the hub. Safe to run every time."""
    path = Path(args.file) if args.file else MANUAL_FILE
    if not path.exists():
        print(f"No {path.name} found - nothing to import.")
        return
    store = Store(CFG.data_dir)
    init_db()
    sources = store.sources()
    items, problems = parse_manual_text(path.read_text(encoding="utf-8"), sources, max_age_days=args.days)
    settings = store.settings()
    clf = Classifier(store.keywords(), settings["thresholds"])
    added = dup = promoted = 0
    with db() as c:
        for sid, arts in items.items():
            src = next(x for x in sources if x["id"] == sid)
            new, _ = ingest(c, arts, src, clf, settings)
            added += new
            dup += len(arts) - new
            # You chose these headlines yourself, so they must be visible even when the keyword classifier would
            # rate them 'excluded'/'low' (e.g. a housing-policy story). Also repairs rows imported by v1.2.4.
            for a in arts:
                promoted += c.execute(
                    "UPDATE articles SET relevance='medium' WHERE canonical_url=? AND relevance IN ('excluded','low')",
                    (canonical_url(a["url"]),)).rowcount
            print(f"{src['name']:<16} read={len(arts):<3} new={new}")
    for pr in problems:
        print("WARNING:", pr)
    print(f"Manual import done: {added} added, {dup} already present, {promoted} made visible, "
          f"{len(problems)} line(s) with problems.")



def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # Devanagari on Windows consoles
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="nepse_news_hub", description=f"{APP_NAME} v{VERSION}")
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("serve", help="run dashboard + scheduler (default)")
    sp.add_argument("--host", default=HOST)
    sp.add_argument("--port", type=int, default=PORT)
    sp.add_argument("--no-scheduler", action="store_true")
    sc = sub.add_parser("collect", help="run one collection pass now")
    sc.add_argument("--source", action="append")
    sc.add_argument("--force", action="store_true")
    sc.add_argument("--rediscover", action="store_true")
    sv = sub.add_parser("verify-sources", help="live audit of every source")
    sv.add_argument("--only", action="append")
    sv.add_argument("--apply", action="store_true")
    sub.add_parser("selftest", help="offline functional tests")
    sub.add_parser("publish", help="build public_site/index.html (static, headlines + links only)")
    sm = sub.add_parser("manual-import", help="add headlines typed into manual_headlines.txt")
    sm.add_argument("--file")
    sm.add_argument("--days", type=int, default=3)
    si = sub.add_parser("init", help="create data files")
    si.add_argument("--reset-sources", action="store_true")
    si.add_argument("--reset-keywords", action="store_true")
    si.add_argument("--reset-settings", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args(["serve"])
    setup_logging()
    if args.cmd == "selftest":
        sys.exit(cmd_selftest(args))
    {"serve": cmd_serve, "collect": cmd_collect, "verify-sources": cmd_verify, "init": cmd_init, "publish": cmd_publish,
     "manual-import": cmd_manual_import}[args.cmd](args)

# --------------------------------------------------------------------------------------
# Dashboard (served at /; also reused for the standalone snapshot export)
# --------------------------------------------------------------------------------------
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Binjal Halwai NewsPortal — Nepal Stock Market News (NEPSE)</title>
<meta name="description" content="Nepal stock market news aggregator — NEPSE, SEBON, IPO, dividend, banking and financial news from top Nepali publishers in one place.">
<link rel="canonical" href="https://binjalhalwai.github.io/">
<meta property="og:type" content="website">
<meta property="og:url" content="https://binjalhalwai.github.io/">
<meta property="og:title" content="Binjal Halwai NewsPortal — Nepal Stock Market News (NEPSE)">
<meta property="og:description" content="Nepal stock market news aggregator — NEPSE, SEBON, IPO, dividend, banking and financial news from top Nepali publishers in one place.">
<meta property="og:site_name" content="Binjal Halwai NewsPortal">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="Binjal Halwai NewsPortal — Nepal Stock Market News (NEPSE)">
<meta name="twitter:description" content="Nepal stock market news aggregator — NEPSE, SEBON, IPO, dividend, banking and financial news from top Nepali publishers.">
<meta name="robots" content="index,follow">
<style>
:root{--bg:#14170b;--p:#1f2412;--p2:#232914;--ln:rgba(238,241,220,.12);--tx:#ffffff;--mu:#dfe5c8;--ac:#a3b565;--ach:#b8c97c;--ac2:#b8c97c;--lk:#b8c97c;--hi:#ff7a5c;--md:#38bdf8;--lo:#838b68;--ok:#4ade80;--wa:#f5c542;--bad:#fb7185;--ch:#2a3116;--gA:#b8c97c;--gB:#a3b565;--onac:#14170b;--hd1:#1a1e0f;--hd2:#1a1e0f;--hdtx:#ffffff;--hdmu:#dfe5c8;--hdln:rgba(238,241,220,.28);--hdbg:rgba(238,241,220,.06);--s2:#4ade80;--s3:#b8c97c;--s4:#8b9470;--s5:#fb7185;--s6:#a3b565;--s7:#7fb59a}
[data-theme=light]{--bg:#f7f8ee;--p:#ffffff;--p2:#fafbf2;--ln:rgba(77,91,42,.18);--tx:#000000;--mu:#3d4526;--ac:#4d5b2a;--ach:#3d4922;--ac2:#65772f;--lk:#65772f;--hi:#d9381e;--md:#0369a1;--lo:#8b9470;--wa:#8a5a00;--ok:#16a34a;--bad:#e11d48;--ch:#e7ebd2;--gA:#65772f;--gB:#4d5b2a;--onac:#ffffff;--hd1:#f1f3e0;--hd2:#f1f3e0;--hdtx:#000000;--hdmu:#3d4526;--hdln:rgba(77,91,42,.35);--hdbg:#ffffff;--s2:#16a34a;--s3:#65772f;--s4:#a3b565;--s5:#e11d48;--s6:#8b9470;--s7:#4d8f6b}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--tx);font:13px/1.45 "Segoe UI",system-ui,-apple-system,Roboto,"Nirmala UI","Noto Sans Devanagari",sans-serif}
a{color:var(--lk);text-decoration:none}a:hover{text-decoration:underline}button,select,input,textarea{font:inherit;color:inherit}
.mono{font-family:Consolas,"JetBrains Mono",monospace}
.top{position:sticky;top:0;z-index:20;display:flex;gap:14px;align-items:center;padding:8px 14px;background:var(--p);border-bottom:2px solid var(--ac);flex-wrap:wrap}
.brand{display:flex;gap:10px;align-items:center}.logo{width:34px;height:34px;display:grid;place-items:center;background:var(--ac);color:var(--onac);font-weight:800;border-radius:6px}
.brand b{display:block;letter-spacing:.06em;font-size:26px;font-weight:800;line-height:1.1;white-space:nowrap}.brand small{display:block;color:var(--mu);font-size:11px;margin-top:2px}.brand .logo{width:46px;height:46px;font-size:18px}
.search{flex:1;min-width:200px}.search input{width:100%;padding:8px 12px;background:var(--p2);border:1px solid var(--ln);border-radius:6px}
.hr{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.clk{text-align:right;line-height:1.2}.clk small{display:block;color:var(--mu);font-size:10px}
.btn{background:var(--ac);color:var(--onac);border:0;border-radius:5px;padding:6px 11px;font-weight:600;cursor:pointer;white-space:nowrap;display:inline-block}
.btn:disabled{opacity:.6}.btn.gh{background:transparent;color:var(--tx);border:1px solid var(--ln);font-weight:500}.btn.gh:hover{border-color:var(--ac);text-decoration:none}
.ic{background:transparent;border:1px solid var(--ln);border-radius:5px;padding:3px 8px;cursor:pointer;color:var(--tx)}.ic:hover{border-color:var(--ac);text-decoration:none}.ic.on{color:var(--ac)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--mu);margin-right:5px}.dot.ok{background:var(--ok)}.dot.wa{background:var(--wa)}.dot.bad{background:var(--bad)}
.stats{display:grid;grid-template-columns:repeat(7,minmax(120px,1fr));gap:1px;background:var(--ln);border-bottom:1px solid var(--ln)}
.stat{background:var(--p2);padding:8px 12px;color:var(--tx);display:block}.stat:hover{text-decoration:none}
.sl{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--mu)}.sv{font-size:20px;font-weight:700;font-family:Consolas,monospace}.sv.sm{font-size:13px;color:var(--lk)}.ss{font-size:11px;color:var(--mu)}.stat.bad .sv{color:var(--bad)}
.lay{display:grid;grid-template-columns:250px 1fr}.side{background:var(--p);border-right:1px solid var(--ln);padding:6px 0;position:sticky;top:72px;align-self:start;max-height:calc(100vh - 72px);overflow:auto}
.nh{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--mu);padding:10px 14px 4px}
.nav{display:flex;justify-content:space-between;gap:8px;width:100%;text-align:left;background:none;border:0;border-left:3px solid transparent;padding:5px 14px;cursor:pointer;color:var(--tx)}
.nav:hover{background:var(--p2)}.nav.act{border-left-color:var(--ac);background:var(--p2);color:var(--ac)}.nav.zero{opacity:.5}.cnt{font-family:Consolas,monospace;font-size:11px;color:var(--mu)}
main{padding:10px 16px;min-width:0}
.flt{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:end;padding:6px 0 10px;border-bottom:1px solid var(--ln)}
.flt label{display:flex;flex-direction:column;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--mu);gap:2px}
select,input,textarea{background:var(--p2);border:1px solid var(--ln);border-radius:5px;padding:5px 7px;color:var(--tx)}
.seg{display:flex;border:1px solid var(--ln);border-radius:5px;overflow:hidden}.seg button{background:none;border:0;padding:5px 10px;cursor:pointer;color:var(--tx)}.seg button.on{background:var(--ac);color:var(--onac)}
.menu{position:relative}.menu summary{list-style:none;cursor:pointer}.menu summary::-webkit-details-marker{display:none}
.menu>div{position:absolute;right:0;top:calc(100% + 4px);background:var(--p);border:1px solid var(--ln);border-radius:6px;min-width:240px;z-index:30;box-shadow:0 8px 24px rgba(0,0,0,.35)}
.menu>div button{display:block;width:100%;text-align:left;background:none;border:0;padding:8px 12px;cursor:pointer;color:var(--tx)}.menu>div button:hover{background:var(--p2)}
#fm{color:var(--mu);font-size:12px;padding:8px 0}.feed{list-style:none;margin:0;padding:0}
.art{display:grid;grid-template-columns:54px 1fr auto;gap:10px;padding:9px 6px;border-bottom:1px solid var(--ln);align-items:start}.art:hover{background:var(--p2)}
.num{font-family:Consolas,monospace;color:var(--ac);font-weight:700}.hl{font-size:14.5px;font-weight:600;color:var(--tx);line-height:1.35}.hl:hover{color:var(--lk)}
.meta{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;margin-top:3px;font-size:11.5px;color:var(--mu)}
.src{background:none;border:0;padding:0;color:var(--lk);font-weight:600;cursor:pointer;font-size:11.5px}
.chip{background:var(--ch);border:1px solid var(--ln);border-radius:10px;padding:0 8px;font-size:11px;color:var(--tx);cursor:pointer}
.rel{font-weight:700;font-size:10.5px;text-transform:uppercase}.r-high{color:var(--hi)}.r-medium{color:var(--md)}.r-low,.r-excluded{color:var(--lo)}
.lg{font-family:Consolas,monospace;font-size:10.5px;border:1px solid var(--ln);border-radius:3px;padding:0 4px}
.bdg{font-size:10px;font-weight:700;border-radius:3px;padding:1px 5px}.bdg.off{background:var(--ac);color:var(--onac)}.bdg.rn{background:none;border:1px dashed var(--mu);color:var(--tx);cursor:pointer}
.warn{color:var(--wa)}.acts{display:flex;gap:6px;align-items:center;white-space:nowrap}.read{font-size:12px;font-weight:600;border:1px solid var(--ln);border-radius:5px;padding:3px 8px}
.feed.cards .art{background:var(--p);border:1px solid var(--ln);border-radius:8px;margin-bottom:8px;padding:12px}
.sum{margin:6px 0 0;max-width:95ch}.lbl{font-size:10px;text-transform:uppercase;color:var(--mu);border:1px solid var(--ln);border-radius:3px;padding:0 4px}.lbl.ai{color:var(--ac);border-color:var(--ac)}
.kws{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.kw{font-size:10.5px;color:var(--mu);background:var(--ch);border-radius:3px;padding:0 5px}
.relz{grid-column:2/-1;margin:4px 0 0;padding:6px 10px;border-left:2px solid var(--ac);list-style:none;font-size:12.5px}
.empty{padding:40px 10px;text-align:center;color:var(--mu);list-style:none}#pg{display:flex;gap:8px;align-items:center;justify-content:center;padding:14px 0}
.vh{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}.vh h2{margin:6px 0}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.pill{border:1px solid var(--ln);border-radius:10px;padding:1px 9px;font-size:11.5px}.st-active{color:var(--ok)}.st-partial{color:var(--wa)}.st-error,.st-inactive{color:var(--bad)}.st-manual-only,.st-disabled,.st-unverified{color:var(--mu)}
.panel{background:var(--p);border:1px solid var(--ln);border-radius:8px;padding:12px 14px;margin:10px 0}.panel h3{margin:0 0 10px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.tw{overflow:auto;border:1px solid var(--ln);border-radius:8px}
.tbl{border-collapse:collapse;width:100%;font-size:12px}.tbl th,.tbl td{padding:6px 8px;border-bottom:1px solid var(--ln);text-align:left;vertical-align:top}.tbl th{position:sticky;top:0;background:var(--p);font-size:10.5px;text-transform:uppercase;color:var(--mu)}
.tbl td.err{max-width:300px;color:var(--mu);word-break:break-word}
.g2{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px 18px}.fld{display:flex;flex-direction:column;gap:3px;font-size:12px}.fld>span{font-weight:600}.fld small{color:var(--mu)}
textarea{width:100%;min-height:340px;font-family:Consolas,monospace;font-size:12px}
dialog{background:var(--p);color:var(--tx);border:1px solid var(--ln);border-radius:10px;width:min(680px,94vw)}dialog::backdrop{background:rgba(0,0,0,.55)}
#toast{position:fixed;bottom:16px;right:16px;display:flex;flex-direction:column;gap:6px;z-index:50}.t{background:var(--p);border:1px solid var(--ln);border-left:3px solid var(--ok);padding:8px 12px;border-radius:6px;max-width:400px;box-shadow:0 6px 20px rgba(0,0,0,.3)}.t.bad{border-left-color:var(--bad)}
.snap{background:var(--ac);color:var(--onac);padding:4px 14px;font-size:12px;font-weight:600}
@media(max-width:1100px){.stats{grid-template-columns:repeat(4,1fr)}}
@media(max-width:860px){.lay{grid-template-columns:1fr}.side{position:static;max-height:none;display:flex;overflow-x:auto;border-right:0;border-bottom:1px solid var(--ln)}.nh{display:none}.nav{white-space:nowrap;border-left:0;border-bottom:2px solid transparent;width:auto}.nav.act{border-bottom-color:var(--ac)}.stats{grid-template-columns:repeat(2,1fr)}.art{grid-template-columns:40px 1fr}.acts{grid-column:2}.clk small{display:none}.brand b{font-size:18px;white-space:normal}}

/* ===== Binjal Halwai NewsPortal - theme layer ===== */
body{background:radial-gradient(1200px 420px at 85% -120px,color-mix(in srgb,var(--ac) 16%,transparent),transparent 60%),var(--bg)}
.top{position:sticky;background:linear-gradient(100deg,var(--hd1) 0%,var(--hd2) 100%);color:var(--hdtx);border-bottom:0;padding:10px 20px;box-shadow:0 4px 18px color-mix(in srgb,var(--gB) 18%,transparent)}
.top::after{content:"";position:absolute;left:0;right:0;bottom:-3px;height:3px;background:linear-gradient(90deg,var(--ac2),var(--gA) 50%,var(--gB))}
.brand .logo{background:linear-gradient(135deg,var(--gA),var(--gB));color:var(--onac);border-radius:8px;box-shadow:0 0 0 2px rgba(255,255,255,.55),0 4px 14px color-mix(in srgb,var(--gB) 35%,transparent)}
.brand b{color:var(--hdtx)}.brand b span{color:var(--ac2)}
.brand small{color:var(--hdmu)}
.top .search input{background:var(--hdbg);border:1px solid var(--hdln);color:var(--hdtx)}
.top .search input::placeholder{color:var(--hdmu)}.top .search input:focus{outline:0;border-color:var(--ac);box-shadow:0 0 0 3px color-mix(in srgb,var(--ac) 28%,transparent)}
.top .clk{color:var(--hdtx)}.top .clk small{color:var(--hdmu)}
.top .ic{border-color:var(--hdln);color:var(--hdtx);background:var(--hdbg)}.top .ic:hover{border-color:var(--ac);background:color-mix(in srgb,var(--ac) 14%,transparent)}
.btn{background:linear-gradient(135deg,var(--gA),var(--gB));color:var(--onac);box-shadow:0 2px 8px color-mix(in srgb,var(--gB) 30%,transparent);transition:transform .12s}.btn:hover{transform:translateY(-1px);background:var(--ach)}
.btn.gh{background:transparent;box-shadow:none;color:var(--tx)}
.stats{gap:0;background:transparent;border-bottom:0;padding:12px 16px 4px;grid-template-columns:repeat(7,minmax(120px,1fr));column-gap:10px}
.stat{background:var(--p);border:1px solid var(--ln);border-top:3px solid var(--ac2);border-radius:8px;padding:10px 14px;transition:transform .12s,box-shadow .12s}
.stat:hover{transform:translateY(-2px);box-shadow:0 8px 20px color-mix(in srgb,var(--gB) 18%,transparent)}
.stat:nth-child(2){border-top-color:var(--s2)}.stat:nth-child(3){border-top-color:var(--s3)}.stat:nth-child(4){border-top-color:var(--s4)}.stat:nth-child(5){border-top-color:var(--s5)}.stat:nth-child(6){border-top-color:var(--s6)}.stat:nth-child(7){border-top-color:var(--s7)}
.sv{font-size:24px}
.side{background:var(--p);border-right:1px solid var(--ln)}
.nh{color:var(--ac);font-weight:700;letter-spacing:.12em}
.nav{border-radius:0 6px 6px 0;margin:1px 0;transition:background .12s}.nav:hover{background:var(--ch)}
.nav.act{background:linear-gradient(90deg,color-mix(in srgb,var(--ac) 22%,transparent),transparent);font-weight:700}
.cnt{background:var(--ch);border-radius:9px;padding:0 7px}
.seg button.on{background:linear-gradient(135deg,var(--gA),var(--gB));color:var(--onac);font-weight:700}
.art{border:1px solid transparent;border-bottom:1px solid var(--ln);border-left:3px solid transparent;border-radius:6px;padding:11px 8px;transition:all .12s}
.art:hover{background:var(--p);border-left-color:var(--ac);box-shadow:0 4px 16px rgba(0,0,0,.18)}
.num{background:var(--ch);border-radius:6px;text-align:center;padding:2px 0;height:fit-content;font-size:12px}
.hl{font-size:15.5px;font-weight:700;letter-spacing:.005em}
.chip{border-radius:12px}.chip:hover{border-color:var(--ac);color:var(--ac)}
.read{background:var(--ch)}.read:hover{background:var(--ac);color:var(--onac);border-color:var(--ac);text-decoration:none}
.r-high{color:#fff;background:var(--hi);border-radius:3px;padding:1px 6px}.r-medium{color:#fff;background:#0284c7;border-radius:3px;padding:1px 6px}
.panel,.tw{border-radius:10px;box-shadow:0 2px 10px color-mix(in srgb,var(--gB) 10%,transparent)}
.feed.cards .art{border-radius:10px;border:1px solid var(--ln);border-top:3px solid var(--ac);box-shadow:0 2px 10px rgba(0,0,0,.14)}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:var(--ln);border-radius:6px}::-webkit-scrollbar-thumb:hover{background:var(--ac)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.disc{margin:24px 0 0;padding:16px 20px;border-top:3px solid var(--ac);background:var(--p);color:var(--mu);font-size:12px;text-align:center;line-height:1.6}
@media(max-width:860px){.top{padding:8px 12px}}
</style></head><body>
<a href="#maincontent" class="sr-only" style="position:absolute;top:4px;left:4px;z-index:100;background:var(--ac);color:var(--onac);padding:6px 12px;border-radius:4px;text-decoration:none">Skip to main content</a>
<div id="snap" class="snap" hidden></div>
<header class="top" role="banner">
 <div class="brand"><span class="logo mono" aria-hidden="true">BH</span><div><b>BINJAL HALWAI <span>NEWSPORTAL</span></b><small>NEPSE News Hub — Nepal Stock Market News, All in One Place</small></div></div>
 <div class="search" role="search"><input id="q" type="search" placeholder="Search headlines, companies, keywords…  ( / )  e.g. NABIL, dividend, हकप्रद" aria-label="Search news headlines, companies and keywords"></div>
 <div class="hr">
  <div class="clk"><span id="clock" class="mono" aria-live="off"></span><small>Nepal Standard Time (UTC+05:45)</small></div>
  <div class="clk"><span id="lastUpd">—</span><small>Last successful collection</small></div>
  <button id="bRef" class="btn" aria-label="Refresh news now">⟳ Refresh</button>
  <button id="bHealth" class="ic" title="Source health" aria-label="Source health status"><span id="hDot" class="dot" aria-hidden="true"></span><span id="hTxt"></span></button>
  <button id="bTheme" class="ic" title="Toggle light/dark theme" aria-label="Toggle light and dark theme">◐</button>
  <button id="bSet" class="ic" title="Settings" aria-label="Open settings">⚙</button>
 </div>
</header>
<section class="stats" id="stats" aria-label="News statistics"></section>
<div class="lay"><nav class="side" id="side" aria-label="News categories and navigation"></nav>
<main id="maincontent" aria-label="News feed">
 <div id="vFeed">
  <div class="flt">
   <label>Date<select id="fPreset" aria-label="Filter by date range"><option value="today">Today</option><option value="yesterday">Yesterday</option><option value="24h">Last 24h</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option><option value="all">All time</option><option value="custom">Custom range…</option></select></label>
   <span id="cDates" hidden><input type="date" id="fFrom" aria-label="From date"> – <input type="date" id="fTo" aria-label="To date"></span>
   <label>Source<select id="fSource" aria-label="Filter by source"></select></label>
   <label>Language<select id="fLang" aria-label="Filter by language"><option value="">All</option><option value="en">English</option><option value="ne">नेपाली</option></select></label>
   <label>Mode<select id="fMode" aria-label="Filter mode"><option value="">Default</option><option value="strict">Strict NEPSE-only</option><option value="financial">NEPSE + financial</option><option value="broad">Broad business</option></select></label>
   <label>Relevance<select id="fRel" aria-label="Filter by relevance"><option value="">By mode</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option><option value="all">All (incl. low)</option><option value="excluded">Excluded (audit)</option></select></label>
   <label>Sort<select id="fSort" aria-label="Sort order"><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></label>
   <label>Per page<select id="fPer" aria-label="Articles per page"><option>25</option><option>50</option><option>100</option><option>200</option></select></label>
   <div class="seg" role="group" aria-label="Article view style"><button id="vC" aria-pressed="true">List</button><button id="vK" aria-pressed="false">Cards</button></div>
   <button class="btn gh" id="bClear">Clear filters</button>
   <details class="menu" id="xm"><summary class="btn gh">Export ▾</summary><div>
    <button data-x="csv">Filtered articles → CSV</button><button data-x="xlsx">Filtered articles → Excel (.xlsx)</button>
    <button data-x="daily">Daily digest (Markdown)</button><button data-x="weekly">Weekly NEPSE digest (Markdown)</button>
    <button data-x="snapshot">Standalone HTML snapshot</button><button data-x="sources">Source registry (CSV)</button><button data-x="keywords">Keyword configuration (JSON)</button>
   </div></details>
  </div>
  <div id="fm"></div><ol id="feed" class="feed"></ol><div id="pg"></div>
 </div>
 <div id="vSrc" hidden></div><div id="vSet" hidden></div>
</main></div>
<dialog id="dlg"><form method="dialog" id="dlgF"></form></dialog>
<div id="toast" aria-live="polite"></div>
<footer id="disc" class="disc" hidden role="contentinfo"></footer>
<script>/*__SNAPSHOT__*/</script>
<script>
(function(){'use strict';
const SNAP=window.__SNAPSHOT__||null,$=(s,r)=>(r||document).querySelector(s),TZ='Asia/Kathmandu';
const fDT=new Intl.DateTimeFormat('en-GB',{timeZone:TZ,day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
const fCk=new Intl.DateTimeFormat('en-GB',{timeZone:TZ,weekday:'short',day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const pref=(k,d)=>{try{const v=localStorage.getItem('nnh.'+k);return v===null?d:v}catch(e){return d}};
const setPref=(k,v)=>{try{localStorage.setItem('nnh.'+k,v)}catch(e){}};
const S={q:'',category:'',source:'',lang:'',relevance:'',mode:'',preset:SNAP?'all':pref('preset','7d'),from:'',to:'',sort:'newest',page:1,per:parseInt(pref('per','50'),10)||50,view:pref('view','compact'),saved:false,tab:'feed',meta:null,sources:[],facets:{},ft:0};
function h(tag,a,...kids){const el=document.createElement(tag);if(a)for(const[k,v]of Object.entries(a)){if(v===null||v===undefined||v===false)continue;if(k==='class')el.className=v;else if(k.startsWith('on'))el.addEventListener(k.slice(2),v);else el.setAttribute(k,v===true?'':v)}
 for(const c of kids.flat()){if(c===null||c===undefined||c===false)continue;el.append(c instanceof Node?c:document.createTextNode(String(c)))}return el}
const safe=u=>/^https?:\/\//i.test(u||'')?u:'#',fmt=s=>{if(!s)return'—';const d=new Date(s);return isNaN(d)?'—':fDT.format(d)+' NPT'};
const ago=s=>{if(!s)return'—';const m=Math.round((Date.now()-new Date(s).getTime())/6e4);if(m<1)return'just now';if(m<60)return m+'m ago';const hh=Math.round(m/60);return hh<48?hh+'h ago':Math.round(hh/24)+'d ago'};
const cap=s=>s?s[0].toUpperCase()+s.slice(1):'',fN=n=>(n||0).toLocaleString('en-IN'),sleep=ms=>new Promise(r=>setTimeout(r,ms));
const deb=(f,ms)=>{let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>f(...a),ms)}};
const fld=(l,el,hint)=>h('label',{class:'fld'},h('span',{},l),el,hint?h('small',{},hint):null);
function toast(m,bad){const t=h('div',{class:'t'+(bad?' bad':'')},m);$('#toast').append(t);setTimeout(()=>t.remove(),bad?9000:5000)}
async function copy(txt,m){try{await navigator.clipboard.writeText(txt)}catch(e){const t=h('textarea',{},txt);document.body.append(t);t.select();try{document.execCommand('copy')}catch(e2){}t.remove()}toast(m||'Copied')}
async function api(path,o){o=o||{};const hd={'Accept':'application/json'};if(o.body)hd['Content-Type']='application/json';const tk=pref('token','');if(tk)hd['X-Admin-Token']=tk;
 const r=await fetch(path,{method:o.method||'GET',headers:hd,body:o.body?JSON.stringify(o.body):undefined});
 if(r.status===401&&!o._r){const t=prompt('Admin token required (NNH_ADMIN_TOKEN):');if(t){setPref('token',t);return api(path,Object.assign({},o,{_r:1}))}}
 if(!r.ok){let m=String(r.status);try{const j=await r.json();m=typeof j.detail==='string'?j.detail:JSON.stringify(j.detail||j)}catch(e){}throw new Error(m)}
 return(r.headers.get('content-type')||'').includes('json')?r.json():r.text()}
function qs(x){const p=new URLSearchParams(),m={q:S.q,category:S.category,source:S.source,lang:S.lang,relevance:S.relevance,mode:S.mode,preset:S.preset,sort:S.sort};
 if(S.preset==='custom'){m.date_from=S.from;m.date_to=S.to}if(S.saved)m.saved='1';Object.assign(m,x||{});for(const[k,v]of Object.entries(m))if(v!==''&&v!=null)p.set(k,v);return p.toString()}
const dMode=()=>(S.meta&&S.meta.settings&&S.meta.settings.mode)||'financial',mLabel=m=>(S.meta&&S.meta.modes&&S.meta.modes[m])||m;
function local(all){const q=S.q.trim().toLowerCase();let a=SNAP.articles.filter(x=>(!q||(x.title+' '+x.source_name+' '+x.keywords.join(' ')+' '+x.excerpt).toLowerCase().includes(q))&&(!S.source||x.source_id===S.source)&&(!S.lang||x.language===S.lang)&&(!S.relevance||S.relevance==='all'||x.relevance===S.relevance));
 const fc={};a.forEach(x=>x.categories.forEach(c=>fc[c.id]=(fc[c.id]||0)+1));const ft=a.length;if(S.category)a=a.filter(x=>x.categories.some(c=>c.id===S.category));
 a.sort((x,y)=>(S.sort==='oldest'?1:-1)*(x.sort_at<y.sort_at?-1:x.sort_at>y.sort_at?1:0));if(all)return a;
 const t=a.length,pages=Math.max(1,Math.ceil(t/S.per)),pg=Math.min(S.page,pages);
 return{total:t,page:pg,per_page:S.per,pages,facets:fc,facet_total:ft,items:a.slice((pg-1)*S.per,pg*S.per).map((x,i)=>Object.assign({},x,{number:(pg-1)*S.per+i+1}))}}
async function loadArticles(){let d;try{d=SNAP?local():await api('/api/articles?'+qs({page:S.page,per_page:S.per}))}catch(e){$('#feed').replaceChildren(h('li',{class:'empty'},'Failed to load articles: '+e.message));return}
 S.facets=d.facets||{};S.ft=d.facet_total||0;S.page=d.page;renderSide();renderFeed(d);renderPager(d)}
function renderFeed(d){const f=$('#feed');f.className='feed '+S.view;const a=d.total?(d.page-1)*d.per_page+1:0,b=Math.min(d.total,d.page*d.per_page);
 $('#fm').textContent=d.total?`Showing ${a}–${b} of ${fN(d.total)} · ${S.saved?'saved articles':mLabel(S.mode||dMode())} · ${S.sort==='newest'?'newest first':'oldest first'} · times in NPT`:'';
 if(!d.items.length){f.replaceChildren(h('li',{class:'empty'},S.ft||S.q||S.category?'No articles match the current filters.':'No articles yet. Click ⟳ Refresh to run the first collection — every source is verified live on its first run.'));return}
 f.replaceChildren(...d.items.map(card))}
function card(a){const u=safe(a.url);
 const when=a.published_at?h('span',{title:a.date_quality==='adjusted'?'Feed timestamp was labelled UTC but was NPT; corrected':'Publication time from the source'},fmt(a.published_at)+(a.date_quality==='adjusted'?' *':''))
  :h('span',{class:'warn',title:'The source did not provide a usable publication time'},'Retrieved '+fmt(a.retrieved_at)+' · publication time n/a');
 const meta=h('div',{class:'meta'},h('button',{class:'src',title:'Filter by this source',onclick:()=>{S.source=a.source_id;S.page=1;sync();loadArticles()}},a.source_name),a.official?h('span',{class:'bdg off'},'OFFICIAL'):null,when,
  ...a.categories.map(c=>h('button',{class:'chip',onclick:()=>go({category:c.id,saved:false})},c.label)),h('span',{class:'rel r-'+a.relevance,title:'score '+a.score},cap(a.relevance)),h('span',{class:'lg'},(a.language||'?').toUpperCase()),
  a.related_count?h('button',{class:'bdg rn',onclick:e=>related(a,e.currentTarget.closest('li'))},'+'+a.related_count+' related'):null);
 const body=h('div',{},h('a',{class:'hl',href:u,target:'_blank',rel:'noopener'},a.title),meta);
 if(S.view==='cards'){if(a.summary_ai)body.append(h('p',{class:'sum'},h('span',{class:'lbl ai'},'AI summary'),' ',a.summary_ai));else if(a.excerpt)body.append(h('p',{class:'sum'},h('span',{class:'lbl'},'Publisher excerpt'),' ',a.excerpt));
  if(a.keywords.length)body.append(h('div',{class:'kws'},a.keywords.map(k=>h('span',{class:'kw'},k))))}
 const acts=h('div',{class:'acts'},SNAP?null:h('button',{class:'ic'+(a.saved?' on':''),title:a.saved?'Remove bookmark':'Save',onclick:e=>save(a,e.currentTarget)},a.saved?'★':'☆'),
  h('button',{class:'ic',title:'Copy URL',onclick:()=>copy(a.url,'URL copied')},'⧉'),
  h('button',{class:'ic',title:'Copy headline, source and link',onclick:()=>copy(a.title+' — '+a.source_name+' ('+(a.published_at?fmt(a.published_at):'date n/a')+')\n'+a.url,'Headline copied')},'❝'),
  h('a',{class:'read',href:u,target:'_blank',rel:'noopener'},'Read original →'));
 return h('li',{class:'art'},h('div',{class:'num'},'['+a.number+']'),body,acts)}
async function related(a,li){const ex=li.querySelector('.relz');if(ex){ex.remove();return}let it;
 if(SNAP)it=SNAP.articles.filter(x=>x.group_id===a.group_id&&x.id!==a.id);else try{it=await api('/api/articles/'+a.id+'/related')}catch(e){toast(e.message,true);return}
 li.append(h('ul',{class:'relz'},it.length?it.map(x=>h('li',{},h('a',{href:safe(x.url),target:'_blank',rel:'noopener'},x.title),' — ',h('span',{class:'cnt'},x.source_name+' · '+fmt(x.published_at||x.retrieved_at)))):h('li',{},'No related coverage.')))}
async function save(a,b){try{const r=await api('/api/articles/'+a.id+'/save',{method:'POST',body:{saved:!a.saved}});a.saved=r.saved;b.textContent=a.saved?'★':'☆';b.classList.toggle('on',a.saved);toast(a.saved?'Saved':'Removed from saved');if(S.saved)loadArticles()}catch(e){toast('Could not update: '+e.message,true)}}
function renderPager(d){const p=$('#pg');if(d.pages<=1){p.replaceChildren();return}
 const b=(l,n,dis)=>h('button',{class:'btn gh',disabled:dis||null,onclick:()=>{S.page=n;loadArticles();scrollTo({top:0,behavior:'smooth'})}},l);
 p.replaceChildren(b('« First',1,d.page===1),b('‹ Prev',d.page-1,d.page===1),h('span',{class:'cnt'},'Page '+d.page+' of '+d.pages),b('Next ›',d.page+1,d.page===d.pages),b('Last »',d.pages,d.page===d.pages))}
function renderSide(){const it=(l,c,act,fn,z)=>h('button',{class:'nav'+(act?' act':'')+(z?' zero':''),onclick:fn},h('span',{},l),c!=null?h('span',{class:'cnt'},fN(c)):null),F=S.tab==='feed';
 $('#side').replaceChildren(h('div',{class:'nh'},'News'),it('All News',S.ft,F&&!S.category&&!S.saved&&S.preset!=='24h',()=>go({category:'',saved:false})),
  it('Latest (24h)',null,F&&S.preset==='24h'&&!S.category&&!S.saved,()=>go({category:'',saved:false,preset:'24h'})),h('div',{class:'nh'},'Categories'),
  ...((S.meta&&S.meta.categories)||[]).map(c=>it(c.label,S.facets[c.id]||0,F&&S.category===c.id,()=>go({category:c.id,saved:false}),!S.facets[c.id])),
  h('div',{class:'nh'},'Library'),SNAP?null:it('★ Saved Articles',null,F&&S.saved,()=>go({saved:true,category:''})),
  SNAP?null:it('Source Directory',null,S.tab==='src',()=>tab('src')),SNAP?null:it('Settings',null,S.tab==='set',()=>tab('set')))}
function go(p){Object.assign(S,p,{page:1});sync();tab('feed');loadArticles()}
function tab(t){S.tab=t;$('#vFeed').hidden=t!=='feed';$('#vSrc').hidden=t!=='src';$('#vSet').hidden=t!=='set';if(t==='src')loadSources();if(t==='set')loadSettings();renderSide()}
function sync(){$('#fPreset').value=S.preset;$('#cDates').hidden=S.preset!=='custom';$('#fFrom').value=S.from;$('#fTo').value=S.to;$('#fSource').value=S.source;$('#fLang').value=S.lang;$('#fMode').value=S.mode;$('#fRel').value=S.relevance;$('#fSort').value=S.sort;$('#fPer').value=String(S.per);
 $('#vC').classList.toggle('on',S.view==='compact');$('#vK').classList.toggle('on',S.view==='cards');$('#fMode').options[0].textContent='Default ('+mLabel(dMode())+')'}
function fillSrc(){const s=$('#fSource');s.replaceChildren(h('option',{value:''},'All sources'),...S.sources.slice().sort((a,b)=>a.name.localeCompare(b.name)).map(x=>h('option',{value:x.id},x.name)));s.value=S.source}
async function loadStats(){let s;if(SNAP)s=SNAP.stats;else try{s=await api('/api/stats')}catch(e){return}
 const c=(l,v,sub,cl)=>h('div',{class:'stat'+(cl?' '+cl:'')},h('div',{class:'sl'},l),h('div',{class:'sv'},v),sub?h('div',{class:'ss'},sub):null);
 $('#stats').replaceChildren(c('Relevant articles',fN(s.total),SNAP?'in snapshot':'all time · '+s.mode_label),c('Today',fN(s.today),'since 00:00 NPT'),c('Last 7 days',fN(s.week)),
  c('Active sources',s.sources_active+'/'+s.sources_enabled,s.sources_manual+' manual-only'),c('Source errors',fN(s.sources_error),s.sources_unverified?s.sources_unverified+' not yet verified':'',s.sources_error?'bad':''),
  c('Last successful collection',s.last_success?ago(s.last_success):'never',s.next_run?'next: '+fmt(s.next_run):(s.last_success?fmt(s.last_success):'click Refresh')),
  h('a',{class:'stat',href:'https://www.nepalstock.com/',target:'_blank',rel:'noopener'},h('div',{class:'sl'},'NEPSE index'),h('div',{class:'sv sm'},'nepalstock.com →'),h('div',{class:'ss'},'Not integrated: no documented public API')));
 $('#hDot').className='dot '+(!s.sources_active?'bad':s.sources_error?'wa':'ok');$('#hTxt').textContent=s.sources_active+' live';$('#lastUpd').textContent=s.last_success?fmt(s.last_success):'—'}
async function refresh(rd){const b=$('#bRef');b.disabled=true;try{await api('/api/refresh',{method:'POST',body:{rediscover:!!rd}})}catch(e){if(!/running/i.test(e.message)){toast('Refresh failed: '+e.message,true);b.disabled=false;return}}await poll()}
async function poll(){const b=$('#bRef');b.disabled=true;for(let i=0;i<450;i++){await sleep(2000);let st;try{st=await api('/api/refresh/status')}catch(e){continue}
  if(st.running){b.textContent='⟳ '+st.done+'/'+st.total;continue}const r=st.last_result||{};toast('Collection finished: '+(r.new||0)+' new, '+(r.relevant||0)+' relevant, '+(r.errors||0)+' source errors');break}
 b.disabled=false;b.textContent='⟳ Refresh';loadStats();loadArticles();if(S.tab==='src')loadSources()}
const stCls=s=>s==='active'?'ok':s==='partial'?'wa':(s==='error'||s==='inactive')?'bad':'';
async function loadSources(){const v=$('#vSrc');let l;try{l=await api('/api/sources')}catch(e){v.replaceChildren(h('p',{class:'empty'},'Failed: '+e.message));return}S.sources=l;fillSrc();
 const cnt={};l.forEach(s=>cnt[s.health.status]=(cnt[s.health.status]||0)+1);const man=l.filter(s=>s.health.status!=='active'&&s.health.status!=='partial');
 const row=s=>{const x=s.health,st=x.status;return h('tr',{},h('td',{},h('span',{class:'dot '+stCls(st)})),h('td',{},h('a',{href:safe(s.url),target:'_blank',rel:'noopener'},s.name),h('div',{class:'cnt'},s.id)),
  h('td',{},s.type.replace('_',' ')),h('td',{},s.language),h('td',{},s.method),h('td',{class:'mono'},x.resolved_feeds.length?x.resolved_feeds.map(u=>h('div',{},h('a',{href:safe(u),target:'_blank',rel:'noopener'},u.replace(/^https?:\/\//,'')))):'—'),
  h('td',{},ago(x.last_checked)),h('td',{},ago(x.last_success)),h('td',{class:'mono'},x.articles_total||0),h('td',{class:'mono'},x.relevant_total||0),
  h('td',{class:'err'},h('b',{class:'st-'+st},st),x.last_error?h('div',{},x.last_error):null,s.notes?h('div',{},h('i',{},s.notes)):null,h('div',{},h('small',{},'Evidence: '+s.evidence))),
  h('td',{},h('div',{class:'row'},h('button',{class:'ic',title:'Retry / re-verify now',onclick:()=>act('POST','/api/sources/'+encodeURIComponent(s.id)+'/retry',null,'Re-checking '+s.name+'…',true)},'⟳'),
   h('button',{class:'ic',title:s.enabled?'Disable':'Enable',onclick:()=>act('PUT','/api/sources/'+encodeURIComponent(s.id),{enabled:!s.enabled},s.enabled?'Disabled':'Enabled')},s.enabled?'⏸':'▶'),
   h('a',{class:'ic',href:safe(s.section_url||s.url),target:'_blank',rel:'noopener',title:'Open official news section'},'↗'),h('button',{class:'ic',title:'Edit',onclick:()=>srcDlg(s)},'✎'),
   h('button',{class:'ic',title:'Remove',onclick:()=>{if(confirm('Remove "'+s.name+'"? Collected articles are kept.'))act('DELETE','/api/sources/'+encodeURIComponent(s.id),null,'Removed')}},'✕'))))};
 v.replaceChildren(h('div',{class:'vh'},h('h2',{},'Source Directory ('+l.length+')'),h('div',{class:'row'},h('button',{class:'btn',onclick:()=>srcDlg(null)},'+ Add source'),h('button',{class:'btn gh',onclick:()=>refresh(true)},'Re-verify all'),
   h('a',{class:'btn gh',href:'/api/export/sources.csv'},'Export CSV'),h('a',{class:'btn gh',href:'/api/export/sources.json'},'Export JSON'))),
  h('div',{class:'row',style:'margin:8px 0'},Object.entries(cnt).map(([k,n])=>h('span',{class:'pill st-'+k},k+': '+n))),
  h('div',{class:'panel'},h('b',{},'Manual shortcuts '),h('small',{class:'cnt'},'— not (yet) collected automatically; open the official news pages directly'),h('div',{class:'chips'},man.map(s=>h('a',{class:'chip',href:safe(s.section_url||s.url),target:'_blank',rel:'noopener',title:s.health.status},s.name)))),
  h('div',{class:'tw'},h('table',{class:'tbl'},h('thead',{},h('tr',{},['','Source','Type','Lang','Method','Feed','Checked','Last success','Stored','Relevant','Status / error',''].map(t=>h('th',{},t)))),h('tbody',{},l.map(row)))))}
async function act(m,u,body,msg,wait){try{await api(u,{method:m,body:body||(m==='POST'?{}:undefined)});toast(msg);if(wait)await poll();else{loadSources();loadStats()}}catch(e){if(wait&&/running/i.test(e.message)){await poll();return}toast(e.message,true)}}
function srcDlg(s){const nw=!s;s=s||{name:'',url:'',section_url:'',type:'business_news',language:'both',method:'auto',feeds:[],notes:'',permitted:false,listing:{},min_interval_min:15};
 const i=(id,v,t)=>h('input',{id,type:t||'text',value:v==null?'':v}),sl=(id,v,o)=>{const e=h('select',{id},o.map(x=>h('option',{value:x},x)));e.value=v;return e},pm=h('input',{type:'checkbox',id:'dP'});pm.checked=!!s.permitted;
 $('#dlgF').replaceChildren(h('h3',{},nw?'Add news source':'Edit source — '+s.name),h('div',{class:'g2'},fld('Name',i('dN',s.name)),fld('Website URL',i('dU',s.url,'url')),fld('News section URL',i('dS',s.section_url,'url')),
   fld('Type',sl('dT',s.type,['market_portal','business_news','general_news','regulatory'])),fld('Language',sl('dL',s.language,['en','ne','both'])),
   fld('Collection method',sl('dM',s.method,['auto','rss','listing','manual']),'auto: configured feed → <link rel=alternate> → /feed, /rss'),fld('Min. interval (minutes)',i('dI',s.min_interval_min,'number'))),
  fld('Feed URLs (one per line)',h('textarea',{id:'dF',style:'min-height:64px'},(s.feeds||[]).join('\n'))),
  h('details',{},h('summary',{},'Listing adapter — only for sites whose terms permit automated access'),h('div',{class:'g2'},fld('Terms reviewed; collection permitted',pm),fld('Link CSS selector',i('dLs',(s.listing||{}).link_selector)),fld('Date CSS selector',i('dDs',(s.listing||{}).date_selector)))),
  fld('Notes',i('dNo',s.notes)),h('div',{class:'row',style:'justify-content:flex-end;margin-top:12px'},h('button',{class:'btn gh',value:'cancel'},'Cancel'),
   h('button',{class:'btn',type:'button',onclick:async()=>{const b={name:$('#dN').value.trim(),url:$('#dU').value.trim(),section_url:$('#dS').value.trim(),type:$('#dT').value,language:$('#dL').value,method:$('#dM').value,
    min_interval_min:+$('#dI').value||15,feeds:$('#dF').value.split(/\s+/).filter(Boolean),notes:$('#dNo').value,permitted:pm.checked,listing:{link_selector:$('#dLs').value.trim(),date_selector:$('#dDs').value.trim()}};
    try{await api(nw?'/api/sources':'/api/sources/'+encodeURIComponent(s.id),{method:nw?'POST':'PUT',body:b});$('#dlg').close();toast('Source saved — use ⟳ to verify it now');loadSources()}catch(e){toast('Save failed: '+e.message,true)}}},nw?'Add source':'Save')));
 $('#dlg').showModal()}
async function loadSettings(){const v=$('#vSet');let st,kw;try{[st,kw]=await Promise.all([api('/api/settings'),api('/api/keywords')])}catch(e){v.replaceChildren(h('p',{class:'empty'},'Failed: '+e.message));return}
 const n=(id,val,mi,ma,step)=>h('input',{type:'number',id,value:val,min:mi,max:ma,step:step||1}),sl=(id,val,o)=>{const e=h('select',{id},o.map(x=>h('option',{value:x[0]},x[1])));e.value=String(val);return e},
  ck=(id,val,dis)=>{const e=h('input',{type:'checkbox',id,disabled:dis||null});e.checked=!!val;return e};
 const iv=[['0','Manual only'],['15','Every 15 minutes'],['30','Every 30 minutes'],['60','Every hour'],['180','Every 3 hours']];if(!iv.some(x=>+x[0]===st.interval_min))iv.push([String(st.interval_min),'Every '+st.interval_min+' min']);
 const langs=st.languages||[];
 const coll=h('section',{class:'panel'},h('h3',{},'Collection & filtering'),h('div',{class:'g2'},
  fld('Collection interval',sl('sI',st.interval_min,iv),'Per-source minimum intervals and conditional requests still apply.'),fld('Default filter mode',sl('sM',st.mode,[['strict','Strict NEPSE-only'],['financial','NEPSE + financial market'],['broad','Broad business & economy']])),
  fld('High threshold',n('tH',st.thresholds.high,1,200,.5),'and ≥1 high-tier keyword group'),fld('Medium threshold',n('tM',st.thresholds.medium,.5,200,.5)),fld('Low threshold',n('tL',st.thresholds.low,.5,200,.5)),
  fld('Languages in default feed',h('span',{},ck('sLen',langs.includes('en')),' English  ',ck('sLne',langs.includes('ne')),' Nepali')),fld('Group related coverage',ck('sG',st.group_duplicates)),
  fld('AI summaries (labelled)',ck('sA',st.ai_summaries,!st.ai_available),st.ai_available?'Uses the server-side API key; fetches article pages that robots.txt permits.':'Unavailable: ANTHROPIC_API_KEY not set on the server.'),
  fld('Max AI summaries per run',n('sAm',st.ai_max_per_run,0,200)),fld('Retention (days)',n('sR',st.retention_days,7,3650)),fld('Excluded-article retention (days)',n('sX',st.excluded_retention_days,1,90),'Kept briefly for filter auditing & reclassification.')),
  h('div',{class:'row',style:'margin-top:10px'},h('button',{class:'btn',onclick:async()=>{const b={interval_min:+$('#sI').value,mode:$('#sM').value,thresholds:{high:+$('#tH').value,medium:+$('#tM').value,low:+$('#tL').value},
   languages:[$('#sLen').checked?'en':'',$('#sLne').checked?'ne':''].filter(Boolean),group_duplicates:$('#sG').checked,ai_summaries:$('#sA').checked,ai_max_per_run:+$('#sAm').value,retention_days:+$('#sR').value,excluded_retention_days:+$('#sX').value};
   try{await api('/api/settings',{method:'PUT',body:b});S.meta=await api('/api/meta');toast('Settings saved');sync();loadStats();loadArticles()}catch(e){toast('Rejected: '+e.message,true)}}},'Save settings'),
   h('span',{class:'cnt'},st.next_run?'Next scheduled run: '+fmt(st.next_run):'Scheduler: manual / not running')));
 const ta=h('textarea',{id:'kJ',spellcheck:'false'},JSON.stringify(kw,null,2));
 const saveKw=async()=>{let k;try{k=JSON.parse($('#kJ').value)}catch(e){toast('Invalid JSON: '+e.message,true);return}try{await api('/api/keywords',{method:'PUT',body:k});toast('Keywords saved. New articles use them now; Reclassify re-scores stored ones.')}catch(e){toast('Rejected: '+e.message,true)}};
 const kwp=h('section',{class:'panel'},h('h3',{},'Keywords & relevance rules'),
  h('p',{class:'cnt'},'Groups carry weight, tier and categories. CAPS acronyms (≤6 chars) match case-sensitively; English matches whole words (plural-tolerant); Nepali is normalised (शेयर≈सेयर, ई≈इ, ँ≈ं) and matches word-prefixes. requires_anchor groups need a Nepal anchor (NEPSE, NRB, नेपाल…) for full weight; negative terms without an anchor exclude the article.'),
  h('div',{class:'row'},h('select',{id:'kG'},(kw.groups||[]).map(g=>h('option',{value:g.id},(g.label||g.id)+' ['+g.tier+', w'+g.weight+']'))),h('select',{id:'kL'},h('option',{value:'en'},'English'),h('option',{value:'ne'},'Nepali')),
   h('input',{id:'kT',type:'text',placeholder:'Term, company or symbol — e.g. NABIL, हकप्रद'}),h('button',{class:'btn gh',onclick:()=>{const t=$('#kT').value.trim();if(!t)return;let k;try{k=JSON.parse($('#kJ').value)}catch(e){toast('JSON invalid',true);return}
    const g=k.groups.find(x=>x.id===$('#kG').value),L=$('#kL').value;g[L]=g[L]||[];if(!g[L].includes(t))g[L].push(t);$('#kJ').value=JSON.stringify(k,null,2);$('#kT').value='';saveKw()}},'Add term')),
  h('div',{style:'margin-top:8px'},ta),h('div',{class:'row',style:'margin-top:8px'},h('button',{class:'btn',onclick:saveKw},'Validate & save'),
   h('button',{class:'btn gh',onclick:async()=>{try{const r=await api('/api/reclassify',{method:'POST',body:{}});toast('Reclassified '+r.updated+' articles');loadStats();loadArticles()}catch(e){toast(e.message,true)}}},'Reclassify stored articles'),
   h('a',{class:'btn gh',href:'/api/export/keywords.json'},'Export JSON'),h('label',{class:'btn gh'},'Import JSON',h('input',{type:'file',accept:'.json,application/json',hidden:true,onchange:e=>{const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=()=>{$('#kJ').value=r.result;toast('Loaded — review, then Validate & save')};r.readAsText(f)}}))));
 const prf=h('section',{class:'panel'},h('h3',{},'Display preferences (this browser)'),h('div',{class:'g2'},fld('Theme',sl('pT',pref('theme_g','light'),[['light','Cream'],['dark','Dark brown']])),fld('Default view',sl('pV',pref('view','compact'),[['compact','Compact list'],['cards','Cards']])),
  fld('Default date range',sl('pP',pref('preset','7d'),[['today','Today'],['24h','Last 24h'],['7d','Last 7 days'],['30d','Last 30 days'],['all','All time']])),fld('Articles per page',sl('pN',pref('per','50'),[['25','25'],['50','50'],['100','100'],['200','200']]))),
  h('div',{class:'row',style:'margin-top:10px'},h('button',{class:'btn gh',onclick:()=>{setPref('theme_g',$('#pT').value);setPref('view',$('#pV').value);setPref('preset',$('#pP').value);setPref('per',$('#pN').value);theme();Object.assign(S,{view:$('#pV').value,preset:$('#pP').value,per:+$('#pN').value});sync();toast('Preferences saved in this browser')}},'Save preferences')));
 v.replaceChildren(h('div',{class:'vh'},h('h2',{},'Settings')),coll,kwp,prf)}
function theme(){document.documentElement.dataset.theme=pref('theme_g','light')}
function xport(k){$('#xm').open=false;if(SNAP){if(k!=='csv'){toast('Only CSV export is available inside a snapshot',true);return}
  const rows=[['S.N.','Headline','Publisher','Category','Published (NPT)','Language','Relevance','Summary','URL']].concat(local(true).map((a,i)=>[i+1,a.title,a.source_name,a.categories.map(c=>c.label).join('; '),a.published_at?fmt(a.published_at):'n/a',a.language,a.relevance,a.summary_ai||a.excerpt,a.url]));
  const csv='\ufeff'+rows.map(r=>r.map(v=>{v=String(v==null?'':v);if(/^[=+\-@]/.test(v))v="'"+v;return'"'+v.replace(/"/g,'""')+'"'}).join(',')).join('\r\n');
  const el=h('a',{href:URL.createObjectURL(new Blob([csv],{type:'text/csv'})),download:'nepse_news_snapshot.csv'});document.body.append(el);el.click();el.remove();return}
 location.href={csv:'/api/export/articles.csv?'+qs(),xlsx:'/api/export/articles.xlsx?'+qs(),daily:'/api/export/digest.md?period=daily',weekly:'/api/export/digest.md?period=weekly',snapshot:'/api/export/snapshot.html?'+qs(),sources:'/api/export/sources.csv',keywords:'/api/export/keywords.json'}[k]}
function bind(){const re=()=>{S.page=1;loadArticles()};
 $('#q').addEventListener('input',deb(e=>{S.q=e.target.value;re()},350));
 $('#fPreset').onchange=e=>{S.preset=e.target.value;$('#cDates').hidden=S.preset!=='custom';if(S.preset!=='custom')re();renderSide()};
 $('#fFrom').onchange=$('#fTo').onchange=()=>{S.from=$('#fFrom').value;S.to=$('#fTo').value;if(S.from||S.to)re()};
 [['#fSource','source'],['#fLang','lang'],['#fMode','mode'],['#fRel','relevance'],['#fSort','sort']].forEach(([id,k])=>{$(id).onchange=e=>{S[k]=e.target.value;re()}});
 $('#fPer').onchange=e=>{S.per=+e.target.value;re()};$('#vC').onclick=()=>{S.view='compact';sync();loadArticles()};$('#vK').onclick=()=>{S.view='cards';sync();loadArticles()};
 $('#bClear').onclick=()=>{Object.assign(S,{q:'',category:'',source:'',lang:'',relevance:'',mode:'',preset:SNAP?'all':pref('preset','7d'),from:'',to:'',sort:'newest',saved:false,page:1});$('#q').value='';sync();loadArticles()};
 $('#bRef').onclick=()=>refresh(false);$('#bTheme').onclick=()=>{setPref('theme_g',document.documentElement.dataset.theme==='dark'?'light':'dark');theme()};
 $('#bSet').onclick=()=>tab('set');$('#bHealth').onclick=()=>tab('src');document.querySelectorAll('#xm button').forEach(b=>b.onclick=()=>xport(b.dataset.x));
 document.addEventListener('keydown',e=>{if(e.key==='/'&&!/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)){e.preventDefault();$('#q').focus()}})}
async function init(){theme();bind();const tick=()=>{$('#clock').textContent=fCk.format(new Date())};tick();setInterval(tick,1000);
 if(SNAP){S.meta=SNAP.meta;S.sources=SNAP.sources;['#bRef','#bSet','#bHealth','#xm'].forEach(s=>$(s).hidden=true);['#fPreset','#fMode'].forEach(s=>$(s).closest('label').hidden=true);
  const n=$('#snap');n.hidden=false;n.textContent='Updated '+fmt(SNAP.generated_at)+' · '+SNAP.articles.length+' headlines · Every headline opens the original publisher.';
  const d=$('#disc');d.hidden=false;d.textContent='© Binjal Halwai NewsPortal · News aggregation only — not investment advice. All headlines and articles belong to their respective publishers; please read the full story at the original source.'+(SNAP.contact?' Removal or correction requests: '+SNAP.contact+'.':'')}
 else{try{S.meta=await api('/api/meta')}catch(e){toast('Backend not reachable: '+e.message,true);S.meta={categories:[],modes:{},settings:{}}}}
 fillSrc();sync();renderSide();await Promise.all([loadStats(),loadArticles()]);
 if(!SNAP){api('/api/sources').then(l=>{S.sources=l;fillSrc()}).catch(()=>{});setInterval(loadStats,60000);setInterval(()=>{if(S.tab==='feed'&&S.page===1&&!document.hidden)loadArticles()},300000);
  api('/api/refresh/status').then(st=>{if(st.running)poll()}).catch(()=>{})}}
init();
})();
</script></body></html>
"""


# ======================================================================================
# v1.1 EXTENSIONS (29 Sep 2026): priority sources + repeated-story ("hot") detection.
# Added as a layer on top of v1.0: with nothing flagged, v1.0 behaviour is unchanged.
# ======================================================================================
PRIORITY_IDS = ("sharesansar", "merolagani", "bizmandu", "arthiknews", "aarthiknews", "onlinekhabar",
                "ratopati", "insurancekhabar", "nepsealpha", "nepalipaisa", "bizpati")
_SOURCE_KEYS = _SOURCE_KEYS + ("priority",)
STORE_REF = None

_NEW_SOURCES = [
    _s("insurancekhabar", "Insurance Khabar", "https://insurancekhabar.com/",
       "https://insurancekhabar.com/category/stockmarket/", "business_news", "ne", "auto",
       ["https://insurancekhabar.com/feed/"],
       notes="Added 29 Sep 2026 (priority). WordPress; stock-market, mutual-fund and insurance desks.",
       evidence="Homepage fetched OK 29 Sep 2026; WordPress 6.9 (feed URL unverified)"),
    _s("bizpati", "Bizpati", "https://bizpati.com/", "https://bizpati.com/?cat=150", "business_news", "ne", "auto",
       ["https://bizpati.com/feed/", "https://bizpati.com/?cat=150&feed=rss2"],
       notes="Added 29 Sep 2026 (priority). WordPress; 'Capital' desk = ?cat=150; daily capital-market morning update.",
       evidence="Homepage fetched OK 29 Sep 2026; WordPress 6.9 (feed URL unverified)"),
]

_NOTE_UPDATES = {
    "merolagani": "[ToS checked 29-Sep-2026] Terms of Use s.3 prohibit scripts, APIs, scraping and robots without prior "
                  "written consent -> MANUAL ONLY. Request written permission / a licensed feed before any automation.",
    "nepalipaisa": "[ToS checked 29-Sep-2026] Terms limit use to personal, non-commercial use and bar scripts that copy "
                   "content; article lists load via JavaScript -> MANUAL ONLY unless licensed.",
    "sharesansar": "[ToS checked 29-Sep-2026] Terms page has no explicit automation clause, but robots.txt was NOT verified "
                   "and content is copyrighted. Listing template is pre-filled but inert: set method=listing AND tick "
                   "'permitted' only after checking robots.txt / obtaining a written OK (sharesansar@gmail.com).",
    "nepsealpha": "[29-Sep-2026] Page is JavaScript-rendered (no server-side headlines) and no public feed was found "
                  "-> manual.",
    "arthiknews": "[29-Sep-2026] Your list says arthiknews.com. In your 29-Sep export the working publisher is "
                  "aarthiknews.com (separate entry); this domain produced no articles. Keep only if the domain is correct.",
}


def _store_migrate(self):
    """Idempotent: add new priority sources, flag priority ids, record verification notes."""
    with self.lock:
        lst = self._read(self.p_sources)
        have = {s.get("id") for s in lst}
        changed = False
        for ns in _NEW_SOURCES:
            if ns["id"] not in have:
                lst.append(copy.deepcopy(ns))
                changed = True
        for s in lst:
            sid = s.get("id")
            if "priority" not in s:
                s["priority"] = 1 if sid in PRIORITY_IDS else 0
                changed = True
            if sid in _NOTE_UPDATES and not (s.get("notes") or "").startswith("["):
                s["notes"] = _NOTE_UPDATES[sid]
                changed = True
            if sid == "sharesansar" and not s.get("listing"):
                s["listing"] = {"url": "https://www.sharesansar.com/category/latest",
                                "link_selector": 'a[href*="/newsdetail/"]'}
                changed = True
        if changed:
            self._write(self.p_sources, lst)


_orig_store_init = Store.__init__


def _store_init(self, data_dir):
    global STORE_REF
    _orig_store_init(self, data_dir)
    self.migrate()
    STORE_REF = self


Store.migrate = _store_migrate
Store.__init__ = _store_init

_prio_cache = {"t": 0.0, "ids": set()}


def priority_ids() -> set:
    if STORE_REF is None:
        return set()
    if time.time() - _prio_cache["t"] > 20:
        try:
            _prio_cache.update(t=time.time(), ids={s["id"] for s in STORE_REF.sources() if s.get("priority")})
        except Exception:
            log.exception("priority refresh failed")
    return _prio_cache["ids"]


_orig_fetch_listing = fetch_listing


def fetch_listing(http, src):
    """Listing adapter + date fallback for URLs that end in YYYY-MM-DD (date only, marked 'date_only')."""
    items = _orig_fetch_listing(http, src)
    for it in items:
        if it.get("published_at") is None:
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})/?$", urlparse(it["url"]).path)
            if m:
                try:
                    d = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=NPT)
                    it["published_at"], it["date_quality"] = d.astimezone(UTC), "date_only"
                except ValueError:
                    pass
    return items


# ---- repeated-story clustering (query-time, over the last N hours) ----------------------
def _cluster(rows, window_h=30):
    """Union-find over headline-token similarity. Two headlines join when they share >=3 tokens and either
    Jaccard >= 0.5 or (>=4 shared and overlap coefficient >= 0.8), and were published within window_h hours."""
    n = len(rows)
    toks = [title_tokens(r["title"]) for r in rows]
    when = [parse_iso(r["sort_at"]) for r in rows]
    idx = defaultdict(list)
    for i, t in enumerate(toks):
        for w in t:
            idx[w].append(i)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        ti = toks[i]
        if len(ti) < 3:
            continue
        cnt = Counter()
        for w in ti:
            lst = idx[w]
            if len(lst) > 80:          # ubiquitous tokens (e.g. काठमाडौं) carry no signal and cost time
                continue
            for j in lst:
                if j > i:
                    cnt[j] += 1
        for j, inter in cnt.items():
            if inter < 3 or abs((when[i] - when[j]).total_seconds()) > window_h * 3600:
                continue
            tj = toks[j]
            if inter / len(ti | tj) >= 0.5 or (inter >= 4 and inter / min(len(ti), len(tj)) >= 0.8):
                a, b = find(i), find(j)
                if a != b:
                    parent[b] = a
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


_hot_cache = {}


def hot_clusters(hours=72):
    st = STORE_REF.settings() if STORE_REF else DEFAULT_SETTINGS
    tiers = ["high"] if st["mode"] == "strict" else ["high", "medium"]
    key = (tuple(tiers), int(hours))
    ent = _hot_cache.get(key)
    if ent and time.time() - ent[0] < 60:
        return ent[1]
    since = iso(utcnow() - timedelta(hours=int(hours)))
    with db() as c:
        rows = c.execute("SELECT id,title,url,source_id,source_name,published_at,sort_at,relevance,primary_category "
                         f"FROM articles WHERE relevance IN ({','.join('?' * len(tiers))}) AND sort_at>=? "
                         "ORDER BY sort_at DESC LIMIT 4000", [*tiers, since]).fetchall()
    prio = priority_ids()
    out = []
    for g in _cluster(rows):
        if len(g) < 2:
            continue
        mem = sorted((rows[i] for i in g), key=lambda r: r["sort_at"])
        srcs = {r["source_id"] for r in mem}
        rep = next((r for r in mem if r["source_id"] in prio), mem[0])
        cat = Counter(r["primary_category"] for r in mem if r["primary_category"]).most_common(1)
        out.append({
            "id": mem[0]["id"], "title": rep["title"], "url": rep["url"], "source_name": rep["source_name"],
            "n_sources": len(srcs), "n_articles": len(mem), "first_at": mem[0]["sort_at"], "last_at": mem[-1]["sort_at"],
            "category": CATEGORIES.get(cat[0][0], cat[0][0]) if cat else "",
            "relevance": "high" if any(r["relevance"] == "high" for r in mem) else "medium",
            "priority_sources": sum(1 for s in srcs if s in prio),
            "ids": [r["id"] for r in mem],
            "members": [{"id": r["id"], "source_id": r["source_id"], "source_name": r["source_name"], "url": r["url"],
                         "title": r["title"], "published_at": r["published_at"], "priority": r["source_id"] in prio}
                        for r in mem][:14]})
    out.sort(key=lambda c: c["last_at"], reverse=True)
    out.sort(key=lambda c: (c["n_sources"], c["n_articles"]), reverse=True)   # stable: ties keep newest first
    _hot_cache[key] = (time.time(), out)
    return out


_cover = {"t": 0.0, "m": {}}


def cover_map() -> dict:
    if time.time() - _cover["t"] > 60:
        cm = {}
        for c in hot_clusters(72):
            if c["n_sources"] < 2:
                continue
            cv = {"cid": c["id"], "n_sources": c["n_sources"], "n_articles": c["n_articles"], "members": c["members"]}
            for i in c["ids"]:
                cm[i] = cv
        _cover.update(t=time.time(), m=cm)
    return _cover["m"]


def hot_article_ids(min_sources=3) -> set:
    return {i for c in hot_clusters(72) if c["n_sources"] >= min_sources for i in c["ids"]}


_orig_serialize = serialize


def serialize(r, number=None, related=0):
    d = _orig_serialize(r, number, related)
    d["priority"] = r["source_id"] in priority_ids()
    try:
        d["cover"] = cover_map().get(r["id"])
    except Exception:
        log.exception("cover map failed")
        d["cover"] = None
    return d


_orig_build_filters = build_filters


def build_filters(p, settings, skip_category=False):
    where, args = _orig_build_filters(p, settings, skip_category)
    extra = []
    if str(p.get("priority") or "") in ("1", "true"):
        ids = sorted(priority_ids())
        if ids:
            extra.append(f"source_id IN ({','.join('?' * len(ids))})")
            args = [*args, *ids]
        else:
            extra.append("0=1")
    if str(p.get("hot") or "") in ("1", "true"):
        hid = sorted(hot_article_ids(max(2, _int(p.get("hot_min"), 3))))
        extra.append(f"id IN ({','.join(str(int(i)) for i in hid)})" if hid else "0=1")
    if extra:
        where = (where + " AND " if where else " WHERE ") + " AND ".join(extra)
    return where, args


_orig_build_digest = build_digest


def build_digest(period):
    md = _orig_build_digest(period)
    try:
        top = [c for c in hot_clusters(24 if period == "daily" else 168) if c["n_sources"] >= 3][:10]
    except Exception:
        log.exception("digest clustering failed")
        return md
    if not top:
        return md
    lines = ["## Most repeated stories (reported by 3+ publishers)", ""]
    for i, c in enumerate(top, 1):
        t = c["title"].replace("[", "(").replace("]", ")")
        srcs = ", ".join(("*" if m["priority"] else "") + m["source_name"] for m in c["members"])
        lines.append(f"{i}. **[{t}]({c['url']})** - {c['n_sources']} sources / {c['n_articles']} reports ({srcs})")
    block = "\n".join(lines) + "\n\n"
    pos = md.find("\n## ")
    return (md[:pos + 1] + block + md[pos + 1:]) if pos != -1 else md + "\n" + block


_orig_create_app = create_app


def create_app(store, engine, sched=None):
    app = _orig_create_app(store, engine, sched)

    @app.get("/api/trending")
    def trending(hours: int = 24, min_sources: int = 3, limit: int = 12):
        hours, min_sources, limit = max(6, min(168, hours)), max(2, min(10, min_sources)), max(1, min(40, limit))
        cl = [c for c in hot_clusters(hours) if c["n_sources"] >= min_sources][:limit]
        return {"hours": hours, "min_sources": min_sources,
                "clusters": [{k: v for k, v in c.items() if k != "ids"} for c in cl]}

    return app


_EXT_CSS = """
.art.hot{border-left:3px solid var(--hi);background:linear-gradient(90deg,rgba(255,122,69,.12),transparent 45%)}
.hotb{font-weight:700;font-size:10.5px;border:1px solid var(--hi);color:var(--hi);background:none;border-radius:10px;padding:0 8px;cursor:pointer}
.hotb.soft{border-color:var(--ln);color:var(--mu)}
.prio{color:var(--ac);font-weight:700;font-size:11px}
#hotPanel{border:1px solid var(--ln);border-radius:8px;background:var(--p);margin:8px 0}
#hotPanel>summary{cursor:pointer;padding:8px 12px;font-weight:700}
#hotPanel .hp{display:flex;gap:8px;align-items:center;padding:0 12px 8px;flex-wrap:wrap}
.hrow{display:grid;grid-template-columns:54px 1fr;gap:8px;padding:8px 12px;border-top:1px solid var(--ln)}
.hn{font-family:Consolas,monospace;font-weight:700;color:var(--hi)}
.hrow .srcs{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.hrow .srcs a{font-size:11px;border:1px solid var(--ln);border-radius:10px;padding:0 8px;color:var(--lk)}
.hrow .srcs a.p{border-color:var(--ac)}
"""

_EXT_JS = r"""<script>
(function(){'use strict';
if(window.__SNAPSHOT__)return;
const $=(s,r)=>(r||document).querySelector(s),MAP=new Map(),ST={priority:false,hot:false,min:3,hours:24};
const okUrl=u=>/^https?:\/\//i.test(u||'')?u:'#';
function el(tag,cls,txt){const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
const ago=s=>{if(!s)return'';const m=Math.round((Date.now()-new Date(s).getTime())/6e4);return m<60?Math.max(m,0)+'m ago':Math.round(m/60)+'h ago'};
const tok=()=>{try{return localStorage.getItem('nnh.token')||''}catch(e){return''}};
/* 1) hook article requests: add priority/hot filters, capture items so cards can be decorated */
const _f=window.fetch.bind(window);
window.fetch=function(input,init){
  const isArt=typeof input==='string'&&input.indexOf('/api/articles?')===0;
  if(isArt){if(ST.priority)input+='&priority=1';if(ST.hot)input+='&hot=1&hot_min='+ST.min}
  const p=_f(input,init);
  if(!isArt)return p;
  return p.then(async r=>{try{const d=await r.clone().json();(d.items||[]).forEach(x=>MAP.set(x.url,x))}catch(e){}return r});
};
/* 2) decorate cards: priority star + repeated-story badge with every publisher's own link */
const feed=$('#feed');
function toggleMembers(li,cv,self){
  const old=li.querySelector('.relz.hm');if(old){old.remove();return}
  const ul=el('ul','relz hm');
  cv.members.forEach(m=>{const l=el('li'),a=el('a',null,m.title);a.href=okUrl(m.url);a.target='_blank';a.rel='noopener';
    l.append(a,' — ',el('span','cnt',(m.priority?'★ ':'')+m.source_name+(m.url===self?' (this article)':'')));ul.append(l)});
  li.append(ul)}
function decorate(){
  if(!feed)return;
  feed.querySelectorAll('li.art').forEach(li=>{
    if(li.dataset.nnh)return;li.dataset.nnh='1';
    const a=li.querySelector('a.hl'),meta=li.querySelector('.meta');if(!a||!meta)return;
    const it=MAP.get(a.getAttribute('href'));if(!it)return;
    if(it.priority){const s=el('span','prio','★ priority');s.title='Priority source';meta.insertBefore(s,meta.children[1]||null)}
    const cv=it.cover;
    if(cv&&cv.n_sources>=2){
      const hot=cv.n_sources>=ST.min,b=el('button','hotb'+(hot?'':' soft'),(hot?'🔥 ':'')+cv.n_sources+' sources');
      b.title=cv.n_articles+' reports of this story in the last 72h — click for each publisher\'s article';
      b.onclick=()=>toggleMembers(li,cv,it.url);meta.appendChild(b);if(hot)li.classList.add('hot')}
  })}
if(feed)new MutationObserver(decorate).observe(feed,{childList:true});
/* 3) filter toggles */
function reload(){const s=$('#fSort');if(s)s.dispatchEvent(new Event('change'))}
function mkToggle(label,key){const b=el('button','btn gh nnh-tg',label);
  b.onclick=()=>{ST[key]=!ST[key];b.style.borderColor=ST[key]?'var(--ac)':'';b.style.color=ST[key]?'var(--ac)':'';reload()};return b}
const clr=$('#bClear');
if(clr){clr.after(mkToggle('★ Priority sources','priority'),mkToggle('🔥 Repeated only','hot'));
  clr.addEventListener('click',()=>{ST.priority=ST.hot=false;document.querySelectorAll('.nnh-tg').forEach(x=>{x.style.borderColor='';x.style.color=''})},true)}
/* 4) most-repeated-stories panel */
const fm=$('#fm'),panel=el('details');panel.id='hotPanel';panel.open=true;panel.append(el('summary',null,'🔥 Most repeated stories'));
const hp=el('div','hp'),selH=el('select'),selM=el('select'),list=el('div');
[['12','12h'],['24','24h'],['48','48h'],['72','72h']].forEach(([v,t])=>{const o=el('option',null,t);o.value=v;selH.append(o)});selH.value='24';
['2','3','4','5'].forEach(v=>{const o=el('option',null,'≥ '+v+' publishers');o.value=v;selM.append(o)});selM.value='3';
hp.append(el('span','cnt','Same event reported by several publishers. Window:'),selH,selM);panel.append(hp,list);if(fm)fm.before(panel);
function render(cl){
  if(!cl.length){list.replaceChildren(el('div','empty','No story repeated by ≥ '+selM.value+' publishers in this window yet.'));return}
  list.replaceChildren(...cl.map(c=>{
    const row=el('div','hrow'),body=el('div'),a=el('a','hl',c.title);a.href=okUrl(c.url);a.target='_blank';a.rel='noopener';
    row.append(el('div','hn','×'+c.n_sources));body.append(a);
    const meta=el('div','meta');meta.append(el('span',null,c.n_articles+' reports'),el('span','rel r-'+c.relevance,c.relevance),el('span',null,c.category||''),el('span',null,'latest '+ago(c.last_at)));body.append(meta);
    const srcs=el('div','srcs');c.members.forEach(m=>{const l=el('a',m.priority?'p':'',(m.priority?'★ ':'')+m.source_name);l.href=okUrl(m.url);l.target='_blank';l.rel='noopener';l.title=m.title;srcs.append(l)});
    body.append(srcs);row.append(body);return row}))}
async function loadHot(){try{const r=await _f('/api/trending?hours='+selH.value+'&min_sources='+selM.value+'&limit=12');const d=await r.json();render(d.clusters||[])}catch(e){list.replaceChildren(el('div','empty','Trending unavailable: '+e.message))}}
selH.onchange=selM.onchange=()=>{ST.min=+selM.value;ST.hours=+selH.value;loadHot()};loadHot();setInterval(loadHot,180000);
/* 5) star toggle in Source Directory */
const vsrc=$('#vSrc');
async function starRows(){
  if(!vsrc||vsrc.hidden)return;
  let map={};try{(await(await _f('/api/sources')).json()).forEach(s=>map[s.id]=!!s.priority)}catch(e){return}
  vsrc.querySelectorAll('tbody tr').forEach(tr=>{
    if(tr.dataset.star)return;const idEl=tr.querySelector('td:nth-child(2) .cnt'),box=tr.querySelector('td:last-child .row');if(!idEl||!box)return;
    tr.dataset.star='1';const id=idEl.textContent.trim(),b=el('button','ic'+(map[id]?' on':''),map[id]?'★':'☆');b.title='Priority source: highlighted in the feed and used by the ★ filter';
    b.onclick=async()=>{const nv=!b.classList.contains('on');
      const r=await _f('/api/sources/'+encodeURIComponent(id),{method:'PUT',headers:{'Content-Type':'application/json','X-Admin-Token':tok()},body:JSON.stringify({priority:nv})});
      if(r.ok){b.classList.toggle('on',nv);b.textContent=nv?'★':'☆'}else alert('Could not update: HTTP '+r.status+(r.status===401?' (admin token needed)':''))};
    box.prepend(b)})}
if(vsrc)new MutationObserver(()=>starRows()).observe(vsrc,{childList:true});
})();
</script>"""

DASHBOARD_HTML = (DASHBOARD_HTML.replace("</style></head>", _EXT_CSS + "</style></head>", 1)
                  .replace("</body></html>", _EXT_JS + "</body></html>", 1))


# ======================================================================================
# v1.2 EXTENSIONS (29 Sep 2026)
#   1) Your 17-site priority list, ranked 1-17: collected FIRST each run, flagged, shown in a coverage table.
#   2) Repeated-story detection fixed: template headlines ("dividend announced, bonus and cash?") of DIFFERENT
#      companies no longer merge; clusters get a heat level and a score (publishers + priority sources).
#   3) Manual headline entry for portals that forbid automated access (ShareSansar, MeroLagani, Nepali Paisa,
#      Nepse Alpha), so that their stories still join the repeated-story and priority logic.
# Nothing here bypasses any site's terms: sources with permitted=false are still never fetched automatically.
# ======================================================================================
VERSION = "1.2.8"  # Phase-1: SEO/a11y, mobile screener, Nepali NFC, source registry
PRIORITY_RANK = {   # your order: 1 = searched first
    "sharesansar": 1, "merolagani": 2, "nepalipaisa": 3, "nepsealpha": 4, "arthasansar": 5, "bizpati": 6,
    "bajarkochirfar": 7, "eng_bajarkochirfar": 7, "aarthiknews": 8, "abhiyandaily": 10,
    "bizmandu": 11, "insurancekhabar": 12, "bankingsamachar": 13, "fiscalnepal": 14, "karobardaily": 15,
    "onlinekhabar": 16, "ratopati": 17,
}
PRIORITY_IDS = tuple(PRIORITY_RANK)
_SOURCE_KEYS = _SOURCE_KEYS + ("rank",)

_NEW_SOURCES_V12 = [
    _s("eng_bajarkochirfar", "Bajarko Chirfar (English)", "https://eng.bajarkochirfar.com/",
       "https://eng.bajarkochirfar.com/", "market_portal", "en", "auto", ["https://eng.bajarkochirfar.com/feed"],
       notes="Added 29 Sep 2026 (priority #7, English edition from your list). Feed URL unverified; "
             "run 'verify-sources --only eng_bajarkochirfar'.",
       evidence="From user's priority list; feed URL unverified"),
]

_orig_store_migrate = Store.migrate


def _store_migrate_v12(self):
    """Idempotent. Applies your ranked list ONCE (marker file), so stars you change later in the UI are kept."""
    _orig_store_migrate(self)
    marker = self.dir / ".v12_priority_applied"
    with self.lock:
        lst = self._read(self.p_sources)
        have = {s.get("id") for s in lst}
        changed = False
        for ns in _NEW_SOURCES_V12:
            if ns["id"] not in have:
                lst.append(copy.deepcopy(ns))
                changed = True
        first_time = not marker.exists()
        for s in lst:
            sid = s.get("id")
            if first_time:
                s["priority"] = 1 if sid in PRIORITY_RANK else 0
                changed = True
                if sid == "nepalipaisa" and s.get("method") != "manual":
                    s["method"] = "manual"   # its Terms bar scripted copying (see notes): manual headline entry only
            if sid in PRIORITY_RANK and s.get("rank") != PRIORITY_RANK[sid]:
                s["rank"] = PRIORITY_RANK[sid]
                changed = True
        # one-time: arthiknews.com never produced an article; aarthiknews.com is the working site (rank 8)
        marker2 = self.dir / ".v12b_aarthik_applied"
        if not marker2.exists():
            for s in lst:
                if s.get("id") == "arthiknews":
                    s["priority"], s["enabled"] = 0, False
                    s.pop("rank", None)
                    s["notes"] = "[29-Sep-2026] Disabled: produced no articles. aarthiknews.com (rank 8) is the working site."
                elif s.get("id") == "aarthiknews":
                    s["priority"] = 1
            changed = True
            marker2.write_text("applied 2026-09-29\n", encoding="utf-8")
        if changed:
            self._write(self.p_sources, lst)
        if first_time:
            marker.write_text("applied 2026-09-29\n", encoding="utf-8")


Store.migrate = _store_migrate_v12


# ---- v1.2.5: ShareSansar automatic headline collection ---------------------------------------
# Checked 4 Oct 2026: https://www.sharesansar.com/robots.txt has "User-agent: *" with an empty Disallow (nothing
# blocked) and the Terms & Conditions page has no clause against automated access. The collector reads ONLY the
# headline, link and date from the public "latest news" list (never the article text), runs at most every few
# minutes, honours robots.txt through the hub's HTTP client, and every headline links back to ShareSansar.
# The other three portals stay manual (MeroLagani/Nepali Paisa terms forbid scripts; NepseAlpha blocks bots).
_orig_store_migrate_v124 = Store.migrate


def _store_migrate_v125(self):
    _orig_store_migrate_v124(self)
    marker = self.dir / ".v125_sharesansar_auto"
    if marker.exists():          # applied once per data folder, so a later manual change in the UI is kept
        return
    with self.lock:
        lst = self._read(self.p_sources)
        changed = False
        for src in lst:
            if src.get("id") == "sharesansar":
                src["method"], src["permitted"], src["enabled"] = "listing", True, True
                src["listing"] = {"url": "https://www.sharesansar.com/category/latest",
                                  "link_selector": 'a[href*="/newsdetail/"]'}
                src["min_interval_min"] = 5
                src["notes"] = ("[4-Oct-2026] robots.txt allows all (empty Disallow); Terms have no automation clause. "
                                "Headline, link and date only - article text is never copied; every item links to "
                                "ShareSansar.")
                changed = True
        if changed:
            self._write(self.p_sources, lst)
    marker.write_text("applied 2026-10-04\n", encoding="utf-8")


Store.migrate = _store_migrate_v125

_orig_sources = Store.sources


def _sources_ranked(self):
    """Priority sources first, in your rank order, so they are the first submitted to the worker pool."""
    lst = _orig_sources(self)
    return sorted(lst, key=lambda s: (0 if s.get("priority") else 1, s.get("rank") or 999))


Store.sources = _sources_ranked


def rank_map() -> dict:
    if STORE_REF is None:
        return {}
    return {s["id"]: (s.get("rank") or 999) for s in STORE_REF.sources() if s.get("priority")}


# Words that appear in many unrelated headlines (announcement boilerplate and sector labels). They never count as
# evidence that two headlines describe the same event; company names, numbers and places do.
_TEMPLATE_WORDS = ("""
लाभांश घोषणा बोनस नगद कति दिँदैछ दिने सेयर हकप्रद प्रतिशत प्रस्ताव पारित बोलायो साधारण सभा निष्कासन निष्काशन गर्ने गर्यो
गर्‍यो गरेको सञ्चालक समिति बैठक स्वीकृति सूचना जारी आज अन्तिम दिन कम्पनी कम्पनीको लघुवित्त बैंक बीमा लिमिटेड फाइनान्स
हाइड्रोपावर हाइड्रो विकास वित्तीय संस्था बिक्री बिक्रीमा नाफा घाटा वृद्धि घट्यो बढ्यो
र वा पनि तथा अनि यो त्यो लागि सँग बारे बाट देखि सम्म
dividend bonus cash share shares announces announced proposes proposed declares agm approves approved limited bank
""")
# run through the same normaliser as the headlines (it unifies Nepali spellings, e.g. लाभांश -> लाभांस)
_TEMPLATE_TOKENS = set().union(*(title_tokens(w) for w in _TEMPLATE_WORDS.split()))


# ---- clustering v2: template-headline guard --------------------------------------------
def _cluster(rows, window_h=30):
    """Union-find over headline similarity. A pair joins only if it shares >=3 tokens, is similar enough
    (Jaccard >= 0.5, or >=4 shared with overlap >= 0.8), was published within window_h hours AND shares at least one
    RARE token (not boilerplate and not ubiquitous in the window: a company name, a number, a place). Template words
    such as 'dividend / bonus / cash' are common, so two different companies' dividend headlines no longer merge
    unless the headlines are near-identical (Jaccard >= 0.8)."""
    n = len(rows)
    toks = [title_tokens(r["title"]) for r in rows]
    when = [parse_iso(r["sort_at"]) for r in rows]
    df = Counter(w for t in toks for w in t)
    rare_max = max(12, n // 40)   # only ubiquitous words are excluded by frequency; hot-story names must stay 'rare'
    idx = defaultdict(list)
    for i, t in enumerate(toks):
        for w in t:
            idx[w].append(i)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        ti = toks[i]
        if len(ti) < 3:
            continue
        cnt = Counter()
        for w in ti:
            lst = idx[w]
            if len(lst) > 80:
                continue
            for j in lst:
                if j > i:
                    cnt[j] += 1
        for j, inter in cnt.items():
            if inter < 3 or abs((when[i] - when[j]).total_seconds()) > window_h * 3600:
                continue
            tj = toks[j]
            shared = ti & tj
            jac = len(shared) / len(ti | tj)
            if not (jac >= 0.5 or (len(shared) >= 4 and len(shared) / min(len(ti), len(tj)) >= 0.8)):
                continue
            if not any(df[w] <= rare_max and len(w) > 1 and w not in _TEMPLATE_TOKENS for w in shared) and jac < 0.8:
                continue
            if rows[i]["source_id"] == rows[j]["source_id"]:
                continue   # one publisher's two similar headlines are different stories, not "repeated" coverage
            a, b = find(i), find(j)
            if a != b:
                parent[b] = a
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


_hot_clusters_v11 = hot_clusters
_MARKET_LABELS = {CATEGORIES[k] for k in ("official", "market_daily", "market_trends", "ipo", "dividend", "company",
                                          "results", "sebon", "funds", "mna", "investors")}   # stock-market stories rank higher


def hot_clusters(hours=72):
    """v1.1 clusters + heat level, ranked score and best-ranked-source representative."""
    out = _hot_clusters_v11(hours)
    rk = rank_map()
    for c in out:
        n = c["n_sources"]
        c["heat"] = 3 if n >= 6 else 2 if n >= 4 else 1 if n >= 3 else 0
        c["score"] = round(n + 0.6 * c["priority_sources"] + (1.0 if c["relevance"] == "high" else 0.0)
                           + (1.5 if c.get("category") in _MARKET_LABELS else 0.0), 2)
        ranked = [m for m in c["members"] if m["source_id"] in rk]
        if ranked:
            best = min(ranked, key=lambda m: rk[m["source_id"]])
            c["title"], c["url"], c["source_name"] = best["title"], best["url"], best["source_name"]
    out.sort(key=lambda c: c["last_at"], reverse=True)
    out.sort(key=lambda c: (c["score"], c["n_articles"]), reverse=True)
    return out


_orig_create_app_v11 = create_app


def create_app(store, engine, sched=None):
    app = _orig_create_app_v11(store, engine, sched)

    def _admin(request: Request):
        if ADMIN_TOKEN and not hmac.compare_digest(request.headers.get("X-Admin-Token", ""), ADMIN_TOKEN):
            raise HTTPException(401, "Admin token required (X-Admin-Token header)")

    @app.get("/api/priority-status")
    def priority_status():
        since = iso(utcnow() - timedelta(hours=24))
        health = {}
        with db() as c:
            stat = {r["source_id"]: r for r in c.execute(
                "SELECT source_id, COUNT(*) n, SUM(relevance IN ('high','medium')) rel, MAX(sort_at) last "
                "FROM articles WHERE sort_at>=? GROUP BY source_id", (since,))}
            for r in c.execute("SELECT * FROM source_health"):
                health[r["source_id"]] = dict(r)
        rows = []
        for s in store.sources():
            if not s.get("priority"):
                continue
            st = stat.get(s["id"])
            auto = s.get("method") != "manual" and (s.get("method") != "listing" or s.get("permitted"))
            rows.append({"rank": s.get("rank") or 999, "id": s["id"], "name": s["name"],
                         "mode": "manual entry" if not auto else (s.get("method") or "auto"),
                         "n24": int(st["n"]) if st else 0, "rel24": int(st["rel"] or 0) if st else 0,
                         "last": st["last"] if st else None,
                         "status": (health.get(s["id"]) or {}).get("status") or "unverified"})
        rows.sort(key=lambda r: r["rank"])
        return {"rows": rows}

    @app.post("/api/manual-headline", dependencies=[Depends(_admin)])
    def manual_headline(payload: dict = Body(...)):
        """Add a headline you read yourself on a portal that does not allow automated access."""
        sid = str(payload.get("source_id") or "").strip()
        title, url = clean_text(payload.get("title") or ""), str(payload.get("url") or "").strip()
        src = next((s for s in store.sources() if s["id"] == sid), None)
        if src is None:
            raise HTTPException(404, "source not found")
        if not title or len(title) > 300 or not is_http_url(url):
            raise HTTPException(400, "A headline (max 300 chars) and a valid http(s) link are required")
        host = lambda u: (urlparse(u).hostname or "").lower().removeprefix("www.")  # noqa: E731
        if host(url) != host(src["url"]) and not host(url).endswith("." + host(src["url"])):
            raise HTTPException(400, f"The link must be on {host(src['url'])} (the selected source)")
        settings = store.settings()
        clf = Classifier(store.keywords(), settings["thresholds"])
        with db() as c:
            new, _ = ingest(c, [{"url": url, "title": title, "excerpt": "", "published_at": None,
                                 "date_quality": "missing"}], src, clf, settings)
            row = c.execute("SELECT id, relevance FROM articles WHERE canonical_url=?",
                            (canonical_url(url),)).fetchone()
        _hot_cache.clear()
        _cover["t"] = 0.0
        return {"added": bool(new), "duplicate": not new, "id": row["id"] if row else None,
                "relevance": row["relevance"] if row else None}

    @app.post("/api/manual-bulk", dependencies=[Depends(_admin)])
    def manual_bulk(payload: dict = Body(...)):
        """Add several headlines you copied yourself from a portal that does not allow automated access (max 100).
        Nothing is fetched from the portal. A headline without a usable link on the source's own site is stored
        with a portal front-page link plus a unique ?mh= marker so duplicates are still detected."""
        import hashlib
        sid = str(payload.get("source_id") or "").strip()
        src = next((s for s in store.sources() if s["id"] == sid), None)
        if src is None:
            raise HTTPException(404, "source not found")
        raw = payload.get("items")
        if not isinstance(raw, list) or not raw or len(raw) > 100:
            raise HTTPException(400, "Send between 1 and 100 headlines")
        host = lambda u: (urlparse(u).hostname or "").lower().removeprefix("www.")  # noqa: E731
        sh, base = host(src["url"]), src["url"].rstrip("/")
        arts, seen, skipped = [], set(), 0
        for it in raw:
            if not isinstance(it, dict):
                skipped += 1
                continue
            title = clean_text(it.get("title") or "")
            url = str(it.get("url") or "").strip()
            if not (15 <= len(title) <= 300):
                skipped += 1
                continue
            if url and not (is_http_url(url) and (host(url) == sh or host(url).endswith("." + sh))):
                url = ""
            if not url:
                url = base + "/?mh=" + hashlib.sha1(title.lower().encode("utf-8")).hexdigest()[:12]
            if title.lower() in seen:
                continue
            seen.add(title.lower())
            arts.append({"url": url, "title": title, "excerpt": "", "published_at": None,
                         "date_quality": "missing"})
        settings = store.settings()
        clf = Classifier(store.keywords(), settings["thresholds"])
        with db() as c:
            new, _ = ingest(c, arts, src, clf, settings)
        _hot_cache.clear()
        _cover["t"] = 0.0
        return {"added": new, "duplicates": len(arts) - new, "skipped": skipped}

    return app


_EXT_CSS_V12 = """
.art.hot2{border-left:4px solid var(--hi);background:linear-gradient(90deg,rgba(255,122,69,.24),transparent 55%)}
.hotb.h2{background:var(--hi);color:#111}
.hn small{display:block;font-size:10px;font-weight:600;color:var(--mu)}
#prioPanel,#manPanel{border:1px solid var(--ln);border-radius:8px;background:var(--p);margin:8px 0}
#prioPanel>summary,#manPanel>summary{cursor:pointer;padding:8px 12px;font-weight:700}
#prioPanel table{width:100%;border-collapse:collapse;font-size:12px}
#prioPanel td,#prioPanel th{padding:4px 10px;border-top:1px solid var(--ln);text-align:left}
#prioPanel .warn{color:var(--hi);font-weight:600}
#manPanel .mf{display:grid;grid-template-columns:200px 1fr;gap:6px 10px;padding:0 12px 10px}
#manPanel input,#manPanel select{width:100%}
"""

_EXT_JS_V12 = r"""<script>
(function(){'use strict';
if(window.__SNAPSHOT__)return;
const $=(s,r)=>(r||document).querySelector(s);
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
const tok=()=>{try{return localStorage.getItem('nnh.token')||''}catch(e){return''}};
const flames=n=>n>=6?'🔥🔥🔥 ':n>=4?'🔥🔥 ':n>=3?'🔥 ':'';
/* A) stronger highlight for stories carried by >=5 publishers, and flame count on badges */
const feed=$('#feed');
function upgrade(){if(!feed)return;feed.querySelectorAll('button.hotb').forEach(b=>{
  if(b.dataset.v12)return;b.dataset.v12='1';const m=/(\d+) sources/.exec(b.textContent);if(!m)return;const n=+m[1];
  if(n>=3)b.textContent=flames(n)+n+' sources';if(n>=5){b.classList.add('h2');const li=b.closest('li.art');if(li)li.classList.add('hot2')}})}
if(feed)new MutationObserver(upgrade).observe(feed,{childList:true,subtree:true});
/* B) heat marks in the "Most repeated stories" panel */
const hot=$('#hotPanel');
if(hot)new MutationObserver(()=>{hot.querySelectorAll('.hn').forEach(h=>{if(h.dataset.v12)return;h.dataset.v12='1';
  const n=+(h.textContent.replace('×','')||0);if(n>=3)h.textContent=flames(n).trim()+' ×'+n})}).observe(hot,{childList:true,subtree:true});
/* C) priority coverage table */
const anchor=hot||$('#fm');
const pp=el('details');pp.id='prioPanel';pp.append(el('summary',null,'★ Priority sources: coverage in the last 24h'));
const pbody=el('div');pp.append(pbody);if(anchor)anchor.after(pp);
async function loadPrio(){try{const d=await(await fetch('/api/priority-status')).json();
  const t=el('table'),hd=el('tr');['#','Source','How collected','Articles 24h','Relevant','Last item'].forEach(x=>hd.append(el('th',null,x)));t.append(hd);
  d.rows.forEach(r=>{const tr=el('tr');const manual=r.mode==='manual entry';
    [r.rank,r.name,manual?'manual entry (site terms)':r.mode+' / '+r.status,r.n24,r.rel24,r.last?new Date(r.last).toLocaleString():'-'].forEach((x,i)=>{
      const td=el('td',null,String(x));if(i===3&&r.n24===0)td.className='warn';tr.append(td)});t.append(tr)});
  pbody.replaceChildren(t)}catch(e){pbody.textContent='Coverage unavailable: '+e.message}}
loadPrio();setInterval(loadPrio,300000);
/* D) manual headline entry for portals that do not permit automated access */
const mp=el('details');mp.id='manPanel';mp.append(el('summary',null,'＋ Add a headline you read on ShareSansar / MeroLagani / Nepali Paisa / Nepse Alpha'));
const mf=el('div','mf'),sel=el('select'),ti=el('input'),ur=el('input'),bt=el('button','btn',' Add headline'),msg=el('div','cnt');
ti.placeholder='Headline (copy as shown)';ur.placeholder='Link to the article (https://...)';ti.maxLength=300;
mf.append(el('span',null,'Source'),sel,el('span',null,'Headline'),ti,el('span',null,'Article link'),ur,el('span'),bt,el('span'),msg);mp.append(mf);pp.after(mp);
(async()=>{try{(await(await fetch('/api/sources')).json()).filter(s=>s.method==='manual'&&s.priority).sort((a,b)=>(a.rank||99)-(b.rank||99))
  .forEach(s=>{const o=el('option',null,s.name);o.value=s.id;sel.append(o)})}catch(e){}})();
bt.onclick=async()=>{msg.textContent='';if(!sel.value){msg.textContent='No manual-entry source available.';return}
  const r=await fetch('/api/manual-headline',{method:'POST',headers:{'Content-Type':'application/json','X-Admin-Token':tok()},
    body:JSON.stringify({source_id:sel.value,title:ti.value,url:ur.value})});
  let d={};try{d=await r.json()}catch(e){}
  if(r.ok){msg.textContent=d.duplicate?'Already in the hub.':'Added ('+d.relevance+'). It now takes part in repeated-story detection.';ti.value=ur.value=''}
  else msg.textContent='Could not add: '+(d.detail||r.status)};
/* E) bulk paste: you copy the page text yourself, paste it here, tick the headlines to keep */
const bp=el('details');bp.id='bulkPanel';bp.append(el('summary',null,'＋ Paste many headlines at once (copy the portal page yourself, then Ctrl+V here)'));
const bf=el('div','mf'),bsel=el('select'),bta=el('textarea'),blist=el('div'),bsave=el('button','btn',' Add ticked headlines'),bmsg=el('div','cnt');
bta.rows=4;bta.placeholder='On the portal news page press Ctrl+A, then Ctrl+C. Click here and press Ctrl+V.';
bf.append(el('span',null,'Source'),bsel,el('span',null,'Paste here'),bta,el('span',null,'Found'),blist,el('span'),bsave,el('span'),bmsg);bp.append(bf);mp.after(bp);
let bsrc=[],bitems=[],lastHtml='';
(async()=>{try{bsrc=(await(await fetch('/api/sources')).json()).filter(s=>s.method==='manual'&&s.priority).sort((a,b)=>(a.rank||99)-(b.rank||99));
  bsrc.forEach(s=>{const o=el('option',null,s.name);o.value=s.id;bsel.append(o)})}catch(e){}})();
const hostOf=u=>{try{return new URL(u).hostname.toLowerCase().replace(/^www\./,'')}catch(e){return''}};
const okHost=(u,s)=>{const h=hostOf(u),b=hostOf(s.url);return !!h&&(h===b||h.endsWith('.'+b))};
function bparse(html,plain){
  const s=bsrc.find(x=>x.id===bsel.value),out=[],seen=new Set();
  const add=(t,u)=>{t=t.replace(/\s+/g,' ').trim();if(t.length<20||t.length>300||t.split(' ').length<3)return;
    const k=t.toLowerCase();if(seen.has(k))return;seen.add(k);out.push({title:t,url:u||'',on:true})};
  if(html&&s&&/<a\s/i.test(html)){
    const doc=new DOMParser().parseFromString(html,'text/html');   // inert document: nothing is run or loaded
    doc.querySelectorAll('a[href]').forEach(a=>{let u='';try{u=new URL(a.getAttribute('href'),s.url).href}catch(e){}
      if(u&&okHost(u,s))add(a.textContent,u)})}
  if(!out.length)(plain||'').split(/\r?\n/).forEach(l=>add(l,''));
  return out.slice(0,100)}
function bshow(){blist.replaceChildren();
  if(!bitems.length){blist.textContent='Nothing found yet.';return}
  blist.append(el('div','cnt',bitems.length+' possible headlines. Untick anything that is not news.'));
  bitems.forEach(it=>{const row=el('label'),cb=el('input');row.style.cssText='display:flex;gap:6px;align-items:flex-start;padding:2px 0';
    cb.type='checkbox';cb.checked=it.on;cb.style.width='auto';cb.onchange=()=>{it.on=cb.checked};
    row.append(cb,el('span',null,it.title+(it.url?'':'   (no link: will point to the portal front page)')));blist.append(row)})}
bshow();
bta.addEventListener('paste',e=>{const cd=e.clipboardData;if(!cd)return;e.preventDefault();
  lastHtml=cd.getData('text/html');const p=cd.getData('text/plain');bta.value=p.slice(0,30000);bitems=bparse(lastHtml,p);bshow()});
bta.addEventListener('input',()=>{lastHtml='';bitems=bparse('',bta.value);bshow()});
bsel.onchange=()=>{bitems=bparse(lastHtml,bta.value);bshow()};
bsave.onclick=async()=>{bmsg.textContent='';const items=bitems.filter(i=>i.on).map(i=>({title:i.title,url:i.url}));
  if(!bsel.value||!items.length){bmsg.textContent='Choose a source and tick at least one headline.';return}
  const r=await fetch('/api/manual-bulk',{method:'POST',headers:{'Content-Type':'application/json','X-Admin-Token':tok()},
    body:JSON.stringify({source_id:bsel.value,items})});
  let d={};try{d=await r.json()}catch(e){}
  if(r.ok){bmsg.textContent='Added '+d.added+', already in the hub '+d.duplicates+(d.skipped?', skipped '+d.skipped:'')+'.';bta.value='';bitems=[];lastHtml='';bshow()}
  else bmsg.textContent='Could not add: '+(d.detail||r.status)};
})();
</script>"""

_orig_cmd_selftest = cmd_selftest


def cmd_selftest(_args=None) -> int:
    """v1.1 tests + v1.2 regression tests for the clustering guards and the priority ranking."""
    rc = _orig_cmd_selftest(_args)
    fails = 0

    def check(ok, name):
        nonlocal fails
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        fails += 0 if ok else 1

    def row(i, title, src, h=0):
        return {"id": i, "title": title, "source_id": src, "sort_at": iso(datetime(2026, 9, 29, 6, 0, tzinfo=UTC) + timedelta(hours=h))}

    tpl = "%s लाभांश घोषणा, बोनस र नगद कति ?"
    rows = [row(1, tpl % "मितेरी डेभलपमेन्ट बैंकको", "a"), row(2, tpl % "ग्लोबल आइएमई लघुवित्तको", "b"),
            row(3, tpl % "ग्लोबल आइएमई लघुवित्तको", "c"), row(4, tpl % "सानिमा माथिल्लो तामोरको", "d")]
    sizes = sorted(len(g) for g in _cluster(rows))
    check(sizes == [1, 1, 2], f"cluster: different companies' template headlines stay apart {sizes}")
    rows = [row(1, "इस्टर्न हाइड्रोपावरले हकप्रद निष्कासन गर्ने, बोलायो साधारण सभा", "a"),
            row(2, "इस्टर्न हाइड्रोपावरले हकप्रद निष्कासन गर्ने, बोलायो साधारण सभा", "a", 1)]
    check(all(len(g) == 1 for g in _cluster(rows)), "cluster: one publisher never counts as 'repeated'")
    check([k for k, _ in sorted(PRIORITY_RANK.items(), key=lambda kv: kv[1])][:6] ==
          ["sharesansar", "merolagani", "nepalipaisa", "nepsealpha", "arthasansar", "bizpati"], "priority: rank order 1-6")
    print(f"\nv1.2: {'all passed' if not fails else str(fails) + ' FAILED'}")
    return rc or (1 if fails else 0)

DASHBOARD_HTML = (DASHBOARD_HTML.replace("</style></head>", _EXT_CSS_V12 + "</style></head>", 1)
                  .replace("</body></html>", _EXT_JS_V12 + "</body></html>", 1))


if __name__ == "__main__":
    main()
