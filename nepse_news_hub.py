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
#scr .hh{margin:16px 0 6px;font-size:13px}
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

  <div id="v-hyd" class="subv" hidden>
    <div class="bar"><input id="hyd-sq" placeholder="Search symbol" list="syms" aria-label="Search hydropower symbol"><label><input type="checkbox" id="hyd-loss" style="width:16px;height:16px;min-width:0;padding:0"> Hide loss-making</label></div>
    <div class="sst" id="hyd-sst"></div>
    <div class="note" id="hyd-note"></div>
    <h3 class="hh">Hydropower score</h3>
    <div class="note" id="hyd-snote"></div>
    <div class="tw" id="t-hyd0"></div>
    <h3 class="hh">Market &amp; valuation</h3>
    <div class="tw" id="t-hyd1"></div>
    <h3 class="hh">Profitability</h3>
    <div class="tw" id="t-hyd2"></div>
    <h3 class="hh">Leverage &amp; balance sheet</h3>
    <div class="note">Liabilities include loans <em>and</em> payables/provisions. The export has no separate debt or finance-cost field, so these are leverage proxies, not Debt/Equity or interest cover.</div>
    <div class="tw" id="t-hyd3"></div>
    <h3 class="hh">Dividends</h3>
    <div class="note">Dividend Yield in the export is cash-dividend only. Payout = dividend &divide; latest annual EPS (only when EPS &gt; 0); check the dividend fiscal year, which can be older than the EPS year.</div>
    <div class="tw" id="t-hyd4"></div>
    <h3 class="hh">Technical analysis</h3>
    <div class="note">Trend: Uptrend = LTP &gt; SMA20 &gt; SMA50; Downtrend = LTP &lt; SMA20 &lt; SMA50; otherwise Mixed. RSI zone: Oversold &lt; 30, Overbought &gt; 70. Technical readings describe past price behaviour and are not buy/sell recommendations.</div>
    <div class="tw" id="t-hyd5"></div>
    <details class="scard" style="margin-top:12px"><summary><b>Not shown &mdash; data not in the export</b></summary>
      <div class="note">Installed / project MW, generation, capacity utilization, per-MW metrics (revenue, profit, debt), project status and operating stage, total debt, finance cost and interest coverage, and growth / CAGR (only one fiscal period is loaded). These are intentionally left out rather than estimated.</div>
    </details>
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

/* helpers shared with the CALCULATORS tab (same company data, no second database) */
window.NNH_DATA={find:find,g:g,fmt:fmt,esc:esc,date:D.date,go:v=>go(v)};

/* ---------- tabs ---------- */
const nav=document.createElement('nav');nav.className='vtabs';nav.setAttribute('aria-label','Sections');
nav.innerHTML='<button class="on" data-v="n">NEWS PORTAL<em>LATEST</em></button><button data-v="scr">STOCK SCREENER</button><button data-v="calc">CALCULATORS</button><button data-v="aiph">AI PHOTOSHOP</button>';
const sub=document.createElement('nav');sub.className='vtabs sub';sub.setAttribute('aria-label','Stock screener tools');sub.hidden=true;
sub.innerHTML=[['v-mkt','MARKET'],['v-s','SCREENER'],['v-map','HEATMAP'],['v-sig','SIGNALS'],['v-val','VALUATION'],['v-qua','QUALITY'],['v-div','DIVIDENDS'],['v-cmp','COMPARE'],['v-360','STOCK 360'],['v-bnk','BANKING'],['v-hyd','HYDROPOWER'],['v-wch','WATCHLIST']]
 .map(a=>`<button data-v="${a[0]}">${a[1]}</button>`).join('');
const top=document.querySelector('.top');
if(top){top.after(nav);nav.after(sub)}else{document.body.prepend(sub);document.body.prepend(nav)}
let curV='n',lastSub='v-mkt';
function go(v){
  const calc=(v==='calc'),aip=(v==='aiph');
  if(v==='scr')v=lastSub;                       // STOCK SCREENER tab -> reopen the last tool used
  if(v!=='n'&&!calc&&!aip&&!sub.querySelector(`[data-v="${v}"]`))return;
  const news=(v==='n');
  if(!news&&!calc&&!aip)lastSub=v;
  nav.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x.dataset.v===(news?'n':calc?'calc':aip?'aiph':'scr')));
  sub.hidden=news||calc||aip;
  sub.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x.dataset.v===v));
  const cn=document.getElementById('calc-nav');if(cn)cn.hidden=!calc;
  ['.stats','.lay'].forEach(q=>{const el=document.querySelector(q);if(el)el.style.display=news?'':'none'});
  const sec=document.getElementById('scr');if(sec)sec.hidden=news||calc||aip;
  const ap=document.getElementById('aiph');if(ap)ap.hidden=!aip;
  const cs=document.getElementById('calc');if(cs)cs.hidden=!calc;
  curV=v;
  if(calc){if(window.NNH_CALC)window.NNH_CALC.show()}
  else{try{if(/^#calc/.test(location.hash))history.replaceState(null,'',location.pathname+location.search)}catch(e){}}
  if(!news&&!calc&&!aip){document.querySelectorAll('#scr .subv').forEach(el=>{el.hidden=(el.id!==v)});renderView()}
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

/* ---------- HYDROPOWER ---------- */
/* Company list and score weights are injected from Python (HYDRO_SYMBOLS / HYDRO_SCORE_WEIGHTS). All values are
   derived from the existing screener rows through EX/g(); nothing is typed in per company. */
const HY=new Set(__HYDRO__),HW=__HYDW__;
const HYD=R.filter(r=>HY.has(r[CO]));
['Op Margin','Liab / Assets','Cash Payout','Total Payout','Total Dividend'].forEach(x=>PCT.add(x));
(function(){
  const N=['Revenue (NPR mn)','Op Profit (NPR mn)','Net Profit (NPR mn)','Assets (NPR mn)','Liabilities (NPR mn)','Equity (NPR mn)','Paid-up (NPR mn)',
    'Op Margin','Liab / Assets','Liab / Equity (x)','Mkt Cap / Paid-up (x)','Cash Payout','Total Payout','Total Dividend','Trend','RSI Zone',
    'Hydropower Score','Profitability','Valuation','Leverage','Dividend','Score inputs'];
  N.forEach(n=>EX[n]={});
  const mn=v=>num(v)?v/1000:null,rd=(v,d)=>num(v)?Math.round(v*d)/d:null;   // export is in NPR thousands
  const pr=(f,low)=>{const a=HYD.map(f).filter(num).sort((x,y)=>x-y);return v=>{if(!num(v)||a.length<2)return null;let lo=0;while(lo<a.length&&a[lo]<v)lo++;const p=100*lo/(a.length-1);return low?100-p:p}};
  const avg=a=>{a=a.filter(x=>x!=null);return a.length?a.reduce((s,x)=>s+x,0)/a.length:null};
  const peOk=r=>{const e=g(r,'EPS (Annu.)'),p=g(r,'P/E (Annu.)');return num(e)&&e>0&&num(p)&&p>0?p:null};
  const pbOk=r=>{const b=g(r,'P/B');return num(b)&&b>0?b:null};
  HYD.forEach(r=>{
    const s=r[CO],A=g(r,'Total Assets'),L=g(r,'Total Liabilities'),Rv=g(r,'Revenue'),Op=g(r,'Operating Profit'),eps=g(r,'EPS (Annu.)');
    const eq=num(A)&&num(L)?A-L:null;
    EX['Revenue (NPR mn)'][s]=mn(Rv);EX['Op Profit (NPR mn)'][s]=mn(Op);EX['Net Profit (NPR mn)'][s]=mn(g(r,'Net Profit'));
    EX['Assets (NPR mn)'][s]=mn(A);EX['Liabilities (NPR mn)'][s]=mn(L);EX['Equity (NPR mn)'][s]=mn(eq);EX['Paid-up (NPR mn)'][s]=mn(g(r,'Paid Up Cap'));
    EX['Op Margin'][s]=num(Op)&&num(Rv)&&Rv>0?rd(Op/Rv*100,10):null;
    EX['Liab / Assets'][s]=num(L)&&num(A)&&A>0?rd(L/A*100,10):null;
    EX['Liab / Equity (x)'][s]=num(L)&&num(eq)&&eq>0?rd(L/eq,100):null;          // equity <= 0 -> not meaningful
    const mc=g(r,'Market Cap'),pu=g(r,'Paid Up Cap');
    EX['Mkt Cap / Paid-up (x)'][s]=num(mc)&&num(pu)&&pu>0?rd(mc/pu,100):null;
    const px=g(r,'LTP'),s20=g(r,'SMA20'),s50=g(r,'SMA50'),rsi=g(r,'RSI');
    EX['Trend'][s]=num(px)&&num(s20)&&num(s50)?(px>s20&&s20>s50?'Uptrend':px<s20&&s20<s50?'Downtrend':'Mixed'):null;
    EX['RSI Zone'][s]=num(rsi)?(rsi<30?'Oversold':rsi>70?'Overbought':'Neutral'):null;
    const ca=g(r,'Cash'),tv=g(r,'Total Div'),to=tv!=null?tv:g(r,'Total');   // the dividend total may arrive as 'Total' or 'Total Div'
    EX['Total Dividend'][s]=num(to)?to:null;
    EX['Cash Payout'][s]=num(eps)&&eps>0&&num(ca)?rd(ca/eps*100,10):null;
    EX['Total Payout'][s]=num(eps)&&eps>0&&num(to)?rd(to/eps*100,10):null;
  });
  /* Transparent score: percentile rank (0-100) against the other listed hydropower companies only. */
  const rROE=pr(r=>g(r,'ROE (Annu.)')),rROA=pr(r=>g(r,'ROA (Annu.)')),rNPM=pr(r=>g(r,'NPM'));
  const rPE=pr(peOk,true),rPB=pr(pbOk,true),rLV=pr(r=>EX['Liab / Assets'][r[CO]],true),rDY=pr(r=>g(r,'Dividend Yield'));
  HYD.forEach(r=>{
    const s=r[CO],pe=peOk(r);
    const c={
      Profitability:avg([rROE(g(r,'ROE (Annu.)')),rROA(g(r,'ROA (Annu.)')),rNPM(g(r,'NPM'))]),
      // loss-making (EPS <= 0 or P/E <= 0) cannot be "cheap on earnings": P/E part scores 0
      Valuation:avg([num(g(r,'P/E (Annu.)'))||num(g(r,'EPS (Annu.)'))?(pe==null?0:rPE(pe)):null,rPB(pbOk(r))]),
      Leverage:rLV(EX['Liab / Assets'][s]),
      Dividend:num(g(r,'Dividend Yield'))?rDY(g(r,'Dividend Yield')):null};
    let w=0,t=0,n=0;
    Object.keys(HW).forEach(k=>{if(c[k]!=null){w+=HW[k];t+=HW[k]*c[k];n++}});
    ['Profitability','Valuation','Leverage','Dividend'].forEach(k=>{EX[k][s]=c[k]==null?null:Math.round(c[k])});
    EX['Hydropower Score'][s]=n>=3&&w>0?Math.round(t/w):null;                      // needs >= 3 of 4 components
    EX['Score inputs'][s]=n+'/4';
  });
})();
const medn=a=>{a=a.filter(num).sort((x,y)=>x-y);if(!a.length)return null;const m=a.length>>1;return a.length%2?a[m]:(a[m-1]+a[m])/2};
const meanv=a=>{a=a.filter(num);return a.length?a.reduce((x,y)=>x+y,0)/a.length:null};
function r_hyd(){
  const q=($('#hyd-sq').value||'').trim().toLowerCase(),hide=$('#hyd-loss').checked;
  const rs=HYD.filter(r=>(!q||String(r[CO]).toLowerCase().includes(q))&&(!hide||!(g(r,'Net Profit')<0)));
  const col=n=>rs.map(r=>g(r,n)),pos=n=>col(n).filter(v=>num(v)&&v>0);
  const mm=(a,d,u)=>medn(a)==null?'–':fmt(medn(a),d)+(u||'')+' <small>(mean '+fmt(meanv(a),d)+(u||'')+')</small>';
  const mc=col('Market Cap').filter(num).reduce((a,b)=>a+b,0);
  const loss=rs.filter(r=>g(r,'Net Profit')<0).length,pay=rs.filter(r=>g(r,'Total Dividend')>0).length;
  $('#hyd-sst').innerHTML=[['Companies',rs.length],['Total mkt cap (Arba)',arba(mc)],['Median P/E (profitable)',mm(pos('P/E (Annu.)'),1)],['Median P/B',mm(pos('P/B'),2)],
    ['Median ROE',mm(col('ROE (Annu.)'),1,'%')],['Loss-making',loss],['Paid a dividend',pay]].map(a=>`<div><small>${a[0]}</small><b>${a[1]}</b></div>`).join('');
  $('#hyd-note').innerHTML=`Company list: ${HYD.length} of ${HY.size} symbols found in the loaded data. <b>The list is not an official NEPSE classification &mdash; verify it.</b> Edit HYDRO_SYMBOLS in nepse_news_hub.py or put hydro_symbols.txt next to the script. Latest annual figures; financials in NPR million, market cap in Arba. Click a column title to sort.`;
  $('#hyd-snote').textContent='Score = weighted average of percentile ranks among the listed hydropower companies: '+Object.keys(HW).map(k=>k+' '+HW[k]+'%').join(', ')+'. Growth and operating (MW / generation) components are not scored because that data is not loaded. Needs at least 3 of 4 components. Relative ranking only, not a recommendation.';
  put('hyd0','#t-hyd0',['Company','Hydropower Score','Profitability','Valuation','Leverage','Dividend','Score inputs','LTP','Market Cap'],rs,{k:'Hydropower Score',asc:false});
  put('hyd1','#t-hyd1',['Company','LTP','% Change','1Y','Market Cap','P/E (Annu.)','P/B','EPS (Annu.)','BVPS','Mkt Cap / Paid-up (x)','Graham Upside %'],rs,{k:'Market Cap',asc:false});
  put('hyd2','#t-hyd2',['Company','Revenue (NPR mn)','Op Profit (NPR mn)','Net Profit (NPR mn)','Op Margin','NPM','ROE (Annu.)','ROA (Annu.)','Asset Turnover','EPS (Annu.)'],rs,{k:'Net Profit (NPR mn)',asc:false});
  put('hyd3','#t-hyd3',['Company','Assets (NPR mn)','Liabilities (NPR mn)','Equity (NPR mn)','Liab / Assets','Liab / Equity (x)','Paid-up (NPR mn)'],rs,{k:'Liab / Assets',asc:true});
  put('hyd5','#t-hyd5',['Company','LTP','% Change','1M','3M','6M','1Y','YTD','52 Wk High','52 Wk Low','% Below 52 Wk H','% Above 52 Wk L','180D VWAP','SMA20','SMA50','EMA20','EMA50','RSI','RSI Zone','MACD','Stochastic','ADX','BETA','Trend'],rs,{k:'RSI',asc:true});
  put('hyd4','#t-hyd4',['Company','Div Fiscal Year','Cash','Bonus','Total Dividend','Dividend Yield','Cash Payout','Total Payout','EPS (Annu.)'],rs,{k:'Dividend Yield',asc:false});
}

const VIEWS={'v-mkt':r_mkt,'v-s':r_s,'v-map':r_map,'v-sig':r_sig,'v-val':r_val,'v-qua':r_qua,'v-div':r_div,'v-cmp':r_cmp,'v-360':r_360,'v-bnk':r_bnk,'v-hyd':r_hyd,'v-wch':r_wch};
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
scr.addEventListener('input',e=>{if(['sq','div-sq','hyd-sq'].includes(e.target.id))renderView()});
scr.addEventListener('change',e=>{
  if(e.target.id==='hyd-loss'){renderView();return}
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


# --------------------------------------------------------------------------------------
# CALCULATORS tab (v1.4): third main tab, injected after SCREENER_BLOCK so it can reuse its
# find()/g()/fmt() helpers through window.NNH_DATA. To add a calculator, add one entry to the
# CALCS registry in the UI script below (and its pure engine function in the engines script).
# --------------------------------------------------------------------------------------
CALCULATORS_BLOCK = r'''<style>
#calc{padding:12px 16px 28px;max-width:1200px;margin:0 auto;min-width:0}
#calc[hidden],#calc-nav[hidden]{display:none!important}
#calc .chead h2{margin:4px 0 4px;font-size:20px}
#calc .chead p{margin:0 0 12px;color:var(--mu);font-size:13px;max-width:80ch}
#calc h3{margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mu)}
#calc .csec{margin:14px 0 6px;font-size:12px;color:var(--mu);text-transform:uppercase;letter-spacing:.04em}
#calc .cgrid{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:14px;align-items:start}
#calc .cgrid.even{grid-template-columns:repeat(2,minmax(0,1fr))}
@media(max-width:820px){#calc .cgrid,#calc .cgrid.even{grid-template-columns:minmax(0,1fr)}}
#calc .cc{background:var(--p);border:1px solid var(--ln);border-radius:8px;padding:14px;min-width:0}
#calc .fld{margin:0 0 10px;min-width:0}
#calc .fld[hidden]{display:none}
#calc label{display:block;font-size:12px;font-weight:600;margin:0 0 4px}
#calc .hint{font-weight:400;color:var(--mu)}
#calc input:not([type=checkbox]),#calc select{width:100%;background:var(--p);color:var(--tx);border:1px solid var(--ln);border-radius:6px;padding:6px 10px;min-height:38px;font:inherit;box-sizing:border-box}
#calc input[readonly]{background:var(--ch)}
#calc input:focus-visible,#calc select:focus-visible,#calc button:focus-visible,#calc summary:focus-visible{outline:2px solid var(--ac);outline-offset:1px}
#calc label.chk{display:flex;gap:8px;align-items:center;font-weight:600;margin:10px 0}
#calc label.chk input{width:auto;min-height:0}
#calc .row3{display:grid;grid-template-columns:1fr 1.4fr 1fr;gap:8px}
#calc .chips,#calc .seg{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}
#calc .chip,#calc .cbtn{background:var(--p);color:var(--tx);border:1px solid var(--ln);border-radius:16px;padding:5px 12px;min-height:32px;font:inherit;font-size:12px;cursor:pointer}
#calc .cbtn{border-radius:6px;min-height:36px}
#calc .cbtn.small{min-height:28px;padding:2px 10px}
#calc .chip:hover,#calc .cbtn:hover{border-color:var(--ac);color:var(--ac)}
#calc .chip.on{background:var(--ac);color:var(--onac);border-color:transparent}
#calc .cact{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
#calc .fx{margin-top:6px}
#calc .symmsg{font-size:11px;color:var(--mu);min-height:1em}
#calc .chd{border-bottom:1px solid var(--ln);padding-bottom:10px;margin-bottom:12px}
#calc .chd small{color:var(--mu);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
#calc .cbig{font-size:clamp(22px,4vw,32px);font-weight:700;line-height:1.2;font-variant-numeric:tabular-nums}
#calc .csub{color:var(--mu);font-size:12px;margin-top:2px}
#calc .csub b{color:var(--tx)}
#calc .cs{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:0 0 6px}
#calc .cs>div{background:var(--ch);border:1px solid var(--ln);border-top:3px solid var(--ac2);border-radius:8px;padding:8px 12px;min-width:0}
#calc .cs small{display:block;color:var(--mu);font-size:10px;text-transform:uppercase;letter-spacing:.04em}
#calc .cs b{display:block;font-size:15px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
#calc .cs.tiles{grid-template-columns:repeat(4,minmax(0,1fr))}
#calc .cs.tiles b{font-size:22px}
@media(max-width:520px){#calc .cs.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}#calc .cs{grid-template-columns:repeat(auto-fit,minmax(130px,1fr))}}
#calc .u{color:var(--ok)}#calc .d{color:var(--bad)}
#calc .badge{display:inline-block;border-radius:6px;padding:1px 8px;font-size:11px;font-weight:700;background:var(--ch);border:1px solid var(--ln)}
#calc .badge.ok{color:var(--ok);border-color:var(--ok)}#calc .badge.wa{color:var(--wa);border-color:var(--wa)}#calc .badge.bad{color:var(--bad);border-color:var(--bad)}
#calc .ct{overflow:auto;max-height:60vh;border:1px solid var(--ln);border-radius:8px;margin:6px 0;-webkit-overflow-scrolling:touch}
#calc table{border-collapse:collapse;width:100%;font-size:12px}
#calc th{position:sticky;top:0;background:var(--ch);text-align:right;padding:7px 9px;white-space:nowrap;border-bottom:1px solid var(--ln);font-size:11px}
#calc td{padding:6px 9px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--ln);font-variant-numeric:tabular-nums}
#calc th:first-child,#calc td:first-child{text-align:left}
#calc table.avt td{padding:4px 6px}#calc table.avt input,#calc table.avt select{min-width:84px}
#calc .ckv{display:grid;grid-template-columns:1fr auto;gap:4px 14px;font-size:12px}
#calc .ckv span:nth-child(odd){color:var(--mu)}#calc .ckv span:nth-child(even){text-align:right;font-variant-numeric:tabular-nums}
#calc .ckv.one{grid-template-columns:1fr auto}
#calc .cwarn{background:var(--ch);border:1px solid var(--ln);border-left:3px solid var(--wa);border-radius:6px;padding:8px 12px;margin:0 0 12px;font-size:12px}
#calc .cerr{background:var(--ch);border:1px solid var(--bad);border-radius:6px;padding:8px 12px;font-size:13px;color:var(--bad)}
#calc .cnote,#calc .cdisc,#calc .cmu{color:var(--mu);font-size:12px}
#calc .cnote{margin-top:12px}#calc .cdisc{margin-top:14px;border-top:1px solid var(--ln);padding-top:10px}
#calc .cnotes{margin:8px 0 0;padding-left:18px;font-size:12px;color:var(--mu)}
#calc details.cdet{margin:10px 0;font-size:12px;color:var(--mu)}#calc details.cdet summary{cursor:pointer;font-weight:600;color:var(--tx)}
#calc svg.chart{width:100%;height:auto;display:block;margin-top:12px}#calc svg.chart text{fill:var(--mu)}#calc svg.chart line{stroke:var(--ln)}
#calc .legend{display:flex;gap:14px;font-size:11px;color:var(--mu);margin:4px 0 10px}#calc .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
#calc .swap{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);gap:8px;align-items:end;margin-bottom:10px}
#calc .res{margin-top:12px;min-height:3.2em}
#calc .todaybox{margin-bottom:10px}
#calc .gh{margin:18px 0 8px;font-size:14px;text-transform:none;letter-spacing:0;color:var(--tx)}#calc .gh small{font-weight:400;color:var(--mu);font-size:12px;margin-left:6px}
#calc .ovg{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,240px),1fr));gap:10px}
#calc .ovc{text-align:left;background:var(--p);color:var(--tx);border:1px solid var(--ln);border-radius:8px;padding:12px;cursor:pointer;font:inherit;display:flex;flex-direction:column;gap:4px;min-height:64px}
#calc .ovc:hover{border-color:var(--ac)}#calc .ovc b{font-size:13px}#calc .ovc span{font-size:12px;color:var(--mu)}
@media(max-width:640px){#calc{padding:10px 8px 24px}#calc input:not([type=checkbox]),#calc select{font-size:16px;min-height:42px}#calc .row3{grid-template-columns:1fr}}
#calc-nav.vtabs{display:block;overflow:visible;white-space:normal;padding:8px 14px}#calc-nav[hidden]{display:none!important}
#calc-nav .cn-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}#calc-nav .cn-row[hidden]{display:none}
#calc-nav #cn-groups button{font-size:12px;font-weight:700;padding:5px 14px}
#calc-nav .cn-sub{margin-top:8px;padding:8px 0 0;border-top:1px dashed var(--ln)}#calc-nav .cn-sub button{font-size:11px;font-weight:600;background:var(--p2);min-height:28px}#calc-nav .cn-sub button.on{background:var(--ac)}#calc-nav .cn-sub button[hidden]{display:none}
@media(max-width:480px){.vtabs:not(.sub){display:grid;grid-template-columns:1fr 1fr;gap:4px;padding:8px 8px 0;overflow:visible}.vtabs:not(.sub) button{padding:9px 7px;font-size:11px;text-align:center;border-radius:8px}.vtabs:not(.sub) em{margin-left:4px;padding:1px 4px}#calc-nav.vtabs{padding:8px 8px}#calc-nav .cn-row{gap:5px}#calc-nav #cn-groups button{padding:5px 10px;font-size:11px}#calc-nav .cn-sub button{padding:4px 9px}}
#calc .nwbox{border:1px solid var(--ln);background:var(--ch);border-radius:8px;padding:10px 12px;margin:0 0 8px}#calc .nwh{display:flex;justify-content:space-between;align-items:center;gap:8px}#calc .nwh small{color:var(--mu);font-size:10px;text-transform:uppercase;letter-spacing:.04em}#calc .nwtxt{font-size:16px;font-weight:600;line-height:1.5;margin-top:4px;overflow-wrap:anywhere}#calc .nwtxt[lang=ne]{font-size:17px}
#calc .lbrow{display:flex;align-items:center;justify-content:space-between;gap:6px}#calc .lbrow label{margin:0 0 4px}
#calc .tipbtn{flex:0 0 auto;width:22px;height:22px;min-height:22px;border-radius:50%;border:1px solid var(--ln);background:var(--ch);color:var(--ac);font:700 12px/1 inherit;cursor:pointer;padding:0}
#calc .tipbtn:hover{border-color:var(--ac)}#calc .tipbox{background:var(--ch);border:1px solid var(--ln);border-radius:6px;padding:6px 10px;margin:0 0 6px;font-size:12px;color:var(--mu)}#calc .tipbox[hidden]{display:none}
#calc .cbtn.primary{background:var(--ac);color:var(--onac);border-color:transparent}#calc .cbtn.primary:hover{color:var(--onac);filter:brightness(1.08)}
#calc .cform{margin-top:14px;padding:10px 12px;border:1px solid var(--ln);border-radius:8px;font-size:12px;color:var(--mu)}#calc .cform b{color:var(--tx)}#calc .cform ul{margin:6px 0 0;padding-left:18px}#calc .cform li{margin:2px 0}
#calc .cwarn{margin:8px 0}
#calc details.rules{margin:0 0 12px;background:var(--p);border:1px solid var(--ln);border-radius:8px;padding:8px 14px}#calc details.rules .rg{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:4px 10px;margin-top:10px}
#calc details.rules table input{width:110px;min-height:32px}
</style>
<nav class="vtabs sub" id="calc-nav" aria-label="Calculators" hidden></nav>
<section id="calc" hidden aria-label="Calculators"><div id="calc-body"></div></section>
<script>
/*
 * bs_core.js - BS (Bikram Sambat) <-> AD (Gregorian) conversion core.
 * No dependencies. Works in browsers and Node.
 *
 * Method: lookup table of days per BS month (29..32), anchored at
 *   BS 2000-01-01 = AD 1943-04-14.
 * Data encoding: one string per BS year, 12 digits, digit = days - 28.
 * Supported range: BS 2000-01-01 .. 2090-12-30  (AD 1943-04-14 .. 2034-04-13)
 *
 * OVERRIDES: put corrections here if your official patro disagrees with the table,
 * e.g.  OVERRIDES[2062] = [31,31,31,32,31,31,29,30,29,30,29,31];
 */
(function (root) {
  'use strict';

  var FIRST_YEAR = 2000;
  var LAST_YEAR = 2090;
  var EPOCH_UTC = Date.UTC(1943, 3, 14); // AD 1943-04-14 == BS 2000-01-01
  var DAY_MS = 86400000;

  var RAW = '243432221213,334333212122,334432212122,343432221123,243432221213,334333212122,334432212122,343432221123,333433122113,334333212122,334432212122,343432221123,333433122122,334333212122,334432212122,343432221123,333433122122,334333212122,343432212122,343432221213,333433212122,334333212122,343432221122,343432221213,333433212122,334333212122,343432221123,243432221213,334333212122,334342212122,343432221123,243432221213,334333212122,334432212122,343432221123,243433122113,334333212122,334432212122,343432221123,333433122122,334333212122,334432212122,343432221123,333433122122,334333212122,343432212122,343432221123,333433212122,334333212122,343432221122,343432221213,333433212122,334333212122,343432221122,343432221213,334333212122,334342212122,343432221123,243432221213,334333212122,334432212122,343432221123,243433121213,334333212122,334432212122,343432221123,333433122113,334333212122,334432212122,343432221123,333433122122,334333212122,343432212122,343432221123,333433212122,334333212122,343432221122,343432221213,333433212122,334333212122,343432221122,343432221213,334333212122,334333212122,334332221222,343423221222,243432221222,334333221222,234423221222,243432221222,243432221222'; // replaced by build script; comma separated, one entry per year

  var OVERRIDES = {};

  var MONTHS_EN = ['Baisakh', 'Jestha', 'Ashadh', 'Shrawan', 'Bhadra', 'Ashwin',
    'Kartik', 'Mangsir', 'Poush', 'Magh', 'Falgun', 'Chaitra'];
  var MONTHS_NE = ['बैशाख', 'जेष्ठ', 'आषाढ', 'श्रावण', 'भाद्र', 'आश्विन',
    'कार्तिक', 'मंसिर', 'पौष', 'माघ', 'फाल्गुन', 'चैत्र'];
  var WEEKDAYS_EN = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  var WEEKDAYS_NE = ['आइतबार', 'सोमबार', 'मङ्गलबार', 'बुधबार', 'बिहीबार', 'शुक्रबार', 'शनिबार'];

  var yearMonths = {};   // year -> [12 ints]
  var yearStart = {};    // year -> days since epoch at BS year start
  var totalDays = 0;

  function build() {
    var rows = RAW.split(',');
    var acc = 0, y, m, arr;
    for (y = FIRST_YEAR; y <= LAST_YEAR; y++) {
      if (OVERRIDES[y]) {
        arr = OVERRIDES[y].slice();
      } else {
        var s = rows[y - FIRST_YEAR];
        arr = [];
        for (m = 0; m < 12; m++) arr.push(parseInt(s.charAt(m), 10) + 28);
      }
      yearMonths[y] = arr;
      yearStart[y] = acc;
      acc += arr.reduce(function (a, b) { return a + b; }, 0);
    }
    totalDays = acc;
  }

  function daysInMonth(y, m) {
    if (!yearMonths[y]) throw new RangeError('BS year ' + y + ' is outside ' + FIRST_YEAR + '-' + LAST_YEAR);
    if (m < 1 || m > 12) throw new RangeError('BS month must be 1-12');
    return yearMonths[y][m - 1];
  }

  function utcToParts(ms) {
    var d = new Date(ms);
    return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate(), weekday: d.getUTCDay() };
  }

  /** BS -> AD. Throws RangeError for impossible or unsupported dates. */
  function bsToAd(y, m, d) {
    y = +y; m = +m; d = +d;
    if (!(Number.isInteger(y) && Number.isInteger(m) && Number.isInteger(d))) throw new TypeError('Year, month and day must be whole numbers');
    var dim = daysInMonth(y, m);
    if (d < 1 || d > dim) throw new RangeError('BS ' + y + ' ' + MONTHS_EN[m - 1] + ' has ' + dim + ' days (you entered ' + d + ')');
    var offset = yearStart[y];
    for (var i = 0; i < m - 1; i++) offset += yearMonths[y][i];
    offset += d - 1;
    return utcToParts(EPOCH_UTC + offset * DAY_MS);
  }

  /** AD -> BS. Throws RangeError for invalid or unsupported dates. */
  function adToBs(y, m, d) {
    y = +y; m = +m; d = +d;
    if (!(Number.isInteger(y) && Number.isInteger(m) && Number.isInteger(d))) throw new TypeError('Year, month and day must be whole numbers');
    var t = Date.UTC(y, m - 1, d);
    var chk = new Date(t);
    if (chk.getUTCFullYear() !== y || chk.getUTCMonth() !== m - 1 || chk.getUTCDate() !== d) {
      throw new RangeError('AD ' + y + '-' + m + '-' + d + ' is not a real calendar date');
    }
    var offset = Math.round((t - EPOCH_UTC) / DAY_MS);
    if (offset < 0 || offset >= totalDays) {
      throw new RangeError('AD date is outside the supported range (1943-04-14 to 2034-04-13)');
    }
    var lo = FIRST_YEAR, hi = LAST_YEAR;
    while (lo < hi) { // largest year with yearStart <= offset
      var mid = (lo + hi + 1) >> 1;
      if (yearStart[mid] <= offset) lo = mid; else hi = mid - 1;
    }
    var rem = offset - yearStart[lo], mon = 0;
    while (rem >= yearMonths[lo][mon]) { rem -= yearMonths[lo][mon]; mon++; }
    return { year: lo, month: mon + 1, day: rem + 1, weekday: chk.getUTCDay() };
  }

  var DEV = '०१२३४५६७८९';
  function toDevanagari(s) { return String(s).replace(/[0-9]/g, function (c) { return DEV.charAt(+c); }); }

  build();

  var api = {
    FIRST_YEAR: FIRST_YEAR, LAST_YEAR: LAST_YEAR, OVERRIDES: OVERRIDES,
    MONTHS_EN: MONTHS_EN, MONTHS_NE: MONTHS_NE, WEEKDAYS_EN: WEEKDAYS_EN, WEEKDAYS_NE: WEEKDAYS_NE,
    daysInMonth: daysInMonth, bsToAd: bsToAd, adToBs: adToBs, toDevanagari: toDevanagari,
    rebuild: build
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.BSCore = api;
})(typeof window !== 'undefined' ? window : this);

/*
 * age_core.js - calendar-aware age calculation for AD (Gregorian) and BS (Bikram Sambat).
 * Depends on bs_core.js (BSCore) only for BS mode.
 *
 * Method ("anniversary" method, same idea as dateutil.relativedelta):
 *   1. Find the largest whole number of months n such that DOB + n months <= target date.
 *      Adding months keeps the day-of-month, but if the target month is shorter the day is
 *      clamped to the month's last day (31 Jan + 1 month = 28/29 Feb).
 *   2. years = floor(n / 12), months = n % 12, days = target - (DOB + n months).
 * This never produces negative days, unlike the simple "borrow from previous month" method
 * which breaks for dates such as 31 Jan -> 1 Mar.
 *
 * Leap-day birthdays (29 Feb) in non-leap years: jurisdictions differ, so the rule is an option:
 *   'feb28' (default) -> anniversary falls on 28 Feb
 *   'mar1'            -> anniversary falls on 1 Mar
 */
(function (root) {
  'use strict';
  var DAY = 86400000;
  var BS = root.BSCore || (typeof require !== 'undefined' ? require('./bs_core.built.js') : null);

  function isLeap(y) { return (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0; }

  var CAL = {
    AD: {
      dim: function (y, m) { return [31, isLeap(y) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]; },
      ord: function (y, m, d) { return Math.round(Date.UTC(y, m - 1, d) / DAY); } // days since 1970-01-01
    },
    BS: {
      dim: function (y, m) { return BS.daysInMonth(y, m); },
      ord: function (y, m, d) { var a = BS.bsToAd(y, m, d); return Math.round(Date.UTC(a.year, a.month - 1, a.day) / DAY); }
    }
  };

  function weekdayOf(ord) { return (((ord + 4) % 7) + 7) % 7; } // 1970-01-01 was a Thursday (4); 0 = Sunday

  function addMonths(cal, dob, n, leapRule) {
    var total = dob.y * 12 + (dob.m - 1) + n;
    var y = Math.floor(total / 12), m = total - y * 12 + 1;
    var dim = cal.dim(y, m);
    if (dob.d <= dim) return { y: y, m: m, d: dob.d };
    if (cal === CAL.AD && leapRule === 'mar1' && dob.m === 2 && dob.d === 29) return { y: y, m: 3, d: 1 };
    return { y: y, m: m, d: dim };
  }

  function validate(cal, p, label) {
    if (!p || !Number.isInteger(p.y) || !Number.isInteger(p.m) || !Number.isInteger(p.d)) throw new TypeError(label + ' is not a complete date');
    if (p.m < 1 || p.m > 12) throw new RangeError(label + ': month must be 1-12');
    var dim = cal.dim(p.y, p.m);
    if (p.d < 1 || p.d > dim) throw new RangeError(label + ': day must be 1-' + dim + ' for that month');
  }

  /**
   * @param {'AD'|'BS'} calName  calendar used for entering AND counting
   * @param {{y,m,d}} dob
   * @param {{y,m,d}} to         date to calculate the age on
   * @param {{leapRule?: 'feb28'|'mar1'}} [opts]
   */
  function calculate(calName, dob, to, opts) {
    var cal = CAL[calName]; if (!cal) throw new TypeError('Unknown calendar ' + calName);
    var leapRule = (opts && opts.leapRule) || 'feb28';
    validate(cal, dob, 'Date of birth'); validate(cal, to, 'Calculation date');
    var dobOrd = cal.ord(dob.y, dob.m, dob.d), toOrd = cal.ord(to.y, to.m, to.d);
    if (toOrd < dobOrd) throw new RangeError('The calculation date is before the date of birth');

    var ordOf = function (n) { var a = addMonths(cal, dob, n, leapRule); return cal.ord(a.y, a.m, a.d); };
    var n = Math.max(0, (to.y - dob.y) * 12 + (to.m - dob.m));
    while (n > 0 && ordOf(n) > toOrd) n--;
    while (true) { // safety: try one more month only if it still fits (BS year limit ends the loop)
      var fits = false;
      try { fits = ordOf(n + 1) <= toOrd; } catch (e) { fits = false; }
      if (!fits) break;
      n++;
    }
    var anniv = addMonths(cal, dob, n, leapRule);
    var years = Math.floor(n / 12), months = n % 12, days = toOrd - cal.ord(anniv.y, anniv.m, anniv.d);
    var totalDays = toOrd - dobOrd;

    var isBirthday = n > 0 && months === 0 && days === 0;
    var next = null, daysToNext = null;
    try {
      next = isBirthday ? { y: to.y, m: to.m, d: to.d } : addMonths(cal, dob, (years + 1) * 12, leapRule);
      daysToNext = cal.ord(next.y, next.m, next.d) - toOrd;
    } catch (e) { next = null; daysToNext = null; } // beyond supported BS range

    return {
      years: years, months: months, days: days,
      totalDays: totalDays, totalWeeks: Math.floor(totalDays / 7), extraDays: totalDays % 7, totalMonths: n,
      bornWeekday: weekdayOf(dobOrd), isBirthday: isBirthday,
      next: next, nextWeekday: next ? weekdayOf(cal.ord(next.y, next.m, next.d)) : null, daysToNext: daysToNext
    };
  }

  var api = { calculate: calculate, isLeap: isLeap, CAL: CAL };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.AgeCore = api;
})(typeof window !== 'undefined' ? window : this);

/* Numbers to Words: Nepali rupees and paisa, lakh-crore system, English + Nepali (Devanagari).
   API: NumWords.convert(input) -> { ok, english, nepali, formatted, formattedNe, rupees, paisa }  or  { ok:false, error, empty }
   Works in the browser (window.NumWords) and in Node (module.exports). No dependencies. */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.NumWords = api;
})(typeof window !== 'undefined' ? window : this, function () {
  'use strict';
  var EN_ONES = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen'];
  var EN_TENS = ['', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety'];
  var EN_SCALE = ['thousand', 'lakh', 'crore', 'arab', 'kharab', 'neel'];
  /* Nepali words for 0-99 (each number has its own word) */
  var NE = ['शून्य', 'एक', 'दुई', 'तीन', 'चार', 'पाँच', 'छ', 'सात', 'आठ', 'नौ',
    'दश', 'एघार', 'बाह्र', 'तेह्र', 'चौध', 'पन्ध्र', 'सोह्र', 'सत्र', 'अठार', 'उन्नाइस',
    'बीस', 'एक्काइस', 'बाइस', 'तेइस', 'चौबीस', 'पच्चीस', 'छब्बीस', 'सत्ताइस', 'अठ्ठाइस', 'उनान्तीस',
    'तीस', 'एकतीस', 'बत्तीस', 'तेत्तीस', 'चौँतीस', 'पैँतीस', 'छत्तीस', 'सैँतीस', 'अठतीस', 'उनान्चालीस',
    'चालीस', 'एकचालीस', 'बयालीस', 'त्रिचालीस', 'चौवालीस', 'पैंतालीस', 'छयालीस', 'सतचालीस', 'अठचालीस', 'उनान्पचास',
    'पचास', 'एकाउन्न', 'बाउन्न', 'त्रिपन्न', 'चौवन्न', 'पचपन्न', 'छपन्न', 'सन्ताउन्न', 'अन्ठाउन्न', 'उनान्साठी',
    'साठी', 'एकसट्ठी', 'बैसट्ठी', 'त्रिसट्ठी', 'चौंसट्ठी', 'पैंसट्ठी', 'छयसट्ठी', 'सतसट्ठी', 'अठसट्ठी', 'उनान्सत्तरी',
    'सत्तरी', 'एकहत्तर', 'बहत्तर', 'त्रिहत्तर', 'चौहत्तर', 'पचहत्तर', 'छयहत्तर', 'सतहत्तर', 'अठहत्तर', 'उनान्असी',
    'असी', 'एकासी', 'बयासी', 'त्रियासी', 'चौरासी', 'पचासी', 'छयासी', 'सतासी', 'अठासी', 'उनान्नब्बे',
    'नब्बे', 'एकानब्बे', 'बयानब्बे', 'त्रियानब्बे', 'चौरानब्बे', 'पन्चानब्बे', 'छयानब्बे', 'सन्तानब्बे', 'अन्ठानब्बे', 'उनान्सय'];
  var NE_SCALE = ['हजार', 'लाख', 'करोड', 'अरब', 'खरब', 'नील'];
  var MAX_DIGITS = 15;

  function en99(n) { return n < 20 ? EN_ONES[n] : EN_TENS[Math.floor(n / 10)] + (n % 10 ? ' ' + EN_ONES[n % 10] : ''); }
  function en999(n) { var h = Math.floor(n / 100), r = n % 100, p = []; if (h) p.push(EN_ONES[h] + ' hundred'); if (r) p.push(en99(r)); return p.join(' '); }
  function ne999(n) { var h = Math.floor(n / 100), r = n % 100, p = []; if (h) p.push(NE[h] + ' सय'); if (r) p.push(NE[r]); return p.join(' '); }

  /* Indian grouping: last 3 digits, then 2-digit groups (thousand, lakh, crore, arab, kharab, neel) */
  function words(n, hundreds, small, scales) {
    if (n === 0) return '';
    var parts = [], h = n % 1000, rest = Math.floor(n / 1000), g = [], i;
    for (i = 0; i < scales.length && rest > 0; i++) { g.push(rest % 100); rest = Math.floor(rest / 100); }
    for (i = g.length - 1; i >= 0; i--) if (g[i]) parts.push(small(g[i]) + ' ' + scales[i]);
    if (h) parts.push(hundreds(h));
    return parts.join(' ');
  }
  function enWords(n) { return words(n, en999, en99, EN_SCALE); }
  function neWords(n) { return words(n, ne999, function (x) { return NE[x]; }, NE_SCALE); }

  function indianGroup(intStr) {
    if (intStr.length <= 3) return intStr;
    var head = intStr.slice(0, -3), tail = intStr.slice(-3);
    return head.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + tail;
  }
  function toNeDigits(s) { return s.replace(/\d/g, function (d) { return String.fromCharCode(0x0966 + Number(d)); }); }

  function normalise(input) {
    var s = String(input == null ? '' : input).trim();
    s = s.replace(/[\u0966-\u096F]/g, function (c) { return String(c.charCodeAt(0) - 0x0966); });   // Devanagari digits
    s = s.replace(/[,\s\u00A0_]/g, '').replace(/^(rs\.?|npr|nrs\.?|\u0930\u0941\.?)/i, '');
    return s;
  }

  function convert(input) {
    var s = normalise(input);
    if (s === '') return { ok: false, empty: true, error: '' };
    if (/^-\d*\.?\d*$/.test(s) && /\d/.test(s)) return { ok: false, error: 'Negative amounts are not supported.' };
    if (!/^(\d+\.?\d*|\.\d+)$/.test(s)) return { ok: false, error: 'Please enter a valid number, e.g. 73269.77' };
    var parts = s.split('.'), ip = (parts[0] || '0').replace(/^0+(?=\d)/, ''), fp = parts[1] || '';
    if (ip.length > MAX_DIGITS) return { ok: false, error: 'Number is too large. Maximum supported is 15 digits (up to 99 neel).' };
    var total = BigInt(ip) * 100n + BigInt((fp + '00').slice(0, 2));
    if (fp.length > 2 && fp.charAt(2) >= '5') total += 1n;               // round half up to 2 decimals
    var rupees = total / 100n, paisa = Number(total % 100n);
    if (rupees.toString().length > MAX_DIGITS) return { ok: false, error: 'Number is too large. Maximum supported is 15 digits (up to 99 neel).' };
    var r = Number(rupees), en, ne;
    if (r === 0 && paisa === 0) { en = 'zero rupees'; ne = 'शून्य रुपैयाँ'; }
    else {
      en = (r > 0 ? enWords(r) + (r === 1 ? ' rupee' : ' rupees') : '') + (paisa ? (r > 0 ? ' and ' : '') + en99(paisa) + ' paisa' : '');
      ne = (r > 0 ? neWords(r) + ' रुपैयाँ' : '') + (paisa ? (r > 0 ? ' र ' : '') + NE[paisa] + ' पैसा' : '');
    }
    en = en + ' only'; en = en.charAt(0).toUpperCase() + en.slice(1);
    var fig = indianGroup(rupees.toString()) + '.' + (paisa < 10 ? '0' : '') + paisa;
    return { ok: true, english: en, nepali: ne + ' मात्र', formatted: fig, formattedNe: toNeDigits(fig), rupees: r, paisa: paisa };
  }
  return { convert: convert, MAX_DIGITS: MAX_DIGITS };
});

/* ===== NNH calculator engines: pure functions, no DOM. Rates live in one place (RATES). ===== */
(function (root) {
'use strict';
var E = {};
function CalcError(field, message, blank) { this.name = 'CalculatorInputError'; this.field = field; this.detail = message; this.blank = !!blank; this.message = field + ': ' + message; }
CalcError.prototype = Object.create(Error.prototype);
E.CalcError = CalcError;

function num(value, field, o) {
  o = o || {};
  if (value === null || value === undefined || (typeof value === 'string' && value.trim() === '')) throw new CalcError(field, 'is required', true);
  if (typeof value === 'boolean') throw new CalcError(field, 'must be a number');
  var n = value;
  if (typeof value === 'string') { n = Number(value.trim().replace(/,/g, '')); }
  if (typeof n !== 'number' || !isFinite(n)) throw new CalcError(field, 'must be a valid number');
  var minInc = o.minInclusive !== false, maxInc = o.maxInclusive !== false;
  if (o.min != null && (n < o.min || (n === o.min && !minInc))) throw new CalcError(field, 'must be ' + (minInc ? 'at least ' : 'greater than ') + o.min);
  if (o.max != null && (n > o.max || (n === o.max && !maxInc))) throw new CalcError(field, 'must be ' + (maxInc ? 'at most ' : 'less than ') + o.max);
  return n;
}
function int(value, field, o) { var n = num(value, field, o); if (Math.floor(n) !== n) throw new CalcError(field, 'must be a whole number'); return n; }
/* Excel ROUND: half away from zero, decimal-text based (avoids 1.005 style errors) */
function roundHalfUp(x, dp) {
  if (!isFinite(x)) return x;
  var sign = x < 0 ? -1 : 1, a = Math.abs(x);
  if (a < 1e-6) return 0;
  if (a >= 1e15) return sign * Math.round(a);
  var shifted = Number(a.toPrecision(15) + 'e' + dp);
  return sign * Number(Math.round(shifted) + 'e-' + dp);
}
var r2 = function (x) { return roundHalfUp(x, 2); }, r4 = function (x) { return roundHalfUp(x, 4); };
/* INT(ROUND(x,8)): guards float noise such as 100*(1+0.15) = 114.99999999999999 */
var floor8 = function (x) { return Math.floor(Number(x.toFixed(8))); };
E.num = num; E.roundHalfUp = roundHalfUp;

/* ---- Rates / fee schedule (single source of truth for Buy, Sell, Averaging) ---- */
var RATES = {
  brokerage: { slabs: [[50000, 0.0036], [500000, 0.0033], [2000000, 0.0031], [10000000, 0.0027], [Infinity, 0.0024]], minFee: 10, minFeeUpTo: 50000 },
  sebon: 0.00015,
  dp: 25,
  cgt: { individual_short: 0.05, individual_long: 0.0375, institutional: 0.10 },
  tds: 0.05,
  /* valuation settings (editable in the Rates & rules panel) */
  valuation: { fair_band_pct: 10, terminal_growth_warn_pct: 5, mos_strong_pct: 30, mos_moderate_pct: 10 }
};
E.RATES = RATES;
E.DEFAULT_RATES = JSON.parse(JSON.stringify(RATES, function (k, v) { return v === Infinity ? '__inf__' : v; }), function (k, v) { return v === '__inf__' ? Infinity : v; });
function brokerage(gross) {
  var rate = 0, i;
  for (i = 0; i < RATES.brokerage.slabs.length; i++) { if (gross <= RATES.brokerage.slabs[i][0]) { rate = RATES.brokerage.slabs[i][1]; break; } }
  var fee = gross * rate;
  if (gross <= RATES.brokerage.minFeeUpTo) fee = Math.max(fee, RATES.brokerage.minFee);
  return { rate: rate, fee: fee };
}
E.brokerage = brokerage;

/* ---- BUY ---- */
E.calculateBuy = function (i) {
  var price = num(i.stock_price, 'Price', { min: 0, minInclusive: false });
  var qty = int(i.quantity, 'Quantity', { min: 1 });
  var type = i.investor_type || 'Individual';
  if (['Individual', 'Institutional', 'Government'].indexOf(type) < 0) throw new CalcError('Investor type', 'invalid');
  var gross = price * qty, b = brokerage(gross), sebon = gross * RATES.sebon, dp = RATES.dp;
  var total = gross + b.fee + sebon + dp;
  return { gross_transaction_value: r2(gross), brokerage_rate: r2(b.rate * 100), brokerage_charge: r2(b.fee), sebon_fee: r2(sebon),
    dp_charge: dp, total_purchase_cost: r2(total), effective_cost_per_share: r2(total / qty) };
};

/* ---- SELL ---- (cgt: optional override {individual_short, individual_long, institutional} as fractions) */
E.calculateSell = function (i, cgtOverride) {
  var price = num(i.sell_price, 'Sell price', { min: 0, minInclusive: false });
  var qty = int(i.quantity, 'Quantity', { min: 1 });
  var cost = num(i.purchase_cost_per_share, 'Purchase cost per share', { min: 0 });
  var type = i.investor_type, hold = i.holding_period, dpOn = !!i.dp_applicable;
  if (['Individual', 'Institutional'].indexOf(type) < 0) throw new CalcError('Investor type', 'must be Individual or Institutional');
  if (['\u2264 365 Days', '> 365 Days'].indexOf(hold) < 0) throw new CalcError('Holding period', 'invalid');
  var cg = cgtOverride || RATES.cgt;
  var gross = price * qty, pcost = cost * qty, b = brokerage(gross), sebon = gross * RATES.sebon, dp = dpOn ? RATES.dp : 0;
  var fees = b.fee + sebon + dp, before = gross - pcost - fees;
  var rate = type === 'Institutional' ? cg.institutional : (hold === '\u2264 365 Days' ? cg.individual_short : cg.individual_long);
  var tax = before > 0 ? before * rate : 0;
  var net = gross - fees - tax;
  return { gross_sales_value: r2(gross), purchase_cost: r2(pcost), brokerage_rate: r2(b.rate * 100), broker_commission: r2(b.fee), sebon_fee: r2(sebon),
    dp_charge: dp, total_selling_fees: r2(fees), net_profit_before_cgt: r2(before), cgt_rate: r2(rate * 100), capital_gains_tax: r2(tax),
    net_cash_received: r2(net), net_profit_after_cgt: r2(net - pcost) };
};

/* ---- AVERAGING / WACC ----
   Secondary batches carry broker + SEBON fee; IPO and Bonus carry none.
   Quantity: every valid batch counts (a Bonus batch adds shares at zero cost). */
E.calculateWacc = function (batches) {
  var rows = [], totalQty = 0, totalCost = 0;
  batches.forEach(function (b, k) {
    var type = b.type || 'Secondary';
    if (['Secondary', 'IPO', 'Bonus'].indexOf(type) < 0) throw new CalcError('Batch ' + (k + 1) + ' type', 'invalid');
    var qty = num(b.quantity, 'Batch ' + (k + 1) + ' quantity', { min: 0 });
    var price = num(b.purchase_price, 'Batch ' + (k + 1) + ' price', { min: 0 });
    var row = { batch_no: b.batch_no != null ? b.batch_no : k + 1, type: type, quantity: qty, purchase_price: price,
      gross_value: 0, broker_fee: 0, sebon_fee: 0, total_investment_cost: 0, counted: false };
    var valid = qty > 0 && (price > 0 || type === 'Bonus');
    if (valid) {
      var gross = qty * price, bf = 0, sf = 0;
      if (type === 'Secondary') { bf = brokerage(gross).fee; sf = gross * RATES.sebon; }
      row.gross_value = r2(gross); row.broker_fee = r2(bf); row.sebon_fee = r2(sf); row.total_investment_cost = r2(gross + bf + sf); row.counted = true;
      totalQty += qty; totalCost += row.total_investment_cost;
    }
    rows.push(row);
  });
  return { batches: rows, total_quantity: totalQty, total_cost: r2(totalCost), wacc: totalQty > 0 ? r2(totalCost / totalQty) : 0 };
};

/* ---- BONUS ADJUSTMENT ---- (bonus_percentage as fraction, 0 <= x < 1) */
E.calculateBonus = function (i) {
  var p = num(i.market_price, 'Market price', { min: 0, minInclusive: false });
  var b = num(i.bonus_percentage, 'Bonus %', { min: 0, max: 1, maxInclusive: false });
  var q = int(i.quantity, 'Quantity', { min: 1 });
  var adj = p / (1 + b), nq = floor8(q * (1 + b)), pre = p * q, post = adj * nq;
  return { pre_market_price: r2(p), bonus_percentage: b, pre_quantity: q, adjusted_price: r2(adj), new_quantity: nq,
    pre_portfolio_value: r2(pre), post_portfolio_value: r2(post), value_verified: Math.abs(pre - post) < 1.0 };
};

/* ---- RIGHT SHARE ADJUSTMENT ---- (right_ratio fraction, 0 <= x < 1) */
E.calculateRight = function (i) {
  var u = int(i.initial_units, 'Units held', { min: 1 });
  var p = num(i.market_price, 'Market price', { min: 0, minInclusive: false });
  var rr = num(i.right_ratio, 'Right ratio', { min: 0, max: 1, maxInclusive: false });
  var sp = num(i.subscription_price, 'Subscription price', { min: 0, minInclusive: false });
  var pre = u * p, allot = u * rr, cash = allot * sp, post = u + allot, basis = pre + cash;
  var adj = (p + rr * sp) / (1 + rr), postW = post * adj;
  return { initial_units: u, market_price: r2(p), right_ratio: rr, subscription_price: r2(sp), pre_adjustment_wealth: r2(pre),
    right_shares_allotted: r2(allot), additional_cash: r2(cash), total_post_shares: r2(post), total_cost_basis: r2(basis),
    adjusted_price: r2(adj), post_adjustment_wealth: r2(postW), wealth_verified: Math.abs(basis - postW) < 1.0 };
};

/* ---- DIVIDEND & TDS ---- (percentages as fractions) */
E.calculateDividend = function (i) {
  var units = int(i.holding_units, 'Holding units', { min: 1 });
  var bp = num(i.bonus_percentage, 'Bonus %', { min: 0 });
  var cp = num(i.cash_dividend_percentage, 'Cash dividend %', { min: 0 });
  var fv = num(i.face_value == null ? 100 : i.face_value, 'Face value', { min: 0, minInclusive: false });
  var t = num(i.tds_rate == null ? RATES.tds : i.tds_rate, 'TDS rate', { min: 0, max: 1, maxInclusive: false });
  var ent = Number((units * bp).toFixed(8)), cred = Math.floor(ent), frac = ent - cred, imputed = ent * fv;
  var cash = units * fv * cp, base = cash + imputed, tds = base * t;
  var after = cash - tds, net, due, status;
  if (after < 0) { net = 0; due = Math.abs(after); status = 'PAY TAX SHORTFALL'; } else { net = after; due = 0; status = 'TAX COVERED'; }
  return { holding_units: units, face_value: r2(fv), bonus_percentage: bp, cash_dividend_percentage: cp, tds_rate: t,
    bonus_shares_entitled: roundHalfUp(ent, 4), bonus_shares_credited: cred, fractional_bonus: roundHalfUp(frac, 4), bonus_imputed_value: r2(imputed),
    gross_cash_dividend: r2(cash), taxable_income_base: r2(base), total_tds_payable: r2(tds), tds_on_cash: r2(cash * t), tds_on_bonus: r2(imputed * t),
    cash_after_tds: r2(after), net_cash_credited: r2(net), out_of_pocket_due: r2(due), status: status,
    net_dividend_value_after_tax: r2(net - due + imputed), min_cash_dividend_to_cover_tax: (t * bp) / (1 - t), demat_locked: due > 0 };
};

/* ---- CAGR ---- */
E.calculateCagr = function (i) {
  var a = num(i.initial_investment, 'Initial investment', { min: 0, minInclusive: false });
  var b = num(i.final_value, 'Final value', { min: 0, minInclusive: false });
  var y = num(i.duration_years, 'Duration (years)', { min: 0, minInclusive: false });
  var profit = b - a, abs = profit / a, mult = b / a, cagr = Math.pow(mult, 1 / y) - 1, simple = abs / y;
  var ytd = cagr > 0 ? Math.log(2) / Math.log(1 + cagr) : 'N/A', r72 = cagr > 0 ? 72 / (cagr * 100) : 'N/A';
  var need = Math.pow(2, 1 / y) - 1;
  return { profit: r2(profit), absolute_return_pct: r2(abs * 100), growth_multiple: r2(mult), cagr_pct: r2(cagr * 100),
    simple_annual_return_pct: r2(simple * 100), years_to_double: typeof ytd === 'number' ? r2(ytd) : ytd,
    rule_of_72: typeof r72 === 'number' ? r2(r72) : r72, cagr_to_double_pct: r2(need * 100) };
};

/* ---- EMI ---- (annual_interest_rate as fraction). method: 'reducing' (default) | 'flat' */
E.calculateEmi = function (i) {
  var P = num(i.principal, 'Loan amount', { min: 0, minInclusive: false });
  var rate = num(i.annual_interest_rate, 'Interest rate', { min: 0 });
  var yrs = num(i.tenure_years, 'Tenure (years)', { min: 0, minInclusive: false });
  var n = Math.round(yrs * 12);
  if (n < 1 || n > 360) throw new CalcError('Tenure (years)', 'must be between 1 month and 30 years');
  var flat = i.method === 'flat', r = rate / 12, emi;
  if (flat) emi = (P + P * rate * (n / 12)) / n;
  else emi = r === 0 ? P / n : P * r * Math.pow(1 + r, n) / (Math.pow(1 + r, n) - 1);
  var total = emi * n, bal = P, sched = [], m, intr, prin;
  for (m = 1; m <= n; m++) {
    intr = flat ? P * rate * (n / 12) / n : bal * r; prin = emi - intr; bal = Math.max(bal - prin, 0);
    sched.push({ month: m, interest: intr, principal: prin, balance: bal });
  }
  return { monthly_emi: r2(emi), total_repayment: r2(total), total_interest: r2(total - P), num_installments: n, schedule: sched };
};

/* ---- SIP ---- (step-up and rate as percent numbers; freq = installments per year) */
E.calculateSip = function (i) {
  var P = num(i.amount, 'Installment amount', { min: 0, minInclusive: false });
  var r = num(i.rate, 'Expected return', { min: 0, max: 60 });
  var y = int(i.years, 'Duration', { min: 1, max: 50 });
  var s = num(i.step == null || i.step === '' ? 0 : i.step, 'Step-up', { min: 0, max: 100 });
  var m = int(i.freq, 'Frequency', { min: 1, max: 12 });
  var ir = r / 100 / m, bal = 0, inv = 0, rows = [], yy, k, p;
  for (yy = 0; yy < y; yy++) {
    p = P * Math.pow(1 + s / 100, yy);
    for (k = 0; k < m; k++) { bal = (bal + p) * (1 + ir); inv += p; }
    rows.push({ y: yy + 1, p: p, inv: inv, gain: bal - inv, bal: bal });
  }
  return { maturity: bal, invested: inv, gain: bal - inv, gain_pct: inv ? (bal - inv) / inv * 100 : 0, rows: rows };
};


/* =====================================================================================
   TRADING, CORPORATE-ACTION AND VALUATION ENGINES (v1.5)
   Conventions: the helper functions that follow the spec signatures take rates as fractions
   (0.0036 = 0.36%). The calculators below take percentages as plain numbers (15 = 15%).
   All NEPSE rules (commission slabs, SEBON fee, DP charge, CGT) come from RATES above.
   ===================================================================================== */
function blank(v) { return v === null || v === undefined || String(v).trim() === ''; }
function optional(v, field, o) { return blank(v) ? null : num(v, field, o); }
function slabRate(value) { var s = RATES.brokerage.slabs, i; for (i = 0; i < s.length; i++) if (value <= s[i][0]) return s[i][1]; return s[s.length - 1][1]; }
function commission(value, rateOverride) { var rate = rateOverride == null ? slabRate(value) : rateOverride; return { rate: rate, fee: Math.max(value * rate, RATES.brokerage.minFee) }; }
var pctChange = function (a, b) { return a === 0 ? null : (b - a) / a * 100; };

/* ---- spec helpers ---- */
E.validatePositiveNumber = function (value, allowZero) {          /* returns '' when valid, otherwise the message */
  if (blank(value)) return 'Enter required data.';
  var n = typeof value === 'number' ? value : Number(String(value).trim().replace(/,/g, ''));
  if (!isFinite(n)) return 'Enter a valid number.';
  if (n < 0) return 'Value cannot be negative.';
  if (n === 0 && !allowZero) return 'Value must be greater than zero.';
  return '';
};
E.formatNpr = function (v) { if (v == null || !isFinite(v)) return '\u2013'; return (v < 0 ? '-Rs ' : 'Rs ') + new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Math.abs(v)); };
E.calculatePercentageChange = function (oldValue, newValue) { return pctChange(num(oldValue, 'Old value'), num(newValue, 'New value')); };
E.calculateBuyCost = function (quantity, buyPrice, brokerRate) {
  var q = num(quantity, 'Quantity', { min: 0, minInclusive: false }), p = num(buyPrice, 'Buy price', { min: 0, minInclusive: false });
  var v = q * p, c = commission(v, brokerRate == null ? null : num(brokerRate, 'Broker rate', { min: 0, max: 1 })), s = v * RATES.sebon;
  return { traded_value: v, brokerage_rate: c.rate, brokerage: c.fee, sebon_fee: s, total_buy_cost: v + c.fee + s };
};
E.calculateSellProceeds = function (quantity, sellPrice, brokerRate, dpCharge) {
  var q = num(quantity, 'Quantity', { min: 0, minInclusive: false }), p = num(sellPrice, 'Sell price', { min: 0, minInclusive: false });
  var v = q * p, c = commission(v, brokerRate == null ? null : num(brokerRate, 'Broker rate', { min: 0, max: 1 })), s = v * RATES.sebon;
  var dp = dpCharge == null ? RATES.dp : num(dpCharge, 'DP charge', { min: 0 }), ch = c.fee + s + dp;
  return { traded_value: v, brokerage_rate: c.rate, brokerage: c.fee, sebon_fee: s, dp_charge: dp, total_sell_charges: ch, net_sell_proceeds: v - ch };
};
E.calculateCgt = function (buyCost, netSellProceeds, holdingDays, shortRate, longRate) {   /* CGT only on a positive gain */
  var sr = shortRate == null ? RATES.cgt.individual_short : shortRate, lr = longRate == null ? RATES.cgt.individual_long : longRate;
  var rate = holdingDays <= 365 ? sr : lr, gain = Math.max(0, netSellProceeds - buyCost), tax = gain * rate;
  return { taxable_gain: gain, applicable_rate: rate, cgt_amount: tax, net_profit_after_tax: netSellProceeds - buyCost - tax };
};
E.calculateWeightedAveragePrice = function (oldQuantity, oldAveragePrice, newQuantity, newPurchasePrice) {
  var tq = oldQuantity + newQuantity, ti = oldQuantity * oldAveragePrice + newQuantity * newPurchasePrice;
  if (!(tq > 0)) throw new CalcError('Quantity', 'total quantity must be greater than zero');
  return { total_quantity: tq, total_investment: ti, new_average_price: ti / tq };
};
function daysBetween(a, b) {
  var re = /^(\d{4})-(\d{2})-(\d{2})$/, x = re.exec(String(a || '')), y = re.exec(String(b || ''));
  if (!x) throw new CalcError('Buy date', 'is required', true); if (!y) throw new CalcError('Sell date', 'is required', true);
  return Math.round((Date.UTC(+y[1], +y[2] - 1, +y[3]) - Date.UTC(+x[1], +x[2] - 1, +x[3])) / 86400000);
}
E.daysBetween = daysBetween;
var rateOpt = function (pct, field) { return blank(pct) ? null : num(pct, field, { min: 0, max: 100 }) / 100; };

/* smallest sell price (to the paisa) whose net proceeds reach requiredNet; net proceeds never decrease as price rises */
function solveSellPrice(requiredNet, q, rate, dp) {
  var s = RATES.sebon, rmax = rate == null ? Math.max.apply(null, RATES.brokerage.slabs.map(function (x) { return x[1]; })) : rate;
  if (rmax + s >= 1) throw new CalcError('Broker rate', 'is too high to recover costs');
  var net = function (P) { var v = q * P, c = commission(v, rate); return v - c.fee - v * s - dp; };
  var hi = Math.ceil(((requiredNet + dp + RATES.brokerage.minFee) / (q * (1 - s - rmax))) * 100) + 100, lo = 0, mid;
  while (lo < hi) { mid = Math.floor((lo + hi) / 2); if (net(mid / 100) >= requiredNet - 1e-9) hi = mid; else lo = mid + 1; }
  return lo / 100;
}

/* ---- 1 PROFIT / LOSS ---- */
E.calculateProfitLoss = function (i) {
  var bp = num(i.buy_price, 'Buy price', { min: 0, minInclusive: false }), sp = num(i.sell_price, 'Sell price', { min: 0, minInclusive: false }), q = int(i.quantity, 'Quantity', { min: 1 });
  var br = rateOpt(i.broker_rate_pct, 'Broker rate'), dp = optional(i.dp_charge, 'DP charge', { min: 0 }), ov = rateOpt(i.cgt_rate_pct, 'CGT rate');
  var days = daysBetween(i.buy_date, i.sell_date); if (days < 0) throw new CalcError('Sell date', 'must be on or after the buy date');
  var buy = E.calculateBuyCost(q, bp, br), sell = E.calculateSellProceeds(q, sp, br, dp), cg = E.calculateCgt(buy.total_buy_cost, sell.net_sell_proceeds, days, ov, ov);
  var gross = (sp - bp) * q, net = sell.net_sell_proceeds - buy.total_buy_cost - cg.cgt_amount;
  return { holding_days: days, total_buy_cost: buy.total_buy_cost, buy_charges: buy.brokerage + buy.sebon_fee, total_sell_value: sell.traded_value, sell_charges: sell.total_sell_charges,
    net_sell_proceeds: sell.net_sell_proceeds, gross_profit_loss: gross, taxable_gain: cg.taxable_gain, cgt_rate_pct: cg.applicable_rate * 100, cgt_amount: cg.cgt_amount,
    net_profit_loss: net, return_pct: net / buy.total_buy_cost * 100, result: net > 0 ? 'Profit' : net < 0 ? 'Loss' : 'Break-even', term: days <= 365 ? 'Short-term (365 days or less)' : 'Long-term (more than 365 days)' };
};

/* ---- 2 BREAK-EVEN ---- */
E.calculateBreakEven = function (i) {
  var bp = num(i.buy_price, 'Buy price', { min: 0, minInclusive: false }), q = int(i.quantity, 'Quantity', { min: 1 }), br = rateOpt(i.broker_rate_pct, 'Broker rate'), dp = optional(i.dp_charge, 'DP charge', { min: 0 });
  var buy = E.calculateBuyCost(q, bp, br), dpv = dp == null ? RATES.dp : dp, P = solveSellPrice(buy.total_buy_cost, q, br, dpv), sell = E.calculateSellProceeds(q, P, br, dpv);
  return { break_even_price: P, total_buy_cost: buy.total_buy_cost, sell_charges: sell.total_sell_charges, price_increase: P - bp, price_increase_pct: pctChange(bp, P) };
};

/* ---- 3 TARGET SELL PRICE (CGT is grossed up exactly, so the net profit equals the target) ---- */
E.calculateTargetSellPrice = function (i) {
  var bp = num(i.buy_price, 'Buy price', { min: 0, minInclusive: false }), q = int(i.quantity, 'Quantity', { min: 1 }), br = rateOpt(i.broker_rate_pct, 'Broker rate'), dp = optional(i.dp_charge, 'DP charge', { min: 0 });
  var ov = rateOpt(i.cgt_rate_pct, 'CGT rate'), days = daysBetween(i.buy_date, i.sell_date); if (days < 0) throw new CalcError('Expected sell date', 'must be on or after the buy date');
  var buy = E.calculateBuyCost(q, bp, br), dpv = dp == null ? RATES.dp : dp, t = ov != null ? ov : (days <= 365 ? RATES.cgt.individual_short : RATES.cgt.individual_long), D;
  if (i.mode === 'pct') D = buy.total_buy_cost * num(i.desired_return_pct, 'Desired return %', { min: 0 }) / 100; else D = num(i.desired_profit, 'Desired net profit', { min: 0 });
  if (t >= 1) throw new CalcError('CGT rate', 'must be below 100%');
  var need = D > 0 ? D / (1 - t) : 0, P = solveSellPrice(buy.total_buy_cost + need, q, br, dpv), sell = E.calculateSellProceeds(q, P, br, dpv), cg = E.calculateCgt(buy.total_buy_cost, sell.net_sell_proceeds, days, ov, ov);
  return { target_price: P, required_gross_profit: (P - bp) * q, estimated_cgt: cg.cgt_amount, cgt_rate_pct: cg.applicable_rate * 100, net_profit: cg.net_profit_after_tax, desired_net_profit: D, total_buy_cost: buy.total_buy_cost,
    price_increase_pct: pctChange(bp, P), holding_days: days, term: days <= 365 ? 'Short-term (365 days or less)' : 'Long-term (more than 365 days)' };
};

/* ---- 4 POSITION SIZE ---- (shares = risk amount / risk per share; the spec text divides by the price twice, which is corrected here) */
E.calculatePositionSize = function (i) {
  var cap = num(i.total_capital, 'Total capital', { min: 0, minInclusive: false }), rp = num(i.risk_pct, 'Risk per trade %', { min: 0, max: 100, minInclusive: false }), en = num(i.entry_price, 'Entry price', { min: 0, minInclusive: false });
  var sl = num(i.stop_loss, 'Stop-loss price', { min: 0, minInclusive: false }), al = optional(i.max_alloc_pct, 'Maximum allocation %', { min: 0, max: 100, minInclusive: false });
  if (sl >= en) throw new CalcError('Stop-loss price', 'must be below the entry price');
  var riskAmt = cap * rp / 100, rps = en - sl, byRisk = Math.floor(Number((riskAmt / rps).toFixed(8))), reqCap = byRisk * en;
  var limit = al == null ? cap : cap * al / 100, byCap = Math.floor(Number((limit / en).toFixed(8))), shares = Math.min(byRisk, byCap), warnings = [];
  if (reqCap > cap) warnings.push('The risk-based position needs more capital than you have. The share count is capped by your capital.');
  else if (al != null && reqCap > limit) warnings.push('The position is capped by your maximum allocation.');
  if (shares < 1) warnings.push('Capital or risk budget is too small for even one share at this entry and stop-loss.');
  var val = shares * en, risk = shares * rps;
  return { max_risk_amount: riskAmt, risk_per_share: rps, risk_based_shares: byRisk, required_capital: reqCap, shares: shares, position_size_rs: val, actual_risk: risk, actual_risk_pct: risk / cap * 100, position_pct_of_capital: val / cap * 100, warnings: warnings };
};

/* ---- 5 RISK / REWARD ---- */
E.calculateRiskReward = function (i) {
  var en = num(i.entry_price, 'Entry price', { min: 0, minInclusive: false }), sl = num(i.stop_loss, 'Stop-loss price', { min: 0, minInclusive: false }), tg = num(i.target_price, 'Target price', { min: 0, minInclusive: false });
  var q = int(i.quantity, 'Quantity', { min: 1 }), wp = optional(i.win_probability_pct, 'Win probability %', { min: 0, max: 100 });
  if (sl >= en) throw new CalcError('Stop-loss price', 'must be below the entry price');
  if (tg <= en) throw new CalcError('Target price', 'must be above the entry price');
  var risk = en - sl, reward = tg - en, out = { risk_per_share: risk, reward_per_share: reward, rr_ratio: reward / risk, total_risk: risk * q, total_reward: reward * q, break_even_win_rate_pct: risk / (risk + reward) * 100, expected_value_per_share: null, expected_value_total: null };
  if (wp != null) { out.expected_value_per_share = wp / 100 * reward - (1 - wp / 100) * risk; out.expected_value_total = out.expected_value_per_share * q; }
  return out;
};

/* ---- 6 BONUS SHARES (cost basis) ---- */
E.calculateBonusShares = function (i) {
  var q = int(i.quantity, 'Existing quantity', { min: 1 }), avg = num(i.avg_price, 'Average buy price', { min: 0, minInclusive: false }), b = num(i.bonus_pct, 'Bonus %', { min: 0, max: 1000 });
  var ent = Number((q * b / 100).toFixed(8)), cred = Math.floor(ent), nq = q + cred, inv = q * avg;
  return { bonus_entitled: ent, bonus_shares: cred, fractional: ent - cred, new_quantity: nq, total_investment: inv, new_average_price: inv / nq, old_average_price: avg };
};

/* ---- 7 RIGHT SHARES ---- */
E.calculateRightShares = function (i) {
  var q = int(i.quantity, 'Existing quantity', { min: 1 }), avg = num(i.avg_price, 'Average buy price', { min: 0, minInclusive: false }), r = num(i.right_pct, 'Right %', { min: 0, max: 1000 }), rp = num(i.right_price, 'Right issue price', { min: 0, minInclusive: false });
  var ent = Number((q * r / 100).toFixed(8)), sh = Math.floor(ent), cash = sh * rp, nq = q + sh, tot = q * avg + cash, na = tot / nq;
  return { right_entitled: ent, right_shares: sh, fractional: ent - sh, cash_required: cash, new_quantity: nq, total_investment: tot, new_average_price: na, average_change: na - avg, average_change_pct: pctChange(avg, na), old_average_price: avg };
};

/* ---- 8 COMBINED BONUS + RIGHT (order matters because the second action works on the first action's quantity) ---- */
E.calculateCombinedActions = function (i) {
  var q = int(i.quantity, 'Existing quantity', { min: 1 }), avg = num(i.avg_price, 'Average price', { min: 0, minInclusive: false }), b = num(i.bonus_pct, 'Bonus %', { min: 0, max: 1000 });
  var r = num(i.right_pct, 'Right %', { min: 0, max: 1000 }), rp = num(i.right_price, 'Right issue price', { min: 0, minInclusive: false }), mp = optional(i.market_price, 'Market price', { min: 0, minInclusive: false });
  var order = i.order === 'right_first' ? ['right', 'bonus'] : ['bonus', 'right'], qty = q, inv = q * avg, cashTotal = 0, bonusTotal = 0, rightTotal = 0;
  var steps = [{ action: 'Start', added: 0, cash: 0, quantity: qty, total_investment: inv, avg: inv / qty }];
  order.forEach(function (a) {
    var pctv = a === 'bonus' ? b : r, ent = Number((qty * pctv / 100).toFixed(8)), sh = Math.floor(ent), cash = a === 'right' ? sh * rp : 0;
    qty += sh; inv += cash; cashTotal += cash; if (a === 'bonus') bonusTotal += sh; else rightTotal += sh;
    steps.push({ action: a === 'bonus' ? 'Bonus ' + b + '%' : 'Right ' + r + '% @ Rs ' + rp, added: sh, cash: cash, quantity: qty, total_investment: inv, avg: inv / qty });
  });
  return { bonus_shares: bonusTotal, right_shares: rightTotal, cash_required: cashTotal, final_quantity: qty, final_average_price: inv / qty, total_investment: inv, steps: steps,
    adjusted_price: mp == null ? null : (q * mp + cashTotal) / qty, first_action: order[0] };
};

/* ---- 9 DIVIDEND REINVESTMENT ---- */
E.calculateDividendReinvestment = function (i) {
  var q = int(i.quantity, 'Existing quantity', { min: 1 }), avg = num(i.avg_price, 'Average price', { min: 0, minInclusive: false }), dps = num(i.dps, 'Dividend per share', { min: 0, minInclusive: false });
  var rpx = num(i.reinvest_price, 'Reinvestment price', { min: 0, minInclusive: false }), pc = blank(i.reinvest_pct) ? 100 : num(i.reinvest_pct, 'Reinvestment %', { min: 0, max: 100 }), ch = blank(i.charge_pct) ? 0 : num(i.charge_pct, 'Charges %', { min: 0, max: 20 });
  var total = q * dps, amount = total * pc / 100, unit = rpx * (1 + ch / 100), ns = Math.floor(Number((amount / unit).toFixed(8))), used = ns * unit, nq = q + ns, inv = q * avg + used;
  return { total_dividend: total, amount_reinvested: amount, new_shares: ns, cash_used: used, remaining_cash: amount - used, cash_not_reinvested: total - amount, new_quantity: nq, new_average_price: inv / nq, total_investment: inv,
    value_before: q * rpx, value_after: nq * rpx, value_change: (nq - q) * rpx };
};

/* ---- VALUATION ---- */
var V = RATES.valuation;
function verdict(upsidePct) { return upsidePct > V.fair_band_pct ? { text: 'Undervalued', kind: 'ok' } : upsidePct < -V.fair_band_pct ? { text: 'Overvalued', kind: 'bad' } : { text: 'Fairly valued', kind: 'wa' }; }
function vs(value, price) {   /* upside / margin of safety against the market price (price is optional) */
  if (price == null || !(value > 0)) return { upside_pct: null, mos_pct: null, verdict: null };
  return { upside_pct: (value - price) / price * 100, mos_pct: (value - price) / value * 100, verdict: verdict((value - price) / price * 100) };
}
E.verdict = verdict;
var priceOpt = function (v) { return optional(v, 'Current market price', { min: 0, minInclusive: false }); };

E.calculateGraham = function (i) {
  var eps = num(i.eps, 'EPS', { min: 0, minInclusive: false }), g = num(i.growth_pct, 'Expected growth %', { min: 0, max: 100 }), y = num(i.bond_yield_pct, 'AAA bond yield %', { min: 0, minInclusive: false }), p = priceOpt(i.price);
  var value = eps * (8.5 + 2 * g) * (4.4 / y), o = vs(value, p); o.value = value; o.price = p; return o;
};
E.calculatePeValuation = function (i) {
  var eps = num(i.eps, 'EPS', { min: 0, minInclusive: false }), p = num(i.price, 'Current market price', { min: 0, minInclusive: false }), t = num(i.target_pe, 'Target P/E', { min: 0, minInclusive: false });
  var fair = eps * t, o = vs(fair, p); o.current_pe = p / eps; o.target_pe = t; o.fair_value = fair; o.price = p; return o;
};
E.calculatePbValuation = function (i) {
  var b = num(i.bvps, 'Book value per share', { min: 0, minInclusive: false }), p = num(i.price, 'Current market price', { min: 0, minInclusive: false }), t = num(i.target_pb, 'Target P/B', { min: 0, minInclusive: false });
  var fair = b * t, o = vs(fair, p); o.current_pb = p / b; o.target_pb = t; o.fair_value = fair; o.price = p; return o;
};
E.calculateDividendYield = function (i) {
  var d = num(i.dps, 'Annual dividend per share', { min: 0, minInclusive: false }), p = num(i.price, 'Current market price', { min: 0, minInclusive: false }), avg = optional(i.avg_price, 'Average buy price', { min: 0, minInclusive: false });
  var q = optional(i.quantity, 'Quantity', { min: 1 }), n = { annual: 1, semi: 2, quarterly: 4 }[i.frequency || 'annual'] || 1;
  return { dividend_yield_pct: d / p * 100, yield_on_cost_pct: avg == null ? null : d / avg * 100, annual_income: q == null ? null : d * q, per_payment_income: q == null ? null : d * q / n, payments_per_year: n };
};
E.calculateDdm = function (i) {
  var d0 = num(i.d0, 'Current annual dividend', { min: 0, minInclusive: false }), g = num(i.growth_pct, 'Dividend growth %', { min: -50, max: 100 }), r = num(i.required_pct, 'Required return %', { min: 0, minInclusive: false }), p = priceOpt(i.price);
  if (g >= r) throw new CalcError('Growth rate', 'must be lower than the required return');
  var d1 = d0 * (1 + g / 100), value = d1 / ((r - g) / 100), o = vs(value, p); o.next_dividend = d1; o.value = value; o.price = p; return o;
};
E.calculateDcf = function (i) {
  var f0 = num(i.fcf, 'Free cash flow', { min: 0, minInclusive: false }), g1 = num(i.growth_pct, 'Initial growth %', { min: -50, max: 100 }), gt = num(i.terminal_pct, 'Terminal growth %', { min: -10, max: 100 });
  var w = num(i.discount_pct, 'Discount rate (WACC) %', { min: 0, minInclusive: false }), n = int(i.years, 'Projection years', { min: 1, max: 30 }), nd = num(i.net_debt, 'Net debt'), sh = num(i.shares, 'Shares outstanding', { min: 0, minInclusive: false }), p = priceOpt(i.price);
  if (gt >= w) throw new CalcError('Terminal growth', 'must be lower than the discount rate');
  var rows = [], pvSum = 0, t, fcf, pv;
  for (t = 1; t <= n; t++) { fcf = f0 * Math.pow(1 + g1 / 100, t); pv = fcf / Math.pow(1 + w / 100, t); pvSum += pv; rows.push({ year: t, fcf: fcf, pv: pv }); }
  var tv = rows[n - 1].fcf * (1 + gt / 100) / ((w - gt) / 100), pvTv = tv / Math.pow(1 + w / 100, n), ev = pvSum + pvTv, eq = ev - nd, fair = eq / sh, warnings = [];
  if (gt > V.terminal_growth_warn_pct) warnings.push('Terminal growth above ' + V.terminal_growth_warn_pct + '% is rarely sustainable for ever. Treat the result with caution.');
  if (eq <= 0) warnings.push('Equity value is zero or negative with these inputs, so there is no meaningful value per share.');
  var o = vs(fair, p); o.rows = rows; o.pv_sum = pvSum; o.terminal_value = tv; o.pv_terminal = pvTv; o.enterprise_value = ev; o.equity_value = eq; o.value = fair; o.price = p; o.warnings = warnings; o.terminal_share_pct = pvTv / ev * 100; return o;
};
function hasData(o) { return Object.keys(o).some(function (k) { return !blank(o[k]); }); }
E.calculateIntrinsicValue = function (i) {
  var m = i.method || 'graham', p = priceOpt(i.price), list = [], skipped = [];
  var runs = [['Graham', 'graham', E.calculateGraham, { eps: i.g_eps, growth_pct: i.g_growth, bond_yield_pct: i.g_yield }], ['DDM', 'ddm', E.calculateDdm, { d0: i.d_d0, growth_pct: i.d_growth, required_pct: i.d_req }],
    ['DCF', 'dcf', E.calculateDcf, { fcf: i.c_fcf, growth_pct: i.c_g1, terminal_pct: i.c_gt, discount_pct: i.c_w, years: i.c_years, net_debt: i.c_nd, shares: i.c_sh }]];
  runs.forEach(function (r) {
    if (m !== 'blend' && m !== r[1]) return;
    if (m === 'blend' && !hasData(r[3])) { skipped.push(r[0]); return; }
    try { list.push({ method: r[0], value: r[2](r[3]).value }); } catch (e) { if (e && e.name === 'CalculatorInputError') { e.message = r[0] + ': ' + e.message; e.field = r[0] + ' ' + e.field; } throw e; }
  });
  if (!list.length) throw new CalcError('Inputs', 'enter the data for at least one method', true);
  var vals = list.map(function (x) { return x.value; }).sort(function (a, b) { return a - b; }), avg = vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
  var med = vals.length % 2 ? vals[(vals.length - 1) / 2] : (vals[vals.length / 2 - 1] + vals[vals.length / 2]) / 2, sel = m === 'blend' ? avg : vals[0], o = vs(sel, p);
  o.methods = list; o.average = avg; o.median = med; o.selected = sel; o.skipped = skipped; o.price = p; o.is_blend = m === 'blend'; return o;
};
E.calculateTargetPrice = function (i) {
  var p = num(i.price, 'Current market price', { min: 0, minInclusive: false }), t;
  if (i.mode === 'iv') t = num(i.intrinsic_value, 'Intrinsic value', { min: 0, minInclusive: false }); else t = num(i.eps, 'EPS', { min: 0, minInclusive: false }) * num(i.target_pe, 'Target P/E', { min: 0, minInclusive: false });
  return { target_price: t, price: p, upside_pct: (t - p) / p * 100, required_change: t - p, verdict: verdict((t - p) / p * 100) };
};
E.calculateMarginOfSafety = function (i) {
  var iv = num(i.intrinsic_value, 'Intrinsic value', { min: 0, minInclusive: false }), p = num(i.price, 'Current market price', { min: 0, minInclusive: false }), m = (iv - p) / iv * 100, text, kind;
  if (m >= V.mos_strong_pct) { text = 'Strong margin of safety'; kind = 'ok'; } else if (m >= V.mos_moderate_pct) { text = 'Moderate margin of safety'; kind = 'ok'; }
  else if (m >= 0) { text = 'Limited margin of safety'; kind = 'wa'; } else { text = 'Trading above estimated intrinsic value'; kind = 'bad'; }
  return { margin_of_safety_pct: m, verdict: { text: text, kind: kind }, intrinsic_value: iv, price: p };
};
E.calculateEarningsYield = function (i) {
  var eps = num(i.eps, 'EPS', { min: 0, minInclusive: false }), p = num(i.price, 'Current market price', { min: 0, minInclusive: false }), rf = optional(i.risk_free_pct, 'Risk-free / fixed-deposit rate %', { min: 0, max: 100 }), ey = eps / p * 100, o = { earnings_yield_pct: ey, risk_free_pct: rf, difference_pp: null, interpretation: '' };
  if (rf != null) { o.difference_pp = ey - rf; o.interpretation = ey > rf ? 'The earnings yield is above the risk-free rate: you are paid more than a safe deposit for the extra risk.' : ey < rf ? 'The earnings yield is below the risk-free rate: a safe deposit pays more than these earnings, so the price assumes strong growth.' : 'The earnings yield equals the risk-free rate.'; }
  return o;
};


/* ---- MARGIN CALL (v2.0.0) and PIVOT POINTS (v1.0.0): the project's verified JS ports, embedded unchanged
        (only the import/export lines are removed). They share this file's num / roundHalfUp / error class. ---- */
E.calculateMarginCall = (function (CalculatorInputError, num, roundHalfUp) {
/**
 * NEPSE Margin Call Calculator - JavaScript port (no dependencies).
 *
 * Same logic, same input/output keys (snake_case) and same rounding rule as
 * calculators/margin_call.py, so a result from the browser equals a result from the API.
 * Works in browsers (<script type="module">) and Node 18+.
 *
 * Spec: documentation/specs/NEPSE_Margin_Call_Calculator_v2.xlsx
 * Rules: SEBON Margin Trading Facility Directive 2082 (min initial 30%, min maintenance 20%).
 */



const CALCULATOR_ID = "margin_call";
const VERSION = "2.0.0";
const STATUS_SAFE = "SAFE";
const STATUS_WARNING = "WARNING";
const STATUS_MARGIN_CALL = "MARGIN CALL";

const MAX_PRICE = 100000;
const MAX_AMOUNT = 1e12;

const r2 = (x) => roundHalfUp(x, 2);
const r4 = (x) => roundHalfUp(x, 4);

/**
 * @param {object} i
 * @param {number|string} i.stock_purchase_price  Rs per share
 * @param {number|string} i.own_cash              Rs
 * @param {number|string} i.broker_loan           Rs
 * @param {number|string} i.maintenance_margin    fraction (0.20 = 20%)
 * @param {number|string} i.current_ltp           Rs
 * @param {number|string} [i.min_initial_margin=0.30]
 * @param {number|string} [i.warning_buffer=0.05]
 * @returns {object} same keys as MarginCallResult in Python
 */
function calculateMarginCall(i) {
  const price = num(i.stock_purchase_price, "stock_purchase_price", { min: 0.01, max: MAX_PRICE });
  const cash = num(i.own_cash, "own_cash", { min: 0, max: MAX_AMOUNT, minInclusive: false });
  const loan = num(i.broker_loan, "broker_loan", { min: 0, max: MAX_AMOUNT });
  const m = num(i.maintenance_margin, "maintenance_margin", { min: 0, max: 1, minInclusive: false, maxInclusive: false });
  const ltp = num(i.current_ltp, "current_ltp", { min: 0.01, max: MAX_PRICE });
  const minInit = num(i.min_initial_margin ?? 0.30, "min_initial_margin", { min: 0, max: 1, minInclusive: false });
  const buffer = num(i.warning_buffer ?? 0.05, "warning_buffer", { min: 0, max: 1 });

  const pool = cash + loan;
  const initRatio = cash / pool;
  const initOk = initRatio >= minInit - 0.000001;

  // Excel: INT(ROUND(pool/price, 8))
  const shares = Math.floor(Number((pool / price).toFixed(8)));
  if (shares < 1) {
    throw new CalculatorInputError(
      "own_cash",
      `cash + loan (Rs ${pool.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}) is less than one share price (Rs ${price.toFixed(2)})`
    );
  }

  const cost = shares * price;
  const unused = pool - cost;

  const trigger = loan / (shares * (1 - m));
  const fall = price - trigger;
  const fallPct = trigger / price - 1;
  const valueAtTrigger = shares * trigger;
  const equityAtTrigger = valueAtTrigger - loan;
  const ratioAtTrigger = valueAtTrigger > 0 ? equityAtTrigger / valueAtTrigger : 0;

  const valueNow = shares * ltp;
  const equityNow = valueNow - loan;
  const ratioNow = equityNow / valueNow;
  const ltv = loan / valueNow;
  const cushion = ltp - trigger;
  const cushionPct = trigger > 0 ? ltp / trigger - 1 : 0;

  let status;
  if (ltp < trigger) status = STATUS_MARGIN_CALL;
  else if (ltp <= trigger * (1 + buffer)) status = STATUS_WARNING;
  else status = STATUS_SAFE;

  const topUp = Math.max(0, loan - (1 - m) * valueNow);
  let sell;
  if (topUp === 0) sell = 0;
  else if (equityNow <= 0) sell = null;
  else sell = Math.ceil((valueNow - equityNow / m) / ltp);

  // lowest price (to the paisa) at which margin is still met; guard float noise before ceiling
  const minSafe = loan > 0 ? Math.ceil(Number((trigger * 100).toFixed(6))) / 100 : 0;

  const notes = [];
  if (loan === 0) notes.push("No broker loan: a margin call cannot occur on this position.");
  if (!initOk) {
    notes.push(`Own cash is ${(initRatio * 100).toFixed(1)}% of buying power, below the required minimum initial margin of ${(minInit * 100).toFixed(1)}%.`);
  }
  if (status === STATUS_MARGIN_CALL) notes.push("Current price is below the trigger price: the maintenance margin is not met.");
  else if (status === STATUS_WARNING) notes.push(`Current price is within ${(buffer * 100).toFixed(1)}% above the trigger price.`);

  return {
    total_buying_power: r2(pool),
    initial_margin_ratio: r4(initRatio),
    initial_margin_check: initOk ? "OK" : "BELOW MINIMUM",
    shares_purchased: shares,
    purchase_value: r2(cost),
    unused_buying_power: r2(unused),
    trigger_price: r2(trigger),
    price_fall_to_trigger: r2(fall),
    price_fall_pct: r4(fallPct * 100),
    portfolio_at_trigger: r2(valueAtTrigger),
    equity_at_trigger: r2(equityAtTrigger),
    equity_ratio_at_trigger: r4(ratioAtTrigger),
    portfolio_at_ltp: r2(valueNow),
    current_equity: r2(equityNow),
    current_equity_ratio: r4(ratioNow),
    loan_to_value: r4(ltv),
    cushion_above_trigger: r2(cushion),
    cushion_above_trigger_pct: r4(cushionPct * 100),
    status,
    cash_top_up: r2(topUp),
    shares_to_sell: sell,
    min_safe_price: r2(minSafe),
    notes,
  };
}

return calculateMarginCall;
})(CalcError, num, roundHalfUp);

E.calculatePivotPoints = (function (CalculatorInputError, num, roundHalfUp) {
/**
 * Pivot Point Calculator (Floor, Woodie, Camarilla, DeMark) - JavaScript port, no dependencies.
 * Same logic, keys (snake_case) and rounding as calculators/pivot_points.py.
 * Spec: documentation/specs/NEPSE_Pivot_Point_Calculator.xlsx
 */


const CALCULATOR_ID = "pivot_points";
const VERSION = "1.0.0";
const GRID_LEVELS = ["R4", "R3", "R2", "R1", "PP", "S1", "S2", "S3", "S4"];

const MAX_PRICE = 100000;
const SHOWN = {
  floor: ["r3", "r2", "r1", "pp", "s1", "s2", "s3"],
  woodie: ["r2", "r1", "pp", "s1", "s2"],
  camarilla: ["r4", "r3", "r2", "r1", "s1", "s2", "s3", "s4"],
  demark: ["r1", "s1"], // DeMark PP is computed but not shown in the website grid
};

/**
 * @param {{high:number|string, low:number|string, open_price:number|string, close_price:number|string}} i
 */
function calculatePivotPoints(i) {
  const h = num(i.high, "high", { min: 0.01, max: MAX_PRICE });
  const l = num(i.low, "low", { min: 0.01, max: MAX_PRICE });
  const o = num(i.open_price, "open_price", { min: 0.01, max: MAX_PRICE });
  const c = num(i.close_price, "close_price", { min: 0.01, max: MAX_PRICE });
  if (h < l) throw new CalculatorInputError("high", "must be at least the low price");
  if (o < l || o > h) throw new CalculatorInputError("open_price", "must be between the low and high price");
  if (c < l || c > h) throw new CalculatorInputError("close_price", "must be between the low and high price");

  const rng = h - l;
  const ppF = (h + l + c) / 3;
  const floor = { pp: ppF, r1: 2 * ppF - l, r2: ppF + rng, r3: h + 2 * (ppF - l), s1: 2 * ppF - h, s2: ppF - rng, s3: l - 2 * (h - ppF) };
  const ppW = (h + l + 2 * c) / 4;
  const woodie = { pp: ppW, r1: 2 * ppW - l, r2: ppW + rng, s1: 2 * ppW - h, s2: ppW - rng };
  const camarilla = {
    r4: c + (rng * 1.1) / 2, r3: c + (rng * 1.1) / 4, r2: c + (rng * 1.1) / 6, r1: c + (rng * 1.1) / 12,
    s1: c - (rng * 1.1) / 12, s2: c - (rng * 1.1) / 6, s3: c - (rng * 1.1) / 4, s4: c - (rng * 1.1) / 2,
  };
  let demarkCase, x;
  if (c < o) { demarkCase = "Close < Open"; x = h + 2 * l + c; }
  else if (c > o) { demarkCase = "Close > Open"; x = 2 * h + l + c; }
  else { demarkCase = "Close = Open"; x = h + l + 2 * c; }
  const demark = { pp: x / 4, r1: x / 2 - l, s1: x / 2 - h };

  const round = (set) => Object.fromEntries(Object.entries(set).map(([k, v]) => [k, roundHalfUp(v, 2)]));
  const out = { floor: round(floor), woodie: round(woodie), camarilla: round(camarilla), demark: round(demark) };
  const grid = GRID_LEVELS.map((level) => {
    const key = level.toLowerCase();
    const row = { level };
    for (const m of ["floor", "woodie", "camarilla", "demark"]) row[m] = SHOWN[m].includes(key) ? out[m][key] : null;
    return row;
  });
  return { ...out, demark_case: demarkCase, demark_x: roundHalfUp(x, 2), range: roundHalfUp(rng, 2), grid };
}

return calculatePivotPoints;
})(CalcError, num, roundHalfUp);

/* ---- UNITS (table from the toolkit; Nepal land units included) ---- */
var UNITS={
 length:{label:"Length",u:{km:["Kilometre",1000],m:["Metre",1],cm:["Centimetre",.01],mm:["Millimetre",.001],mi:["Mile",1609.344],yd:["Yard",.9144],ft:["Foot",.3048],"in":["Inch",.0254],haath:["Haath (NP)",.4572],dhanush:["Dhanush (NP)",1.8288]},def:["km","m"]},
 weight:{label:"Weight",u:{t:["Tonne",1000],kg:["Kilogram",1],g:["Gram",.001],mg:["Milligram",1e-6],lb:["Pound",.45359237],oz:["Ounce",.028349523125],tola:["Tola (NP)",.011664]},def:["t","kg"]},
 area:{label:"Area",u:{km2:["Sq Km",1e6],m2:["Sq Metre",1],ha:["Hectare",1e4],acre:["Acre",4046.8564224],ft2:["Sq Foot",.09290304],ropani:["Ropani (NP)",508.737047],aana:["Aana (NP)",31.79606544],paisa:["Paisa (NP)",7.94901636],dam:["Dam (NP)",1.98725409],bigha:["Bigha (NP)",6772.631616],kattha:["Kattha (NP)",338.6315808],dhur:["Dhur (NP)",16.93157904]},def:["ropani","aana"]},
 temp:{label:"Temp",u:{C:["Celsius"],F:["Fahrenheit"],K:["Kelvin"]},def:["C","F"]},
 speed:{label:"Speed",u:{kmh:["km/h",1/3.6],ms:["m/s",1],mph:["mph",.44704],knot:["Knot",1852/3600]},def:["kmh","ms"]},
 data:{label:"Data",u:{bit:["Bit",.125],B:["Byte",1],KB:["Kilobyte",1024],MB:["Megabyte",1048576],GB:["Gigabyte",1073741824],TB:["Terabyte",1099511627776]},def:["GB","MB"]},
 time:{label:"Time",u:{ms:["Millisecond",.001],s:["Second",1],min:["Minute",60],hr:["Hour",3600],day:["Day",86400],week:["Week",604800],year:["Year (365 d)",31536000]},def:["hr","s"]}
};
E.UNITS = UNITS;
E.convertUnit = function (cat, value, from, to) {
  var C = UNITS[cat]; if (!C || !C.u[from] || !C.u[to]) throw new CalcError('Unit', 'unknown unit');
  var v = num(value, 'Value');
  if (cat === 'temp') {
    var c = from === 'C' ? v : from === 'F' ? (v - 32) * 5 / 9 : v - 273.15;
    return to === 'C' ? c : to === 'F' ? c * 9 / 5 + 32 : c + 273.15;
  }
  return v * C.u[from][1] / C.u[to][1];
};

root.NNH_ENGINES = E;
})(typeof window !== 'undefined' ? window : this);

</script>
<script>
/* ===== CALCULATORS tab UI. Registry-driven: add a calculator by adding one entry to CALCS. ===== */
(function () {
'use strict';
var E = window.NNH_ENGINES, D = window.NNH_DATA;
var host = document.getElementById('calc'), nav = document.getElementById('calc-nav'), body = document.getElementById('calc-body');
if (!E || !D || !host || !nav || !body) return;
var anchor = document.querySelector('nav.vtabs.sub:not(#calc-nav)') || document.querySelector('nav.vtabs');
if (anchor) anchor.after(nav);   // sits under the main tabs, like the Stock Screener tools bar
var esc = D.esc;
var shared = { wacc: null };   // values passed between calculators in this browser session only

/* ---------- formatting helpers ---------- */
var nf2 = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
function rs(n) { if (n == null || !isFinite(n)) return '\u2013'; return (n < 0 ? '-Rs ' : 'Rs ') + nf2.format(Math.abs(n)); }
function n2(n) { return n == null || !isFinite(n) ? '\u2013' : nf2.format(n); }
function pct(n, d) { return n == null || !isFinite(n) ? '\u2013' : n.toFixed(d == null ? 2 : d) + '%'; }
function whole(n) { return D.fmt(n, 0); }
function P(s) { var t = String(s == null ? '' : s).trim().replace(/,/g, ''), n = parseFloat(t); return t !== '' && isFinite(n) ? n / 100 : s; }  /* percent input -> fraction */
function H(label, value, sub) { return '<div class="chd"><small>' + label + '</small><div class="cbig">' + value + '</div>' + (sub ? '<div class="csub">' + sub + '</div>' : '') + '</div>'; }
function S(items) { return '<div class="cs">' + items.map(function (a) { return '<div><small>' + a[0] + '</small><b' + (a[2] ? ' class="' + a[2] + '"' : '') + '>' + a[1] + '</b></div>'; }).join('') + '</div>'; }
function T(head, rows, cls) {
  return '<div class="ct"><table' + (cls ? ' class="' + cls + '"' : '') + '><thead><tr>' + head.map(function (h) { return '<th>' + h + '</th>'; }).join('') + '</tr></thead><tbody>' +
    rows.map(function (r) { return '<tr>' + r.map(function (c) { return '<td>' + c + '</td>'; }).join('') + '</tr>'; }).join('') + '</tbody></table></div>';
}
function KV(rows) { return '<div class="ckv">' + rows.map(function (a) { return '<span>' + a[0] + '</span><span>' + a[1] + '</span>'; }).join('') + '</div>'; }
function B(text, kind) { return '<span class="badge ' + (kind || '') + '">' + text + '</span>'; }
function sec(title, inner) { return '<h4 class="csec">' + title + '</h4>' + inner; }
function sgn(n) { return n > 0 ? 'u' : n < 0 ? 'd' : ''; }
function feeNote() {
  var R = E.RATES, sl = R.brokerage.slabs, prev = 0, lines = sl.map(function (s) {
    var t = s[0] === Infinity ? 'above Rs ' + whole(prev) : (prev ? 'Rs ' + whole(prev) + ' to ' : 'up to ') + (prev ? 'Rs ' : 'Rs ') + whole(s[0]);
    if (!prev && s[0] !== Infinity) t = 'up to Rs ' + whole(s[0]);
    prev = s[0]; return t + ': ' + (s[1] * 100).toFixed(2) + '%';
  });
  return '<details class="cdet"><summary>Fee schedule used</summary><p>Brokerage: ' + lines.join('; ') + '. Minimum brokerage Rs ' + R.brokerage.minFee + ' on transactions up to Rs ' + whole(R.brokerage.minFeeUpTo) +
    '. SEBON fee ' + (R.sebon * 100).toFixed(3) + '%. DP charge Rs ' + R.dp + ' per transaction. Brokers may charge differently; confirm with your broker.</p></details>';
}

function FORM(lines) { return '<div class="cform"><b>How it is calculated</b><ul>' + lines.map(function (l) { return '<li>' + l + '</li>'; }).join('') + '</ul></div>'; }
function WARN(list) { return (list || []).map(function (w) { return '<div class="cwarn" role="note">' + esc(w) + '</div>'; }).join(''); }
function isoDaysAgo(n) { var d = new Date(Date.now() + 345 * 60000 - n * 86400000); return d.getUTCFullYear() + '-' + ('0' + (d.getUTCMonth() + 1)).slice(-2) + '-' + ('0' + d.getUTCDate()).slice(-2); }
function pnl(n) { return '<span class="' + (n > 0 ? 'u' : n < 0 ? 'd' : '') + '">' + rs(n) + '</span>'; }
var DISC = 'Estimates for information only, not investment, tax or legal advice. Confirm current fees and tax rates with SEBON, NEPSE, your broker or the Inland Revenue Department.';

var LABELS = { stock_purchase_price: 'Purchase price', own_cash: 'Own cash', broker_loan: 'Broker loan', maintenance_margin: 'Maintenance margin (use a percentage such as 20)', current_ltp: 'Current price', min_initial_margin: 'Minimum initial margin', warning_buffer: 'Warning buffer', high: 'High', low: 'Low', open_price: 'Open', close_price: 'Close' };
function nice(m) { return String(m).replace(/^([a-z_]+): /, function (all, k) { return (LABELS[k] || k) + ': '; }); }
function copyText(btn) {
  var src = btn.closest('.nwbox').querySelector('.nwtxt'), text = src.textContent, done = function () { var t = btn.textContent; btn.textContent = 'Copied'; setTimeout(function () { btn.textContent = t; }, 1500); };
  function fallback() { var ta = document.createElement('textarea'); ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (e) { /* ignore */ } document.body.removeChild(ta); }
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
}
/* ---------- generic form calculator ---------- */
function fv(f) { return typeof f.value === 'function' ? f.value() : f.value; }
function ensureNum(x) { return x == null || x === '' || !isFinite(x) ? null : +x; }
function formCalc(def) {
  return function (el) {
    var pre = 'c_' + def.id + '_';
    function fieldHtml(f) {
      var id = pre + f.id, val = fv(f), lab = '<label for="' + id + '">' + f.label + (f.hint ? ' <span class="hint">' + f.hint + '</span>' : '') + '</label>', inner;
      if (f.tip) lab = '<div class="lbrow">' + lab + '<button type="button" class="tipbtn" aria-label="What does this mean?" aria-expanded="false">?</button></div><div class="tipbox" hidden>' + f.tip + '</div>';
      if (f.kind === 'sel') inner = lab + '<select id="' + id + '">' + f.opts.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (String(o[0]) === String(val) ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') + '</select>';
      else if (f.kind === 'chk') inner = '<label class="chk"><input id="' + id + '" type="checkbox"' + (val ? ' checked' : '') + '> ' + f.label + '</label>';
      else if (f.kind === 'txt') inner = lab + '<input id="' + id + '" type="text" inputmode="decimal" autocomplete="off" spellcheck="false" placeholder="' + (f.placeholder || '') + '" value="' + (val == null ? '' : val) + '">';
      else if (f.kind === 'date') inner = lab + '<input id="' + id + '" type="date" value="' + (val || '') + '">';
      else if (f.kind === 'sym') inner = lab + '<input id="' + id + '" type="text" list="syms" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="e.g. NABIL">';
      else inner = lab + '<input id="' + id + '" type="number" inputmode="decimal" step="' + (f.step || 'any') + '"' + (f.min != null ? ' min="' + f.min + '"' : '') + (f.max != null ? ' max="' + f.max + '"' : '') + ' value="' + (val == null ? '' : val) + '">';
      return '<div class="fld" data-f="' + f.id + '"' + (f.showIf ? ' data-show="' + f.showIf + '"' : '') + (f.showWhen ? ' data-showwhen=\'' + JSON.stringify(f.showWhen) + '\'' : '') + '>' + inner + (f.extra ? '<div class="fx" id="' + pre + f.id + '_x"></div>' : '') + '</div>';
    }
    var main = def.fields.filter(function (f) { return !f.adv; }), adv = def.fields.filter(function (f) { return f.adv; });
    el.innerHTML = '<div class="chead"><h2>' + def.name + '</h2><p>' + def.blurb + '</p></div>' +
      (def.warn ? '<div class="cwarn" role="note">' + def.warn + '</div>' : '') +
      '<div class="cgrid"><section class="cc" aria-label="Inputs"><h3>Inputs</h3>' +
      (def.examples ? '<div class="chips" role="group" aria-label="Load an example">' + def.examples.map(function (x, k) { return '<button type="button" class="chip" data-ex="' + k + '">' + x.label + '</button>'; }).join('') + '</div>' : '') +
      main.map(fieldHtml).join('') +
      (adv.length ? '<details class="cdet"><summary>' + (def.advTitle || 'Advanced') + '</summary>' + adv.map(fieldHtml).join('') + '</details>' : '') +
      '<div class="symmsg" id="' + pre + 'msg" role="status"></div>' +
      '<div class="cact"><button type="button" class="cbtn primary" data-act="calc">Calculate</button><button type="button" class="cbtn" data-act="reset">Reset</button></div></section>' +
      '<section class="cc" aria-label="Result" aria-live="polite"><h3>Result</h3><div id="' + pre + 'out"></div></section></div>' +
      (def.notes ? '<div class="cnote">' + (typeof def.notes === 'function' ? def.notes() : def.notes) + '</div>' : '') + '<p class="cdisc">' + DISC + '</p>';
    var out = el.querySelector('#' + pre + 'out'), msg = el.querySelector('#' + pre + 'msg');
    function $f(id) { return el.querySelector('#' + pre + id); }
    function read() {
      var v = {}; def.fields.forEach(function (f) { var n = $f(f.id); v[f.id] = f.kind === 'chk' ? n.checked : n.value; }); return v;
    }
    function write(vals) { Object.keys(vals).forEach(function (k) { var n = $f(k); if (!n) return; if (n.type === 'checkbox') n.checked = !!vals[k]; else n.value = vals[k]; }); }
    function vis() {
      el.querySelectorAll('[data-show]').forEach(function (d) { d.hidden = !$f(d.getAttribute('data-show')).checked; });
      el.querySelectorAll('[data-showwhen]').forEach(function (d) { var w = JSON.parse(d.getAttribute('data-showwhen')); d.hidden = w['in'].indexOf($f(w.id).value) < 0; });
    }
    function run() {
      vis();
      try { out.innerHTML = def.run(read()); }
      catch (e) {
        if (e && e.name === 'CalculatorInputError') out.innerHTML = e.blank ? '<div class="cwarn" role="alert"><b>Enter required data:</b> ' + esc(nice(e.field)) + '</div>' : '<div class="cerr" role="alert">Please check: ' + esc(nice(e.message)) + '</div>';
        else { if (window.console) console.error('calculator ' + def.id, e); out.innerHTML = '<div class="cerr" role="alert">This input could not be calculated. Please review the values.</div>'; }
      }
    }
    function defaults() { var o = {}; def.fields.forEach(function (f) { o[f.id] = f.kind === 'sym' ? '' : fv(f); }); return o; }
    var api = { set: function (id, val) { var n = $f(id); if (n) n.value = val; }, run: run, el: el, $f: $f, read: read };
    el.addEventListener('input', function (e) { if (e.target.id === pre + 'sym' && def.fill) trySym(true); run(); });
    el.addEventListener('change', function (e) { if (e.target.id === pre + 'sym' && def.fill) trySym(false); run(); });
    el.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.dataset.copy != null) { copyText(b); return; }
      if (b.classList.contains('tipbtn')) { var tb = b.closest('.fld').querySelector('.tipbox'); tb.hidden = !tb.hidden; b.setAttribute('aria-expanded', !tb.hidden); return; }
      if (b.dataset.act === 'calc') { run(); return; }
      if (b.dataset.ex != null) { write(def.examples[+b.dataset.ex].values); msg.textContent = ''; run(); }
      else if (b.dataset.act === 'reset') { write(defaults()); msg.textContent = ''; run(); }
      else if (def.onClick && b.dataset.act) { def.onClick(b.dataset.act, api); }
    });
    function trySym(quiet) {
      var s = ($f('sym').value || '').trim(), r = s ? D.find(s) : null;
      if (!r) { msg.textContent = (s && !quiet) ? 'Company not found in the screener data. Enter the values manually.' : ''; return; }
      var vals = def.fill(r), o = {};
      Object.keys(vals).forEach(function (k) { var x = ensureNum(vals[k]); if (x != null) o[k] = Math.round(x * 100) / 100; });
      if (!Object.keys(o).length) { msg.textContent = 'No price data for this company in the screener. Enter the values manually.'; return; }
      var names = Object.keys(o).map(function (k) { var f = def.fields.filter(function (x) { return x.id === k; })[0]; return f ? String(f.label).replace(/<[^>]*>/g, '').replace(/\s*\(.*$/, '').toLowerCase() : k; });
      write(o); msg.textContent = (def.fillMsg ? def.fillMsg(r, o) : 'Loaded ' + names.join(', ')) + ' (screener data' + (D.date ? ' as of ' + esc(D.date) : '') + '). You can edit any value.';
    }
    if (def.after) def.after(api);
    run();
  };
}
var SYMF = { id: 'sym', kind: 'sym', label: 'Company <span class="hint">optional, fills the latest price</span>' };
var fillLtp = function (map) { return function (r) { var o = {}, p = D.g(r, 'LTP'); Object.keys(map).forEach(function (k) { o[k] = p; }); return o; }; };

/* ---------- calculator definitions ---------- */
var CALCS = [];
function reg(group, def, extra) { def.group = group; CALCS.push(Object.assign({ id: def.id, name: def.name, blurb: def.blurb, group: group, badge: def.badge || '', render: formCalc(def) }, extra || {})); }

/* BUY */
reg('trade', {
  id: 'buy', name: 'Buy Cost', blurb: 'Total cost of buying shares on NEPSE: brokerage, SEBON fee and DP charge, with the effective cost per share.',
  fields: [SYMF,
    { id: 'price', label: 'Buy price (Rs per share)', value: 500, step: '0.01' },
    { id: 'qty', label: 'Quantity (kitta)', value: 100, step: '1' },
    { id: 'inv', kind: 'sel', label: 'Investor type', value: 'Individual', opts: [['Individual', 'Individual'], ['Institutional', 'Institutional'], ['Government', 'Government']] }],
  examples: [{ label: '100 @ Rs 500', values: { price: 500, qty: 100 } }, { label: '5,000 @ Rs 1,200', values: { price: 1200, qty: 5000 } }],
  fill: fillLtp({ price: 1 }),
  run: function (v) {
    var r = E.calculateBuy({ stock_price: v.price, quantity: v.qty, investor_type: v.inv });
    return H('Total purchase cost', rs(r.total_purchase_cost), 'Effective cost per share: <b>' + rs(r.effective_cost_per_share) + '</b>') +
      S([['Gross value', rs(r.gross_transaction_value)], ['Brokerage (' + r.brokerage_rate + '%)', rs(r.brokerage_charge)], ['SEBON fee', rs(r.sebon_fee)], ['DP charge', rs(r.dp_charge)]]);
  },
  notes: function () { return feeNote() + '<p>The effective cost per share is the break-even starting point; selling costs and capital gains tax come on top (see Sell &amp; CGT).</p>'; }
});

/* SELL */
reg('trade', {
  id: 'sell', name: 'Sell &amp; CGT', blurb: 'Net cash from selling shares after brokerage, SEBON fee, DP charge and capital gains tax, with profit after tax.',
  badge: 'Confirm CGT',
  warn: '<b>Check the CGT rate.</b> The capital gains tax rates below are editable defaults and may not reflect the latest budget or Finance Act changes. Confirm the rate that applies to you before relying on this result.',
  fields: [SYMF,
    { id: 'price', label: 'Sell price (Rs per share)', value: 600, step: '0.01' },
    { id: 'qty', label: 'Quantity (kitta)', value: 100, step: '1' },
    { id: 'cost', label: 'Purchase cost per share (Rs)', hint: 'WACC', value: 500, step: '0.01', extra: true },
    { id: 'inv', kind: 'sel', label: 'Investor type', value: 'Individual', opts: [['Individual', 'Individual'], ['Institutional', 'Institutional']] },
    { id: 'hold', kind: 'sel', label: 'Holding period', value: '\u2264 365 Days', opts: [['\u2264 365 Days', '365 days or less'], ['> 365 Days', 'More than 365 days']] },
    { id: 'dp', kind: 'chk', label: 'DP charge applies', value: true },
    { id: 'cs', label: 'CGT: individual, up to 365 days (%)', value: function () { return E.RATES.cgt.individual_short * 100; }, step: 'any', adv: true },
    { id: 'cl', label: 'CGT: individual, over 365 days (%)', value: function () { return E.RATES.cgt.individual_long * 100; }, step: 'any', adv: true },
    { id: 'ci', label: 'CGT: institutional (%)', value: function () { return E.RATES.cgt.institutional * 100; }, step: 'any', adv: true }],
  advTitle: 'Capital gains tax rates (editable)',
  examples: [{ label: '100: Rs 500 \u2192 Rs 600', values: { price: 600, qty: 100, cost: 500 } }, { label: 'Loss example', values: { price: 450, qty: 100, cost: 500 } }],
  fill: fillLtp({ price: 1 }),
  after: function (api) {
    var x = api.el.querySelector('#c_sell_cost_x');
    function paint() { var key = String(shared.wacc); if (x.dataset.w === key) return; x.dataset.w = key; x.innerHTML = shared.wacc ? '<button type="button" class="cbtn small" data-act="usewacc">Use WACC from Averaging (Rs ' + n2(shared.wacc) + ')</button>' : ''; }
    paint(); api.el.addEventListener('focusin', paint);
  },
  onClick: function (act, api) { if (act === 'usewacc' && shared.wacc) { api.set('cost', shared.wacc); api.run(); } },
  run: function (v) {
    var cg = { individual_short: P(v.cs), individual_long: P(v.cl), institutional: P(v.ci) };
    ['individual_short', 'individual_long', 'institutional'].forEach(function (k) { E.num(cg[k], 'CGT rate', { min: 0, max: 1 }); });
    var r = E.calculateSell({ sell_price: v.price, quantity: v.qty, purchase_cost_per_share: v.cost, investor_type: v.inv, holding_period: v.hold, dp_applicable: v.dp }, cg);
    return H('Net cash received', rs(r.net_cash_received), 'Net profit after CGT: <b class="' + sgn(r.net_profit_after_cgt) + '">' + rs(r.net_profit_after_cgt) + '</b>') +
      S([['Gross sales value', rs(r.gross_sales_value)], ['Purchase cost', rs(r.purchase_cost)], ['Broker commission (' + r.brokerage_rate + '%)', rs(r.broker_commission)], ['SEBON fee', rs(r.sebon_fee)],
        ['DP charge', rs(r.dp_charge)], ['Total selling fees', rs(r.total_selling_fees)], ['Profit before CGT', rs(r.net_profit_before_cgt), sgn(r.net_profit_before_cgt)], ['CGT (' + r.cgt_rate + '%)', rs(r.capital_gains_tax)]]);
  },
  notes: function () { return feeNote() + '<p>CGT is applied only when profit after selling fees is positive.</p>'; }
});

/* AVERAGING (custom UI) */
CALCS.push({ id: 'averaging', group: 'trade', name: 'Averaging (WACC)', badge: '',
  blurb: 'Weighted average cost per share across several purchases: secondary market, IPO and bonus batches.',
  render: function (el) {
    var rows = [['Secondary', 1000, 500], ['Secondary', 1000, 450], ['Secondary', 800, 420]];
    function draw() {
      el.querySelector('#av_rows').innerHTML = rows.map(function (r, k) {
        return '<tr><td>' + (k + 1) + '</td><td><select data-k="' + k + '" data-c="0" aria-label="Batch ' + (k + 1) + ' type">' + ['Secondary', 'IPO', 'Bonus'].map(function (t) { return '<option' + (t === r[0] ? ' selected' : '') + '>' + t + '</option>'; }).join('') + '</select></td>' +
          '<td><input type="number" inputmode="decimal" step="1" data-k="' + k + '" data-c="1" value="' + r[1] + '" aria-label="Batch ' + (k + 1) + ' quantity"></td>' +
          '<td><input type="number" inputmode="decimal" step="0.01" data-k="' + k + '" data-c="2" value="' + r[2] + '" aria-label="Batch ' + (k + 1) + ' price"></td>' +
          '<td><button type="button" class="cbtn small" data-rm="' + k + '" aria-label="Remove batch ' + (k + 1) + '">&times;</button></td></tr>';
      }).join('');
    }
    el.innerHTML = '<div class="chead"><h2>Averaging (WACC)</h2><p>Weighted average cost per share across several purchases: secondary market, IPO and bonus batches.</p></div>' +
      '<div class="cgrid"><section class="cc" aria-label="Batches"><h3>Purchase batches</h3><div class="ct"><table class="avt"><thead><tr><th>#</th><th>Type</th><th>Quantity</th><th>Price (Rs)</th><th></th></tr></thead><tbody id="av_rows"></tbody></table></div>' +
      '<div class="cact"><button type="button" class="cbtn" id="av_add">+ Add batch</button><button type="button" class="cbtn" id="av_reset">Reset</button></div></section>' +
      '<section class="cc" aria-live="polite"><h3>Result</h3><div id="av_out"></div></section></div>' +
      '<div class="cnote">' + feeNote() + '<p>Secondary-market batches include broker commission and SEBON fee. IPO and bonus batches carry no fees. A bonus batch adds shares at zero cost, which lowers the average. Enter bonus batches with price 0.</p></div><p class="cdisc">' + DISC + '</p>';
    var out = el.querySelector('#av_out');
    function run() {
      try {
        var used = rows.filter(function (x) { return !(String(x[1]).trim() === '' && String(x[2]).trim() === ''); });
        if (!used.length) { shared.wacc = null; out.innerHTML = '<p class="cmu">Enter at least one batch.</p>'; return; }
        var r = E.calculateWacc(used.map(function (x, k) { return { batch_no: k + 1, type: x[0], quantity: x[1], purchase_price: x[2] }; }));
        shared.wacc = r.wacc > 0 ? r.wacc : null;
        out.innerHTML = H('Weighted average cost per share', rs(r.wacc), 'Total quantity ' + whole(r.total_quantity) + ' kitta &middot; total cost ' + rs(r.total_cost)) +
          T(['#', 'Type', 'Qty', 'Gross', 'Broker', 'SEBON', 'Total cost'], r.batches.map(function (b) {
            return [b.batch_no, b.type, whole(b.quantity), n2(b.gross_value), n2(b.broker_fee), n2(b.sebon_fee), b.counted ? n2(b.total_investment_cost) : '<span class="cmu">not counted</span>'];
          })) + (r.wacc > 0 ? '<p class="cnote">This WACC is available in <b>Sell &amp; CGT</b> via the &ldquo;Use WACC&rdquo; button.</p>' : '');
      } catch (e) { out.innerHTML = '<div class="cerr" role="alert">Please check: ' + esc(e.message) + '</div>'; }
    }
    el.addEventListener('input', function (e) { var t = e.target; if (t.dataset.k == null) return; var c = +t.dataset.c; rows[+t.dataset.k][c] = c === 0 ? t.value : t.value; run(); });
    el.addEventListener('change', function (e) { var t = e.target; if (t.dataset.k == null) return; if (+t.dataset.c === 0) { rows[+t.dataset.k][0] = t.value; run(); } });
    el.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.id === 'av_add') { if (rows.length < 20) { rows.push(['Secondary', '', '']); draw(); run(); } }
      else if (b.id === 'av_reset') { rows = [['Secondary', 1000, 500], ['Secondary', 1000, 450], ['Secondary', 800, 420]]; draw(); run(); }
      else if (b.dataset.rm != null && rows.length > 1) { rows.splice(+b.dataset.rm, 1); draw(); run(); }
    });
    draw(); run();
  } });

/* MARGIN CALL */
reg('trade', {
  id: 'margin', name: 'Margin Call', blurb: 'Trigger price, cushion and the cash or shares needed to meet maintenance margin on a margin-financed position.',
  fields: [SYMF,
    { id: 'price', label: 'Purchase price (Rs per share)', value: 500, step: '0.01' },
    { id: 'cash', label: 'Own cash (Rs)', value: 300000, step: 'any' },
    { id: 'loan', label: 'Broker loan (Rs)', value: 700000, step: 'any' },
    { id: 'mm', label: 'Maintenance margin (%)', value: 20, step: 'any' },
    { id: 'ltp', label: 'Current price, LTP (Rs)', value: 480, step: '0.01' },
    { id: 'mi', label: 'Minimum initial margin (%)', value: 30, step: 'any', adv: true },
    { id: 'wb', label: 'Warning buffer above trigger (%)', value: 5, step: 'any', adv: true }],
  advTitle: 'Margin rules (editable)',
  examples: [{ label: 'Rs 500 stock, above trigger', values: { price: 500, cash: 300000, loan: 700000, mm: 20, ltp: 480 } },
    { label: 'Hydro Rs 700, near trigger', values: { price: 700, cash: 600000, loan: 1400000, mm: 20, ltp: 625 } },
    { label: 'Bank Rs 350, below trigger', values: { price: 350, cash: 450000, loan: 1050000, mm: 20, ltp: 295 } }],
  fill: fillLtp({ price: 1, ltp: 1 }),
  run: function (v) {
    var r = E.calculateMarginCall({ stock_purchase_price: v.price, own_cash: v.cash, broker_loan: v.loan, maintenance_margin: P(v.mm), current_ltp: v.ltp, min_initial_margin: P(v.mi), warning_buffer: P(v.wb) });
    var k = r.status === 'SAFE' ? 'ok' : r.status === 'WARNING' ? 'wa' : 'bad';
    return '<div class="chd"><small>Status</small><div class="cbig">' + B(r.status, k) + '</div><div class="csub">Trigger price <b>' + rs(r.trigger_price) + '</b> &middot; current price ' + rs(+v.ltp) + ' &middot; cushion <b class="' + sgn(r.cushion_above_trigger) + '">' + rs(r.cushion_above_trigger) + ' (' + pct(r.cushion_above_trigger_pct) + ')</b></div></div>' +
      sec('Position', S([['Buying power', rs(r.total_buying_power)], ['Shares purchased', whole(r.shares_purchased)], ['Purchase value', rs(r.purchase_value)], ['Unused buying power', rs(r.unused_buying_power)], ['Initial margin', pct(r.initial_margin_ratio * 100) + ' ' + (r.initial_margin_check === 'OK' ? B('OK', 'ok') : B('Below minimum', 'bad'))]])) +
      sec('At the trigger price', S([['Trigger price', rs(r.trigger_price)], ['Fall from purchase price', rs(r.price_fall_to_trigger) + ' (' + pct(r.price_fall_pct) + ')'], ['Portfolio value', rs(r.portfolio_at_trigger)], ['Equity ratio', pct(r.equity_ratio_at_trigger * 100)]])) +
      sec('At the current price', S([['Portfolio value', rs(r.portfolio_at_ltp)], ['Equity', rs(r.current_equity), sgn(r.current_equity)], ['Equity ratio', pct(r.current_equity_ratio * 100)], ['Loan to value', pct(r.loan_to_value * 100)]])) +
      sec('To restore maintenance margin', S([['Cash top-up needed', rs(r.cash_top_up)], ['Or shares to sell', r.shares_to_sell == null ? 'Sell all' : whole(r.shares_to_sell)], ['Lowest safe price', rs(r.min_safe_price)]])) +
      (r.notes.length ? '<ul class="cnotes">' + r.notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>' : '');
  },
  notes: '<p>The margin rules (minimum initial margin 30%, maintenance margin 20%) are editable defaults. Rules and broker policies can differ and change, so confirm them with your broker.</p>'
});

/* BONUS */
reg('corp', {
  id: 'bonus', name: 'Bonus Price Adjustment', blurb: 'Adjusted price and new quantity after bonus shares, with a check that portfolio value is unchanged.',
  fields: [SYMF, { id: 'price', label: 'Pre-book-closure price (Rs)', value: 500, step: '0.01' }, { id: 'b', label: 'Bonus (%)', value: 20, step: 'any' }, { id: 'qty', label: 'Shares held (kitta)', value: 100, step: '1' }],
  examples: [{ label: '20% bonus on 100 @ Rs 500', values: { price: 500, b: 20, qty: 100 } }, { label: '15% bonus on 100', values: { price: 800, b: 15, qty: 100 } }],
  fill: fillLtp({ price: 1 }),
  run: function (v) {
    var r = E.calculateBonus({ market_price: v.price, bonus_percentage: P(v.b), quantity: v.qty });
    return H('Adjusted price', rs(r.adjusted_price), 'New quantity <b>' + whole(r.new_quantity) + '</b> kitta (+' + whole(r.new_quantity - r.pre_quantity) + ' bonus)') +
      S([['Price before', rs(r.pre_market_price)], ['Quantity before', whole(r.pre_quantity)], ['Value before', rs(r.pre_portfolio_value)], ['Value after', rs(r.post_portfolio_value)], ['Value check', r.value_verified ? B('Unchanged', 'ok') : B('Differs', 'wa')]]);
  },
  notes: '<p>Adjusted price = price &divide; (1 + bonus %). The quantity shown is whole shares only; fractional entitlements are not included.</p>'
});

/* RIGHT */
reg('corp', {
  id: 'right', name: 'Right Price Adjustment', blurb: 'Ex-right adjusted price, extra cash needed and new holding after a right issue.',
  fields: [SYMF, { id: 'units', label: 'Shares held (kitta)', value: 1000, step: '1' }, { id: 'price', label: 'Pre-closure price (Rs)', value: 600, step: '0.01' },
    { id: 'rr', label: 'Right ratio (%)', hint: '50 means 1 right share for every 2 held', value: 50, step: 'any' }, { id: 'sp', label: 'Subscription price (Rs per right share)', value: 100, step: '0.01' }],
  examples: [{ label: '50% right @ Rs 100', values: { units: 1000, price: 600, rr: 50, sp: 100 } }, { label: '25% right @ Rs 100', values: { units: 400, price: 450, rr: 25, sp: 100 } }],
  fill: fillLtp({ price: 1 }),
  run: function (v) {
    var r = E.calculateRight({ initial_units: v.units, market_price: v.price, right_ratio: P(v.rr), subscription_price: v.sp });
    return H('Adjusted ex-right price', rs(r.adjusted_price), 'Total shares after right: <b>' + n2(r.total_post_shares) + '</b>') +
      S([['Right shares allotted', n2(r.right_shares_allotted)], ['Additional cash needed', rs(r.additional_cash)], ['Wealth before', rs(r.pre_adjustment_wealth)], ['Total cost basis', rs(r.total_cost_basis)], ['Wealth after', rs(r.post_adjustment_wealth)], ['Value check', r.wealth_verified ? B('Consistent', 'ok') : B('Differs', 'wa')]]);
  },
  notes: '<p>Adjusted price = (price + right ratio &times; subscription price) &divide; (1 + right ratio). Allotment is shown unrounded; the actual allotment follows the company&rsquo;s rounding rules.</p>'
});

/* DIVIDEND */
reg('corp', {
  id: 'dividend', name: 'Dividend &amp; TDS', blurb: 'Bonus and cash dividend, tax on both, cash credited and any tax shortfall you must pay.',
  fields: [SYMF, { id: 'units', label: 'Shares held (kitta)', value: 1000, step: '1' }, { id: 'b', label: 'Bonus dividend (%)', value: 15, step: 'any' }, { id: 'c', label: 'Cash dividend (%)', value: 5.7, step: 'any' },
    { id: 'fv', label: 'Face value (Rs)', value: 100, step: 'any', adv: true }, { id: 't', label: 'Tax rate on dividend (%)', value: function () { return E.RATES.tds * 100; }, step: 'any', adv: true }],
  advTitle: 'Face value and tax rate (editable)',
  examples: [{ label: '15% bonus + 5.7% cash', values: { b: 15, c: 5.7, units: 1000 } }, { label: 'Tax shortfall case', values: { b: 20, c: 1, units: 1000 } }],
  fill: function (r) { return { b: D.g(r, 'Bonus'), c: D.g(r, 'Cash') }; },
  fillMsg: function (r) { var fy = D.g(r, 'Div Fiscal Year'); return 'Loaded bonus and cash dividend %' + (fy ? ' for ' + esc(fy) : '') + '; verify against the company notice'; },
  run: function (v) {
    var r = E.calculateDividend({ holding_units: v.units, bonus_percentage: P(v.b), cash_dividend_percentage: P(v.c), face_value: v.fv, tds_rate: P(v.t) });
    return '<div class="chd"><small>Result</small><div class="cbig">' + B(r.status === 'TAX COVERED' ? 'Tax covered' : 'Tax shortfall to pay', r.status === 'TAX COVERED' ? 'ok' : 'bad') + '</div><div class="csub">Net cash credited <b>' + rs(r.net_cash_credited) + '</b>' + (r.out_of_pocket_due > 0 ? ' &middot; pay <b class="d">' + rs(r.out_of_pocket_due) + '</b> from your pocket' : '') + '</div></div>' +
      sec('Bonus shares', S([['Entitled', n2(r.bonus_shares_entitled)], ['Credited (whole shares)', whole(r.bonus_shares_credited)], ['Fractional', r.fractional_bonus.toFixed(4)], ['Taxable value of bonus', rs(r.bonus_imputed_value)]])) +
      sec('Cash and tax', S([['Gross cash dividend', rs(r.gross_cash_dividend)], ['Taxable base', rs(r.taxable_income_base)], ['Total tax (' + pct(r.tds_rate * 100) + ')', rs(r.total_tds_payable)], ['Tax on cash', rs(r.tds_on_cash)], ['Tax on bonus', rs(r.tds_on_bonus)]])) +
      sec('Outcome', S([['Cash after tax', rs(r.cash_after_tds), sgn(r.cash_after_tds)], ['Net cash credited', rs(r.net_cash_credited)], ['Out-of-pocket tax due', rs(r.out_of_pocket_due)], ['Net dividend value after tax', rs(r.net_dividend_value_after_tax)], ['Cash dividend needed to cover tax', pct(r.min_cash_dividend_to_cover_tax * 100, 3)]]));
  },
  notes: '<p>Bonus shares and cash dividend are added together as the taxable dividend. The tax rate is an editable default; confirm the rate that applies to you with the Inland Revenue Department or the company.</p>'
});

/* PIVOT */
reg('tech', {
  id: 'pivot', name: 'Pivot Points', blurb: 'Floor, Woodie, Camarilla and DeMark support and resistance levels from a day\u2019s high, low, open and close.',
  fields: [SYMF, { id: 'h', label: 'High (Rs)', value: 480, step: '0.01' }, { id: 'l', label: 'Low (Rs)', value: 450, step: '0.01' }, { id: 'o', label: 'Open (Rs)', value: 477, step: '0.01' }, { id: 'c', label: 'Close (Rs)', value: 470, step: '0.01' }],
  examples: [{ label: 'Close < Open', values: { h: 480, l: 450, o: 477, c: 470 } }, { label: 'Close > Open', values: { h: 520, l: 495, o: 500, c: 515 } }, { label: 'Close = Open', values: { h: 312, l: 298, o: 305, c: 305 } }],
  fill: function (r) { return { h: D.g(r, 'Day High'), l: D.g(r, 'Day Low'), o: D.g(r, 'Open'), c: D.g(r, 'LTP') }; },
  fillMsg: function () { return 'Loaded the latest day high, low and price; check the open value'; },
  run: function (v) {
    var r = E.calculatePivotPoints({ high: v.h, low: v.l, open_price: v.o, close_price: v.c });
    var cell = function (x) { return x == null ? '<span class="cmu">\u2013</span>' : n2(x); };
    return H('Pivot (Floor)', rs(r.floor.pp), 'Range ' + rs(r.range) + ' &middot; DeMark case: ' + r.demark_case) +
      T(['Level', 'Floor', 'Woodie', 'Camarilla', 'DeMark'], r.grid.map(function (g) { return ['<b>' + g.level + '</b>', cell(g.floor), cell(g.woodie), cell(g.camarilla), cell(g.demark)]; }), 'piv');
  },
  notes: '<p>Levels are calculated from the previous session\u2019s prices and are a technical-analysis aid, not a prediction or recommendation. R = resistance, S = support, PP = pivot point.</p>'
});

/* CAGR */
reg('growth', {
  id: 'cagr', name: 'CAGR', blurb: 'Compound annual growth rate of an investment, with absolute return and time to double.',
  fields: [{ id: 'a', label: 'Initial investment (Rs)', value: 100000, step: 'any' }, { id: 'f', label: 'Final value (Rs)', value: 250000, step: 'any' }, { id: 'y', label: 'Duration (years)', value: 5, step: 'any' }],
  examples: [{ label: 'Rs 1 lakh \u2192 2.5 lakh in 5 yrs', values: { a: 100000, f: 250000, y: 5 } }, { label: 'Loss-making', values: { a: 100000, f: 80000, y: 3 } }],
  run: function (v) {
    var r = E.calculateCagr({ initial_investment: v.a, final_value: v.f, duration_years: v.y });
    var na = function (x, suf) { return typeof x === 'number' ? n2(x) + (suf || '') : 'N/A'; };
    return H('CAGR', '<span class="' + sgn(r.cagr_pct) + '">' + pct(r.cagr_pct) + '</span>', 'Absolute return ' + pct(r.absolute_return_pct) + ' &middot; growth multiple ' + n2(r.growth_multiple) + '&times;') +
      S([['Profit / loss', rs(r.profit), sgn(r.profit)], ['Simple annual return', pct(r.simple_annual_return_pct)], ['Years to double at this CAGR', na(r.years_to_double, ' yrs')], ['Rule of 72', na(r.rule_of_72, ' yrs')], ['CAGR needed to double in this time', pct(r.cagr_to_double_pct)]]);
  },
  notes: '<p>CAGR = (final value &divide; initial value)<sup>1 &divide; years</sup> &minus; 1. It assumes no additional deposits or withdrawals in between; use XIRR for irregular cash flows.</p>'
});

/* EMI */
reg('growth', {
  id: 'emi', name: 'Loan EMI', blurb: 'Monthly instalment, total interest and repayment schedule for a loan.',
  fields: [{ id: 'p', label: 'Loan amount (Rs)', value: 2500000, step: 'any' }, { id: 'r', label: 'Interest rate (% per year)', value: 10.5, step: 'any' }, { id: 'y', label: 'Tenure (years)', value: 15, step: 'any' },
    { id: 'm', kind: 'sel', label: 'Interest method', value: 'reducing', opts: [['reducing', 'Reducing balance (standard EMI)'], ['flat', 'Flat rate']] }],
  examples: [{ label: 'Rs 25 lakh, 15 yrs, 10.5%', values: { p: 2500000, r: 10.5, y: 15, m: 'reducing' } }, { label: 'Rs 5 lakh, 3 yrs, 12%', values: { p: 500000, r: 12, y: 3, m: 'reducing' } }],
  run: function (v) {
    var r = E.calculateEmi({ principal: v.p, annual_interest_rate: P(v.r), tenure_years: v.y, method: v.m }), yrs = [], k, cur;
    r.schedule.forEach(function (s) { k = Math.ceil(s.month / 12); if (!yrs[k - 1]) yrs[k - 1] = { i: 0, p: 0, b: 0 }; cur = yrs[k - 1]; cur.i += s.interest; cur.p += s.principal; cur.b = s.balance; });
    return H('Monthly EMI', rs(r.monthly_emi), r.num_installments + ' monthly instalments') + S([['Total interest', rs(r.total_interest)], ['Total repayment', rs(r.total_repayment)]]) +
      sec('Year-wise summary', T(['Year', 'Principal paid', 'Interest paid', 'Closing balance'], yrs.map(function (y, i) { return [i + 1, n2(y.p), n2(y.i), n2(y.b)]; }))) +
      '<details class="cdet"><summary>Month-by-month schedule</summary>' + T(['Month', 'Interest', 'Principal', 'Balance'], r.schedule.map(function (s) { return [s.month, n2(s.interest), n2(s.principal), n2(s.balance)]; })) + '</details>';
  },
  notes: '<p>Banks and finance companies quote rates in different ways. Use the method stated in your loan offer. Fees, insurance and rate resets are not included.</p>'
});

/* SIP */
function sipChart(rows) {
  var W = 640, H_ = 220, L = 8, Bm = 24, T_ = 8, n = rows.length, max = rows[n - 1].bal, bw = (W - L * 2) / n, g = Math.min(6, bw * 0.25), s = '';
  rows.forEach(function (r, k) {
    var x = L + k * bw + g / 2, w = bw - g, hi = (r.inv / max) * (H_ - Bm - T_), hg = Math.max((r.gain / max) * (H_ - Bm - T_), 0);
    s += '<rect x="' + x.toFixed(1) + '" y="' + (H_ - Bm - hi).toFixed(1) + '" width="' + w.toFixed(1) + '" height="' + hi.toFixed(1) + '" style="fill:var(--ac)"/>';
    s += '<rect x="' + x.toFixed(1) + '" y="' + (H_ - Bm - hi - hg).toFixed(1) + '" width="' + w.toFixed(1) + '" height="' + hg.toFixed(1) + '" style="fill:var(--ok)"/>';
    if (n <= 15 || r.y % 5 === 0 || k === n - 1) s += '<text x="' + (x + w / 2).toFixed(1) + '" y="' + (H_ - 8) + '" text-anchor="middle" font-size="11">' + r.y + '</text>';
  });
  s += '<line x1="' + L + '" x2="' + (W - L) + '" y1="' + (H_ - Bm) + '" y2="' + (H_ - Bm) + '"/>';
  return '<svg class="chart" viewBox="0 0 ' + W + ' ' + H_ + '" role="img" aria-label="Yearly growth of invested amount and gain">' + s + '</svg><div class="legend"><span><i class="sw" style="background:var(--ac)"></i>Invested</span><span><i class="sw" style="background:var(--ok)"></i>Gain</span></div>';
}
reg('growth', {
  id: 'sip', name: 'SIP (Mutual Fund)', blurb: 'Estimated future value of regular investments in a mutual fund, with optional yearly step-up and tax on gains.',
  fields: [{ id: 'f', kind: 'sel', label: 'Investment frequency', value: 12, opts: [[12, 'Monthly'], [4, 'Quarterly'], [2, 'Semi-annually'], [1, 'Annually']] },
    { id: 'a', label: 'Amount per instalment (Rs)', value: 5000, step: 'any' }, { id: 'r', label: 'Expected return (% a year)', value: 12, step: 'any' }, { id: 'y', label: 'Duration (years)', value: 10, step: '1' },
    { id: 's', label: 'Yearly step-up (%)', hint: 'optional', value: 0, step: 'any' }, { id: 'tx', kind: 'chk', label: 'Show estimated tax on gains', value: false },
    { id: 'tr', label: 'Tax rate on gains (%)', hint: 'enter the rate that applies to you', value: 5, step: 'any', showIf: 'tx' }],
  examples: [{ label: 'Rs 2,000 / 2 yrs', values: { a: 2000, r: 12, y: 2, f: 12, s: 0 } }, { label: 'Rs 1,000 / 10 yrs', values: { a: 1000, r: 12, y: 10, f: 12, s: 0 } }, { label: 'Rs 10,000 + 10% step-up', values: { a: 10000, r: 12, y: 10, f: 12, s: 10 } }],
  run: function (v) {
    var r = E.calculateSip({ amount: v.a, rate: v.r, years: v.y, step: v.s, freq: v.f }), tax = '';
    if (v.tx) {
      var t = E.num(v.tr, 'Tax rate', { min: 0, max: 100 }), tx = Math.max(r.gain, 0) * t / 100;
      tax = '<div class="cwarn">Tax on gains at ' + t + '%: <b>' + rs(tx) + '</b> &middot; amount after tax: <b>' + rs(r.maturity - tx) + '</b><br><small>The rate is editable because rules can change. Confirm the current rate with the fund manager or the Inland Revenue Department.</small></div>';
    }
    return H('Estimated maturity value', rs(r.maturity)) + S([['Total invested', rs(r.invested)], ['Estimated gain', rs(r.gain), 'u'], ['Gain on investment', pct(r.gain_pct, 1)]]) + tax + sipChart(r.rows) +
      '<details class="cdet"><summary>Year-by-year schedule</summary>' + T(['Year', 'Instalment', 'Invested', 'Gain', 'Balance'], r.rows.map(function (x) { return [x.y, whole(x.p), whole(x.inv), whole(x.gain), whole(x.bal)]; })) + '</details>';
  },
  notes: '<p>Each instalment is invested at the start of its period and earns the periodic rate (annual rate &divide; instalments per year). With a step-up, the instalment rises by that percentage at the start of each new year.</p><p>For illustration only. Mutual fund returns are not fixed or guaranteed, and the result ignores entry or exit loads, fund expenses and NAV movements. Mutual fund investments are subject to market risk; read the scheme document before investing.</p>'
});

/* UNITS (custom) */
CALCS.push({ id: 'units', group: 'conv', name: 'Unit Converter', blurb: 'Length, weight, area (with Nepali land units), temperature, speed, data and time.',
  render: function (el) {
    var cat = 'area', U = E.UNITS;
    function opts(C, sel) { return Object.keys(C.u).map(function (k) { return '<option value="' + esc(k) + '"' + (k === sel ? ' selected' : '') + '>' + esc(k) + ' \u2013 ' + esc(C.u[k][0]) + '</option>'; }).join(''); }
    function draw(keepVal) {
      var C = U[cat];
      el.innerHTML = '<div class="chead"><h2>Unit Converter</h2><p>Length, weight, area (with Nepali land units), temperature, speed, data and time.</p></div>' +
        '<div class="cc"><div class="seg" role="group" aria-label="Category">' + Object.keys(U).map(function (k) { return '<button type="button" class="chip' + (k === cat ? ' on' : '') + '" data-cat="' + k + '" aria-pressed="' + (k === cat) + '">' + U[k].label + '</button>'; }).join('') + '</div>' +
        '<div class="swap"><div class="fld"><label for="u_v">Value</label><input id="u_v" type="number" inputmode="decimal" step="any" value="' + (keepVal == null ? 1 : keepVal) + '"></div><span></span><div class="fld"><label for="u_r">Result</label><input id="u_r" readonly></div></div>' +
        '<div class="swap"><div class="fld"><label for="u_f">From</label><select id="u_f">' + opts(C, C.def[0]) + '</select></div><button type="button" class="cbtn" id="u_x" aria-label="Swap units">&#8652;</button><div class="fld"><label for="u_t">To</label><select id="u_t">' + opts(C, C.def[1]) + '</select></div></div>' +
        '<div id="u_all"></div></div>' + (cat === 'area' ? '<div class="cnote"><p>Hill system: 1 ropani = 16 aana = 64 paisa = 256 dam = 5,476 sq ft. Terai: 1 bigha = 20 kattha = 400 dhur. Official land records may differ slightly.</p></div>' : '');
      calc();
    }
    function calc() {
      var C = U[cat], v = el.querySelector('#u_v').value, f = el.querySelector('#u_f').value, t = el.querySelector('#u_t').value, r = el.querySelector('#u_r'), all = el.querySelector('#u_all');
      try { r.value = D.fmt(E.convertUnit(cat, v, f, t), 6); all.innerHTML = '<h4 class="csec">All ' + U[cat].label.toLowerCase() + ' units</h4>' + KV(Object.keys(C.u).map(function (k) { return [esc(k) + ' \u2013 ' + esc(C.u[k][0]), D.fmt(E.convertUnit(cat, v, f, k), 6)]; })); }
      catch (e) { r.value = ''; all.innerHTML = '<p class="cmu">Enter a value to convert.</p>'; }
    }
    el.addEventListener('input', function (e) { if (e.target.id === 'u_v') calc(); });
    el.addEventListener('change', function (e) { if (e.target.id === 'u_f' || e.target.id === 'u_t') calc(); });
    el.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.dataset.cat) { cat = b.dataset.cat; draw(el.querySelector('#u_v').value); }
      else if (b.id === 'u_x') { var f = el.querySelector('#u_f'), t = el.querySelector('#u_t'), x = f.value; f.value = t.value; t.value = x; calc(); }
    });
    draw();
  } });

/* DATE CONVERTER (custom) */
var B_ = window.BSCore, A_ = window.AgeCore, MON_AD = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
function opt(sel, value, text) { var o = document.createElement('option'); o.value = value; o.textContent = text; sel.appendChild(o); }
function pad(n) { return (n < 10 ? '0' : '') + n; }
function nepalToday() { var t = new Date(Date.now() + 345 * 60000); return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate() }; }  /* Nepal time = UTC+5:45 */
CALCS.push({ id: 'date', group: 'dates', name: 'BS \u21C4 AD Date Converter', blurb: 'Convert between Bikram Sambat (Nepali) and Gregorian (English) dates.',
  render: function (el) {
    el.innerHTML = '<div class="chead"><h2>BS \u21C4 AD Date Converter</h2><p>Convert between Bikram Sambat (Nepali) and Gregorian (English) dates.</p></div><div class="cc todaybox" id="dt_today" aria-live="polite"></div>' +
      '<label class="chk"><input type="checkbox" id="dt_dev"> Show result in Nepali (\u0926\u0947\u0935\u0928\u093E\u0917\u0930\u0940)</label>' +
      '<div class="cgrid even"><section class="cc"><h3>BS \u2192 AD</h3><p class="cmu">Bikram Sambat (' + B_.FIRST_YEAR + ' \u2013 ' + B_.LAST_YEAR + ')</p><div class="row3"><div class="fld"><label for="dt_by">Year</label><select id="dt_by"></select></div><div class="fld"><label for="dt_bm">Month</label><select id="dt_bm"></select></div><div class="fld"><label for="dt_bd">Day</label><select id="dt_bd"></select></div></div><div class="res" id="dt_rb" role="status"></div></section>' +
      '<section class="cc"><h3>AD \u2192 BS</h3><p class="cmu">Gregorian (14 Apr 1943 \u2013 13 Apr 2034)</p><div class="row3"><div class="fld"><label for="dt_ay">Year</label><input id="dt_ay" type="number" inputmode="numeric" min="1943" max="2034"></div><div class="fld"><label for="dt_am">Month</label><select id="dt_am"></select></div><div class="fld"><label for="dt_ad">Day</label><input id="dt_ad" type="number" inputmode="numeric" min="1" max="31"></div></div><div class="res" id="dt_ra" role="status"></div></section></div>' +
      '<div class="cnote"><p>Results use a month-length table for BS ' + B_.FIRST_YEAR + '\u2013' + B_.LAST_YEAR + ' and are a reference only. For legal or official documents, confirm against the official government calendar.</p></div>';
    var q = function (id) { return el.querySelector('#' + id); };
    for (var y = B_.FIRST_YEAR; y <= B_.LAST_YEAR; y++) opt(q('dt_by'), y, y);
    for (var m = 1; m <= 12; m++) { opt(q('dt_bm'), m, m + ' \u2013 ' + B_.MONTHS_EN[m - 1] + ' (' + B_.MONTHS_NE[m - 1] + ')'); opt(q('dt_am'), m, m + ' \u2013 ' + MON_AD[m - 1]); }
    function dev() { return q('dt_dev').checked; }
    function dg(n) { return dev() ? B_.toDevanagari(n) : String(n); }
    function wd(i) { return dev() ? B_.WEEKDAYS_NE[i] : B_.WEEKDAYS_EN[i]; }
    function days(keep) { var dim = B_.daysInMonth(+q('dt_by').value, +q('dt_bm').value), s = q('dt_bd'); s.innerHTML = ''; for (var d = 1; d <= dim; d++) opt(s, d, d); s.value = Math.min(keep || 1, dim); }
    function show(id, html, err) { var n = q(id); n.className = 'res' + (err ? ' cerr' : ''); n.innerHTML = html; }
    function toAd() {
      try { var r = B_.bsToAd(q('dt_by').value, q('dt_bm').value, q('dt_bd').value), iso = r.year + '-' + pad(r.month) + '-' + pad(r.day);
        show('dt_rb', '<div class="cbig">' + dg(iso) + '</div><div class="csub">' + dg(r.day) + ' ' + MON_AD[r.month - 1] + ' ' + dg(r.year) + ' &middot; ' + wd(r.weekday) + '</div>'); }
      catch (e) { show('dt_rb', esc(e.message), true); }
    }
    function toBs() {
      try { var r = B_.adToBs(q('dt_ay').value, q('dt_am').value, q('dt_ad').value);
        show('dt_ra', '<div class="cbig">' + dg(r.year + '-' + pad(r.month) + '-' + pad(r.day)) + '</div><div class="csub">' + dg(r.day) + ' ' + (dev() ? B_.MONTHS_NE : B_.MONTHS_EN)[r.month - 1] + ' ' + dg(r.year) + ' BS &middot; ' + wd(r.weekday) + '</div>'); }
      catch (e) { show('dt_ra', esc(e.message), true); }
    }
    function today() {
      var t = nepalToday(), b = B_.adToBs(t.y, t.m, t.d), d = new Date(Date.UTC(t.y, t.m - 1, t.d)).getUTCDay();
      q('dt_today').innerHTML = '<small>Today (Nepal time)</small><div class="cbig">' + dg(b.year + '-' + pad(b.month) + '-' + pad(b.day)) + ' BS</div><div class="csub">' + dg(b.day) + ' ' + (dev() ? B_.MONTHS_NE : B_.MONTHS_EN)[b.month - 1] + ' ' + dg(b.year) + ' &middot; ' + wd(d) + ' &middot; AD ' + t.y + '-' + pad(t.m) + '-' + pad(t.d) + '</div>';
    }
    var t0 = nepalToday(), b0 = B_.adToBs(t0.y, t0.m, t0.d);
    q('dt_by').value = b0.year; q('dt_bm').value = b0.month; days(b0.day); q('dt_ay').value = t0.y; q('dt_am').value = t0.m; q('dt_ad').value = t0.d;
    el.addEventListener('change', function (e) { var id = e.target.id; if (id === 'dt_by' || id === 'dt_bm') days(+q('dt_bd').value); if (id.indexOf('dt_b') === 0) toAd(); else if (id.indexOf('dt_a') === 0) toBs(); else { toAd(); toBs(); today(); } });
    el.addEventListener('input', function (e) { if (e.target.id === 'dt_ay' || e.target.id === 'dt_ad') toBs(); });
    today(); toAd(); toBs();
  } });

/* AGE (custom) */
CALCS.push({ id: 'age', group: 'dates', name: 'Age Calculator', blurb: 'Exact age in years, months and days in AD or BS, with total days, weekday of birth and next birthday.',
  render: function (el) {
    el.innerHTML = '<div class="chead"><h2>Age Calculator</h2><p>Exact age in years, months and days in AD or BS, with total days, weekday of birth and next birthday.</p></div>' +
      '<div class="cgrid"><section class="cc" aria-label="Inputs"><div class="seg" role="group" aria-label="Calendar"><button type="button" class="chip on" id="ag_cAD" aria-pressed="true">AD &middot; English</button><button type="button" class="chip" id="ag_cBS" aria-pressed="false">BS &middot; \u0928\u0947\u092A\u093E\u0932\u0940</button></div>' +
      '<div class="fld"><label id="ag_l1">Date of birth</label><div id="ag_dAD"><input type="date" id="ag_dob" min="1900-01-01" aria-labelledby="ag_l1"></div><div id="ag_dBS" class="row3" hidden role="group" aria-labelledby="ag_l1"><select id="ag_dY" aria-label="Birth year BS"></select><select id="ag_dM" aria-label="Birth month BS"></select><select id="ag_dD" aria-label="Birth day BS"></select></div></div>' +
      '<div class="fld"><label id="ag_l2">Calculate age on</label><div id="ag_oAD"><input type="date" id="ag_on" min="1900-01-01" aria-labelledby="ag_l2"></div><div id="ag_oBS" class="row3" hidden role="group" aria-labelledby="ag_l2"><select id="ag_oY" aria-label="Year BS"></select><select id="ag_oM" aria-label="Month BS"></select><select id="ag_oD" aria-label="Day BS"></select></div></div>' +
      '<div class="cact"><button type="button" class="cbtn" id="ag_today">Today</button></div>' +
      '<div class="fld" id="ag_leap" hidden><label for="ag_rule">Born on 29 February: in non-leap years count the birthday on</label><select id="ag_rule"><option value="feb28">28 February (common legal convention)</option><option value="mar1">1 March</option></select></div>' +
      '<label class="chk"><input type="checkbox" id="ag_dev"> \u0928\u0947\u092A\u093E\u0932\u0940 \u0905\u0902\u0915 (Nepali digits)</label></section>' +
      '<section class="cc" aria-label="Result" aria-live="polite"><h3>Result</h3><div class="cs tiles"><div><small>Years</small><b id="ag_tY">\u2014</b></div><div><small>Months</small><b id="ag_tM">\u2014</b></div><div><small>Days</small><b id="ag_tD">\u2014</b></div><div><small>Weeks old</small><b id="ag_tW">\u2014</b></div></div><div id="ag_panel" class="ckv one"></div></section></div>' +
      '<div class="cnote"><p>BS support covers ' + B_.FIRST_YEAR + '\u2013' + B_.LAST_YEAR + ' BS (14 Apr 1943 \u2013 13 Apr 2034 AD). In BS mode months and days are counted with BS month lengths, so they can differ by a day or two from the AD result.</p></div>';
    var q = function (id) { return el.querySelector('#' + id); }, cal = 'AD';
    ['d', 'o'].forEach(function (k) {
      for (var y = B_.FIRST_YEAR; y <= B_.LAST_YEAR; y++) opt(q('ag_' + k + 'Y'), y, y);
      for (var m = 1; m <= 12; m++) opt(q('ag_' + k + 'M'), m, m + ' \u00B7 ' + B_.MONTHS_EN[m - 1]);
    });
    function fill(k, keep) { var dim = B_.daysInMonth(+q('ag_' + k + 'Y').value, +q('ag_' + k + 'M').value), s = q('ag_' + k + 'D'); s.innerHTML = ''; for (var d = 1; d <= dim; d++) opt(s, d, d); s.value = Math.min(keep || +s.value || 1, dim); }
    ['d', 'o'].forEach(function (k) { q('ag_' + k + 'Y').value = 2052; q('ag_' + k + 'M').value = 1; fill(k, 1); });
    function dev() { return q('ag_dev').checked; }
    function f(n) { var s = n.toLocaleString('en-US'); return dev() ? B_.toDevanagari(s) : s; }
    function dtxt(c, p) { var s = c === 'AD' ? p.d + ' ' + MON_AD[p.m - 1].slice(0, 3) + ' ' + p.y : p.d + ' ' + B_.MONTHS_EN[p.m - 1] + ' ' + p.y + ' BS'; return dev() ? B_.toDevanagari(s) : s; }
    function wd(i) { return dev() ? B_.WEEKDAYS_NE[i] : B_.WEEKDAYS_EN[i]; }
    function iso(p) { return p.y + '-' + pad(p.m) + '-' + pad(p.d); }
    function parse(s) { var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || ''); return m ? { y: +m[1], m: +m[2], d: +m[3] } : null; }
    function a2b(p) { var r = B_.adToBs(p.y, p.m, p.d); return { y: r.year, m: r.month, d: r.day }; }
    function b2a(p) { var r = B_.bsToAd(p.y, p.m, p.d); return { y: r.year, m: r.month, d: r.day }; }
    function getP(k) { return cal === 'AD' ? parse(q(k === 'd' ? 'ag_dob' : 'ag_on').value) : { y: +q('ag_' + k + 'Y').value, m: +q('ag_' + k + 'M').value, d: +q('ag_' + k + 'D').value }; }
    function setP(k, p) { if (cal === 'AD') { q(k === 'd' ? 'ag_dob' : 'ag_on').value = p ? iso(p) : ''; return; } q('ag_' + k + 'Y').value = p.y; q('ag_' + k + 'M').value = p.m; fill(k, p.d); q('ag_' + k + 'D').value = p.d; }
    function clear() { ['Y', 'M', 'D', 'W'].forEach(function (c) { q('ag_t' + c).textContent = '\u2014'; }); }
    function err(msg) { clear(); q('ag_panel').innerHTML = '<div class="cerr" role="alert">' + esc(msg) + '</div>'; }
    function setCal(next) {
      if (next === cal) return;
      var dob = getP('d'), on = getP('o');
      try {
        var cv = function (p) { return p ? (next === 'BS' ? a2b(p) : b2a(p)) : null; }, nd = cv(dob), no = cv(on);
        cal = next; q('ag_cAD').setAttribute('aria-pressed', cal === 'AD'); q('ag_cBS').setAttribute('aria-pressed', cal === 'BS');
        q('ag_cAD').classList.toggle('on', cal === 'AD'); q('ag_cBS').classList.toggle('on', cal === 'BS');
        q('ag_dAD').hidden = q('ag_oAD').hidden = cal !== 'AD'; q('ag_dBS').hidden = q('ag_oBS').hidden = cal !== 'BS';
        if (nd) setP('d', nd); else if (cal === 'AD') q('ag_dob').value = '';
        if (no) setP('o', no);
      } catch (e) { err('BS mode supports dates from 14 Apr 1943 to 13 Apr 2034 AD. ' + e.message); return; }
      run();
    }
    function run() {
      var dob = getP('d'), on = getP('o');
      q('ag_leap').hidden = !(cal === 'AD' && dob && dob.m === 2 && dob.d === 29);
      if (!dob) { clear(); q('ag_panel').innerHTML = '<span class="cmu">Enter a date of birth to see the age.</span>'; return; }
      if (!on) { err('Choose the date to calculate the age on.'); return; }
      try {
        var r = A_.calculate(cal, dob, on, { leapRule: q('ag_rule').value });
        q('ag_tY').textContent = f(r.years); q('ag_tM').textContent = f(r.months); q('ag_tD').textContent = f(r.days); q('ag_tW').textContent = f(r.totalWeeks);
        var other = ''; try { other = cal === 'AD' ? 'Date of birth in BS: ' + dtxt('BS', a2b(dob)) : 'Date of birth in AD: ' + dtxt('AD', b2a(dob)); } catch (e) { other = ''; }
        var next = r.next ? dtxt(cal, r.next) + ' (' + wd(r.nextWeekday) + ')' : 'beyond supported range';
        var dn = r.daysToNext === null ? '\u2014' : (r.isBirthday ? (dev() ? '\u0906\u091C!' : 'Today, happy birthday!') : f(r.daysToNext));
        var rows = [['Total days', f(r.totalDays)], ['Total weeks', f(r.totalWeeks) + (r.extraDays ? ' + ' + f(r.extraDays) + (dev() ? ' \u0926\u093F\u0928' : ' d') : '')], ['Total months', f(r.totalMonths)], ['Born on', wd(r.bornWeekday)], ['Next birthday', next], ['Days to next birthday', dn]];
        q('ag_panel').innerHTML = KV(rows.map(function (x) { return [esc(x[0]), esc(x[1])]; })) + (other ? '<p class="cmu">' + esc(other) + '</p>' : '');
      } catch (e) { err(e.message); }
    }
    el.addEventListener('change', function (e) {
      var id = e.target.id;
      if (/^ag_[do][YM]$/.test(id)) fill(id.charAt(3));
      run();
    });
    el.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.id === 'ag_cAD') setCal('AD'); else if (b.id === 'ag_cBS') setCal('BS');
      else if (b.id === 'ag_today') { var t = nepalToday(); setP('o', cal === 'AD' ? t : a2b(t)); run(); }
    });
    setP('o', nepalToday()); run();
  } });

/* NUMBERS TO WORDS */
function nwBox(label, text, lang) { return '<div class="nwbox"><div class="nwh"><small>' + label + '</small><button type="button" class="cbtn small" data-copy="1">Copy</button></div><div class="nwtxt" lang="' + lang + '">' + esc(text) + '</div></div>'; }
reg('conv', {
  id: 'numwords', name: 'Numbers to Words', blurb: 'Write an amount in words in English and Nepali (rupees and paisa, lakh\u2013crore system), for cheques, vouchers and letters.',
  fields: [{ id: 'amt', kind: 'txt', label: 'Amount (Rs)', hint: 'commas and Nepali digits are fine', value: '73269.77', placeholder: 'e.g. 73269.77' }],
  examples: [{ label: '73,269.77', values: { amt: '73269.77' } }, { label: '50,000', values: { amt: '50000' } }, { label: '1 lakh', values: { amt: '100000' } }, { label: '1,25,00,000', values: { amt: '1,25,00,000' } }, { label: '\u0967\u0968\u0969\u096A (Nepali digits)', values: { amt: '\u0967\u0968\u0969\u096A' } }],
  run: function (v) {
    var r = window.NumWords.convert(v.amt);
    if (r.empty) return '<p class="cmu">Type an amount to see it in words.</p>';
    if (!r.ok) return '<div class="cerr" role="alert">' + esc(r.error) + '</div>';
    return nwBox('In English', r.english, 'en') + nwBox('\u0928\u0947\u092A\u093E\u0932\u0940\u092E\u093E', r.nepali, 'ne') + nwBox('In figures', 'Rs ' + r.formatted, 'en') + nwBox('\u0905\u0902\u0915\u092E\u093E', '\u0930\u0941 ' + r.formattedNe, 'ne');
  },
  notes: '<p>Up to 15 digits (99 neel), rounded to the nearest paisa. Always read the words back before using them on a cheque or legal document, and check the Nepali spelling against your organisation\u2019s standard.</p>'
});

/* =====================================================================================
   TRADING & COSTS, CORPORATE ACTIONS, INVESTMENT & VALUATION calculators (v1.5)
   ===================================================================================== */
function spct(n) { return n == null || !isFinite(n) ? '\u2013' : '<span class="' + (n > 0 ? 'u' : n < 0 ? 'd' : '') + '">' + (n > 0 ? '+' : '') + n.toFixed(2) + '%</span>'; }
function verdictB(v) { return v ? B(v.text, v.kind) : ''; }
function vsBlock(o) {
  if (o.upside_pct == null) return '<p class="cmu">Enter the current market price to see upside and margin of safety.</p>';
  return sec('Against the current price', S([['Current price', rs(o.price)], ['Upside / downside', spct(o.upside_pct)], ['Margin of safety', spct(o.mos_pct)]])) + '<p>' + verdictB(o.verdict) + '</p>';
}
var BR = { id: 'br', label: 'Broker commission rate (%)', hint: 'blank = NEPSE slab', value: '', step: 'any', adv: true };
var DPF = { id: 'dp', label: 'DP charge on sale (Rs)', hint: 'blank = default', value: '', step: 'any', adv: true };
var CGF = { id: 'cg', label: 'CGT rate override (%)', hint: 'blank = by holding period', value: '', step: 'any', adv: true };
var T_EPS = 'EPS (earnings per share): the company\u2019s yearly profit divided by its number of shares.', T_BV = 'BVPS (book value per share): net worth (assets minus liabilities) divided by the number of shares.',
  T_DPS = 'DPS (dividend per share): the cash dividend paid on each share in a year.', T_WACC = 'WACC (weighted average cost of capital): the blended return a company must earn to satisfy its lenders and shareholders. Used here as the discount rate.',
  T_TG = 'Terminal growth: the steady yearly growth assumed for ever after the projection years. Keep it low, below long-run economic growth.', T_MOS = 'Margin of safety: how far the price is below your estimate of value, as a share of that value. A bigger margin leaves more room for error.',
  T_RR = 'R:R (reward to risk): how much you could gain for each rupee you risk. An R:R of 2 means a Rs 2 gain target for every Rs 1 at risk.';
var TRADE_NOTE = '<p>Charges use the Rates &amp; rules panel (commission slabs and minimum, SEBON fee, DP charge on the sell side, CGT). Brokers can charge differently, so enter your broker\u2019s rate under advanced settings if it differs. Results are estimates.</p>';
var PLAIN_FEES = 'Brokerage = traded value &times; broker rate (at least the minimum); SEBON fee = traded value &times; SEBON rate; DP charge applies once on the sell side.';

/* ---------- A. TRADING & COSTS ---------- */
reg('trade', {
  id: 'pl', name: 'Profit / Loss', blurb: 'Net profit or loss on a trade after buy and sell charges and capital gains tax, with return % and holding period.',
  fields: [SYMF, { id: 'b', label: 'Buy price (Rs per share)', value: 500, step: '0.01' }, { id: 's', label: 'Sell price (Rs per share)', value: 600, step: '0.01' }, { id: 'q', label: 'Quantity (kitta)', value: 100, step: '1' },
    { id: 'bd', kind: 'date', label: 'Buy date', value: function () { return isoDaysAgo(120); } }, { id: 'sd', kind: 'date', label: 'Sell date', value: function () { return isoDaysAgo(0); } }, BR, DPF, CGF],
  advTitle: 'Charges and tax overrides (optional)',
  warn: '<b>Check the CGT rate.</b> CGT follows the Rates &amp; rules panel and may not reflect the latest budget changes. Confirm the rate that applies to you.',
  fill: fillLtp({ s: 1 }), fillMsg: function () { return 'Loaded the latest price into the sell price'; },
  run: function (v) {
    var r = E.calculateProfitLoss({ buy_price: v.b, sell_price: v.s, quantity: v.q, buy_date: v.bd, sell_date: v.sd, broker_rate_pct: v.br, dp_charge: v.dp, cgt_rate_pct: v.cg }), k = r.net_profit_loss > 0 ? 'ok' : r.net_profit_loss < 0 ? 'bad' : 'wa';
    return '<div class="chd"><small>Net profit / loss</small><div class="cbig">' + pnl(r.net_profit_loss) + '</div><div class="csub">' + B(r.result, k) + ' Net return <b>' + spct(r.return_pct) + '</b> &middot; held ' + r.holding_days + ' days &middot; ' + r.term + '</div></div>' +
      S([['Total buy cost', rs(r.total_buy_cost)], ['Total sell value', rs(r.total_sell_value)], ['Buy-side charges', rs(r.buy_charges)], ['Sell-side charges (incl. DP)', rs(r.sell_charges)], ['Gross profit / loss', pnl(r.gross_profit_loss)], ['Taxable gain', rs(r.taxable_gain)], ['CGT rate', pct(r.cgt_rate_pct, 2)], ['CGT amount', rs(r.cgt_amount)]]) +
      FORM([PLAIN_FEES, 'Taxable gain = net sale proceeds &minus; total buy cost (never below zero). CGT = taxable gain &times; rate: 365 days or less, or more than 365 days.', 'Net profit = net sale proceeds &minus; total buy cost &minus; CGT. Return % = net profit &divide; total buy cost.']);
  },
  notes: TRADE_NOTE + '<p>If you sell at a loss, no CGT is charged. Holding days are counted from the buy date to the sell date.</p>'
});

reg('trade', {
  id: 'breakeven', name: 'Break-Even Price', blurb: 'The lowest sell price that recovers all buy-side and sell-side costs.',
  fields: [SYMF, { id: 'b', label: 'Buy price (Rs per share)', value: 500, step: '0.01' }, { id: 'q', label: 'Quantity (kitta)', value: 100, step: '1' }, BR, DPF],
  advTitle: 'Charges (optional)', fill: fillLtp({ b: 1 }),
  run: function (v) {
    var r = E.calculateBreakEven({ buy_price: v.b, quantity: v.q, broker_rate_pct: v.br, dp_charge: v.dp });
    return H('Break-even sell price', rs(r.break_even_price), 'Needs a rise of <b>' + rs(r.price_increase) + '</b> (' + pct(r.price_increase_pct, 2) + ') from your buy price') +
      S([['Total buy cost', rs(r.total_buy_cost)], ['Sell-side charges at break-even', rs(r.sell_charges)], ['Rise needed per share', rs(r.price_increase)], ['Rise needed', pct(r.price_increase_pct, 2)]]) +
      FORM([PLAIN_FEES, 'Break-even price = (total buy cost + DP charge) &divide; (quantity &times; (1 &minus; broker rate &minus; SEBON rate)), rounded up to the next paisa.', 'CGT is zero at break-even because there is no gain.']);
  },
  notes: TRADE_NOTE
});

reg('trade', {
  id: 'target', name: 'Target Sell Price', blurb: 'The sell price needed to reach a net profit (after all charges and CGT) that you choose.',
  fields: [SYMF, { id: 'b', label: 'Buy price (Rs per share)', value: 500, step: '0.01' }, { id: 'q', label: 'Quantity (kitta)', value: 100, step: '1' },
    { id: 'mode', kind: 'sel', label: 'I want to set my goal as', value: 'profit', opts: [['profit', 'Net profit in Rs'], ['pct', 'Net return %']] },
    { id: 'dprofit', label: 'Desired net profit (Rs)', value: 5000, step: 'any', showWhen: { id: 'mode', 'in': ['profit'] } }, { id: 'dpct', label: 'Desired net return (%)', value: 10, step: 'any', showWhen: { id: 'mode', 'in': ['pct'] } },
    { id: 'bd', kind: 'date', label: 'Buy date', value: function () { return isoDaysAgo(60); } }, { id: 'sd', kind: 'date', label: 'Expected sell date', value: function () { return isoDaysAgo(-60); } }, BR, DPF, CGF],
  advTitle: 'Charges and tax overrides (optional)', fill: fillLtp({ b: 1 }),
  run: function (v) {
    var r = E.calculateTargetSellPrice({ buy_price: v.b, quantity: v.q, mode: v.mode, desired_profit: v.dprofit, desired_return_pct: v.dpct, buy_date: v.bd, sell_date: v.sd, broker_rate_pct: v.br, dp_charge: v.dp, cgt_rate_pct: v.cg });
    return H('Target sell price', rs(r.target_price), 'A rise of <b>' + pct(r.price_increase_pct, 2) + '</b> from your buy price &middot; ' + r.term) +
      S([['Required gross profit', rs(r.required_gross_profit)], ['Estimated CGT (' + pct(r.cgt_rate_pct, 2) + ')', rs(r.estimated_cgt)], ['Net profit at this price', pnl(r.net_profit)], ['Desired net profit', rs(r.desired_net_profit)], ['Total buy cost', rs(r.total_buy_cost)]]) +
      FORM([PLAIN_FEES, 'Before-tax gain needed = desired net profit &divide; (1 &minus; CGT rate), so the tax is covered exactly.', 'Target price = the lowest price (to the paisa) where net sale proceeds reach total buy cost + that gain.']);
  },
  notes: TRADE_NOTE + '<p>Net return % is measured on the total buy cost, including buying charges. The CGT rate depends on the buy and expected sell dates.</p>'
});

reg('trade', {
  id: 'possize', name: 'Position Size', blurb: 'How many shares to buy so that a stop-loss exit loses no more than your chosen share of capital.',
  fields: [{ id: 'cap', label: 'Total trading capital (Rs)', value: 1000000, step: 'any' }, { id: 'risk', label: 'Risk per trade (% of capital)', value: 1, step: 'any' }, { id: 'en', label: 'Entry price (Rs)', value: 500, step: '0.01' }, { id: 'sl', label: 'Stop-loss price (Rs)', value: 470, step: '0.01' },
    { id: 'al', label: 'Maximum allocation to one stock (% of capital)', hint: 'optional', value: '', step: 'any', adv: true }],
  advTitle: 'Limits (optional)', fill: function (r) { return { en: D.g(r, 'LTP') }; },
  run: function (v) {
    var r = E.calculatePositionSize({ total_capital: v.cap, risk_pct: v.risk, entry_price: v.en, stop_loss: v.sl, max_alloc_pct: v.al });
    return H('Shares to buy', whole(r.shares) + ' kitta', 'Position value <b>' + rs(r.position_size_rs) + '</b> (' + pct(r.position_pct_of_capital, 1) + ' of capital)') + WARN(r.warnings) +
      S([['Maximum risk amount', rs(r.max_risk_amount)], ['Risk per share', rs(r.risk_per_share)], ['Shares allowed by risk', whole(r.risk_based_shares)], ['Capital needed for that size', rs(r.required_capital)], ['Actual risk amount', rs(r.actual_risk)], ['Actual risk', pct(r.actual_risk_pct, 3)]]) +
      FORM(['Maximum risk = capital &times; risk %.', 'Risk per share = entry price &minus; stop-loss price.', 'Shares = maximum risk &divide; risk per share, rounded down, then limited by your capital and allocation cap.', 'Charges and slippage are not included, so real losses can be slightly larger.']);
  },
  notes: '<p>Assumes a long (buy) position with the stop-loss below the entry price. A stop-loss is not guaranteed to fill at your price.</p>'
});

reg('trade', {
  id: 'rr', name: 'Risk / Reward', blurb: 'Risk and reward per share and in total, reward-to-risk ratio, expected value and the win rate you need to break even.',
  fields: [SYMF, { id: 'en', label: 'Entry price (Rs)', value: 500, step: '0.01' }, { id: 'sl', label: 'Stop-loss price (Rs)', value: 470, step: '0.01' }, { id: 'tg', label: 'Target price (Rs)', value: 575, step: '0.01', tip: T_RR },
    { id: 'q', label: 'Quantity (kitta)', value: 100, step: '1' }, { id: 'wp', label: 'Win probability (%)', hint: 'optional', value: '', step: 'any' }],
  fill: fillLtp({ en: 1 }),
  run: function (v) {
    var r = E.calculateRiskReward({ entry_price: v.en, stop_loss: v.sl, target_price: v.tg, quantity: v.q, win_probability_pct: v.wp }), ev = r.expected_value_per_share;
    return H('Reward to risk', '1 : ' + r.rr_ratio.toFixed(2), r.rr_ratio >= 2 ? B('Reward is at least twice the risk', 'ok') : r.rr_ratio >= 1 ? B('Reward exceeds risk', 'wa') : B('Risk exceeds reward', 'bad')) +
      S([['Risk per share', rs(r.risk_per_share)], ['Reward per share', rs(r.reward_per_share)], ['Total risk', rs(r.total_risk), 'd'], ['Total reward', rs(r.total_reward), 'u'], ['Break-even win rate', pct(r.break_even_win_rate_pct, 1)]]) +
      (ev == null ? '<p class="cmu">Enter a win probability to see the expected value.</p>' : sec('Expected value', S([['Per share', pnl(ev)], ['Full position', pnl(r.expected_value_total)]]))) +
      FORM(['Risk per share = entry &minus; stop-loss. Reward per share = target &minus; entry. R:R = reward &divide; risk.', 'Break-even win rate = risk &divide; (risk + reward).', 'Expected value per share = win % &times; reward &minus; (1 &minus; win %) &times; risk.', 'All figures are before brokerage, SEBON fee, DP charge and CGT.']);
  },
  notes: '<p>A positive expected value only helps if your win probability is realistic. Probability is your own estimate, not a forecast.</p>'
});

/* ---------- B. CORPORATE ACTIONS ---------- */
reg('corp', {
  id: 'bonusshares', name: 'Bonus Share Calculator', blurb: 'New quantity and new average cost per share after bonus shares. Bonus shares add shares, not cash.',
  fields: [SYMF, { id: 'q', label: 'Existing quantity (kitta)', value: 1000, step: '1' }, { id: 'a', label: 'Average buy price (Rs)', value: 600, step: '0.01' }, { id: 'b', label: 'Bonus (%)', value: 15, step: 'any' }],
  fill: fillLtp({ a: 1 }), fillMsg: function () { return 'Loaded the latest price into the average price (replace it with your own cost)'; },
  run: function (v) {
    var r = E.calculateBonusShares({ quantity: v.q, avg_price: v.a, bonus_pct: v.b });
    return H('New average price', rs(r.new_average_price), 'From ' + rs(r.old_average_price) + ' &middot; new quantity <b>' + whole(r.new_quantity) + '</b> kitta') +
      S([['Bonus shares received', whole(r.bonus_shares)], ['Fractional entitlement', r.fractional.toFixed(4)], ['New total quantity', whole(r.new_quantity)], ['Total investment (unchanged)', rs(r.total_investment)], ['Cost per share before', rs(r.old_average_price)], ['Cost per share after', rs(r.new_average_price)]]) +
      FORM(['Bonus shares = existing quantity &times; bonus %, whole shares only.', 'New quantity = existing quantity + bonus shares.', 'New average price = total investment &divide; new quantity. No new cash is invested, so the total investment stays the same.']);
  },
  notes: '<p>Fractional bonus entitlements are not credited as shares here. For the effect on the market price, use Bonus Price Adjustment.</p>'
});

reg('corp', {
  id: 'rightshares', name: 'Right Share Calculator', blurb: 'Right shares you can apply for, the cash needed, and your new quantity and average cost.',
  fields: [SYMF, { id: 'q', label: 'Existing quantity (kitta)', value: 1000, step: '1' }, { id: 'a', label: 'Average buy price (Rs)', value: 600, step: '0.01' }, { id: 'r', label: 'Right (%)', hint: '50 = 1 right share for every 2 held', value: 50, step: 'any' }, { id: 'p', label: 'Right issue price (Rs)', value: 100, step: '0.01' }],
  fill: fillLtp({ a: 1 }), fillMsg: function () { return 'Loaded the latest price into the average price (replace it with your own cost)'; },
  run: function (v) {
    var r = E.calculateRightShares({ quantity: v.q, avg_price: v.a, right_pct: v.r, right_price: v.p });
    return H('Cash required', rs(r.cash_required), 'For <b>' + whole(r.right_shares) + '</b> right shares &middot; new quantity <b>' + whole(r.new_quantity) + '</b> kitta') +
      S([['Right shares eligible', whole(r.right_shares)], ['Fractional entitlement', r.fractional.toFixed(4)], ['New total quantity', whole(r.new_quantity)], ['Total investment', rs(r.total_investment)], ['New average price', rs(r.new_average_price)], ['Change in average price', rs(r.average_change) + ' (' + pct(r.average_change_pct, 2) + ')']]) +
      FORM(['Right shares = existing quantity &times; right %, whole shares only.', 'Cash required = right shares &times; right issue price.', 'New average price = (old investment + cash required) &divide; new quantity.']);
  },
  notes: '<p>This is the cost view. For the effect on the market price, use Right Price Adjustment.</p>'
});

reg('corp', {
  id: 'combined', name: 'Bonus + Right Combined', blurb: 'Apply a bonus and a right issue in the order they happen and see the final quantity and average cost.',
  fields: [SYMF, { id: 'q', label: 'Existing quantity (kitta)', value: 1000, step: '1' }, { id: 'a', label: 'Average price (Rs)', value: 600, step: '0.01' }, { id: 'b', label: 'Bonus (%)', value: 15, step: 'any' }, { id: 'r', label: 'Right (%)', value: 50, step: 'any' }, { id: 'p', label: 'Right issue price (Rs)', value: 100, step: '0.01' },
    { id: 'o', kind: 'sel', label: 'Order of actions', value: 'bonus_first', opts: [['bonus_first', 'Bonus first, then right'], ['right_first', 'Right first, then bonus']] }, { id: 'm', label: 'Market price before the actions (Rs)', hint: 'optional, for the adjusted price', value: '', step: '0.01', adv: true }],
  advTitle: 'Adjusted price (optional)', fill: fillLtp({ a: 1, m: 1 }),
  run: function (v) {
    var r = E.calculateCombinedActions({ quantity: v.q, avg_price: v.a, bonus_pct: v.b, right_pct: v.r, right_price: v.p, order: v.o, market_price: v.m });
    return H('Final average price', rs(r.final_average_price), 'Final quantity <b>' + whole(r.final_quantity) + '</b> kitta &middot; total invested <b>' + rs(r.total_investment) + '</b>') +
      S([['Bonus shares', whole(r.bonus_shares)], ['Right shares', whole(r.right_shares)], ['Cash for right shares', rs(r.cash_required)], ['Final quantity', whole(r.final_quantity)], ['Total investment', rs(r.total_investment)], ['Theoretical adjusted price', r.adjusted_price == null ? 'Enter market price' : rs(r.adjusted_price)]]) +
      sec('Step by step', T(['Step', 'Shares added', 'Cash', 'Quantity', 'Total investment', 'Average price'], r.steps.map(function (s) { return [esc(s.action), whole(s.added), n2(s.cash), whole(s.quantity), n2(s.total_investment), n2(s.avg)]; }))) +
      FORM(['Each action works on the quantity left by the one before it, so the order changes the share count.', 'Bonus shares = quantity &times; bonus %; right shares = quantity &times; right %; both whole shares only.', 'Final average price = total investment &divide; final quantity. Only the right issue adds cash.', 'Theoretical adjusted price = (quantity &times; market price + cash paid) &divide; final quantity.']);
  },
  notes: '<p>The actual market price after book closure is set by trading. The adjusted price shown is the theoretical value-neutral price.</p>'
});

reg('corp', {
  id: 'drip', name: 'Dividend Reinvestment', blurb: 'Shares bought by reinvesting a cash dividend, and your new quantity and weighted average price.',
  fields: [SYMF, { id: 'q', label: 'Existing quantity (kitta)', value: 1000, step: '1' }, { id: 'a', label: 'Average price (Rs)', value: 600, step: '0.01' }, { id: 'd', label: 'Cash dividend per share (Rs)', value: 5.7, step: '0.01', tip: T_DPS },
    { id: 'p', label: 'Reinvestment price (Rs)', value: 500, step: '0.01' }, { id: 'pc', label: 'Share of dividend to reinvest (%)', value: 100, step: 'any' }, { id: 'ch', label: 'Charges on the purchase (%)', hint: 'optional, e.g. brokerage', value: '', step: 'any', adv: true }],
  advTitle: 'Purchase charges (optional)', fill: fillLtp({ p: 1 }),
  run: function (v) {
    var r = E.calculateDividendReinvestment({ quantity: v.q, avg_price: v.a, dps: v.d, reinvest_price: v.p, reinvest_pct: v.pc, charge_pct: v.ch });
    return H('New shares bought', whole(r.new_shares) + ' kitta', 'New quantity <b>' + whole(r.new_quantity) + '</b> &middot; new average price <b>' + rs(r.new_average_price) + '</b>') +
      S([['Total dividend received', rs(r.total_dividend)], ['Amount reinvested', rs(r.amount_reinvested)], ['Cash used', rs(r.cash_used)], ['Cash left over from reinvested amount', rs(r.remaining_cash)], ['Dividend not reinvested', rs(r.cash_not_reinvested)], ['Share value before', rs(r.value_before)], ['Share value after', rs(r.value_after)], ['Change in share value', pnl(r.value_change)]]) +
      FORM(['Total dividend = quantity &times; dividend per share (enter the amount you actually receive, after any tax).', 'New shares = amount reinvested &divide; price (including charges), whole shares only.', 'New average price = (old investment + cash used) &divide; new quantity.', 'Share value is measured at the reinvestment price.']);
  },
  notes: '<p>Assumes the reinvestment price stays the same for all shares bought. Dividend tax is not deducted unless you enter the net dividend per share.</p>'
});

/* ---------- C. INVESTMENT & VALUATION ---------- */
function loadEps(r) { return { price: D.g(r, 'LTP'), eps: D.g(r, 'EPS (Annu.)') }; }
reg('val', {
  id: 'graham', name: 'Graham Value', blurb: 'Benjamin Graham\u2019s growth formula for a rough intrinsic value per share.',
  fields: [SYMF, { id: 'eps', label: 'EPS (Rs)', value: 35, step: '0.01', tip: T_EPS }, { id: 'g', label: 'Expected growth rate (% a year)', hint: 'next 7\u201310 years', value: 6, step: 'any' }, { id: 'y', label: 'Current AAA bond yield (%)', value: 9, step: 'any' }, { id: 'price', label: 'Current market price (Rs)', hint: 'optional', value: '', step: '0.01' }],
  fill: loadEps,
  run: function (v) {
    var r = E.calculateGraham({ eps: v.eps, growth_pct: v.g, bond_yield_pct: v.y, price: v.price });
    return H('Graham intrinsic value', rs(r.value), r.verdict ? verdictB(r.verdict) : 'Add the current price to compare') + vsBlock(r) +
      FORM(['Value = EPS &times; (8.5 + 2 &times; growth %) &times; (4.4 &divide; bond yield %).', 'Upside = (value &minus; price) &divide; price. Margin of safety = (value &minus; price) &divide; value.', 'Verdict: more than ' + E.RATES.valuation.fair_band_pct + '% upside = undervalued; within &plusmn;' + E.RATES.valuation.fair_band_pct + '% = fairly valued; otherwise overvalued.']);
  },
  notes: '<p>The formula was designed for stable, profitable companies and uses a US bond yield of 4.4% as its base. Use it as one of several checks. Use a Nepal AAA-rated corporate bond yield that you trust.</p>'
});
reg('val', {
  id: 'pe', name: 'P/E Valuation', blurb: 'Fair value from earnings and a target price-to-earnings multiple.',
  fields: [SYMF, { id: 'eps', label: 'EPS (Rs)', value: 35, step: '0.01', tip: T_EPS }, { id: 'price', label: 'Current market price (Rs)', value: 600, step: '0.01' }, { id: 'pe', label: 'Target or sector average P/E', value: 18, step: 'any' }], fill: loadEps,
  run: function (v) {
    var r = E.calculatePeValuation({ eps: v.eps, price: v.price, target_pe: v.pe });
    return H('Fair value', rs(r.fair_value), verdictB(r.verdict)) + S([['Current P/E', r.current_pe.toFixed(2) + '\u00D7'], ['Target P/E', r.target_pe + '\u00D7'], ['Current price', rs(r.price)], ['Upside / downside', spct(r.upside_pct)]]) +
      FORM(['Current P/E = price &divide; EPS.', 'Fair value = EPS &times; target P/E.', 'Upside = (fair value &minus; price) &divide; price.']);
  },
  notes: '<p>Compare with companies in the same sector. A low P/E can signal a cheap price or falling earnings.</p>'
});
reg('val', {
  id: 'pb', name: 'P/B Valuation', blurb: 'Fair value from book value and a target price-to-book multiple.',
  fields: [SYMF, { id: 'bv', label: 'Book value per share (Rs)', value: 250, step: '0.01', tip: T_BV }, { id: 'price', label: 'Current market price (Rs)', value: 450, step: '0.01' }, { id: 'pb', label: 'Target or sector average P/B', value: 2, step: 'any' }],
  fill: function (r) { var pb = D.g(r, 'P/B'), p = D.g(r, 'LTP'); return { price: p, bv: pb > 0 ? p / pb : null }; }, fillMsg: function () { return 'Loaded the latest price and book value (price divided by P/B)'; },
  run: function (v) {
    var r = E.calculatePbValuation({ bvps: v.bv, price: v.price, target_pb: v.pb });
    return H('Fair value', rs(r.fair_value), verdictB(r.verdict)) + S([['Current P/B', r.current_pb.toFixed(2) + '\u00D7'], ['Target P/B', r.target_pb + '\u00D7'], ['Current price', rs(r.price)], ['Upside / downside', spct(r.upside_pct)]]) +
      FORM(['Current P/B = price &divide; book value per share.', 'Fair value = book value per share &times; target P/B.', 'Upside = (fair value &minus; price) &divide; price.']);
  },
  notes: '<p>P/B suits banks, insurers and other asset-heavy companies best. Compare within the same sector.</p>'
});
reg('val', {
  id: 'divyield', name: 'Dividend Yield', blurb: 'Dividend yield, yield on your cost and expected dividend income.',
  fields: [SYMF, { id: 'd', label: 'Annual dividend per share (Rs)', value: 15, step: '0.01', tip: T_DPS }, { id: 'price', label: 'Current market price (Rs)', value: 600, step: '0.01' }, { id: 'a', label: 'Your average buy price (Rs)', hint: 'optional', value: '', step: '0.01' },
    { id: 'q', label: 'Quantity (kitta)', hint: 'optional', value: '', step: '1' }, { id: 'f', kind: 'sel', label: 'Dividend paid', value: 'annual', opts: [['annual', 'Once a year'], ['semi', 'Twice a year'], ['quarterly', 'Quarterly']] }],
  fill: function (r) { return { price: D.g(r, 'LTP') }; },
  run: function (v) {
    var r = E.calculateDividendYield({ dps: v.d, price: v.price, avg_price: v.a, quantity: v.q, frequency: v.f });
    return H('Dividend yield', pct(r.dividend_yield_pct, 2), r.yield_on_cost_pct == null ? 'Enter your average buy price to see the yield on cost' : 'Yield on your cost: <b>' + pct(r.yield_on_cost_pct, 2) + '</b>') +
      (r.annual_income == null ? '<p class="cmu">Enter your quantity to see dividend income.</p>' : S([['Annual dividend income', rs(r.annual_income)], ['Income per payment (' + r.payments_per_year + ' a year)', rs(r.per_payment_income)]])) +
      FORM(['Dividend yield = annual dividend per share &divide; current price.', 'Yield on cost = annual dividend per share &divide; your average buy price.', 'Annual income = dividend per share &times; quantity.']);
  },
  notes: '<p>Dividends are not guaranteed. Dividend tax is not deducted here.</p>'
});
reg('val', {
  id: 'ddm', name: 'Dividend Discount Model', blurb: 'Value a share from its expected, steadily growing dividends.',
  fields: [SYMF, { id: 'd0', label: 'Current annual dividend per share (Rs)', value: 15, step: '0.01', tip: T_DPS }, { id: 'g', label: 'Expected dividend growth (% a year)', value: 4, step: 'any' }, { id: 'r', label: 'Required rate of return (%)', value: 13, step: 'any' }, { id: 'price', label: 'Current market price (Rs)', hint: 'optional', value: '', step: '0.01' }],
  fill: function (r) { return { price: D.g(r, 'LTP') }; },
  run: function (v) {
    var r = E.calculateDdm({ d0: v.d0, growth_pct: v.g, required_pct: v.r, price: v.price });
    return H('Intrinsic value', rs(r.value), r.verdict ? verdictB(r.verdict) : 'Add the current price to compare') + S([['Next-year dividend', rs(r.next_dividend)]]) + vsBlock(r) +
      FORM(['Next-year dividend D1 = current dividend &times; (1 + growth %).', 'Value = D1 &divide; (required return &minus; growth).', 'Growth must be lower than the required return, otherwise the formula has no answer.']);
  },
  notes: '<p>Very sensitive to the gap between the required return and growth. Works best for companies with a stable dividend record.</p>'
});
reg('val', {
  id: 'dcf', name: 'DCF Valuation', blurb: 'Two-stage discounted cash flow: growth for some years, then steady terminal growth.',
  fields: [SYMF, { id: 'fcf', label: 'Current free cash flow (Rs)', value: 800000000, step: 'any' }, { id: 'g1', label: 'Initial growth rate (% a year)', value: 10, step: 'any' }, { id: 'gt', label: 'Terminal growth rate (%)', value: 4, step: 'any', tip: T_TG },
    { id: 'w', label: 'Discount rate / WACC (%)', value: 12, step: 'any', tip: T_WACC }, { id: 'n', label: 'Projection years', value: 5, step: '1' }, { id: 'nd', label: 'Net debt (Rs)', hint: 'negative if net cash', value: 2000000000, step: 'any' },
    { id: 'sh', label: 'Shares outstanding', value: 100000000, step: 'any' }, { id: 'price', label: 'Current market price (Rs)', hint: 'optional', value: '', step: '0.01' }],
  fill: function (r) { return { price: D.g(r, 'LTP') }; },
  run: function (v) {
    var r = E.calculateDcf({ fcf: v.fcf, growth_pct: v.g1, terminal_pct: v.gt, discount_pct: v.w, years: v.n, net_debt: v.nd, shares: v.sh, price: v.price });
    return H('Fair value per share', rs(r.value), r.verdict ? verdictB(r.verdict) : 'Add the current price to compare') + WARN(r.warnings) +
      S([['Enterprise value', rs(r.enterprise_value)], ['Equity value', rs(r.equity_value)], ['Terminal value', rs(r.terminal_value)], ['PV of terminal value', rs(r.pv_terminal)], ['Terminal value share of total', pct(r.terminal_share_pct, 1)]]) + vsBlock(r) +
      sec('Projection', T(['Year', 'Free cash flow', 'Present value'], r.rows.map(function (x) { return [x.year, n2(x.fcf), n2(x.pv)]; }))) +
      FORM(['Free cash flow in year t = current FCF &times; (1 + initial growth)<sup>t</sup>; present value = FCF &divide; (1 + WACC)<sup>t</sup>.', 'Terminal value = last-year FCF &times; (1 + terminal growth) &divide; (WACC &minus; terminal growth), then discounted back.', 'Enterprise value = sum of present values + present value of terminal value. Equity value = enterprise value &minus; net debt. Per share = equity value &divide; shares.', 'Terminal growth must be lower than the discount rate.']);
  },
  notes: '<p>The result is very sensitive to growth and the discount rate. When the terminal value is most of the total, the answer depends mainly on assumptions about the far future.</p>'
});
reg('val', {
  id: 'intrinsic', name: 'Intrinsic Value', blurb: 'Choose Graham, DDM or DCF, or blend them. Each method is shown separately.',
  fields: [SYMF, { id: 'method', kind: 'sel', label: 'Valuation method', value: 'blend', opts: [['graham', 'Graham'], ['ddm', 'Dividend discount model'], ['dcf', 'DCF'], ['blend', 'Blended average of all three']] }, { id: 'price', label: 'Current market price (Rs)', hint: 'optional', value: '', step: '0.01' },
    { id: 'g_eps', label: 'Graham: EPS (Rs)', value: 35, step: '0.01', tip: T_EPS, showWhen: { id: 'method', 'in': ['graham', 'blend'] } }, { id: 'g_growth', label: 'Graham: growth (% a year)', value: 6, step: 'any', showWhen: { id: 'method', 'in': ['graham', 'blend'] } }, { id: 'g_yield', label: 'Graham: AAA bond yield (%)', value: 9, step: 'any', showWhen: { id: 'method', 'in': ['graham', 'blend'] } },
    { id: 'd_d0', label: 'DDM: annual dividend per share (Rs)', value: 15, step: '0.01', tip: T_DPS, showWhen: { id: 'method', 'in': ['ddm', 'blend'] } }, { id: 'd_growth', label: 'DDM: dividend growth (%)', value: 4, step: 'any', showWhen: { id: 'method', 'in': ['ddm', 'blend'] } }, { id: 'd_req', label: 'DDM: required return (%)', value: 13, step: 'any', showWhen: { id: 'method', 'in': ['ddm', 'blend'] } },
    { id: 'c_fcf', label: 'DCF: current free cash flow (Rs)', value: 800000000, step: 'any', showWhen: { id: 'method', 'in': ['dcf', 'blend'] } }, { id: 'c_g1', label: 'DCF: initial growth (%)', value: 10, step: 'any', showWhen: { id: 'method', 'in': ['dcf', 'blend'] } }, { id: 'c_gt', label: 'DCF: terminal growth (%)', value: 4, step: 'any', tip: T_TG, showWhen: { id: 'method', 'in': ['dcf', 'blend'] } },
    { id: 'c_w', label: 'DCF: discount rate / WACC (%)', value: 12, step: 'any', tip: T_WACC, showWhen: { id: 'method', 'in': ['dcf', 'blend'] } }, { id: 'c_years', label: 'DCF: projection years', value: 5, step: '1', showWhen: { id: 'method', 'in': ['dcf', 'blend'] } },
    { id: 'c_nd', label: 'DCF: net debt (Rs)', value: 2000000000, step: 'any', showWhen: { id: 'method', 'in': ['dcf', 'blend'] } }, { id: 'c_sh', label: 'DCF: shares outstanding', value: 100000000, step: 'any', showWhen: { id: 'method', 'in': ['dcf', 'blend'] } }],
  fill: function (r) { return { price: D.g(r, 'LTP'), g_eps: D.g(r, 'EPS (Annu.)') }; },
  run: function (v) {
    var r = E.calculateIntrinsicValue({ method: v.method, price: v.price, g_eps: v.g_eps, g_growth: v.g_growth, g_yield: v.g_yield, d_d0: v.d_d0, d_growth: v.d_growth, d_req: v.d_req, c_fcf: v.c_fcf, c_g1: v.c_g1, c_gt: v.c_gt, c_w: v.c_w, c_years: v.c_years, c_nd: v.c_nd, c_sh: v.c_sh });
    return H(r.is_blend ? 'Blended intrinsic value (average)' : 'Intrinsic value', rs(r.selected), r.verdict ? verdictB(r.verdict) : 'Add the current price to compare') +
      sec('By method', T(['Method', 'Value per share'], r.methods.map(function (m) { return [m.method, n2(m.value)]; }).concat(r.is_blend ? [['<b>Average</b>', '<b>' + n2(r.average) + '</b>'], ['<b>Median</b>', '<b>' + n2(r.median) + '</b>']] : []))) +
      (r.skipped.length ? '<p class="cmu">Not included (no data entered): ' + r.skipped.join(', ') + '.</p>' : '') + vsBlock(r) +
      FORM(['Graham = EPS &times; (8.5 + 2 &times; growth) &times; (4.4 &divide; bond yield). DDM = next dividend &divide; (required return &minus; growth). DCF = two-stage discounted cash flow per share.', 'Blended value = simple average of the methods that have data. The median is shown beside it.', 'Margin of safety = (value &minus; price) &divide; value.']);
  },
  notes: '<p>Different methods can give very different answers. A wide spread is a sign that assumptions matter more than the maths.</p>'
});
reg('val', {
  id: 'targetprice', name: 'Target Price', blurb: 'A target price from EPS and a target P/E, or from your own intrinsic value, and the move needed from today.',
  fields: [SYMF, { id: 'mode', kind: 'sel', label: 'Based on', value: 'pe', opts: [['pe', 'EPS and target P/E'], ['iv', 'My intrinsic value']] }, { id: 'eps', label: 'EPS (Rs)', value: 35, step: '0.01', tip: T_EPS, showWhen: { id: 'mode', 'in': ['pe'] } }, { id: 'pe', label: 'Target P/E', value: 18, step: 'any', showWhen: { id: 'mode', 'in': ['pe'] } },
    { id: 'iv', label: 'Intrinsic value (Rs)', value: 700, step: '0.01', showWhen: { id: 'mode', 'in': ['iv'] } }, { id: 'price', label: 'Current market price (Rs)', value: 600, step: '0.01' }], fill: loadEps,
  run: function (v) {
    var r = E.calculateTargetPrice({ mode: v.mode, eps: v.eps, target_pe: v.pe, intrinsic_value: v.iv, price: v.price });
    return H('Target price', rs(r.target_price), verdictB(r.verdict)) + S([['Current price', rs(r.price)], ['Upside / downside', spct(r.upside_pct)], ['Price change needed', pnl(r.required_change)]]) +
      FORM(['Target price = EPS &times; target P/E, or your intrinsic value.', 'Upside = (target &minus; current price) &divide; current price.']);
  },
  notes: '<p>A target price is an estimate, not a forecast or a recommendation.</p>'
});
reg('val', {
  id: 'mos', name: 'Margin of Safety', blurb: 'How far the market price sits below your estimate of intrinsic value.',
  fields: [SYMF, { id: 'iv', label: 'Intrinsic value (Rs)', value: 700, step: '0.01' }, { id: 'price', label: 'Current market price (Rs)', value: 500, step: '0.01', tip: T_MOS }], fill: function (r) { return { price: D.g(r, 'LTP') }; },
  run: function (v) {
    var r = E.calculateMarginOfSafety({ intrinsic_value: v.iv, price: v.price });
    return '<div class="chd"><small>Margin of safety</small><div class="cbig">' + spct(r.margin_of_safety_pct) + '</div><div class="csub">' + verdictB(r.verdict) + '</div></div>' + S([['Intrinsic value', rs(r.intrinsic_value)], ['Market price', rs(r.price)]]) +
      FORM(['Margin of safety = (intrinsic value &minus; market price) &divide; intrinsic value.', 'Strong: ' + E.RATES.valuation.mos_strong_pct + '% or more. Moderate: ' + E.RATES.valuation.mos_moderate_pct + '% to below ' + E.RATES.valuation.mos_strong_pct + '%. Limited: 0% to below ' + E.RATES.valuation.mos_moderate_pct + '%. Below 0%: trading above estimated value.']);
  },
  notes: '<p>The margin of safety is only as reliable as the intrinsic value you enter.</p>'
});
reg('val', {
  id: 'ey', name: 'Earnings Yield', blurb: 'Earnings as a percentage of the price, compared with a risk-free or fixed-deposit rate.',
  fields: [SYMF, { id: 'eps', label: 'EPS (Rs)', value: 35, step: '0.01', tip: T_EPS }, { id: 'price', label: 'Current market price (Rs)', value: 600, step: '0.01' }, { id: 'rf', label: 'Risk-free or fixed-deposit rate (%)', hint: 'optional', value: 8, step: 'any' }], fill: loadEps,
  run: function (v) {
    var r = E.calculateEarningsYield({ eps: v.eps, price: v.price, risk_free_pct: v.rf });
    return H('Earnings yield', pct(r.earnings_yield_pct, 2), r.difference_pp == null ? 'Enter a risk-free or fixed-deposit rate to compare' : 'Difference from the risk-free rate: ' + spct(r.difference_pp).replace('%', ' pts')) + (r.interpretation ? '<p>' + esc(r.interpretation) + '</p>' : '') +
      FORM(['Earnings yield = EPS &divide; current price (the inverse of P/E).', 'Difference = earnings yield &minus; risk-free rate, in percentage points.']);
  },
  notes: '<p>Earnings can change and are not paid out in full as dividends. This is a quick comparison, not a return you will receive.</p>'
});

/* ---------- groups, navigation, overview ---------- */
var GROUPS = [
  { id: 'trade', name: 'Trading & Costs', blurb: 'Buy, sell, average and margin' },
  { id: 'corp', name: 'Corporate Actions', blurb: 'Bonus, right and dividend adjustments' },
  { id: 'tech', name: 'Technical', blurb: 'Support and resistance levels' },
  { id: 'val', name: 'Investment & Valuation', blurb: 'Intrinsic value, P/E, P/B, DCF and margin of safety' },
  { id: 'growth', name: 'Growth & Loans', blurb: 'SIP, CAGR and EMI' },
  { id: 'dates', name: 'Date Converter', blurb: 'BS \u21C4 AD dates and exact age' },
  { id: 'conv', name: 'Converter Tools', blurb: 'Numbers to words and unit conversion' }
];
/* order inside each group (a new calculator without an entry here goes to the end of its group) */
var ORDER = ['buy', 'sell', 'averaging', 'margin', 'pl', 'breakeven', 'target', 'possize', 'rr', 'bonusshares', 'rightshares', 'combined', 'drip', 'bonus', 'right', 'dividend', 'pivot',
  'graham', 'pe', 'pb', 'divyield', 'ddm', 'dcf', 'intrinsic', 'targetprice', 'mos', 'ey', 'sip', 'cagr', 'emi', 'date', 'age', 'numwords', 'units'];
CALCS.sort(function (a, b) { var i = ORDER.indexOf(a.id), j = ORDER.indexOf(b.id); return (i < 0 ? 99 : i) - (j < 0 ? 99 : j); });
var byId = {}; CALCS.forEach(function (c) { byId[c.id] = c; });
var cur = null, lastIn = {};   // lastIn: last calculator opened in each group

/* Two-level bar: ALL + the five groups; under it, the calculators of the selected group */
nav.innerHTML = '<div class="cn-row" id="cn-groups" role="group" aria-label="Calculator groups"><button type="button" data-g="home">ALL</button>' +
  GROUPS.map(function (g) { return '<button type="button" data-g="' + g.id + '">' + g.name.replace('&', '&amp;').toUpperCase() + '</button>'; }).join('') + '</div>' +
  '<div class="cn-row cn-sub" id="cn-subs" role="group" aria-label="Calculators in this group" hidden>' +
  CALCS.map(function (c) { return '<button type="button" data-c="' + c.id + '" data-grp="' + c.group + '" hidden>' + c.name.toUpperCase() + '</button>'; }).join('') + '</div>';
var subs = nav.querySelector('#cn-subs');

function home() {
  body.innerHTML = '<div class="chead"><h2>Calculators</h2><p>Practical calculators for Nepal\u2019s capital market and personal finance. Enter your numbers; nothing is stored or sent anywhere.</p></div>' +
    GROUPS.map(function (g) {
      return '<h3 class="gh">' + g.name.replace('&', '&amp;') + ' <small>' + g.blurb + '</small></h3><div class="ovg">' + CALCS.filter(function (c) { return c.group === g.id; }).map(function (c) {
        return '<button type="button" class="ovc" data-c="' + c.id + '"><b>' + c.name + (c.badge ? ' <span class="badge wa">' + c.badge + '</span>' : '') + '</b><span>' + c.blurb + '</span></button>';
      }).join('') + '</div>';
    }).join('') + '<p class="cdisc">' + DISC + '</p>';
}
function open(id, quiet) {
  if (id !== 'home' && !byId[id]) id = 'home';
  cur = id;
  var grp = id === 'home' ? 'home' : byId[id].group;
  if (id !== 'home') lastIn[grp] = id;
  nav.querySelectorAll('#cn-groups button').forEach(function (b) { var on = b.dataset.g === grp; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); });
  subs.hidden = grp === 'home';
  subs.querySelectorAll('button').forEach(function (b) { b.hidden = b.dataset.grp !== grp; b.classList.toggle('on', b.dataset.c === id); });
  body.innerHTML = '';
  if (id === 'home') home(); else { var w = document.createElement('div'); w.className = 'cwrap'; body.appendChild(w); byId[id].render(w); }
  if (!quiet) {
    try { history.replaceState(null, '', id === 'home' ? '#calc' : '#calc/' + id); } catch (e) { /* ignore */ }
    window.scrollTo(0, 0);
  }
}
nav.addEventListener('click', function (e) {
  var b = e.target.closest('button'); if (!b) return;
  var curGroup = cur === null ? null : (cur === 'home' ? 'home' : byId[cur].group);
  if (b.dataset.g === curGroup || (b.dataset.c && b.dataset.c === cur)) return;   // already there: keep what the user typed
  if (b.dataset.g) { if (b.dataset.g === 'home') open('home'); else open(lastIn[b.dataset.g] || CALCS.filter(function (c) { return c.group === b.dataset.g; })[0].id); }
  else if (b.dataset.c) open(b.dataset.c);
});
body.addEventListener('click', function (e) { var b = e.target.closest('.ovc'); if (b) open(b.dataset.c); });

/* ---------- Rates & rules panel: the one place where NEPSE rules are edited (applies until the page is reloaded) ---------- */
var RT = E.RATES, DEF = E.DEFAULT_RATES, panel = document.createElement('details');
panel.className = 'cdet rules'; panel.id = 'cn-rules';
function rf(id, label, val, step) { return '<div class="fld"><label for="rl_' + id + '">' + label + '</label><input id="rl_' + id + '" type="number" inputmode="decimal" step="' + (step || 'any') + '" value="' + val + '"></div>'; }
function rulesHtml() {
  var s = RT.brokerage.slabs, rows = s.map(function (x, k) {
    return '<tr><td>' + (k === s.length - 1 ? 'Above Rs ' + whole(s[k - 1][0]) : (k ? 'Rs ' + whole(s[k - 1][0]) + ' to ' : 'Up to ') + '<input id="rl_lim' + k + '" type="number" step="any" value="' + x[0] + '" aria-label="Upper limit of slab ' + (k + 1) + '">') + '</td><td><input id="rl_rate' + k + '" type="number" step="any" value="' + +(x[1] * 100).toFixed(4) + '" aria-label="Rate of slab ' + (k + 1) + ' in percent"> %</td></tr>';
  }).join('');
  return '<summary>Rates &amp; rules (editable)</summary><p class="cmu">NEPSE-style defaults used by the trading calculators. Change them if your broker or the rules differ. Changes apply to all calculators until you reload the page and are not saved or sent anywhere.</p>' +
    '<div class="ct"><table class="avt"><thead><tr><th>Broker commission: transaction value (Rs)</th><th>Rate</th></tr></thead><tbody>' + rows + '</tbody></table></div><div class="rg">' +
    rf('min', 'Minimum commission (Rs)', RT.brokerage.minFee) + rf('sebon', 'SEBON fee (% of value)', +(RT.sebon * 100).toFixed(4)) + rf('dp', 'DP charge (Rs, sell side)', RT.dp) +
    rf('cs', 'CGT: held 365 days or less (%)', +(RT.cgt.individual_short * 100).toFixed(4)) + rf('cl', 'CGT: held more than 365 days (%)', +(RT.cgt.individual_long * 100).toFixed(4)) + rf('ci', 'CGT: institutional (%)', +(RT.cgt.institutional * 100).toFixed(4)) + rf('tds', 'Dividend tax (%)', +(RT.tds * 100).toFixed(4)) +
    rf('band', 'Fairly valued band (+/- % of price)', RT.valuation.fair_band_pct) + rf('tg', 'Warn if terminal growth above (%)', RT.valuation.terminal_growth_warn_pct) + rf('ms', 'Strong margin of safety from (%)', RT.valuation.mos_strong_pct) + rf('mm', 'Moderate margin of safety from (%)', RT.valuation.mos_moderate_pct) +
    '</div><div class="cact"><button type="button" class="cbtn primary" data-r="apply">Apply</button><button type="button" class="cbtn" data-r="reset">Reset to defaults</button></div><div class="symmsg" id="rl_msg" role="status"></div>';
}
function drawRules() { var o = panel.open; panel.innerHTML = rulesHtml(); panel.open = o; }
function applyRules() {
  var q = function (id) { return panel.querySelector('#rl_' + id); }, g = function (id, label, o) { return E.num(q(id).value, label, o); }, msg = panel.querySelector('#rl_msg');
  try {
    var n = RT.brokerage.slabs.length, lim = [], rate = [], k;
    for (k = 0; k < n; k++) { rate.push(g('rate' + k, 'Commission rate ' + (k + 1), { min: 0, max: 5 }) / 100); if (k < n - 1) lim.push(g('lim' + k, 'Slab limit ' + (k + 1), { min: 0, minInclusive: false })); }
    for (k = 1; k < lim.length; k++) if (lim[k] <= lim[k - 1]) throw new E.CalcError('Slab limits', 'must increase from one slab to the next');
    var v = { min: g('min', 'Minimum commission', { min: 0 }), sebon: g('sebon', 'SEBON fee', { min: 0, max: 5 }) / 100, dp: g('dp', 'DP charge', { min: 0 }), cs: g('cs', 'CGT up to 365 days', { min: 0, max: 100 }) / 100, cl: g('cl', 'CGT over 365 days', { min: 0, max: 100 }) / 100,
      ci: g('ci', 'Institutional CGT', { min: 0, max: 100 }) / 100, tds: g('tds', 'Dividend tax', { min: 0, max: 99 }) / 100, band: g('band', 'Fairly valued band', { min: 0, max: 100 }), tg: g('tg', 'Terminal growth warning', { min: 0, max: 20 }), ms: g('ms', 'Strong margin of safety', { min: 0, max: 100 }), mm: g('mm', 'Moderate margin of safety', { min: 0, max: 100 }) };
    if (v.mm > v.ms) throw new E.CalcError('Margin of safety', 'moderate level must not exceed the strong level');
    for (k = 0; k < n; k++) { RT.brokerage.slabs[k][1] = rate[k]; if (k < n - 1) RT.brokerage.slabs[k][0] = lim[k]; }
    RT.brokerage.minFee = v.min; RT.brokerage.minFeeUpTo = RT.brokerage.slabs[0][0]; RT.sebon = v.sebon; RT.dp = v.dp; RT.cgt.individual_short = v.cs; RT.cgt.individual_long = v.cl; RT.cgt.institutional = v.ci; RT.tds = v.tds;
    RT.valuation.fair_band_pct = v.band; RT.valuation.terminal_growth_warn_pct = v.tg; RT.valuation.mos_strong_pct = v.ms; RT.valuation.mos_moderate_pct = v.mm;
    if (cur) open(cur, true); drawRules(); panel.querySelector('#rl_msg').textContent = 'Applied. The open calculator was refreshed with the new rules.';
  } catch (e) { if (e && e.name === 'CalculatorInputError') msg.textContent = 'Please check: ' + e.message; else throw e; }
}
function resetRules() {
  var s = RT.brokerage.slabs; DEF.brokerage.slabs.forEach(function (x, k) { s[k][0] = x[0]; s[k][1] = x[1]; });
  RT.brokerage.minFee = DEF.brokerage.minFee; RT.brokerage.minFeeUpTo = DEF.brokerage.minFeeUpTo; RT.sebon = DEF.sebon; RT.dp = DEF.dp; RT.tds = DEF.tds;
  ['individual_short', 'individual_long', 'institutional'].forEach(function (k) { RT.cgt[k] = DEF.cgt[k]; }); Object.keys(DEF.valuation).forEach(function (k) { RT.valuation[k] = DEF.valuation[k]; });
  if (cur) open(cur, true); drawRules(); panel.querySelector('#rl_msg').textContent = 'Defaults restored.';
}
drawRules(); host.insertBefore(panel, body);
panel.addEventListener('click', function (e) { var b = e.target.closest('button'); if (!b) return; if (b.dataset.r === 'apply') applyRules(); else if (b.dataset.r === 'reset') resetRules(); });

window.NNH_CALC = { show: function () { if (!cur) open('home', true); }, open: open, list: function () { return CALCS.map(function (c) { return c.id; }); } };

var m = /^#calc(?:\/([a-z]+))?$/.exec(location.hash || '');
if (m) { D.go('calc'); open(m[1] || 'home', true); }
})();

</script>
'''


# --------------------------------------------------------------------------------------
# HYDROPOWER tab configuration
# --------------------------------------------------------------------------------------
# The Smart Wealth Pro export has NO sector column, so the Stock Screener cannot tell which companies are
# hydropower. This list was taken from a third-party sector listing (hamroshare.com.np "Hydro Power" sector,
# retrieved 2026-10-08, cross-checked by name against ashishdanai.com.np's NEPSE class list). It is NOT an official
# NEPSE/SEBON classification and is UNVERIFIED: check it against NEPSE before relying on it.
# To override without editing this file, create hydro_symbols.txt next to this script (symbols separated by
# spaces, commas or new lines; lines starting with # are ignored). Symbols missing from the loaded data are skipped.
# Not included on purpose: HIDCL (an investment company, not a generator); CHDC (named as hydro by one source only).
HYDRO_SYMBOLS = (
    "SOHL", "UPPER", "SAHAS", "CHCL", "BPCL", "API", "MEN", "SHPC", "TAMOR", "MBJC", "GVL", "NGPL", "RADHI",
    "SGHL", "SMHL", "MANDU", "RHPL", "PHCL", "SKHL", "AHPC", "AKPL", "SHEL", "SJCL", "RLEL", "SNORL", "UPCL",
    "RIDI", "BHL", "UMHL", "VLUCL", "AKJCL", "USHEC", "NHPC", "HPPL", "HDHPC", "KAHL", "MEL", "SSHL", "TPKHL",
    "LEC", "SIKLES", "BJHL", "DORDI", "TVCL", "MEPDL", "SIPD", "BHCL", "DHEL", "SKHEL", "NYADI", "HURJA",
    "BHDC", "GLH", "GHL", "SMJC", "SGHC", "KPCL", "MSHL", "RFPL", "SPDL", "SANVI", "BUNGAL", "RURU", "HHL",
    "BARUN", "MMKJL", "KHPL", "YMHL", "UHEWA", "PMHPL", "PPCL", "SMH", "BGWT", "MHNL", "RHGCL", "MAKAR", "PPL",
    "MHL", "TPC", "APHL", "NHDL", "UNHPL", "KKHC", "MKHC", "UMRH", "HIMSTAR", "MHCL", "BNHC", "MABEL", "CKHL",
    "CHL", "MEHL", "EHPL", "IHL", "MKHL", "SPC", "ULHC", "TSHL", "DOLTI", "BEDC", "JOSHI", "SPHL", "MCHL",
    "AHL", "MKJC", "KBSH", "RAWA", "SPL", "USHL", "BHPL", "DHPL",
)
# Weights (percent) of the Hydropower Score components. Components with no data are dropped and the rest re-weighted.
HYDRO_SCORE_WEIGHTS = {"Profitability": 35, "Valuation": 25, "Leverage": 25, "Dividend": 15}


def hydro_symbols() -> list:
    f = Path(__file__).resolve().parent / "hydro_symbols.txt"
    syms = HYDRO_SYMBOLS
    if f.is_file():
        try:
            txt = "\n".join(l for l in f.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
            syms = re.split(r"[\s,;]+", txt)
        except Exception as exc:
            log.warning("hydro_symbols.txt ignored: %s", exc)
    return sorted({x.strip().upper() for x in syms if re.fullmatch(r"[A-Za-z0-9]{1,12}", x.strip())})


def hydro_weights() -> dict:
    w = {k: float(v) for k, v in HYDRO_SCORE_WEIGHTS.items() if isinstance(v, (int, float)) and v > 0}
    return w or {"Profitability": 1.0}


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


# --------------------------------------------------------------------------------------
# AI PHOTOSHOP tab (v1.5): fourth main tab, placed right after CALCULATORS.
# First tool: "Remove Image Background". The browser sends the image to YOUR serverless proxy
# (Cloudflare Worker, see cloudflare-worker/). The proxy holds the REMOVE_BG_API_KEY secret.
# The key is NEVER in this file or in the GitHub repository.
# To change the proxy address, edit PROXY_URL near the top of the script below.
# --------------------------------------------------------------------------------------
AI_PHOTOSHOP_BLOCK = r'''<style>
#aiph{padding:12px 16px 28px;max-width:1200px;margin:0 auto;min-width:0}
#aiph[hidden]{display:none!important}
#aiph .chead h2{margin:4px 0 4px;font-size:20px}
#aiph .chead p{margin:0 0 12px;color:var(--mu);font-size:13px;max-width:80ch}
#aiph .agrid{display:grid;grid-template-columns:minmax(0,4fr) minmax(0,8fr);gap:14px;align-items:start}
@media(max-width:820px){#aiph .agrid{grid-template-columns:minmax(0,1fr)}}
#aiph .cc{background:var(--p);border:1px solid var(--ln);border-radius:8px;padding:14px;min-width:0}
#aiph h3{margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mu)}
#aiph .drop{border:2px dashed var(--ln);border-radius:8px;padding:18px 12px;text-align:center;background:var(--ch);transition:border-color .15s ease}
#aiph .drop.over{border-color:var(--ac)}
#aiph .drop p{margin:8px 0 0;font-size:12px;color:var(--mu)}
#aiph .abtn{background:var(--ac);color:var(--onac);border:0;border-radius:6px;padding:8px 16px;min-height:38px;font:inherit;font-weight:700;font-size:13px;cursor:pointer}
#aiph .abtn:hover:not(:disabled){background:var(--ach)}
#aiph .abtn.ghost{background:var(--p);color:var(--tx);border:1px solid var(--ln);font-weight:600}
#aiph .abtn.ghost:hover:not(:disabled){border-color:var(--ac);color:var(--ac);background:var(--p)}
#aiph .abtn:disabled{opacity:.45;cursor:not-allowed}
#aiph .abtn:focus-visible,#aiph .drop:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
#aiph .aact{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
#aiph .finfo{margin-top:10px;font-size:12px;color:var(--mu);overflow-wrap:anywhere;min-height:1.2em}
#aiph .finfo b{color:var(--tx)}
#aiph .amsg{margin-top:10px;font-size:13px;min-height:1.3em}
#aiph .amsg.err{background:var(--ch);border:1px solid var(--bad);border-radius:6px;padding:8px 12px;color:var(--bad)}
#aiph .amsg.ok{color:var(--ok)}
#aiph .anote{margin-top:14px;border-top:1px solid var(--ln);padding-top:10px;font-size:11.5px;color:var(--mu)}
#aiph .pv{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
@media(max-width:560px){#aiph .pv{grid-template-columns:minmax(0,1fr)}}
#aiph .pane{min-width:0}
#aiph .pane small{display:block;margin:0 0 6px;color:var(--mu);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
#aiph .box{position:relative;border:1px solid var(--ln);border-radius:8px;min-height:240px;aspect-ratio:1/1;display:grid;place-items:center;overflow:hidden;background:var(--ch)}
#aiph .box.chk{background-color:var(--p);background-image:linear-gradient(45deg,var(--ch) 25%,transparent 25%,transparent 75%,var(--ch) 75%),linear-gradient(45deg,var(--ch) 25%,transparent 25%,transparent 75%,var(--ch) 75%);background-size:20px 20px;background-position:0 0,10px 10px}
#aiph .box img{max-width:100%;max-height:100%;object-fit:contain;display:block}
#aiph .box .ph{color:var(--mu);font-size:12px;padding:12px;text-align:center}
#aiph .box[hidden],#aiph .ph[hidden],#aiph img[hidden]{display:none}
#aiph .busy{position:absolute;inset:0;display:none;flex-direction:column;gap:10px;align-items:center;justify-content:center;background:rgba(0,0,0,.55);color:#fff;font-size:13px;text-align:center;padding:10px}
#aiph .busy.on{display:flex}
#aiph .spin{width:34px;height:34px;border:4px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:aiphspin .8s linear infinite}
@keyframes aiphspin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){#aiph .spin{animation-duration:2.4s}}
@media(max-width:640px){#aiph{padding:10px 8px 24px}#aiph .abtn{flex:1 1 auto}}
@media(max-width:480px){.vtabs:not(.sub){overflow-x:auto}}
/* ===== AI Photoshop: tool switcher + Unblur & Enhance Photo ===== */
#aiph .aitools{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}
#aiph .aitool{background:var(--p);color:var(--tx);border:1px solid var(--ln);border-radius:16px;padding:6px 16px;min-height:36px;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
#aiph .aitool:hover{border-color:var(--ac);color:var(--ac)}
#aiph .aitool.on{background:var(--ac);color:var(--onac);border-color:transparent}
#aiph .aitool:focus-visible,#aiph .chip:focus-visible,#aiph .segb:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
#aiph .engrid{display:grid;grid-template-columns:minmax(0,4fr) minmax(0,8fr);grid-template-rows:auto 1fr;grid-template-areas:"up cmp" "ctl cmp";gap:14px;align-items:start}
#aiph .en-up{grid-area:up}#aiph .en-ctl{grid-area:ctl}
#aiph .en-cmp{grid-area:cmp;position:sticky;top:84px}
@media(max-width:820px){#aiph .engrid{grid-template-columns:minmax(0,1fr);grid-template-rows:none;grid-template-areas:"up" "cmp" "ctl"}#aiph .en-cmp{position:static}}
#aiph .chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}
#aiph .chip{background:var(--p);color:var(--tx);border:1px solid var(--ln);border-radius:16px;padding:5px 12px;min-height:34px;font:inherit;font-size:12px;cursor:pointer}
#aiph .chip:hover{border-color:var(--ac);color:var(--ac)}
#aiph .chip.on{background:var(--ac);color:var(--onac);border-color:transparent}
#aiph .srow{display:grid;grid-template-columns:1fr auto;gap:2px 8px;margin:0 0 8px}
#aiph .srow label{font-size:12px;font-weight:600}
#aiph .srow output{font-size:12px;color:var(--mu);font-variant-numeric:tabular-nums}
#aiph .srow input[type=range]{grid-column:1/-1;width:100%;accent-color:var(--ac);margin:2px 0 0;min-height:26px}
#aiph .srow input[type=range]:disabled{opacity:.5}
#aiph .seg2{display:flex;border:1px solid var(--ln);border-radius:6px;overflow:hidden;margin:0 0 12px;width:max-content;max-width:100%}
#aiph .segb{background:var(--p);color:var(--tx);border:0;border-right:1px solid var(--ln);padding:6px 14px;min-height:34px;font:inherit;font-size:12px;font-weight:600;cursor:pointer}
#aiph .segb:last-child{border-right:0}
#aiph .segb.on{background:var(--ac);color:var(--onac)}
#aiph .fsel{width:100%;background:var(--p);color:var(--tx);border:1px solid var(--ln);border-radius:6px;padding:6px 10px;min-height:38px;font:inherit;margin:0 0 10px}
#aiph .cmpwrap{display:flex;justify-content:center}
#aiph .cmpbox{position:relative;width:100%;aspect-ratio:4/3;border:1px solid var(--ln);border-radius:8px;overflow:hidden;background:var(--ch);touch-action:pan-y;user-select:none;-webkit-user-select:none;cursor:ew-resize}
#aiph .cmpbox canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
#aiph .cmpbox canvas[hidden]{display:none}
#aiph .cmpbox canvas.after{clip-path:inset(0 0 0 50%)}
#aiph .enph{position:absolute;inset:0;display:grid;place-items:center;color:var(--mu);font-size:12px;padding:12px;text-align:center;cursor:default}
#aiph .enph[hidden]{display:none}
#aiph .cmpline{position:absolute;top:0;bottom:0;left:50%;width:2px;margin-left:-1px;background:#fff;box-shadow:0 0 0 1px rgba(0,0,0,.4);pointer-events:none}
#aiph .cmpknob{position:absolute;top:50%;left:50%;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;background:#fff;color:#222;display:grid;place-items:center;font-size:15px;box-shadow:0 1px 6px rgba(0,0,0,.5)}
#aiph .cmptag{position:absolute;top:8px;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;background:rgba(0,0,0,.62);color:#fff;padding:2px 9px;border-radius:10px;pointer-events:none}
#aiph .cmptag.l{left:8px}#aiph .cmptag.r{right:8px}
#aiph .cmpbox[data-empty="1"] .cmpline,#aiph .cmpbox[data-empty="1"] .cmptag{display:none}
#aiph #enPos{width:100%;accent-color:var(--ac);margin:10px 0 0;min-height:26px}
#aiph .encap{margin-top:8px;font-size:12px;color:var(--mu);overflow-wrap:anywhere}
</style>
<section id="aiph" hidden aria-label="AI Photoshop">
 <div class="aitools" role="group" aria-label="AI Photoshop tools">
  <button type="button" class="aitool on" data-ait="bg" aria-pressed="true">Remove Background</button>
  <button type="button" class="aitool" data-ait="en" aria-pressed="false">Unblur &amp; Enhance</button>
  <button type="button" class="aitool" data-ait="pass" aria-pressed="false">Passport Photo</button>
 </div>
<div id="aiToolBg">
 <div class="chead"><h2>Remove Image Background</h2>
  <p>Upload a JPG, PNG or WebP photo and get back a transparent PNG with the background removed.</p></div>
 <div class="agrid">
  <div class="cc">
   <h3>1. Choose image</h3>
   <div class="drop" id="aiDrop" tabindex="0" role="button" aria-label="Choose an image or drop it here">
    <button type="button" class="abtn" id="aiPick">Choose Image</button>
    <p>or drag and drop it here<br>JPG, PNG or WebP, up to 10 MB</p>
    <input type="file" id="aiFile" accept="image/jpeg,image/png,image/webp" hidden>
   </div>
   <div class="finfo" id="aiInfo"></div>
   <h3 style="margin-top:14px">2. Remove background</h3>
   <div class="aact">
    <button type="button" class="abtn" id="aiGo">Remove Background</button>
    <button type="button" class="abtn ghost" id="aiReset">Reset</button>
   </div>
   <div class="amsg" id="aiMsg" role="status" aria-live="polite"></div>
   <div class="anote">Your image is sent to an online background-removal service to be processed. Do not upload confidential client or company images.</div>
  </div>
  <div class="cc">
   <h3>3. Before and after</h3>
   <div class="pv">
    <div class="pane"><small>Before</small>
     <div class="box" id="aiBeforeBox"><span class="ph" id="aiBeforePh">Your image will appear here</span><img id="aiBefore" alt="Original image preview" hidden></div></div>
    <div class="pane"><small>After (transparent PNG)</small>
     <div class="box chk" id="aiAfterBox"><span class="ph" id="aiAfterPh">The result will appear here</span><img id="aiAfter" alt="Image with background removed" hidden>
      <div class="busy" id="aiBusy" role="alert"><div class="spin" aria-hidden="true"></div><span>Removing background...<br>this can take a few seconds</span></div></div></div>
   </div>
   <div class="aact"><button type="button" class="abtn" id="aiDl" disabled>Download PNG</button></div>
  </div>
 </div>
</div>
<div id="aiToolEn" hidden>
 <div class="chead"><h2>Unblur &amp; Enhance Photo</h2>
  <p>Sharpen soft photos and improve brightness, contrast and colour. Everything happens inside your browser: your photo is never uploaded and no online service is used.</p></div>
 <div class="engrid">
  <div class="cc en-up">
   <h3>1. Choose photo</h3>
   <div class="drop" id="enDrop" tabindex="0" role="button" aria-label="Choose a photo or drop it here">
    <button type="button" class="abtn" id="enPick">Choose Photo</button>
    <p>or drag and drop it here<br>JPG, PNG or WebP, up to 15 MB</p>
    <input type="file" id="enFile" accept="image/jpeg,image/png,image/webp" hidden>
   </div>
   <div class="finfo" id="enInfo"></div>
   <div class="amsg" id="enMsg" role="status" aria-live="polite"></div>
  </div>

  <div class="cc en-cmp">
   <h3>Before / After</h3>
   <div class="seg2" role="group" aria-label="Comparison view">
    <button type="button" class="segb on" data-view="split" aria-pressed="true">Split</button>
    <button type="button" class="segb" data-view="before" aria-pressed="false">Original</button>
    <button type="button" class="segb" data-view="after" aria-pressed="false">Enhanced</button>
   </div>
   <div class="cmpwrap">
    <div class="cmpbox" id="enBox" data-empty="1">
     <span class="enph" id="enPh">Your photo will appear here</span>
     <canvas id="enBefore" hidden aria-label="Original photo"></canvas>
     <canvas id="enAfter" class="after" hidden aria-label="Enhanced photo"></canvas>
     <span class="cmptag l">Original</span><span class="cmptag r">Enhanced</span>
     <div class="cmpline" id="enLine"><div class="cmpknob" aria-hidden="true">&#8646;</div></div>
     <div class="busy" id="enBusy" role="alert"><div class="spin" aria-hidden="true"></div><span id="enBusyTxt">Processing...</span></div>
    </div>
   </div>
   <input type="range" id="enPos" min="0" max="100" value="50" aria-label="Before and after comparison position">
   <div class="encap" id="enCap">Drag the line on the photo, or use the slider, to compare. The preview is shown at reduced size; the download uses your full-size photo.</div>
  </div>

  <div class="cc en-ctl">
   <h3>2. Presets</h3>
   <div class="chips" role="group" aria-label="Presets">
    <button type="button" class="chip" data-preset="auto">Auto Enhance</button>
    <button type="button" class="chip" data-preset="unblur">Unblur</button>
    <button type="button" class="chip" data-preset="portrait">Portrait</button>
    <button type="button" class="chip" data-preset="product">Product Photo</button>
    <button type="button" class="chip" data-preset="reset">Reset</button>
   </div>
   <h3>3. Fine tune</h3>
   <div class="srow"><label for="enSharp">Sharpness</label><output id="enSharpV" for="enSharp">0</output><input type="range" id="enSharp" min="0" max="100" value="0" disabled></div>
   <div class="srow"><label for="enLocal">Local contrast (clarity)</label><output id="enLocalV" for="enLocal">0</output><input type="range" id="enLocal" min="0" max="100" value="0" disabled></div>
   <div class="srow"><label for="enBright">Brightness</label><output id="enBrightV" for="enBright">0</output><input type="range" id="enBright" min="-100" max="100" value="0" disabled></div>
   <div class="srow"><label for="enContrast">Contrast</label><output id="enContrastV" for="enContrast">0</output><input type="range" id="enContrast" min="-100" max="100" value="0" disabled></div>
   <div class="srow"><label for="enSat">Saturation</label><output id="enSatV" for="enSat">0</output><input type="range" id="enSat" min="-100" max="100" value="0" disabled></div>
   <div class="srow"><label for="enDenoise">Denoise / smoothing</label><output id="enDenoiseV" for="enDenoise">0</output><input type="range" id="enDenoise" min="0" max="100" value="0" disabled></div>
   <h3 style="margin-top:14px">4. Size and download</h3>
   <div class="seg2" role="group" aria-label="Image scale">
    <button type="button" class="segb on" data-scale="1" aria-pressed="true">Original</button>
    <button type="button" class="segb" data-scale="1.5" aria-pressed="false">1.5x</button>
    <button type="button" class="segb" data-scale="2" aria-pressed="false">2x</button>
   </div>
   <select id="enFmt" class="fsel" aria-label="Download format">
    <option value="png">PNG (best quality)</option>
    <option value="jpg">JPG (smaller file)</option>
   </select>
   <div class="aact"><button type="button" class="abtn" id="enDl">Download Enhanced Image</button></div>
   <div class="encap" id="enSize"></div>
   <div class="anote">Private: this tool runs entirely in your browser. Nothing is uploaded or stored.</div>
  </div>
 </div>
</div>
<div id="aiToolPass" hidden>
<div id="pass">
 <div class="chead"><h2>Passport Photo Maker</h2>
  <p>Make a passport photo (Nepal 35 &times; 45 mm by default) and an A4 sheet ready to print. Everything happens inside your browser: your photo is never uploaded or stored.</p></div>

 <div class="ppgrid">

  <!-- 1. UPLOAD -->
  <div class="cc pp-up">
   <h3>1. Choose photo</h3>
   <div class="drop" id="ppDrop" tabindex="0" role="button" aria-label="Choose a photo or drop it here">
    <button type="button" class="abtn" id="ppPick">Choose Photo</button>
    <p>or drag and drop it here<br>JPG, PNG or WebP, up to 15 MB</p>
    <input type="file" id="ppFile" accept="image/jpeg,image/png,image/webp" hidden>
   </div>
   <div class="finfo" id="ppInfo"></div>
   <div class="amsg" id="ppMsg" role="status" aria-live="polite"></div>
  </div>

  <!-- 2. EDITOR: drag + zoom the photo under a fixed 35:45 frame -->
  <div class="cc pp-ed">
   <h3>2. Position your face</h3>
   <div class="ppwrap">
    <div class="ppframe" id="ppFrame" tabindex="0" role="group" aria-label="Photo editor. Drag to move the photo, scroll or use the zoom slider to zoom. Arrow keys also move the photo.">
     <canvas id="ppCanvas" hidden></canvas>
     <span class="ppph" id="ppPh">Your photo will appear here</span>
     <div class="ppguides" id="ppGuides" hidden></div>
     <div class="busy" id="ppBusy" role="alert"><div class="spin" aria-hidden="true"></div><span id="ppBusyTxt">Working...</span></div>
    </div>
   </div>
   <div class="zoomrow"><label for="ppZoom">Zoom</label><input type="range" id="ppZoom" min="-100" max="100" value="0" disabled><output id="ppZoomV" for="ppZoom">100%</output></div>
   <div class="ctlrow">
    <label class="chk"><input type="checkbox" id="ppShowGuides" checked> Show guides</label>
    <button type="button" class="abtn ghost" id="ppFit">Reset position</button>
   </div>
   <div class="bgstat" id="ppBg">Background check: choose a photo first.</div>
   <div class="legend" aria-label="Guide legend">
    <span><i style="border-color:#e11d48"></i>Top of head</span>
    <span><i class="bx"></i>Chin zone (head height)</span>
    <span><i style="border-color:#1d4ed8;border-top-style:dashed"></i>Centre line</span>
    <span><i class="tn"></i>Keep background plain white (outside the dashed safe area)</span>
   </div>
   <div class="encap">Drag the photo to move it. Zoom with the slider, mouse wheel or by pinching. Put the top of the head on the red line and the chin inside the green zone. Photos with a transparent background (for example from <b>AI Photoshop &rarr; Remove Background</b>) get a white background automatically.</div>
  </div>

  <!-- 3. SIZE + ADJUSTMENTS -->
  <div class="cc pp-set">
   <h3>3. Photo size</h3>
   <div class="fld"><label for="ppPreset">Country / size</label>
    <select id="ppPreset" class="fsel">
     <option value="nepal">Nepal Passport: 35 &times; 45 mm</option>
     <option value="us">US Passport: 51 &times; 51 mm</option>
     <option value="custom">Custom size (mm)</option>
    </select></div>
   <div class="cust" id="ppCustom" hidden>
    <div><label for="ppW">Width (mm)</label><input type="number" id="ppW" class="nin" min="20" max="100" step="0.5" value="35"></div>
    <div><label for="ppH">Height (mm)</label><input type="number" id="ppH" class="nin" min="20" max="100" step="0.5" value="45"></div>
    <div><label for="ppHmin">Head height min (%)</label><input type="number" id="ppHmin" class="nin" min="30" max="95" step="1" value="70"></div>
    <div><label for="ppHmax">Head height max (%)</label><input type="number" id="ppHmax" class="nin" min="30" max="95" step="1" value="80"></div>
   </div>
   <div class="encap" id="ppSpec"></div>
   <h3 style="margin-top:14px">4. Adjust</h3>
   <div class="srow"><label for="ppBright">Brightness</label><output id="ppBrightV" for="ppBright">0</output><input type="range" id="ppBright" min="-50" max="50" value="0"></div>
   <div class="srow"><label for="ppContrast">Contrast</label><output id="ppContrastV" for="ppContrast">0</output><input type="range" id="ppContrast" min="-50" max="50" value="0"></div>
   <div class="srow"><label for="ppSharp">Sharpness</label><output id="ppSharpV" for="ppSharp">0</output><input type="range" id="ppSharp" min="0" max="100" value="0"></div>
   <button type="button" class="abtn ghost" id="ppAdjReset" style="margin-top:4px">Reset adjustments</button>
  </div>

 </div>

 <!-- 5. RESULT: final preview, checklist, downloads (outside the grid so the sticky editor can never cover it) -->
 <div class="cc pp-out">
   <h3>5. Check and download</h3>
   <div class="outgrid">
    <div>
     <div class="finalbox" id="ppFinalBox" style="max-width:220px"><canvas id="ppFinal"></canvas></div>
     <div class="encap" id="ppFinalCap" style="text-align:center">Final photo (no guides)</div>
    </div>
    <div>
     <div style="font-size:13px;font-weight:700;margin:0 0 8px">Checklist before you download</div>
     <ul class="checklist" id="ppChecklist">
      <li><label><input type="checkbox"> Plain white background</label></li>
      <li><label><input type="checkbox"> Face centered</label></li>
      <li><label><input type="checkbox"> Eyes open</label></li>
      <li><label><input type="checkbox"> Neutral expression</label></li>
      <li><label><input type="checkbox"> No shadows</label></li>
      <li><label><input type="checkbox"> No hats unless religious/medical reasons</label></li>
     </ul>
    </div>
    <div>
     <div class="dlrow"><button type="button" class="abtn" id="ppDlJpg">Download Digital Photo</button></div>
     <div class="encap" id="ppJpgCap"></div>
     <div class="sheetbox">
      <div class="peoplebox">
       <div class="dlrow"><button type="button" class="abtn ghost" id="ppAddPerson">+ Add this photo to the print sheet</button></div>
       <div class="peoplelist" id="ppPeople" aria-live="polite"></div>
       <div class="encap" id="ppPeopleCap">One person only? Just download the print sheet. For several people: position each person's photo, click the button above, then load the next photo. The copies are shared equally between the people.</div>
      </div>
      <div class="twocol">
       <div><label for="ppPaper">Paper</label>
        <select id="ppPaper" class="fsel"><option value="p46" selected>4 x 6 in photo paper (10 x 15 cm)</option><option value="a4p">A4 portrait (210 x 297 mm)</option><option value="a4l">A4 landscape (297 x 210 mm)</option><option value="p57">5 x 7 in photo paper</option></select></div>
       <div><label for="ppLayout">Layout</label>
        <select id="ppLayout" class="fsel"><option value="center" selected>Centered, evenly spaced</option><option value="top">Compact at the top (saves paper)</option></select></div>
      </div>
      <div class="twocol">
       <div><label for="ppCuts">Cutting guides</label>
        <select id="ppCuts" class="fsel"><option value="edge" selected>Cut marks in the margins (photo studio style)</option><option value="marks">Corner marks at each photo</option><option value="box">Thin lines around photos</option><option value="none">None</option></select></div>
       <div></div>
      </div>
      <div class="twocol">
       <div><label for="ppCopies">Copies on sheet</label>
        <select id="ppCopies" class="fsel"><option value="2">2</option><option value="4">4</option><option value="6">6</option><option value="8" selected>8</option><option value="10">10</option><option value="12">12</option><option value="max">As many as fit</option></select></div>
       <div><label for="ppSheetFmt">Sheet file</label>
        <select id="ppSheetFmt" class="fsel"><option value="jpg" selected>JPG, 300 DPI (for a photo shop)</option><option value="pdf">PDF (best for printing at home or office)</option></select></div>
      </div>
      <div class="dlrow"><button type="button" class="abtn" id="ppDlSheet">Download Print Sheet</button></div>
      <div class="encap" id="ppSheetCap"></div>
     </div>
    </div>
   </div>
   <div class="amsg" id="ppMsg2" role="status" aria-live="polite"></div>
   <div class="anote">Private: this tool runs entirely in your browser. Nothing is uploaded or stored. Rules can change: always confirm the current photo requirements with the passport office before you apply.</div>
 </div>
</div>
</div>
</section>
<script>
(function () {
'use strict';
/* ====== SETTINGS: the only place you may need to edit ====== */
// Address of YOUR Cloudflare Worker (see cloudflare-worker/README). Replace YOUR-SUBDOMAIN after deploying it.
var PROXY_URL = 'https://binjal-photo-api.binjal-halwai.workers.dev/api/remove-background';
var MAX_BYTES = 10 * 1024 * 1024;                      // 10 MB (remove.bg itself allows 22 MB)
var TYPES = ['image/jpeg', 'image/png', 'image/webp'];
var TIMEOUT_MS = 60000;
/* =========================================================== */

var $ = function (id) { return document.getElementById(id); };
var file = null, beforeUrl = null, afterUrl = null, busy = false;
var el = {
  drop: $('aiDrop'), pick: $('aiPick'), input: $('aiFile'), info: $('aiInfo'), go: $('aiGo'), reset: $('aiReset'),
  msg: $('aiMsg'), before: $('aiBefore'), beforePh: $('aiBeforePh'), after: $('aiAfter'), afterPh: $('aiAfterPh'),
  busy: $('aiBusy'), dl: $('aiDl')
};
if (!el.drop) return;

function say(text, kind) { el.msg.textContent = text || ''; el.msg.className = 'amsg' + (kind ? ' ' + kind : ''); }
function sizeText(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB'; }
function revoke(u) { if (u) { try { URL.revokeObjectURL(u); } catch (e) { } } }

function clearAfter() {
  revoke(afterUrl); afterUrl = null;
  el.after.removeAttribute('src'); el.after.hidden = true; el.afterPh.hidden = false; el.dl.disabled = true;
}
function setBusy(on) {
  busy = on; el.busy.classList.toggle('on', on);
  el.go.disabled = on; el.reset.disabled = on; el.pick.disabled = on;
}

/* ---- validation (messages are shown to the user) ---- */
function problem(f) {
  if (!f) return 'No file selected. Please choose an image first.';
  if (TYPES.indexOf(f.type) < 0) return 'Unsupported file type. Please use a JPG, PNG or WebP image.';
  if (f.size > MAX_BYTES) return 'File too large (' + sizeText(f.size) + '). The maximum size is ' + sizeText(MAX_BYTES) + '.';
  if (f.size === 0) return 'This file is empty. Please choose another image.';
  return '';
}

function choose(f) {
  clearAfter(); say('');
  var p = problem(f);
  if (p) {
    file = null; revoke(beforeUrl); beforeUrl = null;
    el.before.removeAttribute('src'); el.before.hidden = true; el.beforePh.hidden = false;
    el.info.textContent = ''; say(p, 'err'); return;
  }
  file = f; revoke(beforeUrl); beforeUrl = URL.createObjectURL(f);
  el.before.src = beforeUrl; el.before.hidden = false; el.beforePh.hidden = true;
  el.info.innerHTML = '';
  var b = document.createElement('b'); b.textContent = f.name;
  el.info.appendChild(b); el.info.appendChild(document.createTextNode(' (' + sizeText(f.size) + ')'));
}

function fail(status, serverMsg) {
  var m, code = ' (error ' + status + (serverMsg ? ': ' + serverMsg : '') + ')';
  if (status === 400) m = 'The service could not process this image. Try a different photo.';
  else if (status === 401) m = 'The background-removal key was rejected by the provider. The site owner must check the API key in the Worker.';
  else if (status === 402) m = 'The background-removal service has run out of credits.';
  else if (status === 403) m = 'The service refused the request. Either this website address is not in the Worker allowed list, or the provider rejected the API key.';
  else if (status === 404) m = 'The Worker address is wrong (page not found). The site owner must check PROXY_URL.';
  else if (status === 413) m = 'File too large for the service. Please use a smaller image.';
  else if (status === 415) m = 'Unsupported file type. Please use a JPG, PNG or WebP image.';
  else if (status === 429) m = 'Too many requests. Please wait a minute and try again.';
  else if (status === 500) m = 'The Worker is not set up correctly (the secret REMOVE_BG_API_KEY is probably missing).';
  else if (status >= 500) m = 'The background-removal service is having problems. Please try again later.';
  else m = 'Something went wrong.';
  say(m + code + ' You can still use Unblur & Enhance or Passport Photo.', 'err');
}

async function run() {
  if (busy) return;
  var p = problem(file);
  if (p) { say(p, 'err'); return; }
  if (PROXY_URL.indexOf('YOUR-SUBDOMAIN') >= 0) {
    say('The background remover is not connected yet. The site owner must set PROXY_URL to the deployed Worker address.', 'err'); return;
  }
  clearAfter(); say(''); setBusy(true);
  var ctl = new AbortController(), timer = setTimeout(function () { ctl.abort(); }, TIMEOUT_MS);
  try {
    var fd = new FormData();
    fd.append('image_file', file, file.name);
    var res = await fetch(PROXY_URL, { method: 'POST', body: fd, signal: ctl.signal });
    if (!res.ok) {
      var detail = '';
      try { var j = await res.json(); detail = (j && (j.error || j.message)) || ''; } catch (e) { }
      fail(res.status, detail); return;
    }
    var blob = await res.blob();
    if (!blob || blob.size === 0 || blob.type.indexOf('image/') !== 0) { say('The service returned an unexpected response. Please try again.', 'err'); return; }
    afterUrl = URL.createObjectURL(blob);
    el.after.src = afterUrl; el.after.hidden = false; el.afterPh.hidden = true; el.dl.disabled = false;
    say('Done. Check the result, then click Download PNG.', 'ok');
  } catch (e) {
    if (e && e.name === 'AbortError') say('The request took too long and was stopped. Please try again.', 'err');
    else say('Could not reach the background-removal Worker. Check your internet connection. If it is fine, the site owner should open the Worker address in a browser (it should show status ok) and check that https://binjalhalwai.github.io is in ALLOWED_ORIGINS.', 'err');
  } finally {
    clearTimeout(timer); setBusy(false);
  }
}

function download() {
  if (!afterUrl) return;
  var base = (file && file.name ? file.name.replace(/\.[^.]+$/, '') : 'image').replace(/[^\w\-]+/g, '_') || 'image';
  var a = document.createElement('a'); a.href = afterUrl; a.download = base + '-no-bg.png';
  document.body.appendChild(a); a.click(); a.remove();
}

function reset() {
  file = null; revoke(beforeUrl); beforeUrl = null; clearAfter(); el.input.value = '';
  el.before.removeAttribute('src'); el.before.hidden = true; el.beforePh.hidden = false; el.info.textContent = ''; say('');
}

el.pick.addEventListener('click', function (e) { e.stopPropagation(); el.input.click(); });
el.drop.addEventListener('click', function () { if (!busy) el.input.click(); });
el.drop.addEventListener('keydown', function (e) { if ((e.key === 'Enter' || e.key === ' ') && e.target === el.drop) { e.preventDefault(); if (!busy) el.input.click(); } });
el.input.addEventListener('change', function () { choose(el.input.files && el.input.files[0]); });
['dragenter', 'dragover'].forEach(function (n) { el.drop.addEventListener(n, function (e) { e.preventDefault(); el.drop.classList.add('over'); }); });
['dragleave', 'drop'].forEach(function (n) { el.drop.addEventListener(n, function (e) { e.preventDefault(); el.drop.classList.remove('over'); }); });
el.drop.addEventListener('drop', function (e) { if (!busy && e.dataTransfer && e.dataTransfer.files.length) choose(e.dataTransfer.files[0]); });
el.go.addEventListener('click', run);
el.reset.addEventListener('click', reset);
el.dl.addEventListener('click', download);
})();
</script>
<script>
(function () {
'use strict';
/* =========================================================================
   UNBLUR & ENHANCE PHOTO  -  runs 100% in the browser (HTML Canvas).
   No upload, no API key, no server.

   Map of this file (for beginners):
     1. SETTINGS  - limits and preset values you can safely edit
     2. ENGINE    - the image filters: blur, denoise, tone, sharpen
     3. SCREEN    - buttons, sliders, preview, before/after, download
   ========================================================================= */

/* ---------------- 1. SETTINGS ---------------- */
var MAX_BYTES = 15 * 1024 * 1024;            // biggest file accepted (15 MB)
var TYPES = ['image/jpeg', 'image/png', 'image/webp'];
var PREVIEW_LONG = 1280;                     // preview size in pixels (smaller = faster sliders)
var MAX_LONG_SIDE = 6000;                    // very large photos are reduced to this (longest side)
var MAX_OUTPUT_PIXELS = 16000000;            // output limit: phones cannot make bigger canvases
var MAX_SOURCE_PIXELS = 100000000;           // refuse absurd sizes that would crash the browser
var JPG_QUALITY = 0.95;                      // JPG download quality (0 to 1)
var LIVE_DELAY = 150;                        // wait (ms) after a slider moves before re-processing

// Slider values (all start at 0). Meaning of each:
//   sharp    0-100   edge sharpening (the "unblur" strength)
//   local    0-100   local contrast / clarity (makes detail pop)
//   bright  -100-100 brighter or darker
//   contrast-100-100 more or less contrast
//   sat     -100-100 colour strength
//   denoise  0-100   smoothing of grain and noise (keeps strong edges)
var DEFAULTS = { sharp: 0, local: 0, bright: 0, contrast: 0, sat: 0, denoise: 0 };

// Presets. Edit the numbers to change how each button behaves.
// ("Auto Enhance" is calculated from your photo, see autoSettings() below.)
var PRESETS = {
  unblur:   { sharp: 70, local: 30, bright: 0, contrast: 6,  sat: 4,  denoise: 25 },
  portrait: { sharp: 25, local: 10, bright: 6, contrast: 4,  sat: 4,  denoise: 35 },
  product:  { sharp: 50, local: 35, bright: 8, contrast: 14, sat: 15, denoise: 15 }
};

/* ---------------- 2. ENGINE ---------------- */

// Fast box blur of ONE channel (an array of 0-255 numbers, one per pixel).
// Used by denoise, local contrast and sharpening.
function blurH(src, dst, w, h, r) {            // horizontal pass
  var div = 2 * r + 1, half = div >> 1;
  for (var y = 0; y < h; y++) {
    var o = y * w, sum = src[o] * (r + 1);
    for (var i = 1; i <= r; i++) sum += src[o + Math.min(i, w - 1)];
    for (var x = 0; x < w; x++) {
      dst[o + x] = (sum + half) / div | 0;
      sum += src[o + Math.min(x + r + 1, w - 1)] - src[o + Math.max(x - r, 0)];
    }
  }
}
function blurV(src, dst, w, h, r) {            // vertical pass
  var div = 2 * r + 1, half = div >> 1, sums = new Int32Array(w), x, y, i, row;
  for (x = 0; x < w; x++) sums[x] = src[x] * (r + 1);
  for (i = 1; i <= r; i++) { row = Math.min(i, h - 1) * w; for (x = 0; x < w; x++) sums[x] += src[row + x]; }
  for (y = 0; y < h; y++) {
    var o = y * w, add = Math.min(y + r + 1, h - 1) * w, sub = Math.max(y - r, 0) * w;
    for (x = 0; x < w; x++) { dst[o + x] = (sums[x] + half) / div | 0; sums[x] += src[add + x] - src[sub + x]; }
  }
}
function blur(plane, w, h, r, passes) {       // 2 passes give a smooth, Gaussian-like blur
  var tmp = new Uint8ClampedArray(plane.length), out = new Uint8ClampedArray(plane.length), cur = plane;
  for (var p = 0; p < passes; p++) { blurH(cur, tmp, w, h, r); blurV(tmp, out, w, h, r); cur = out; }
  return out;
}

// Step A: denoise. Blends each pixel toward its blurred version, but only where the
// difference is small (noise). Strong edges are left alone so they stay sharp.
function denoise(d, w, h, amt, r) {
  var n = w * h, plane = new Uint8ClampedArray(n), thr = 20 + 20 * amt, i;
  for (var c = 0; c < 3; c++) {
    for (i = 0; i < n; i++) plane[i] = d[i * 4 + c];
    var bl = blur(plane, w, h, r, 2);
    for (i = 0; i < n; i++) {
      var diff = bl[i] - plane[i], ad = diff < 0 ? -diff : diff, keep = 1 - ad / thr;
      if (keep > 0) d[i * 4 + c] = plane[i] + diff * amt * keep;
    }
  }
}

// Step B: brightness, contrast, saturation.
// Built with curves that keep pure black and pure white fixed, so dark and bright
// areas are not crushed or blown out (this is what keeps the result natural).
//   brightness : gamma curve        (lifts or lowers the midtones)
//   contrast   : smooth S-curve     (more contrast in the middle, gentle at the ends)
//   saturation : stops at the colour limit so colours never clip or shift hue
function toneCurve(p) {
  var lut = new Float32Array(256), gam = Math.pow(2, -p.bright / 80), c = p.contrast / 100, i, x, y;
  for (i = 0; i < 256; i++) {
    x = Math.pow(i / 255, gam);                                           // brightness
    if (c > 0) {
      var t = Math.min(2, c * 1.8), k1 = Math.min(1, t);
      y = x + k1 * (x * x * (3 - 2 * x) - x);                             // S-curve
      if (t > 1) y = y + (t - 1) * (y * y * (3 - 2 * y) - y);             // stronger S-curve for high values
      x = y;
    } else if (c < 0) x = 0.5 + (x - 0.5) * (1 + c);                      // flatten
    lut[i] = x * 255;
  }
  return lut;
}
function tone(d, n, p) {
  var lut = toneCurve(p), s = 1 + p.sat / 100;
  for (var i = 0, j = 0; i < n; i++, j += 4) {
    var r = lut[d[j]], g = lut[d[j + 1]], b = lut[d[j + 2]];
    if (s !== 1) {
      var gray = 0.299 * r + 0.587 * g + 0.114 * b, m = s, dr = r - gray, dg = g - gray, db = b - gray, q;
      if (s > 1) {                                                        // limit so no channel goes past 0 or 255
        if (dr > 0) { q = (255 - gray) / dr; if (q < m) m = q; } else if (dr < 0) { q = gray / -dr; if (q < m) m = q; }
        if (dg > 0) { q = (255 - gray) / dg; if (q < m) m = q; } else if (dg < 0) { q = gray / -dg; if (q < m) m = q; }
        if (db > 0) { q = (255 - gray) / db; if (q < m) m = q; } else if (db < 0) { q = gray / -db; if (q < m) m = q; }
        if (m < 1) m = 1;
      }
      r = gray + dr * m; g = gray + dg * m; b = gray + db * m;
    }
    d[j] = r; d[j + 1] = g; d[j + 2] = b;
  }
}

// Step C: local contrast + sharpening. Works on brightness (luminance) only, so
// colours do not shift. Three pieces:
//   1) local contrast : big-radius "unsharp mask" (pixel minus its wide blur)
//   2) unsharp mask   : small-radius sharpening of edges
//   3) 3x3 sharpen convolution kernel  [ 0 -a 0 / -a 1+4a -a / 0 -a 0 ]  for fine edges
// Noise guard: tiny differences are boosted less. Halo guard: change is capped (LIMIT).
function detail(d, w, h, p, S, dil) {
  var n = w * h, Y = new Uint8ClampedArray(n), i, j;
  for (i = 0, j = 0; i < n; i++, j += 4) Y[i] = 0.299 * d[j] + 0.587 * d[j + 1] + 0.114 * d[j + 2];
  var lAmt = p.local / 100 * 0.8, uAmt = p.sharp / 100 * 1.1, kAmt = p.sharp / 100 * 0.18, LIMIT = 48;
  var BL = lAmt > 0 ? blur(Y, w, h, Math.max(6, Math.round(Math.max(w, h) * 0.02)), 2) : null;
  var BS = uAmt > 0 ? blur(Y, w, h, Math.max(1, Math.round(2 * S)), 2) : null;
  for (var y = 0; y < h; y++) {
    var row = y * w, up = (y >= dil ? y - dil : 0) * w, dn = (y + dil < h ? y + dil : h - 1) * w;
    for (var x = 0; x < w; x++) {
      var k = row + x, v = Y[k], delta = 0;
      if (BL) { var tt = v / 255; delta += lAmt * (v - BL[k]) * (0.25 + 3 * tt * (1 - tt)); }   // weaker in very dark / very bright areas
      if (BS) {
        var t = v - BS[k], a = t < 0 ? -t : t;
        delta += uAmt * t * (a >= 6 ? 1 : a / 6);
        var xl = x >= dil ? x - dil : 0, xr = x + dil < w ? x + dil : w - 1;
        var lap = 4 * v - Y[row + xl] - Y[row + xr] - Y[up + x] - Y[dn + x], la = lap < 0 ? -lap : lap;
        delta += kAmt * lap * (la >= 12 ? 1 : la / 12);
      }
      if (delta > LIMIT) delta = LIMIT; else if (delta < -LIMIT) delta = -LIMIT;
      j = k * 4; d[j] += delta; d[j + 1] += delta; d[j + 2] += delta;
    }
  }
}

function isIdentity(p) { return !(p.sharp || p.local || p.bright || p.contrast || p.sat || p.denoise); }

// Runs all steps on an ImageData (changes it in place). Filter sizes grow with the
// picture size, so the small preview and the big download look the same.
function enhance(img, p) {
  if (isIdentity(p)) return;
  var w = img.width, h = img.height, d = img.data, S = Math.max(w, h) / PREVIEW_LONG;
  if (p.denoise > 0) denoise(d, w, h, p.denoise / 100, Math.max(1, Math.round(1.5 * S)));
  if (p.bright || p.contrast || p.sat) tone(d, w * h, p);
  if (p.local > 0 || p.sharp > 0) detail(d, w, h, p, S, Math.max(1, Math.round(S)));
}

// "Auto Enhance": looks at the photo's brightness and contrast and picks slider values.
function autoSettings(data) {
  var d = data, n = d.length / 4, hist = new Uint32Array(256), sum = 0, i, y;
  for (i = 0; i < d.length; i += 4) { y = (0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]) | 0; hist[y]++; sum += y; }
  var mean = sum / n, acc = 0, lo = 0, hi = 255;
  for (i = 0; i < 256; i++) { acc += hist[i]; if (acc >= n * 0.01) { lo = i; break; } }
  acc = 0;
  for (i = 255; i >= 0; i--) { acc += hist[i]; if (acc >= n * 0.01) { hi = i; break; } }
  var spread = Math.max(hi - lo, 60);
  var contrast = spread < 215 ? Math.min(35, Math.round((235 / spread - 1) * 60)) : 0;
  var bright = Math.max(-30, Math.min(30, Math.round(118 - mean)));
  return { sharp: 45, local: 25, bright: bright, contrast: contrast, sat: 10, denoise: 12 };
}

/* ---------------- 3. SCREEN ---------------- */
var $ = function (id) { return document.getElementById(id); };
var KEYS = ['sharp', 'local', 'bright', 'contrast', 'sat', 'denoise'];
var IDS = { sharp: 'enSharp', local: 'enLocal', bright: 'enBright', contrast: 'enContrast', sat: 'enSat', denoise: 'enDenoise' };
var el = {
  drop: $('enDrop'), pick: $('enPick'), file: $('enFile'), info: $('enInfo'), msg: $('enMsg'),
  box: $('enBox'), ph: $('enPh'), before: $('enBefore'), after: $('enAfter'), line: $('enLine'), pos: $('enPos'),
  busy: $('enBusy'), busyTxt: $('enBusyTxt'), dl: $('enDl'), fmt: $('enFmt'), size: $('enSize')
};
if (!el.drop) return;
var bctx = el.before.getContext('2d', { willReadFrequently: true });
var actx = el.after.getContext('2d', { willReadFrequently: true });

var work = null;            // the photo (canvas), reduced only if it is huge
var baseData = null;        // original preview pixels (ImageData)
var fileName = 'photo';
var ready = false, scale = 1, pos = 50;
var working = false, dirty = false, liveTimer = null, busyTimer = null;

function say(text, kind) { el.msg.textContent = text || ''; el.msg.className = 'amsg' + (kind ? ' ' + kind : ''); }
function sizeText(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB'; }
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }

function busyOn(text, now) {
  clearTimeout(busyTimer); el.busyTxt.textContent = text || 'Processing...';
  if (now) el.busy.classList.add('on'); else busyTimer = setTimeout(function () { el.busy.classList.add('on'); }, 150);
}
function busyOff() { clearTimeout(busyTimer); el.busy.classList.remove('on'); }

/* --- check the chosen file --- */
function problem(f) {
  if (!f) return 'No image selected. Please choose a photo first.';
  var okType = TYPES.indexOf(f.type) >= 0 || (!f.type && /\.(jpe?g|png|webp)$/i.test(f.name));
  if (!okType) return 'Unsupported file type. Please use a JPG, PNG or WebP image.';
  if (f.size === 0) return 'This file is empty. Please choose another photo.';
  if (f.size > MAX_BYTES) return 'File too large (' + sizeText(f.size) + '). The maximum size is ' + sizeText(MAX_BYTES) + '.';
  return '';
}

/* --- read the photo and prepare the preview --- */
function loadFile(f) {
  say('');
  var p = problem(f);
  if (p) { say(p, 'err'); return; }
  var url = URL.createObjectURL(f), img = new Image();
  img.onload = function () {
    URL.revokeObjectURL(url);
    try { start(img, f); } catch (e) { console.error(e); say('Image processing failed. The photo may be too large for this browser. Try a smaller photo.', 'err'); }
  };
  img.onerror = function () { URL.revokeObjectURL(url); say('This image could not be read. The file may be damaged. Please try another photo.', 'err'); };
  img.src = url;
}

function start(img, f) {
  var nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) { say('This image could not be read. Please try another photo.', 'err'); return; }
  if (nw * nh > MAX_SOURCE_PIXELS) { say('The picture is too large (' + nw + ' x ' + nh + ' pixels). Please use a smaller photo.', 'err'); return; }
  var k = Math.min(1, MAX_LONG_SIDE / Math.max(nw, nh), Math.sqrt(MAX_OUTPUT_PIXELS / (nw * nh)));
  var ww = Math.max(1, Math.round(nw * k)), wh = Math.max(1, Math.round(nh * k));
  var c = document.createElement('canvas'); c.width = ww; c.height = wh;
  var cx = c.getContext('2d'); cx.imageSmoothingEnabled = true; cx.imageSmoothingQuality = 'high';
  cx.drawImage(img, 0, 0, ww, wh);
  var pk = Math.min(1, PREVIEW_LONG / Math.max(ww, wh)), pw = Math.max(1, Math.round(ww * pk)), ph = Math.max(1, Math.round(wh * pk));
  el.before.width = el.after.width = pw; el.before.height = el.after.height = ph;
  bctx.imageSmoothingQuality = 'high'; bctx.drawImage(c, 0, 0, pw, ph);
  baseData = bctx.getImageData(0, 0, pw, ph);
  actx.putImageData(baseData, 0, 0);
  work = c; fileName = f.name.replace(/\.[^.]+$/, '').replace(/[^\w\-]+/g, '_') || 'photo'; ready = true;

  var ratio = pw / ph;                                   // fit the comparison box to the photo
  el.box.style.aspectRatio = pw + ' / ' + ph;
  el.box.style.width = 'min(100%, ' + (70 * ratio).toFixed(2) + 'vh)';
  el.box.removeAttribute('data-empty'); el.ph.hidden = true; el.before.hidden = false; el.after.hidden = false;

  KEYS.forEach(function (key) { $(IDS[key]).disabled = false; });
  setParams(DEFAULTS); markPreset(null); setScale(1); setPos(50);
  el.info.innerHTML = '';
  var b = document.createElement('b'); b.textContent = f.name;
  el.info.appendChild(b);
  el.info.appendChild(document.createTextNode(' (' + sizeText(f.size) + ', ' + nw + ' x ' + nh + ' px)' + (k < 1 ? ' - reduced to ' + ww + ' x ' + wh + ' for processing' : '')));
  say('Photo loaded. Pick a preset or move a slider.', 'ok');
}

/* --- sliders --- */
function readParams() { var p = {}; KEYS.forEach(function (key) { p[key] = +$(IDS[key]).value; }); return p; }
function setParams(p) { KEYS.forEach(function (key) { $(IDS[key]).value = p[key]; $(IDS[key] + 'V').textContent = p[key]; }); }
function markPreset(name) {
  document.querySelectorAll('#aiph [data-preset]').forEach(function (b) { b.classList.toggle('on', b.dataset.preset === name && name !== 'reset'); });
}
KEYS.forEach(function (key) {
  $(IDS[key]).addEventListener('input', function () { $(IDS[key] + 'V').textContent = this.value; markPreset(null); schedule(); });
});
function schedule() { clearTimeout(liveTimer); liveTimer = setTimeout(runPreview, LIVE_DELAY); }

/* --- presets --- */
document.querySelectorAll('#aiph [data-preset]').forEach(function (b) {
  b.addEventListener('click', function () {
    if (!ready) { say('No image selected. Please choose a photo first.', 'err'); return; }
    var name = b.dataset.preset;
    if (name === 'reset') { setParams(DEFAULTS); setScale(1); }
    else setParams(name === 'auto' ? autoSettings(baseData.data) : PRESETS[name]);
    markPreset(name); say(''); clearTimeout(liveTimer); runPreview();
  });
});

/* --- scale (Original, 1.5x, 2x) and output size --- */
function outSize() {
  var f = Math.min(scale, Math.sqrt(MAX_OUTPUT_PIXELS / (work.width * work.height)));
  return { w: Math.round(work.width * f), h: Math.round(work.height * f), limited: f < scale - 0.001 };
}
function showSize() {
  if (!ready) { el.size.textContent = ''; return; }
  var s = outSize();
  el.size.textContent = 'Download size: ' + s.w + ' x ' + s.h + ' px' + (s.limited ? ' (limited to about 16 megapixels to protect your browser)' : '');
}
function setScale(v) {
  scale = v;
  document.querySelectorAll('#aiph [data-scale]').forEach(function (b) { var on = +b.dataset.scale === v; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); });
  showSize();
}
document.querySelectorAll('#aiph [data-scale]').forEach(function (b) { b.addEventListener('click', function () { setScale(+b.dataset.scale); }); });

/* --- live preview (small copy of the photo, so sliders feel fast) --- */
async function runPreview() {
  if (!ready) return;
  if (working) { dirty = true; return; }
  working = true; dirty = false; busyOn('Enhancing...', false);
  try {
    await sleep(20);                                                  // let the loading state appear
    var copy = new ImageData(new Uint8ClampedArray(baseData.data), baseData.width, baseData.height);
    enhance(copy, readParams());
    actx.putImageData(copy, 0, 0);
  } catch (e) { console.error(e); say('Image processing failed. Please try a smaller photo or another browser.', 'err'); }
  finally { working = false; busyOff(); if (dirty) runPreview(); }
}

/* --- before / after slider --- */
function setPos(v) {
  pos = clamp(v, 0, 100);
  el.after.style.clipPath = 'inset(0 0 0 ' + pos + '%)';             // enhanced photo is shown on the RIGHT of the line
  el.line.style.left = pos + '%'; el.pos.value = pos;
  var mode = pos >= 99.5 ? 'before' : pos <= 0.5 ? 'after' : 'split';
  document.querySelectorAll('#aiph [data-view]').forEach(function (b) { var on = b.dataset.view === mode; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); });
}
el.pos.addEventListener('input', function () { setPos(+this.value); });
document.querySelectorAll('#aiph [data-view]').forEach(function (b) {
  b.addEventListener('click', function () { setPos(b.dataset.view === 'before' ? 100 : b.dataset.view === 'after' ? 0 : 50); });
});
var dragging = false;
function moveFrom(e) { var r = el.box.getBoundingClientRect(); if (r.width > 0) setPos((e.clientX - r.left) / r.width * 100); }
el.box.addEventListener('pointerdown', function (e) { if (!ready) return; dragging = true; try { el.box.setPointerCapture(e.pointerId); } catch (x) { } moveFrom(e); });
el.box.addEventListener('pointermove', function (e) { if (dragging) moveFrom(e); });
['pointerup', 'pointercancel'].forEach(function (n) { el.box.addEventListener(n, function () { dragging = false; }); });

/* --- download the full-size result --- */
async function download() {
  if (!ready) { say('No image selected. Please choose a photo first.', 'err'); return; }
  while (working) await sleep(30);                                    // wait for a running preview
  working = true; clearTimeout(liveTimer); say(''); busyOn('Creating full-size image...', true); el.dl.disabled = true;
  var c = null;
  try {
    await sleep(40);
    var s = outSize();
    c = document.createElement('canvas'); c.width = s.w; c.height = s.h;
    var cx = c.getContext('2d', { willReadFrequently: true });
    cx.imageSmoothingEnabled = true; cx.imageSmoothingQuality = 'high';
    cx.drawImage(work, 0, 0, s.w, s.h);                               // 1) scale
    var id = cx.getImageData(0, 0, s.w, s.h);
    enhance(id, readParams());                                        // 2) enhance
    cx.putImageData(id, 0, 0);
    var jpg = el.fmt.value === 'jpg', out = c;
    if (jpg) {                                                        // JPG has no transparency: put the photo on white
      out = document.createElement('canvas'); out.width = s.w; out.height = s.h;
      var ox = out.getContext('2d'); ox.fillStyle = '#fff'; ox.fillRect(0, 0, s.w, s.h); ox.drawImage(c, 0, 0);
    }
    var blob = await new Promise(function (res) { out.toBlob(res, jpg ? 'image/jpeg' : 'image/png', JPG_QUALITY); });
    if (!blob) throw new Error('The browser could not create the file');
    var a = document.createElement('a'), url = URL.createObjectURL(blob);
    a.href = url; a.download = fileName + '-enhanced.' + (jpg ? 'jpg' : 'png');
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
    if (out !== c) { out.width = 0; }
    say('Downloaded ' + a.download + ' (' + s.w + ' x ' + s.h + ' px, ' + sizeText(blob.size) + ').', 'ok');
  } catch (e) {
    console.error(e); say('Image processing failed. Try a smaller photo, a smaller scale, or another browser.', 'err');
  } finally {
    if (c) c.width = 0;
    working = false; busyOff(); el.dl.disabled = false; if (dirty) runPreview();
  }
}

/* --- wire up the upload area and buttons --- */
el.pick.addEventListener('click', function (e) { e.stopPropagation(); el.file.click(); });
el.drop.addEventListener('click', function () { el.file.click(); });
el.drop.addEventListener('keydown', function (e) { if ((e.key === 'Enter' || e.key === ' ') && e.target === el.drop) { e.preventDefault(); el.file.click(); } });
el.file.addEventListener('change', function () { var f = el.file.files && el.file.files[0]; el.file.value = ''; if (f) loadFile(f); else say('No image selected. Please choose a photo first.', 'err'); });
['dragenter', 'dragover'].forEach(function (n) { el.drop.addEventListener(n, function (e) { e.preventDefault(); el.drop.classList.add('over'); }); });
['dragleave', 'drop'].forEach(function (n) { el.drop.addEventListener(n, function (e) { e.preventDefault(); el.drop.classList.remove('over'); }); });
el.drop.addEventListener('drop', function (e) { if (e.dataTransfer && e.dataTransfer.files.length) loadFile(e.dataTransfer.files[0]); });
el.dl.addEventListener('click', download);
setPos(50);
})();
</script>

<script>
/* Tool switcher: "Remove Background" / "Unblur & Enhance" / "Passport Photo" inside the AI Photoshop page */
(function () {
  var btns = document.querySelectorAll('#aiph [data-ait]');
  btns.forEach(function (b) {
    b.addEventListener('click', function () {
      btns.forEach(function (x) { var on = x === b; x.classList.toggle('on', on); x.setAttribute('aria-pressed', on); });
      var bg = document.getElementById('aiToolBg'), en = document.getElementById('aiToolEn'), ps = document.getElementById('aiToolPass');
      if (bg) bg.hidden = b.dataset.ait !== 'bg';
      if (en) en.hidden = b.dataset.ait !== 'en';
      if (ps) ps.hidden = b.dataset.ait !== 'pass';
    });
  });
})();
</script>
'''


# --------------------------------------------------------------------------------------
# PASSPORT PHOTO (v1.6): third tool inside the AI PHOTOSHOP tab, right after Unblur & Enhance.
# (Its HTML is inside AI_PHOTOSHOP_BLOCK as <div id="aiToolPass">; this block holds only its CSS and JavaScript.)
# Passport Photo Maker (Nepal 35x45 mm, US 2x2 in, custom). 100% in the browser: no upload, no API, no server.
# To change sizes or print layout, edit the SETTINGS section at the top of the script below.
# --------------------------------------------------------------------------------------
PASSPORT_PHOTO_BLOCK = r'''<style>
/* ===== PASSPORT PHOTO MAKER (all rules start with #pass so they never affect other pages) ===== */
#pass{min-width:0}
#pass .chead h2{margin:4px 0 4px;font-size:20px}
#pass .chead p{margin:0 0 12px;color:var(--mu);font-size:13px;max-width:80ch}
#pass h3{margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mu)}
#pass .cc{background:var(--p);border:1px solid var(--ln);border-radius:8px;padding:14px;min-width:0}
/* page layout: left = upload + settings, right = editor, bottom = result */
#pass .ppgrid{display:grid;grid-template-columns:minmax(0,4fr) minmax(0,8fr);grid-template-rows:auto 1fr;grid-template-areas:"up ed" "set ed";gap:14px;align-items:start}
#pass .pp-up{grid-area:up}#pass .pp-set{grid-area:set}#pass .pp-out{margin-top:14px}
#pass .pp-ed{grid-area:ed;position:sticky;top:84px}
@media(max-width:820px){#pass .ppgrid{grid-template-columns:minmax(0,1fr);grid-template-rows:none;grid-template-areas:"up" "ed" "set"}#pass .pp-ed{position:static}}
/* buttons, upload area, messages */
#pass .abtn{background:var(--ac);color:var(--onac);border:0;border-radius:6px;padding:8px 16px;min-height:38px;font:inherit;font-weight:700;font-size:13px;cursor:pointer}
#pass .abtn:hover:not(:disabled){background:var(--ach)}
#pass .abtn.ghost{background:var(--p);color:var(--tx);border:1px solid var(--ln);font-weight:600}
#pass .abtn.ghost:hover:not(:disabled){border-color:var(--ac);color:var(--ac);background:var(--p)}
#pass .abtn:disabled{opacity:.45;cursor:not-allowed}
#pass .abtn:focus-visible,#pass .drop:focus-visible,#pass .ppframe:focus-visible,#pass .fsel:focus-visible,#pass input:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
#pass .drop{border:2px dashed var(--ln);border-radius:8px;padding:18px 12px;text-align:center;background:var(--ch);transition:border-color .15s ease}
#pass .drop.over{border-color:var(--ac)}
#pass .drop p{margin:8px 0 0;font-size:12px;color:var(--mu)}
#pass .finfo{margin-top:10px;font-size:12px;color:var(--mu);overflow-wrap:anywhere;min-height:1.2em}
#pass .finfo b{color:var(--tx)}
#pass .amsg{margin-top:10px;font-size:13px;min-height:1.3em}
#pass .amsg.err{background:var(--ch);border:1px solid var(--bad);border-radius:6px;padding:8px 12px;color:var(--bad)}
#pass .amsg.ok{color:var(--ok)}
#pass .amsg.warn{background:var(--ch);border:1px solid var(--wa);border-radius:6px;padding:8px 12px;color:var(--tx)}
#pass .encap{margin-top:8px;font-size:12px;color:var(--mu);overflow-wrap:anywhere}
/* editor frame: the photo is drawn in the canvas, the guides are drawn on top */
#pass .ppwrap{display:flex;justify-content:center}
#pass .ppframe{position:relative;width:100%;aspect-ratio:35/45;background:#fff;border:1px solid var(--ln);border-radius:4px;overflow:hidden;touch-action:none;user-select:none;-webkit-user-select:none;cursor:grab}
#pass .ppframe.drag{cursor:grabbing}
#pass .ppframe canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
#pass .ppframe canvas[hidden]{display:none}
#pass .ppph{position:absolute;inset:0;display:grid;place-items:center;padding:12px;text-align:center;color:#555;font-size:12px;background:#f3f3f3;cursor:default}
#pass .ppph[hidden]{display:none}
#pass .ppguides{position:absolute;inset:0;pointer-events:none}
#pass .ppguides[hidden]{display:none}
#pass .g-safe{position:absolute;border:2px dashed #1d4ed8;box-shadow:0 0 0 999px rgba(29,78,216,.10)}
#pass .g-center{position:absolute;top:0;bottom:0;left:50%;border-left:2px dashed #1d4ed8;margin-left:-1px}
#pass .g-crown{position:absolute;left:0;right:0;height:0;border-top:2px solid #e11d48}
#pass .g-chin{position:absolute;left:0;right:0;background:rgba(22,163,74,.22);border-top:2px solid #16a34a;border-bottom:2px solid #16a34a}
#pass .g-oval{position:absolute;border:2px dashed rgba(255,255,255,.95);border-radius:50%;box-shadow:0 0 0 1px rgba(0,0,0,.55),inset 0 0 0 1px rgba(0,0,0,.35)}
#pass .g-lbl{position:absolute;font-size:10px;font-weight:700;line-height:1;padding:2px 6px;border-radius:8px;color:#fff;white-space:nowrap}
#pass .busy{position:absolute;inset:0;display:none;flex-direction:column;gap:10px;align-items:center;justify-content:center;background:rgba(0,0,0,.55);color:#fff;font-size:13px;text-align:center;padding:10px}
#pass .busy.on{display:flex}
#pass .spin{width:34px;height:34px;border:4px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:ppspin .8s linear infinite}
@keyframes ppspin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){#pass .spin{animation-duration:2.4s}}
#pass .legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:10px;font-size:11px;color:var(--mu)}
#pass .legend i{display:inline-block;width:14px;height:0;border-top:2px solid;margin-right:5px;vertical-align:middle}
#pass .legend i.bx{height:9px;border:0;background:rgba(22,163,74,.35);outline:1px solid #16a34a}
#pass .legend i.tn{height:9px;border:0;background:rgba(29,78,216,.18);outline:1px solid #1d4ed8}
#pass .ctlrow{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin-top:10px}
#pass .ctlrow label.chk{display:flex;gap:6px;align-items:center;font-size:12px;font-weight:600}
#pass .zoomrow{display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:center;margin-top:12px;font-size:12px;font-weight:600}
#pass .zoomrow output{color:var(--mu);font-variant-numeric:tabular-nums;min-width:4.5em;text-align:right}
#pass input[type=range]{width:100%;accent-color:var(--ac);min-height:26px}
#pass .bgstat{margin-top:10px;font-size:12px;border:1px solid var(--ln);border-left-width:4px;border-radius:6px;padding:7px 10px;background:var(--ch)}
#pass .bgstat.ok{border-left-color:var(--ok)}#pass .bgstat.bad{border-left-color:var(--wa)}
/* settings */
#pass .fsel,#pass .nin{width:100%;background:var(--p);color:var(--tx);border:1px solid var(--ln);border-radius:6px;padding:6px 10px;min-height:38px;font:inherit;box-sizing:border-box}
#pass .fld{margin:0 0 10px}
#pass .fld label{display:block;font-size:12px;font-weight:600;margin:0 0 4px}
#pass .cust{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:0 0 10px}
#pass .cust[hidden]{display:none}
#pass .cust label{display:block;font-size:12px;font-weight:600;margin:0 0 4px}
#pass .srow{display:grid;grid-template-columns:1fr auto;gap:2px 8px;margin:0 0 8px}
#pass .srow label{font-size:12px;font-weight:600}
#pass .srow output{font-size:12px;color:var(--mu);font-variant-numeric:tabular-nums}
#pass .srow input[type=range]{grid-column:1/-1}
/* result area: preview | checklist | downloads */
#pass .outgrid{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,4fr) minmax(0,5fr);gap:16px;align-items:start}
@media(max-width:820px){#pass .outgrid{grid-template-columns:minmax(0,1fr)}}
#pass .finalbox{border:1px solid var(--ln);border-radius:4px;background:#fff;display:block;margin:0 auto;max-width:100%;box-shadow:0 2px 8px rgba(0,0,0,.18)}
#pass .finalbox canvas{display:block;width:100%;height:auto}
#pass .checklist{list-style:none;margin:0;padding:0}
#pass .checklist li{margin:0 0 8px}
#pass .checklist label{display:flex;gap:8px;align-items:flex-start;font-size:13px;cursor:pointer;line-height:1.3}
#pass .checklist input{margin-top:2px;width:18px;height:18px;accent-color:var(--ac);flex:0 0 auto}
#pass .dlrow{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 10px}
#pass .dlrow .abtn{flex:1 1 auto}
#pass .sheetbox{border-top:1px solid var(--ln);padding-top:12px;margin-top:6px}
#pass .peoplebox{margin:0 0 12px}
#pass .peoplelist{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 6px}
#pass .person{width:92px;border:1px solid var(--ln);border-radius:6px;background:var(--ch);padding:5px;text-align:center;font-size:11px}
#pass .person img{display:block;width:100%;height:auto;background:#fff;border:1px solid var(--ln);border-radius:3px}
#pass .person b{display:block;margin:4px 0 0;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#pass .person span{display:block;color:var(--mu)}
#pass .person button{margin-top:4px;width:100%;min-height:28px;background:var(--p);color:var(--bad);border:1px solid var(--ln);border-radius:4px;font:inherit;font-size:11px;cursor:pointer}
#pass .person button:hover{border-color:var(--bad)}
#pass .twocol{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:0 0 8px}
#pass .twocol label{display:block;font-size:12px;font-weight:600;margin:0 0 4px}
#pass .anote{margin-top:12px;border-top:1px solid var(--ln);padding-top:10px;font-size:11.5px;color:var(--mu)}
@media(max-width:640px){#pass .fsel,#pass .nin{font-size:16px}}
</style>
<script>
(function () {
'use strict';
/* =========================================================================
   PASSPORT PHOTO MAKER  -  runs 100% in the browser (HTML Canvas).
   No upload, no API key, no server.

   Map of this file (for beginners):
     1. SETTINGS  - photo sizes, limits and print layout you can edit
     2. PICTURE   - drawing the photo into the frame + brightness/contrast/sharpness
     3. EDITOR    - drag / zoom, guides, background check
     4. FILES     - open a photo, download the JPG, build the A4 print sheet (PDF or JPG)
   ========================================================================= */

/* ---------------- 1. SETTINGS ---------------- */
var MAX_BYTES = 15 * 1024 * 1024;     // biggest file accepted (15 MB)
var TYPES = ['image/jpeg', 'image/png', 'image/webp'];
var MAX_WORK_LONG = 4000;             // bigger photos are reduced to this (longest side, px)
var MAX_SOURCE_PIXELS = 100000000;    // refuse absurd sizes that would crash the browser
var DPI = 300;                        // resolution used to turn millimetres into pixels
var EDIT_LONG = 640;                  // editor picture size (px, longest side). Smaller = faster dragging
var JPG_QUALITY = 0.95;               // quality of the downloaded JPG (0 to 1)

// A4 print sheet (all values in millimetres)
// Paper choices for the print sheet (sizes in millimetres).
//   margin = empty border (printers cannot print to the very edge)   gap = smallest space between photos
//   cap    = space kept free at the bottom for the small caption + 35 mm scale bar (0 = no caption)
var PAPERS = {
  a4p: { label: 'A4 portrait',   w: 210,   h: 297,   margin: 10, gap: 3, cap: 6 },
  a4l: { label: 'A4 landscape',  w: 297,   h: 210,   margin: 10, gap: 3, cap: 6 },
  p46: { label: '4 x 6 in',      w: 152.4, h: 101.6, margin: 3,  gap: 2, cap: 0 },
  p57: { label: '5 x 7 in',      w: 177.8, h: 127,   margin: 5,  gap: 2, cap: 0 }
};
var SPREAD_MAX = 10;                  // "Centered" layout: largest space left between photos (mm)
var MARK_OFFSET = 0.4, MARK_LEN = 2.5; // corner cut marks: distance from the photo and length (mm)
var CUT_OFFSET = 0.35;                // thin cut-line boxes are drawn this far outside each photo

// Automatic background check (looks at the top-left and top-right corners)
var BG_WHITE_MIN = 215;               // each colour must be at least this bright (0-255)
var BG_SPREAD_MAX = 28;               // colours must be close to each other (no colour cast)
var BG_STD_MAX = 24;                  // and the area must be plain (no pattern or objects)

// Photo size presets.
//   w, h    : size in mm
//   px      : exact size of the digital photo in pixels
//   head    : allowed head height (top of hair to chin) as a share of the photo height
//   crown   : where the top of the head should be, as a share of the photo height from the top
//   maxKB   : biggest file size (0 = no limit)
var PRESETS = {
  nepal: { label: 'Nepal Passport', w: 35, h: 45, px: [413, 531], head: [0.70, 0.80], crown: 0.067, maxKB: 0,
           note: 'Plain white or very light grey background. Face centered and looking straight at the camera.' },
  us:    { label: 'US Passport', w: 51, h: 51, px: [600, 600], head: [0.50, 0.69], crown: 0.12, maxKB: 240,
           note: 'White or off-white background. Head 1 to 1 3/8 inch (25 to 35 mm). Digital photo: JPG under 240 KB.' }
};
var custom = { w: 35, h: 45, hmin: 70, hmax: 80 };

/* ---------------- screen elements and state ---------------- */
var $ = function (id) { return document.getElementById(id); };
var el = {
  drop: $('ppDrop'), pick: $('ppPick'), file: $('ppFile'), info: $('ppInfo'), msg: $('ppMsg'), msg2: $('ppMsg2'),
  frame: $('ppFrame'), canvas: $('ppCanvas'), ph: $('ppPh'), guides: $('ppGuides'), busy: $('ppBusy'), busyTxt: $('ppBusyTxt'),
  zoom: $('ppZoom'), zoomV: $('ppZoomV'), showG: $('ppShowGuides'), fit: $('ppFit'), bg: $('ppBg'),
  preset: $('ppPreset'), custom: $('ppCustom'), w: $('ppW'), h: $('ppH'), hmin: $('ppHmin'), hmax: $('ppHmax'), spec: $('ppSpec'),
  bright: $('ppBright'), contrast: $('ppContrast'), sharp: $('ppSharp'), adjReset: $('ppAdjReset'),
  final: $('ppFinal'), finalBox: $('ppFinalBox'), finalCap: $('ppFinalCap'), list: $('ppChecklist'),
  dlJpg: $('ppDlJpg'), jpgCap: $('ppJpgCap'), copies: $('ppCopies'), sheetFmt: $('ppSheetFmt'), dlSheet: $('ppDlSheet'), sheetCap: $('ppSheetCap'),
  paper: $('ppPaper'), layout: $('ppLayout'), cuts: $('ppCuts'), addPerson: $('ppAddPerson'), people: $('ppPeople'), peopleCap: $('ppPeopleCap')
};
if (!el.drop) return;
var ectx = el.canvas.getContext('2d', { willReadFrequently: true });
var fctx = el.final.getContext('2d');

var work = null, levels = [], pw = 0, ph = 0;     // the photo and smaller copies of it
var ready = false, busyCount = 0, busyTimer = null;
var zoom = 1, ox = 0.5, oy = 0.5;                 // zoom (1 = photo just fills the frame) and photo centre inside the frame (0..1)
var ew = 413, eh = 531, rafId = 0, lastBg = '';
var guideEls = {};

function say(box, text, kind) { box.textContent = text || ''; box.className = 'amsg' + (kind ? ' ' + kind : ''); }
function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
function sizeText(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB'; }
function mk(w, h) { var c = document.createElement('canvas'); c.width = w; c.height = h; return c; }
function toBlob(c, type, q) { return new Promise(function (res) { c.toBlob(res, type, q); }); }
function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function busyOn(text) {
  busyCount++; el.busyTxt.textContent = text || 'Working...';
  clearTimeout(busyTimer); busyTimer = setTimeout(function () { el.busy.classList.add('on'); }, 120);
}
function busyOff() { busyCount = Math.max(0, busyCount - 1); if (!busyCount) { clearTimeout(busyTimer); el.busy.classList.remove('on'); } }

/* current photo size: preset, or the custom numbers */
function getSpec() {
  if (el.preset.value === 'custom') {
    var hmin = custom.hmin / 100, hmax = custom.hmax / 100;
    return { id: 'custom', label: 'Custom ' + custom.w + 'x' + custom.h + ' mm', w: custom.w, h: custom.h,
             pxW: Math.round(custom.w / 25.4 * DPI), pxH: Math.round(custom.h / 25.4 * DPI),
             hmin: hmin, hmax: hmax, crown: (1 - hmax) * 0.4, maxKB: 0, note: 'Check the exact rules of the office you are applying to.' };
  }
  var p = PRESETS[el.preset.value];
  return { id: el.preset.value, label: p.label, w: p.w, h: p.h, pxW: p.px[0], pxH: p.px[1],
           hmin: p.head[0], hmax: p.head[1], crown: p.crown, maxKB: p.maxKB, note: p.note };
}

/* ---------------- 2. PICTURE ---------------- */

// Smaller copies of the photo (each half the size of the one before) give a cleaner result when the photo is shrunk a lot.
function getLevel(l) {
  while (levels.length <= l) {
    var prev = levels[levels.length - 1], c = mk(Math.max(1, Math.round(prev.width / 2)), Math.max(1, Math.round(prev.height / 2)));
    var cx = c.getContext('2d'); cx.imageSmoothingEnabled = true; cx.imageSmoothingQuality = 'high';
    cx.drawImage(prev, 0, 0, c.width, c.height); levels.push(c);
  }
  return levels[l];
}

// Draw the finished photo (white background + photo + adjustments) into a W x H canvas.
// The same function makes the editor picture AND the exact-size download.
function paint(ctx, W, H) {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, W, H);                  // white behind everything (also fills transparent PNGs)
  if (!work) return;
  var cover = Math.max(W / pw, H / ph), s = cover * zoom, l = 0;     // s = canvas pixels per photo pixel
  if (s < 1) l = Math.min(Math.floor(Math.log(1 / s) / Math.LN2), 8);
  var src = getLevel(l), sl = s * pw / src.width;
  ctx.save(); ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
  ctx.translate(ox * W, oy * H); ctx.scale(sl, sl);
  ctx.drawImage(src, -src.width / 2, -src.height / 2);
  ctx.restore();
  adjust(ctx, W, H);
}

// Brightness, contrast and sharpness (simple per-pixel maths).
function adjust(ctx, W, H) {
  var b = +el.bright.value, c = +el.contrast.value, sh = +el.sharp.value;
  if (!b && !c && !sh) return;
  var id = ctx.getImageData(0, 0, W, H), d = id.data, i, v;
  if (b || c) {
    var f = 1 + c / 100, add = b * 1.5, lut = new Uint8ClampedArray(256);
    for (v = 0; v < 256; v++) lut[v] = (v - 128) * f + 128 + add;
    for (i = 0; i < d.length; i += 4) { d[i] = lut[d[i]]; d[i + 1] = lut[d[i + 1]]; d[i + 2] = lut[d[i + 2]]; }
  }
  if (sh > 0) {                                                       // 3x3 sharpening kernel [0 -a 0 / -a 1+4a -a / 0 -a 0]
    var a = sh / 100 * 0.6, src = new Uint8ClampedArray(d), row = W * 4, x, y, ch, k;
    for (y = 1; y < H - 1; y++) for (x = 1; x < W - 1; x++) {
      k = y * row + x * 4;
      for (ch = 0; ch < 3; ch++) d[k + ch] = src[k + ch] + a * (4 * src[k + ch] - src[k + ch - 4] - src[k + ch + 4] - src[k + ch - row] - src[k + ch + row]);
    }
  }
  ctx.putImageData(id, 0, 0);
}

/* ---------------- 3. EDITOR ---------------- */

function requestRender() { if (!rafId) rafId = requestAnimationFrame(function () { rafId = 0; render(); }); }
function render() {
  if (!ready) return;
  paint(ectx, ew, eh);
  fctx.drawImage(el.canvas, 0, 0);                                    // small "final photo" preview (no guides)
  checkBackground();
}

// Keep the photo from being dragged completely out of the frame.
function clampPos() {
  var s = Math.max(ew / pw, eh / ph) * zoom, hw = pw * s / ew / 2, hh = ph * s / eh / 2;
  ox = clamp(ox, 0.2 - hw, 0.8 + hw); oy = clamp(oy, 0.2 - hh, 0.8 + hh);
}
function zoomAt(z, px, py) {                                          // zoom around a point of the frame (px, py are 0..1)
  z = clamp(z, 0.25, 4); var r = z / zoom;
  ox = px + (ox - px) * r; oy = py + (oy - py) * r; zoom = z;
  clampPos(); zoomUI(); requestRender();
}
function zoomUI() { el.zoom.value = Math.round(50 * Math.log(zoom) / Math.LN2); el.zoomV.textContent = Math.round(zoom * 100) + '%'; }
function resetPosition() { zoom = 1; ox = 0.5; oy = 0.5; zoomUI(); requestRender(); }

el.zoom.addEventListener('input', function () { zoomAt(Math.pow(2, +this.value / 50), 0.5, 0.5); });
el.fit.addEventListener('click', function () { if (ready) resetPosition(); });
el.showG.addEventListener('change', function () { el.guides.hidden = !(ready && el.showG.checked); });

// Mouse / finger: one pointer moves the photo, two pointers pinch-zoom.
var ptrs = {}, startMove = null, startPinch = null;
function frameRect() { return el.frame.getBoundingClientRect(); }
function pointerList() { return Object.keys(ptrs).map(function (k) { return ptrs[k]; }); }
function beginGesture() {
  var l = pointerList(), r = frameRect();
  startMove = startPinch = null;
  if (l.length === 1) startMove = { x: l[0].x, y: l[0].y, ox: ox, oy: oy };
  else if (l.length >= 2) {
    var dx = l[0].x - l[1].x, dy = l[0].y - l[1].y;
    startPinch = { dist: Math.max(1, Math.hypot(dx, dy)), zoom: zoom, mx: (l[0].x + l[1].x) / 2, my: (l[0].y + l[1].y) / 2, ox: ox, oy: oy };
  }
  el.frame.classList.toggle('drag', l.length > 0);
}
el.frame.addEventListener('pointerdown', function (e) {
  if (!ready) return;
  ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
  try { el.frame.setPointerCapture(e.pointerId); } catch (x) { }
  beginGesture(); e.preventDefault();
});
el.frame.addEventListener('pointermove', function (e) {
  if (!ptrs[e.pointerId]) return;
  ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
  var r = frameRect(); if (!r.width) return;
  var l = pointerList();
  if (startPinch && l.length >= 2) {
    var dist = Math.hypot(l[0].x - l[1].x, l[0].y - l[1].y), mx = (l[0].x + l[1].x) / 2, my = (l[0].y + l[1].y) / 2;
    ox = startPinch.ox + (mx - startPinch.mx) / r.width; oy = startPinch.oy + (my - startPinch.my) / r.height;
    var z = clamp(startPinch.zoom * dist / startPinch.dist, 0.25, 4), ratio = z / zoom, px = (mx - r.left) / r.width, py = (my - r.top) / r.height;
    ox = px + (ox - px) * ratio; oy = py + (oy - py) * ratio; zoom = z; clampPos(); zoomUI(); requestRender();
  } else if (startMove) {
    ox = startMove.ox + (e.clientX - startMove.x) / r.width; oy = startMove.oy + (e.clientY - startMove.y) / r.height;
    clampPos(); requestRender();
  }
});
function endPointer(e) { delete ptrs[e.pointerId]; beginGesture(); }
['pointerup', 'pointercancel', 'lostpointercapture'].forEach(function (n) { el.frame.addEventListener(n, endPointer); });
el.frame.addEventListener('wheel', function (e) {                       // mouse wheel zoom
  if (!ready) return; e.preventDefault();
  var r = frameRect(); zoomAt(zoom * Math.pow(1.0015, -e.deltaY), (e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height);
}, { passive: false });
el.frame.addEventListener('keydown', function (e) {                     // keyboard: arrows move, + and - zoom
  if (!ready) return; var st = 0.01, used = true;
  if (e.key === 'ArrowLeft') ox -= st; else if (e.key === 'ArrowRight') ox += st;
  else if (e.key === 'ArrowUp') oy -= st; else if (e.key === 'ArrowDown') oy += st;
  else if (e.key === '+' || e.key === '=') { zoomAt(zoom * 1.05, 0.5, 0.5); e.preventDefault(); return; }
  else if (e.key === '-') { zoomAt(zoom / 1.05, 0.5, 0.5); e.preventDefault(); return; }
  else used = false;
  if (used) { e.preventDefault(); clampPos(); requestRender(); }
});

/* guides drawn over the frame (they are never part of the downloaded photo) */
function buildGuides() {
  var g = el.guides;
  function add(cls, parent) { var d = document.createElement('div'); d.className = cls; (parent || g).appendChild(d); return d; }
  guideEls.safe = add('g-safe'); guideEls.center = add('g-center'); guideEls.chin = add('g-chin'); guideEls.crown = add('g-crown');
  guideEls.oval = add('g-oval');
  guideEls.lc = add('g-lbl'); guideEls.lc.style.background = '#e11d48'; guideEls.lc.textContent = 'Top of head';
  guideEls.lh = add('g-lbl'); guideEls.lh.style.background = '#15803d'; guideEls.lh.textContent = 'Chin here';
  guideEls.lb = add('g-lbl'); guideEls.lb.style.background = '#1d4ed8'; guideEls.lb.textContent = 'White background';
}
function updateGuides(S) {
  var sx = 3 / S.w * 100, sy = 3 / S.h * 100, top = S.crown * 100, hm = (S.hmin + S.hmax) / 2;
  guideEls.safe.style.cssText = 'left:' + sx + '%;right:' + sx + '%;top:' + sy + '%;bottom:' + sy + '%';
  guideEls.crown.style.top = top + '%';
  guideEls.chin.style.top = (S.crown + S.hmin) * 100 + '%'; guideEls.chin.style.height = (S.hmax - S.hmin) * 100 + '%';
  var ovalW = 0.66 * hm * S.h / S.w * 100;                            // a head is about 0.66 times as wide as it is tall
  guideEls.oval.style.cssText = 'top:' + top + '%;height:' + hm * 100 + '%;width:' + ovalW + '%;left:' + (50 - ovalW / 2) + '%';
  guideEls.lc.style.cssText = 'background:#e11d48;left:4px;top:calc(' + top + '% + 4px)';
  guideEls.lh.style.cssText = 'background:#15803d;left:4px;top:calc(' + (S.crown + S.hmin) * 100 + '% + 4px)';
  guideEls.lb.style.cssText = 'background:#1d4ed8;right:4px;top:4px';
}

// Automatic background check: average colour of the two top corners.
function checkBackground() {
  var regions = [[0.02, 0.02, 0.16, 0.28], [0.82, 0.02, 0.16, 0.28]], n = 0, sr = 0, sg = 0, sb = 0, sl = 0, sl2 = 0, i;
  regions.forEach(function (r) {
    var x = Math.round(r[0] * ew), y = Math.round(r[1] * eh), w = Math.max(1, Math.round(r[2] * ew)), h = Math.max(1, Math.round(r[3] * eh));
    var d = ectx.getImageData(x, y, w, h).data;
    for (i = 0; i < d.length; i += 4) { var l = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; sr += d[i]; sg += d[i + 1]; sb += d[i + 2]; sl += l; sl2 += l * l; n++; }
  });
  var mr = sr / n, mg = sg / n, mb = sb / n, ml = sl / n, sd = Math.sqrt(Math.max(0, sl2 / n - ml * ml));
  var lo = Math.min(mr, mg, mb), hi = Math.max(mr, mg, mb), ok = lo >= BG_WHITE_MIN && hi - lo <= BG_SPREAD_MAX && sd <= BG_STD_MAX;
  var text = ok ? 'Background check: the top corners look plain white or very light. Good.'
    : 'Background check: the top corners are not plain white (average colour rgb(' + Math.round(mr) + ', ' + Math.round(mg) + ', ' + Math.round(mb) + (sd > BG_STD_MAX ? ', not plain' : '') +
      ')). Use a white wall, or remove the background first (AI Photoshop > Remove Background) and upload that PNG here.';
  if (text !== lastBg) { lastBg = text; el.bg.textContent = text; el.bg.className = 'bgstat ' + (ok ? 'ok' : 'bad'); }
}

/* size / preset changes */
function applySpec() {
  var S = getSpec(), k = Math.min(1, EDIT_LONG / Math.max(S.pxW, S.pxH));
  ew = Math.max(1, Math.round(S.pxW * k)); eh = Math.max(1, Math.round(S.pxH * k));
  el.canvas.width = ew; el.canvas.height = eh; el.final.width = ew; el.final.height = eh;
  el.frame.style.aspectRatio = S.w + ' / ' + S.h;
  el.frame.style.width = 'min(100%, ' + (62 * S.w / S.h).toFixed(2) + 'vh)';
  el.finalBox.style.aspectRatio = S.w + ' / ' + S.h;
  el.custom.hidden = S.id !== 'custom';
  updateGuides(S);
  var head = Math.round(S.hmin * 100) + '-' + Math.round(S.hmax * 100) + '%';
  el.spec.style.color = ''; el.spec.textContent = S.w + ' x ' + S.h + ' mm  |  digital file ' + S.pxW + ' x ' + S.pxH + ' px at ' + DPI + ' DPI  |  head height ' + head + ' of the photo. ' + S.note;
  el.finalCap.textContent = 'Final photo (no guides): ' + S.w + ' x ' + S.h + ' mm';
  el.jpgCap.textContent = 'JPG, exactly ' + S.pxW + ' x ' + S.pxH + ' px, ' + DPI + ' DPI' + (S.maxKB ? ', kept under ' + S.maxKB + ' KB' : '') + '.';
  updateSheetCaption();
  checkPeopleSize();
  if (ready) { clampPos(); requestRender(); }
}
el.preset.addEventListener('change', applySpec);
function readCustom() {
  var w = parseFloat(el.w.value), h = parseFloat(el.h.value), a = parseFloat(el.hmin.value), b = parseFloat(el.hmax.value);
  if (!(w >= 20 && w <= 100 && h >= 20 && h <= 100)) { setSpecError('Enter a width and a height between 20 and 100 mm.'); return; }
  if (!(a >= 30 && b <= 95 && a < b)) { setSpecError('Head height: enter a minimum below the maximum (between 30 and 95 %).'); return; }
  custom = { w: w, h: h, hmin: a, hmax: b }; applySpec();
}
function setSpecError(t) { el.spec.textContent = t; el.spec.style.color = 'var(--bad)'; }
[el.w, el.h, el.hmin, el.hmax].forEach(function (i) { i.addEventListener('change', readCustom); });

/* adjustments */
[['bright', 'ppBrightV'], ['contrast', 'ppContrastV'], ['sharp', 'ppSharpV']].forEach(function (p) {
  el[p[0]].addEventListener('input', function () { $(p[1]).textContent = this.value; requestRender(); });
});
el.adjReset.addEventListener('click', function () {
  [['bright', 'ppBrightV'], ['contrast', 'ppContrastV'], ['sharp', 'ppSharpV']].forEach(function (p) { el[p[0]].value = 0; $(p[1]).textContent = '0'; });
  requestRender();
});

/* ---------------- 4. FILES ---------------- */

function problem(f) {
  if (!f) return 'No image selected. Please choose a photo first.';
  var okType = TYPES.indexOf(f.type) >= 0 || (!f.type && /\.(jpe?g|png|webp)$/i.test(f.name));
  if (!okType) return 'Unsupported file type. Please use a JPG, PNG or WebP image.';
  if (f.size === 0) return 'This file is empty. Please choose another photo.';
  if (f.size > MAX_BYTES) return 'File too large (' + sizeText(f.size) + '). The maximum size is ' + sizeText(MAX_BYTES) + '.';
  return '';
}
function loadFile(f) {
  say(el.msg, '');
  var p = problem(f);
  if (p) { say(el.msg, p, 'err'); return; }
  busyOn('Loading photo...');
  var url = URL.createObjectURL(f), img = new Image();
  img.onload = function () {
    URL.revokeObjectURL(url);
    try { start(img, f); } catch (e) { console.error(e); say(el.msg, 'Image processing failed. The photo may be too large for this browser. Try a smaller photo.', 'err'); }
    busyOff();
  };
  img.onerror = function () { URL.revokeObjectURL(url); busyOff(); say(el.msg, 'This image could not be read. The file may be damaged. Please try another photo.', 'err'); };
  img.src = url;
}
function start(img, f) {
  var nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) { say(el.msg, 'This image could not be read. Please try another photo.', 'err'); return; }
  if (nw * nh > MAX_SOURCE_PIXELS) { say(el.msg, 'The picture is too large (' + nw + ' x ' + nh + ' pixels). Please use a smaller photo.', 'err'); return; }
  var k = Math.min(1, MAX_WORK_LONG / Math.max(nw, nh)), ww = Math.max(1, Math.round(nw * k)), wh = Math.max(1, Math.round(nh * k));
  var c = mk(ww, wh), cx = c.getContext('2d'); cx.imageSmoothingEnabled = true; cx.imageSmoothingQuality = 'high';
  cx.drawImage(img, 0, 0, ww, wh);
  work = c; pw = ww; ph = wh; levels = [c]; ready = true;
  el.canvas.hidden = false; el.ph.hidden = true; el.guides.hidden = !el.showG.checked; el.zoom.disabled = false;
  Array.prototype.forEach.call(el.list.querySelectorAll('input'), function (i) { i.checked = false; });   // new photo = check again
  zoom = 1; ox = 0.5; oy = 0.5; zoomUI(); lastBg = '';
  el.info.innerHTML = '';
  var b = document.createElement('b'); b.textContent = f.name; el.info.appendChild(b);
  el.info.appendChild(document.createTextNode(' (' + sizeText(f.size) + ', ' + nw + ' x ' + nh + ' px)'));
  say(el.msg, 'Photo loaded. Drag it and zoom until your face fits the guides.', 'ok'); say(el.msg2, '');
  render();
}

function saveBlob(blob, name) {
  var a = document.createElement('a'), url = URL.createObjectURL(blob);
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
}
function unchecked() { return el.list.querySelectorAll('input:not(:checked)').length; }
function doneMessage(text) {
  var n = unchecked();
  if (n) say(el.msg2, text + ' Note: ' + n + ' checklist item' + (n > 1 ? 's are' : ' is') + ' not ticked yet. Please check them before you submit the photo.', 'warn');
  else say(el.msg2, text, 'ok');
}

// Put the resolution (300 DPI) into a JPG file. Canvas JPGs have no real DPI value.
function setJpegDpi(buf, dpi) {
  var b = new Uint8Array(buf);
  if (b[0] !== 0xFF || b[1] !== 0xD8) return buf;
  if (b[2] === 0xFF && b[3] === 0xE0 && b[6] === 0x4A && b[7] === 0x46 && b[8] === 0x49 && b[9] === 0x46) {   // JFIF header exists: change it
    b[13] = 1; b[14] = dpi >> 8; b[15] = dpi & 255; b[16] = dpi >> 8; b[17] = dpi & 255; return b.buffer;
  }
  var seg = [0xFF, 0xE0, 0, 16, 0x4A, 0x46, 0x49, 0x46, 0, 1, 1, 1, dpi >> 8, dpi & 255, dpi >> 8, dpi & 255, 0, 0];   // no JFIF header: add one
  var out = new Uint8Array(b.length + seg.length);
  out.set(b.subarray(0, 2), 0); out.set(seg, 2); out.set(b.subarray(2), 2 + seg.length); return out.buffer;
}
async function jpegBytes(canvas, quality, maxKB) {
  var q = quality, blob, buf;
  for (var i = 0; i < 12; i++) {
    blob = await toBlob(canvas, 'image/jpeg', q);
    if (!blob) throw new Error('The browser could not create the JPG');
    buf = setJpegDpi(await blob.arrayBuffer(), DPI);
    if (!maxKB || buf.byteLength <= maxKB * 1024 || q <= 0.5) break;
    q -= 0.05;                                                        // too big: lower the quality a little and try again
  }
  return { buf: buf, quality: q };
}
function exportCanvas(S) { var c = mk(S.pxW, S.pxH), cx = c.getContext('2d', { willReadFrequently: true }); paint(cx, S.pxW, S.pxH); return c; }

/* --- Download Digital Photo: exact pixel size JPG --- */
async function downloadDigital() {
  if (!ready) { say(el.msg2, 'No image selected. Please choose a photo first.', 'err'); return; }
  say(el.msg2, ''); busyOn('Creating your photo...'); el.dlJpg.disabled = true;
  try {
    await sleep(30);
    var S = getSpec(), c = exportCanvas(S), r = await jpegBytes(c, JPG_QUALITY, S.maxKB);
    c.width = 0;
    var name = 'passport-photo-' + S.id + '-' + S.pxW + 'x' + S.pxH + '.jpg';
    saveBlob(new Blob([r.buf], { type: 'image/jpeg' }), name);
    doneMessage('Downloaded ' + name + ' (' + S.pxW + ' x ' + S.pxH + ' px, ' + sizeText(r.buf.byteLength) + ').');
  } catch (e) { console.error(e); say(el.msg2, 'Image processing failed. Please try a smaller photo or another browser.', 'err'); }
  finally { busyOff(); el.dlJpg.disabled = false; }
}
el.dlJpg.addEventListener('click', downloadDigital);

/* --- A4 print sheet --- */
/* People on the sheet. Each entry = one finished passport photo (a canvas at the exact pixel size) for one person. */
var people = [], MAX_PEOPLE = 12;
function specKey(S) { return S.id + ':' + S.w + 'x' + S.h + ':' + S.pxW + 'x' + S.pxH; }

// Share N copies between P people as equally as possible. Example: 8 copies, 2 people -> 4+4; 4 people -> 2+2+2+2;
// 3 people -> 3+3+2; 5 people -> 2+2+2+1+1. The extra copies go to the first people in the list.
// Returns, for every place on the sheet, the number of the person whose photo goes there (people stay together).
function shareCopies(n, P) {
  var base = Math.floor(n / P), extra = n % P, out = [], counts = [];
  for (var i = 0; i < P; i++) { var c = base + (i < extra ? 1 : 0); counts.push(c); for (var j = 0; j < c; j++) out.push(i); }
  return { who: out, counts: counts };
}

// Work out how many photos fit on the sheet and where each one goes (millimetres from the top-left corner).
// Also builds the cutting guides (corner marks or thin boxes) and the 35 mm scale bar.
function sheetLayout(w, h, copies) {
  var P = PAPERS[el.paper.value] || PAPERS.a4p, g = P.gap;
  var uw = P.w - 2 * P.margin, uh = P.h - 2 * P.margin - P.cap;
  var cols = Math.floor((uw + g) / (w + g)), rows = Math.floor((uh + g) / (h + g));
  if (cols < 1 || rows < 1) return null;
  var n = copies === 'max' ? cols * rows : Math.min(+copies, cols * rows);
  var Pn = Math.max(1, people.length);
  if (n < Pn) return { tooFew: true, n: n, people: Pn, maxFit: cols * rows, paper: P };
  // tidy rectangle: 8 photos = 4 x 2 (not 5 + 3), 12 = 4 x 3, 6 = 3 x 2 ...
  var ru = Math.ceil(n / cols), cu = Math.ceil(n / ru);
  var gx = g, gy = g;
  if (el.layout.value === 'center') {                      // spread the photos out evenly, like a photo studio
    var availH = P.h - P.cap;
    gx = Math.max(g, Math.min(SPREAD_MAX, (P.w - cu * w) / (cu + 1)));
    gy = Math.max(g, Math.min(SPREAD_MAX, (availH - ru * h) / (ru + 1)));
    if (cu > 1 && cu * w + (cu - 1) * gx > uw) gx = (uw - cu * w) / (cu - 1);
    if (ru > 1 && ru * h + (ru - 1) * gy > uh) gy = (uh - ru * h) / (ru - 1);
  }
  var bw = cu * w + (cu - 1) * gx, bh = ru * h + (ru - 1) * gy;
  var x0 = (P.w - bw) / 2, y0 = el.layout.value === 'center' ? (P.h - P.cap - bh) / 2 : P.margin;
  y0 = Math.max(P.margin, y0);
  var sh = shareCopies(n, Pn), pos = [];
  for (var i = 0; i < n; i++) {
    var r = Math.floor(i / cu), c = i % cu, inRow = r === ru - 1 ? n - r * cu : cu;     // last row may have fewer photos: centre it
    pos.push({ x: x0 + c * (w + gx) + (cu - inRow) * (w + gx) / 2, y: y0 + r * (h + gy), who: sh.who[i] });
  }
  // cutting guides as line segments [x1, y1, x2, y2]
  var segs = [], mode = el.cuts.value, m = MARK_OFFSET;
  var bx0 = Math.min.apply(null, pos.map(function (p) { return p.x; })), bx1 = Math.max.apply(null, pos.map(function (p) { return p.x + w; }));
  var by0 = pos[0].y, by1 = pos[pos.length - 1].y + h;
  var room = Math.min(bx0, P.w - bx1, by0, P.h - P.cap - by1);                     // free space outside the block of photos
  var tEdge = Math.min(MARK_LEN, room - m - 0.3), tCorner = Math.min(MARK_LEN, Math.min(gx, gy) / 2 - m - 0.1);
  if (mode === 'marks' && tCorner < 1.2) mode = 'edge';                            // not enough room between photos
  if (mode === 'edge' && tEdge < 1) mode = 'box';                                  // not enough room in the margins: thin lines
  if (mode === 'edge') {
    // short marks in the margins, in line with every photo edge: cut along the lines between two matching marks
    var cx = {}, ry = {};
    pos.forEach(function (p) {
      var kx = p.x.toFixed(2), ky = p.y.toFixed(2);
      (cx[kx] = cx[kx] || { x: p.x, y1: p.y, y2: p.y + h }); cx[kx].y1 = Math.min(cx[kx].y1, p.y); cx[kx].y2 = Math.max(cx[kx].y2, p.y + h);
      (ry[ky] = ry[ky] || { y: p.y, x1: p.x, x2: p.x + w }); ry[ky].x1 = Math.min(ry[ky].x1, p.x); ry[ky].x2 = Math.max(ry[ky].x2, p.x + w);
    });
    Object.keys(cx).forEach(function (k) {
      var c = cx[k]; [c.x, c.x + w].forEach(function (x) { segs.push([x, c.y1 - m - tEdge, x, c.y1 - m], [x, c.y2 + m, x, c.y2 + m + tEdge]); });
    });
    Object.keys(ry).forEach(function (k) {
      var r = ry[k]; [r.y, r.y + h].forEach(function (y) { segs.push([r.x1 - m - tEdge, y, r.x1 - m, y], [r.x2 + m, y, r.x2 + m + tEdge, y]); });
    });
  } else {
    pos.forEach(function (p) {
      var x1 = p.x, x2 = p.x + w, y1 = p.y, y2 = p.y + h;
      if (mode === 'box') {
        var o = CUT_OFFSET; segs.push([x1 - o, y1 - o, x2 + o, y1 - o], [x2 + o, y1 - o, x2 + o, y2 + o], [x2 + o, y2 + o, x1 - o, y2 + o], [x1 - o, y2 + o, x1 - o, y1 - o]);
      } else if (mode === 'marks') {
        [[x1, y1, -1, -1], [x2, y1, 1, -1], [x1, y2, -1, 1], [x2, y2, 1, 1]].forEach(function (q) {
          segs.push([q[0] + q[2] * m, q[1], q[0] + q[2] * (m + tCorner), q[1]], [q[0], q[1] + q[3] * m, q[0], q[1] + q[3] * (m + tCorner)]);
        });
      }
    });
  }
  var bar = P.cap ? { x: P.w - P.margin - 35, y: P.h - P.margin - 1.5, len: 35 } : null;   // 35 mm ruler: measure it after printing
  return { n: n, cols: cu, rows: ru, maxFit: cols * rows, pos: pos, counts: sh.counts, paper: P, segs: segs, bar: bar, mode: mode };
}
function peopleText(L) {
  if (!people.length) return '';
  return ' ' + people.map(function (p, i) { return 'Person ' + (i + 1) + ': ' + L.counts[i]; }).join(', ') + '.';
}
function updateSheetCaption() {
  var S = getSpec(), L = sheetLayout(S.w, S.h, el.copies.value);
  if (!L) el.sheetCap.textContent = 'This photo size does not fit on this paper.';
  else if (L.tooFew) el.sheetCap.textContent = 'You have ' + L.people + ' people but only ' + L.n + ' copies. Choose at least ' + L.people + ' copies so everyone gets a photo.';
  else el.sheetCap.textContent = L.paper.label + ' (' + +L.paper.w.toFixed(1) + ' x ' + +L.paper.h.toFixed(1) + ' mm): ' + L.n + ' photos (' + L.cols + ' x ' + L.rows + '; up to ' + L.maxFit + ' fit). Print at 100% / "Actual size".' + (L.bar ? ' The 35 mm bar on the sheet should measure exactly 35 mm.' : '') + (el.sheetFmt.value === 'jpg' ? ' JPG file: ' + Math.round(L.paper.w / 25.4 * DPI) + ' x ' + Math.round(L.paper.h / 25.4 * DPI) + ' px at ' + DPI + ' DPI.' : '') + peopleText(L);
}
['change'].forEach(function (ev) { el.copies.addEventListener(ev, updateSheetCaption); el.paper.addEventListener(ev, updateSheetCaption); el.layout.addEventListener(ev, updateSheetCaption); el.cuts.addEventListener(ev, updateSheetCaption); el.sheetFmt.addEventListener(ev, updateSheetCaption); });

/* --- several people on one sheet --- */
function renderPeople() {
  el.people.textContent = '';
  var S = getSpec(), L = sheetLayout(S.w, S.h, el.copies.value);
  people.forEach(function (p, i) {
    var d = document.createElement('div'); d.className = 'person';
    var im = document.createElement('img'); im.src = p.thumb; im.alt = 'Photo of person ' + (i + 1);
    var b = document.createElement('b'); b.textContent = 'Person ' + (i + 1); b.title = p.name;
    var sp = document.createElement('span'); sp.textContent = L && !L.tooFew ? L.counts[i] + ' on sheet' : '';
    var rm = document.createElement('button'); rm.type = 'button'; rm.textContent = 'Remove'; rm.setAttribute('aria-label', 'Remove person ' + (i + 1));
    rm.addEventListener('click', function () { people.splice(i, 1); renderPeople(); });
    d.appendChild(im); d.appendChild(b); d.appendChild(sp); d.appendChild(rm); el.people.appendChild(d);
  });
  el.addPerson.textContent = people.length ? '+ Add another person (' + people.length + ' added)' : '+ Add this photo to the print sheet';
  updateSheetCaption();
}
// If the photo size changes, the photos already added no longer match: start the list again.
function checkPeopleSize() {
  if (!people.length) return;
  var k = specKey(getSpec());
  if (people.some(function (p) { return p.key !== k; })) { people = []; renderPeople(); say(el.msg2, 'The photo size changed, so the list of people for the print sheet was cleared. Please add the photos again.', 'warn'); }
}
el.addPerson.addEventListener('click', async function () {
  if (!ready) { say(el.msg2, 'No image selected. Please choose a photo first.', 'err'); return; }
  if (people.length >= MAX_PEOPLE) { say(el.msg2, 'You can add up to ' + MAX_PEOPLE + ' people on one sheet.', 'err'); return; }
  say(el.msg2, ''); el.addPerson.disabled = true;
  try {
    var S = getSpec(), c = exportCanvas(S), t = mk(92 * 2, Math.round(92 * 2 * S.pxH / S.pxW)), tx = t.getContext('2d');
    tx.imageSmoothingQuality = 'high'; tx.drawImage(c, 0, 0, t.width, t.height);
    people.push({ canvas: c, thumb: t.toDataURL('image/jpeg', 0.8), name: (el.info.textContent || '').split(' (')[0] || 'photo', key: specKey(S) });
    renderPeople();
    say(el.msg2, 'Person ' + people.length + ' added to the print sheet. Load the next photo, or choose the number of copies and download the sheet.', 'ok');
  } catch (e) { console.error(e); say(el.msg2, 'Image processing failed. Please try a smaller photo or another browser.', 'err'); }
  finally { el.addPerson.disabled = false; }
});

// Build a small PDF file by hand: one A4 page, the photo stored once and placed several times, thin cut lines.
function buildPdf(jpgs, imgW, imgH, S, L) {
  var enc = new TextEncoder(), parts = [], offs = [], len = 0, k = 72 / 25.4, K = jpgs.length, P = L.paper;
  function push(u8) { parts.push(u8); len += u8.length; }
  function str(s) { push(enc.encode(s)); }
  function f(n) { return n.toFixed(3); }
  var cap = S.label.replace(/[()\\]/g, '') + ' - ' + S.w + ' x ' + S.h + ' mm - ' + L.n + ' photos' + (K > 1 ? ' - ' + K + ' people' : '') + ' - print at 100% (Actual size), do not use Fit to page';
  var c = '';
  L.pos.forEach(function (p) { c += 'q ' + f(S.w * k) + ' 0 0 ' + f(S.h * k) + ' ' + f(p.x * k) + ' ' + f((P.h - p.y - S.h) * k) + ' cm /Im' + p.who + ' Do Q\n'; });
  if (L.segs.length) {                                                 // thin grey cutting guides
    c += '0.45 G 0.25 w\n';
    L.segs.forEach(function (q) { c += f(q[0] * k) + ' ' + f((P.h - q[1]) * k) + ' m ' + f(q[2] * k) + ' ' + f((P.h - q[3]) * k) + ' l S\n'; });
  }
  if (L.bar) {                                                         // caption + 35 mm scale bar (measure it with a ruler after printing)
    var by = (P.h - L.bar.y) * k;
    c += 'BT /F1 7 Tf 0.35 g ' + f(P.margin * k) + ' ' + f((P.margin + 1) * k) + ' Td (' + cap + ') Tj ET\n';
    c += '0.2 G 0.5 w ' + f(L.bar.x * k) + ' ' + f(by) + ' m ' + f((L.bar.x + L.bar.len) * k) + ' ' + f(by) + ' l S\n';
    c += '0.5 w ' + f(L.bar.x * k) + ' ' + f(by - 1.5 * k) + ' m ' + f(L.bar.x * k) + ' ' + f(by + 1.5 * k) + ' l S ' + f((L.bar.x + L.bar.len) * k) + ' ' + f(by - 1.5 * k) + ' m ' + f((L.bar.x + L.bar.len) * k) + ' ' + f(by + 1.5 * k) + ' l S\n';
    c += 'BT /F1 6 Tf 0.35 g ' + f((L.bar.x + 8) * k) + ' ' + f(by + 2.2 * k) + ' Td (35 mm - check after printing) Tj ET\n';
  }
  function obj(n, body) { offs[n] = len; str(n + ' 0 obj\n' + body + '\nendobj\n'); }
  // object numbers: 1 catalog, 2 pages, 3 page, 4 page content, 5 font, 6... one picture per person
  var xo = ''; for (var i = 0; i < K; i++) xo += '/Im' + i + ' ' + (6 + i) + ' 0 R ';
  push(new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2D, 0x31, 0x2E, 0x34, 0x0A, 0x25, 0xE2, 0xE3, 0xCF, 0xD3, 0x0A]));   // %PDF-1.4
  obj(1, '<< /Type /Catalog /Pages 2 0 R >>');
  obj(2, '<< /Type /Pages /Kids [3 0 R] /Count 1 >>');
  obj(3, '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ' + f(P.w * k) + ' ' + f(P.h * k) + '] /Resources << /XObject << ' + xo + '>> /Font << /F1 5 0 R >> >> /Contents 4 0 R >>');
  obj(4, '<< /Length ' + enc.encode(c).length + ' >>\nstream\n' + c + 'endstream');
  obj(5, '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>');
  for (var j = 0; j < K; j++) {
    offs[6 + j] = len;
    str((6 + j) + ' 0 obj\n<< /Type /XObject /Subtype /Image /Width ' + imgW + ' /Height ' + imgH + ' /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ' + jpgs[j].length + ' >>\nstream\n');
    push(jpgs[j]); str('\nendstream\nendobj\n');
  }
  var total = 6 + K, xref = len; str('xref\n0 ' + total + '\n0000000000 65535 f \n');
  for (var n = 1; n < total; n++) str(('0000000000' + offs[n]).slice(-10) + ' 00000 n \n');
  str('trailer\n<< /Size ' + total + ' /Root 1 0 R >>\nstartxref\n' + xref + '\n%%EOF\n');
  var out = new Uint8Array(len), at = 0;
  parts.forEach(function (p) { out.set(p, at); at += p.length; });
  return out;
}

// The same sheet as a JPG picture (A4 at 300 DPI).
async function buildSheetJpg(photos, S, L) {
  var P = L.paper, px = function (mm) { return mm / 25.4 * DPI; }, c = mk(Math.round(px(P.w)), Math.round(px(P.h))), cx = c.getContext('2d');
  cx.fillStyle = '#fff'; cx.fillRect(0, 0, c.width, c.height);
  cx.imageSmoothingEnabled = true; cx.imageSmoothingQuality = 'high';
  L.pos.forEach(function (p) { cx.drawImage(photos[p.who], px(p.x), px(p.y), px(S.w), px(S.h)); });
  if (L.segs.length) {
    cx.strokeStyle = '#737373'; cx.lineWidth = Math.max(1, px(0.2)); cx.beginPath();
    L.segs.forEach(function (q) { cx.moveTo(px(q[0]), px(q[1])); cx.lineTo(px(q[2]), px(q[3])); }); cx.stroke();
  }
  if (L.bar) {
    cx.fillStyle = '#595959'; cx.font = Math.round(px(2.4)) + 'px Arial, sans-serif'; cx.textBaseline = 'alphabetic';
    cx.fillText(S.label + ' - ' + S.w + ' x ' + S.h + ' mm - ' + L.n + ' photos - print at 100% (Actual size)', px(P.margin), c.height - px(P.margin));
    cx.strokeStyle = '#333'; cx.lineWidth = Math.max(1, px(0.2)); cx.beginPath();
    cx.moveTo(px(L.bar.x), px(L.bar.y)); cx.lineTo(px(L.bar.x + L.bar.len), px(L.bar.y));
    cx.moveTo(px(L.bar.x), px(L.bar.y - 1.5)); cx.lineTo(px(L.bar.x), px(L.bar.y + 1.5));
    cx.moveTo(px(L.bar.x + L.bar.len), px(L.bar.y - 1.5)); cx.lineTo(px(L.bar.x + L.bar.len), px(L.bar.y + 1.5)); cx.stroke();
    cx.font = Math.round(px(2)) + 'px Arial, sans-serif'; cx.fillText('35 mm - check after printing', px(L.bar.x + 8), px(L.bar.y - 2.2));
  }
  var r = await jpegBytes(c, JPG_QUALITY, 0); c.width = 0; return r.buf;
}

async function downloadSheet() {
  if (!ready && !people.length) { say(el.msg2, 'No image selected. Please choose a photo first.', 'err'); return; }
  var S = getSpec();
  if (people.some(function (p) { return p.key !== specKey(S); })) { checkPeopleSize(); return; }
  var L = sheetLayout(S.w, S.h, el.copies.value);
  if (!L) { say(el.msg2, 'This photo size does not fit on this paper.', 'err'); return; }
  if (L.tooFew) { say(el.msg2, 'You have ' + L.people + ' people but only ' + L.n + ' copies on the sheet. Choose at least ' + L.people + ' copies.', 'err'); return; }
  say(el.msg2, ''); busyOn('Creating print sheet...'); el.dlSheet.disabled = true;
  var temp = null;
  try {
    await sleep(30);
    // photos on the sheet: the people you added, or (if nobody was added) the photo on screen
    var canvases = people.length ? people.map(function (p) { return p.canvas; }) : [temp = exportCanvas(S)];
    var fmt = el.sheetFmt.value, name, blob, tag = L.n + 'x' + (canvases.length > 1 ? '-' + canvases.length + 'people' : '');
    if (fmt === 'pdf') {
      var jpgs = [];
      for (var i = 0; i < canvases.length; i++) jpgs.push(new Uint8Array((await jpegBytes(canvases[i], JPG_QUALITY, 0)).buf));
      blob = new Blob([buildPdf(jpgs, S.pxW, S.pxH, S, L)], { type: 'application/pdf' }); name = 'passport-sheet-' + el.paper.value + '-' + S.id + '-' + tag + '.pdf';
    } else {
      blob = new Blob([await buildSheetJpg(canvases, S, L)], { type: 'image/jpeg' }); name = 'passport-sheet-' + el.paper.value + '-' + S.id + '-' + tag + '.jpg';
    }
    if (temp) temp.width = 0;
    saveBlob(blob, name);
    var extra = canvases.length > 1 ? ' for ' + canvases.length + ' people (' + L.counts.join(' + ') + ')' : '';
    doneMessage('Downloaded ' + name + ' (' + L.n + ' photos on ' + L.paper.label + extra + ', ' + sizeText(blob.size) + '). Print at 100% / Actual size.');
  } catch (e) { console.error(e); say(el.msg2, 'Image processing failed. Please try fewer copies, a smaller photo or another browser.', 'err'); }
  finally { busyOff(); el.dlSheet.disabled = false; }
}
el.dlSheet.addEventListener('click', downloadSheet);

/* --- upload area --- */
el.pick.addEventListener('click', function (e) { e.stopPropagation(); el.file.click(); });
el.drop.addEventListener('click', function () { el.file.click(); });
el.drop.addEventListener('keydown', function (e) { if ((e.key === 'Enter' || e.key === ' ') && e.target === el.drop) { e.preventDefault(); el.file.click(); } });
el.file.addEventListener('change', function () { var f = el.file.files && el.file.files[0]; el.file.value = ''; if (f) loadFile(f); else say(el.msg, 'No image selected. Please choose a photo first.', 'err'); });
['dragenter', 'dragover'].forEach(function (n) { el.drop.addEventListener(n, function (e) { e.preventDefault(); el.drop.classList.add('over'); }); });
['dragleave', 'drop'].forEach(function (n) { el.drop.addEventListener(n, function (e) { e.preventDefault(); el.drop.classList.remove('over'); }); });
el.drop.addEventListener('drop', function (e) { if (e.dataTransfer && e.dataTransfer.files.length) loadFile(e.dataTransfer.files[0]); });

/* --- start --- */
buildGuides();
applySpec();
})();
</script>
'''


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
    block = (SCREENER_BLOCK.replace("__HYDRO__", json.dumps(hydro_symbols()))
             .replace("__HYDW__", json.dumps(hydro_weights()))
             .replace("__SDATA__", data)) + CALCULATORS_BLOCK + AI_PHOTOSHOP_BLOCK + PASSPORT_PHOTO_BLOCK
    return html_doc.replace("</body>", block + "</body>", 1)


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
    try:
        payload["stories"] = snapshot_stories(not full)       # v1.3 Related Coverage
    except Exception:
        log.exception("snapshot stories failed")
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
    extra = Path(__file__).resolve().parent / "ai-photoshop.html"      # standalone AI Photoshop page (optional)
    if extra.is_file():
        (out / "ai-photoshop.html").write_text(extra.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Copied {extra.name} to {out}")
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
        _story_cache.clear()
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
        _story_cache.clear()
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


# ======================================================================================
# v1.3 RELATED COVERAGE ("story clustering", 8 Oct 2026)
#   One card per story and per NPT day: how many publishers reported it, first report / latest update, and every
#   publisher's own headline with a link back to the original. It re-uses the repeated-story matcher (_cluster)
#   unchanged, so nothing is merged that the hub did not already treat as the same story.
#   Works in the live hub (/api/stories) AND in the static public snapshot (stories are embedded at build time).
#   Public snapshot: headlines, sources, times and links only; never AI text or excerpts.
# ======================================================================================
VERSION = "1.3.0"  # Related Coverage (story clusters) in live hub and public snapshot
_story_cache = {}
STORY_DAYS = 7          # how many NPT days are embedded in the public snapshot
STORY_MAX_MEMBERS = 20  # headlines listed per story


def _npt_day_bounds(day):
    lo = datetime(day.year, day.month, day.day, tzinfo=NPT)
    return iso(lo), iso(lo + timedelta(days=1))


def _build_stories(rows, public=True, min_sources=2):
    """rows: article rows of ONE NPT day. Returns story dicts, best stories first."""
    rk, prio = rank_map(), priority_ids()
    out = []
    for g in _cluster(rows):
        mem = sorted((rows[i] for i in g), key=lambda r: r["sort_at"])
        srcs = {r["source_id"] for r in mem}
        if len(srcs) < max(2, min_sources):
            continue
        ranked = [r for r in mem if r["source_id"] in rk]
        rep = min(ranked, key=lambda r: rk[r["source_id"]]) if ranked else mem[0]
        cat = Counter(r["primary_category"] for r in mem if r["primary_category"]).most_common(1)
        topic = CATEGORIES.get(cat[0][0], cat[0][0]) if cat else ""
        n_prio = sum(1 for s in srcs if s in prio)
        high = any(r["relevance"] == "high" for r in mem)
        n = len(srcs)
        ai = ""
        if not public:
            ai = next((r["summary_ai"] for r in [rep, *mem] if r["summary_ai"]), "") or ""
        out.append({
            "id": mem[0]["id"], "title": rep["title"], "url": rep["url"], "source_name": rep["source_name"],
            "topic": topic, "n_sources": n, "n_articles": len(mem),
            "first_at": mem[0]["sort_at"], "last_at": mem[-1]["sort_at"],
            "first_source": mem[0]["source_name"], "last_source": mem[-1]["source_name"],
            "heat": 3 if n >= 6 else 2 if n >= 4 else 1 if n >= 3 else 0,
            "score": round(n + 0.6 * n_prio + (1.0 if high else 0.0)
                           + (1.5 if topic in _MARKET_LABELS else 0.0), 2),
            "summary": ai[:600],
            "members": [{"source_id": r["source_id"], "source_name": r["source_name"], "title": r["title"],
                         "url": r["url"], "at": r["sort_at"], "priority": r["source_id"] in prio}
                        for r in mem][:STORY_MAX_MEMBERS]})
    out.sort(key=lambda s: s["last_at"], reverse=True)
    out.sort(key=lambda s: (s["score"], s["n_articles"]), reverse=True)   # stable: ties keep newest first
    return out


def stories_for_day(day, min_sources=2, public=True):
    st = STORE_REF.settings() if STORE_REF else DEFAULT_SETTINGS
    tiers = ["high"] if st["mode"] == "strict" else ["high", "medium"]
    key = (day.isoformat(), int(min_sources), bool(public), tuple(tiers))
    ent = _story_cache.get(key)
    if ent and time.time() - ent[0] < 60:
        return ent[1]
    lo, hi = _npt_day_bounds(day)
    with db() as c:
        rows = c.execute("SELECT id,title,url,source_id,source_name,published_at,sort_at,relevance,primary_category,"
                         "summary_ai FROM articles WHERE relevance IN (" + ",".join("?" * len(tiers)) + ") "
                         "AND sort_at>=? AND sort_at<? ORDER BY sort_at DESC LIMIT 4000", [*tiers, lo, hi]).fetchall()
    res = _build_stories(rows, public, min_sources)
    _story_cache[key] = (time.time(), res)
    return res


def snapshot_stories(public=True):
    """Embedded in the static snapshot: the last STORY_DAYS NPT days, stories reported by >= 2 publishers."""
    today = datetime.now(NPT).date()
    days = {}
    for k in range(STORY_DAYS):
        d = today - timedelta(days=k)
        try:
            days[d.isoformat()] = stories_for_day(d, 2, public)
        except Exception:
            log.exception("stories for %s failed", d)
            days[d.isoformat()] = []
    return {"today": today.isoformat(), "days": days}


_orig_create_app_v13 = create_app


def create_app(store, engine, sched=None):
    app = _orig_create_app_v13(store, engine, sched)

    @app.get("/api/stories")
    def api_stories(date: str = "", min_sources: int = 2, limit: int = 60):
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now(NPT).date()
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD")
        if day > datetime.now(NPT).date() or day < datetime.now(NPT).date() - timedelta(days=366):
            raise HTTPException(400, "date out of range")
        ms, limit = max(2, min(10, min_sources)), max(1, min(100, limit))
        return {"date": day.isoformat(), "today": datetime.now(NPT).date().isoformat(), "min_sources": ms,
                "stories": stories_for_day(day, ms, False)[:limit]}

    return app


_EXT_CSS_V13 = """
#stBar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 10px}
#vStories .sf{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 8px}
#vStories .sf select,#vStories .sf input{background:var(--p);border:1px solid var(--ln);border-radius:5px;padding:4px 8px}
#vStories .sn0{font-size:12px;color:var(--mu);margin:0 0 10px}
.story{border:1px solid var(--ln);border-radius:8px;background:var(--p);margin:0 0 10px;padding:10px 12px}
.story.h1{border-left:3px solid var(--hi)}.story.h2,.story.h3{border-left:4px solid var(--hi);background:linear-gradient(90deg,rgba(255,122,69,.16),transparent 55%)}
.story .sh{display:grid;grid-template-columns:54px 1fr;gap:8px;align-items:start}
.story .cn{font-family:Consolas,monospace;font-weight:700;color:var(--hi);font-size:15px;line-height:1.2}
.story .cn small{display:block;font-size:10px;font-weight:600;color:var(--mu)}
.story h3{margin:0;font-size:15px;line-height:1.35}
.story .sm{display:flex;gap:6px 12px;flex-wrap:wrap;font-size:11.5px;color:var(--mu);margin-top:4px;align-items:center}
.story .tp{border:1px solid var(--ln);border-radius:10px;padding:0 8px;background:var(--ch);color:var(--tx)}
.story .ai{margin:8px 0 0;font-size:12.5px}.story .ai b{font-size:10px;letter-spacing:.04em;text-transform:uppercase;color:var(--ac)}
.story ul{list-style:none;margin:8px 0 0;padding:0;border-top:1px solid var(--ln)}
.story li{display:grid;grid-template-columns:128px 1fr 54px;gap:8px;padding:5px 0;border-bottom:1px dashed var(--ln);font-size:12.5px}
.story li:last-child{border-bottom:0}
.story li .sc{font-weight:700;color:var(--ac)}.story li .tm{text-align:right;color:var(--mu);font-variant-numeric:tabular-nums}
@media(max-width:640px){.story li{grid-template-columns:1fr 46px}.story li .sc{grid-column:1/-1}.story .sh{grid-template-columns:44px 1fr}}
"""

_EXT_JS_V13 = r"""<script>
(function(){'use strict';
const SNAP=window.__SNAPSHOT__||null,$=(s,r)=>(r||document).querySelector(s);
const main=$('#maincontent'),vf=$('#vFeed');if(!main||!vf)return;
const TZ='Asia/Kathmandu',okUrl=u=>/^https?:\/\//i.test(u||'')?u:'#';
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
const fT=new Intl.DateTimeFormat('en-GB',{timeZone:TZ,hour:'2-digit',minute:'2-digit',hour12:false});
const fD=new Intl.DateTimeFormat('en-GB',{timeZone:TZ,day:'numeric',month:'short',year:'numeric'});
const fY=new Intl.DateTimeFormat('en-CA',{timeZone:TZ,year:'numeric',month:'2-digit',day:'2-digit'});
const tm=s=>{const d=new Date(s);return isNaN(d)?'':fT.format(d)};
const ymd=d=>fY.format(d);
const flames=n=>n>=6?'🔥🔥🔥 ':n>=4?'🔥🔥 ':n>=3?'🔥 ':'';
const todayStr=()=>(SNAP&&SNAP.stories&&SNAP.stories.today)||ymd(new Date());
const dayLabel=s=>{const t=todayStr();if(s===t)return 'Today';const y=ymd(new Date(new Date(t+'T12:00:00+05:45').getTime()-864e5));return s===y?'Yesterday':''};
const nice=s=>fD.format(new Date(s+'T12:00:00+05:45'));
const ST={on:false,day:todayStr(),min:2};
/* toggle bar + view */
const bar=el('div');bar.id='stBar';
const seg=el('div','seg');seg.setAttribute('role','group');seg.setAttribute('aria-label','News view');
const bA=el('button','on','All articles'),bB=el('button',null,'Related coverage');bA.setAttribute('aria-pressed','true');bB.setAttribute('aria-pressed','false');
bB.title='Same story reported by several publishers, grouped into one card';
seg.append(bA,bB);bar.append(seg);
const view=el('section');view.id='vStories';view.hidden=true;view.setAttribute('aria-label','Related coverage');
const sf=el('div','sf'),selD=el('select'),selM=el('select'),note=el('div','sn0'),list=el('div');
selD.setAttribute('aria-label','Day');selM.setAttribute('aria-label','Minimum publishers');
['2','3','4','5'].forEach(v=>{const o=el('option','','≥ '+v+' publishers');o.value=v;selM.append(o)});
let dateIn=null;
if(!SNAP){dateIn=el('input');dateIn.type='date';dateIn.setAttribute('aria-label','Pick a date');dateIn.max=todayStr()}
function fillDays(){selD.replaceChildren();const t=todayStr();
  const keys=SNAP&&SNAP.stories?Object.keys(SNAP.stories.days).sort().reverse():Array.from({length:7},(_,k)=>ymd(new Date(new Date(t+'T12:00:00+05:45').getTime()-k*864e5)));
  keys.forEach(k=>{const o=el('option','',(dayLabel(k)?dayLabel(k)+' · ':'')+nice(k));o.value=k;selD.append(o)});
  if(!keys.includes(ST.day)){const o=el('option','',nice(ST.day));o.value=ST.day;selD.prepend(o)}selD.value=ST.day}
sf.append(el('span',null,'Day'),selD);if(dateIn)sf.append(dateIn);sf.append(selM);
view.append(sf,note,list);main.prepend(view);main.prepend(bar);
function card(s){
  const a=el('article','story'+(s.heat?' h'+s.heat:''));
  const hd=el('div','sh'),cn=el('div','cn',flames(s.n_sources).trim()+' ×'+s.n_sources),sm=el('small',null,'publishers');cn.append(sm);
  const body=el('div'),h3=el('h3'),l=el('a',null,s.title);l.href=okUrl(s.url);l.target='_blank';l.rel='noopener';h3.append(l);
  const meta=el('div','sm');if(s.topic)meta.append(el('span','tp',s.topic));
  meta.append(el('span',null,nice(ST.day)),el('span',null,s.n_sources+' sources · '+s.n_articles+' reports'),
    el('span',null,'First reported '+tm(s.first_at)+' NPT ('+s.first_source+')'),el('span',null,'Latest update '+tm(s.last_at)+' NPT'));
  body.append(h3,meta);hd.append(cn,body);a.append(hd);
  if(s.summary){const p=el('p','ai');p.append(el('b',null,'AI summary '),document.createTextNode(s.summary));a.append(p)}
  const ul=el('ul');
  s.members.forEach(m=>{const li=el('li'),sc=el('span','sc',(m.priority?'★ ':'')+m.source_name),h=el('a',null,m.title);
    h.href=okUrl(m.url);h.target='_blank';h.rel='noopener';li.append(sc,h,el('span','tm',tm(m.at)));ul.append(li)});
  a.append(ul);
  if(s.n_articles>s.members.length)a.append(el('div','sn0','+ '+(s.n_articles-s.members.length)+' more reports not listed'));
  return a}
function paint(stories){
  const n=stories.filter(s=>s.n_sources>=ST.min);
  note.textContent=n.length?n.length+' stor'+(n.length===1?'y':'ies')+' reported by ≥ '+ST.min+' publishers on '+nice(ST.day)+'. Times are NPT. Each headline is the publisher’s own and links to the original.':'';
  if(!n.length){list.replaceChildren(el('div','empty','No story was reported by ≥ '+ST.min+' publishers on '+nice(ST.day)+(SNAP?' (snapshot covers the last 7 days).':' yet.')));return}
  list.replaceChildren(...n.map(card))}
async function load(){
  fillDays();selM.value=String(ST.min);if(dateIn)dateIn.value=ST.day;
  if(SNAP){const d=(SNAP.stories&&SNAP.stories.days&&SNAP.stories.days[ST.day])||[];paint(d);return}
  try{const r=await fetch('/api/stories?date='+encodeURIComponent(ST.day)+'&min_sources='+ST.min+'&limit=60',{headers:{Accept:'application/json'}});
    if(!r.ok)throw new Error('HTTP '+r.status);paint((await r.json()).stories||[])}
  catch(e){list.replaceChildren(el('div','empty','Related coverage unavailable: '+e.message))}}
function setOn(on){ST.on=on;bA.classList.toggle('on',!on);bB.classList.toggle('on',on);bA.setAttribute('aria-pressed',String(!on));bB.setAttribute('aria-pressed',String(on));
  view.hidden=!on;vf.hidden=on;if(on)load()}
bA.onclick=()=>{if(ST.on){setOn(false)}};bB.onclick=()=>setOn(true);
selD.onchange=()=>{ST.day=selD.value;load()};selM.onchange=()=>{ST.min=+selM.value;load()};
if(dateIn)dateIn.onchange=()=>{if(dateIn.value&&dateIn.value<=todayStr()){ST.day=dateIn.value;load()}};
/* leaving the feed (sidebar: categories, Source Directory, Settings) closes this view */
const side=$('#side');
if(side)side.addEventListener('click',()=>{if(!ST.on)return;view.hidden=true;ST.on=false;bA.classList.add('on');bB.classList.remove('on');
  bA.setAttribute('aria-pressed','true');bB.setAttribute('aria-pressed','false');const vs=$('#vSrc'),vt=$('#vSet');if(vs&&vt&&vs.hidden&&vt.hidden)vf.hidden=false});
/* show the toggle only while the feed area is the active screen */
function syncBar(){const vs=$('#vSrc'),vt=$('#vSet');bar.hidden=!!((vs&&!vs.hidden)||(vt&&!vt.hidden))}
[$('#vSrc'),$('#vSet')].forEach(x=>{if(x)new MutationObserver(syncBar).observe(x,{attributes:true,attributeFilter:['hidden']})});
if(!SNAP)setInterval(()=>{if(ST.on&&ST.day===todayStr()&&!document.hidden)load()},180000);
})();
</script>"""

_orig_cmd_selftest_v13 = cmd_selftest


def cmd_selftest(_args=None) -> int:
    """Earlier tests + v1.3 Related Coverage tests."""
    rc = _orig_cmd_selftest_v13(_args)
    fails = 0

    def check(ok, name):
        nonlocal fails
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        fails += 0 if ok else 1

    def row(i, title, src, name, h=0):
        return {"id": i, "title": title, "source_id": src, "source_name": name, "url": f"https://x.test/{i}",
                "published_at": None, "relevance": "high", "primary_category": "dividend", "summary_ai": "AI TEXT",
                "sort_at": iso(datetime(2026, 10, 8, 4, 0, tzinfo=UTC) + timedelta(hours=h))}

    t = "Chilime Hydropower proposes 10 percent cash dividend for fiscal year 2082/83"
    rows = [row(1, t, "a", "A"), row(2, t + " board decision", "b", "B", 1), row(3, t, "c", "C", 2),
            row(4, "Nepal Rastra Bank monetary policy interest corridor announcement deposit rates", "d", "D", 1)]
    st = _build_stories(rows, public=True)
    check(len(st) == 1 and st[0]["n_sources"] == 3, "stories: 3 publishers join, unrelated headline stays out")
    check(st[0]["summary"] == "" and _build_stories(rows, public=False)[0]["summary"] == "AI TEXT",
          "stories: public build never carries AI text")
    check(st[0]["first_source"] == "A" and st[0]["last_source"] == "C", "stories: first / latest source")
    check(_build_stories(rows, public=True, min_sources=4) == [], "stories: min_sources filter")
    one = [row(1, t, "a", "A"), row(2, t, "a", "A", 1)]
    check(_build_stories(one) == [], "stories: one publisher is never a story")
    print(f"\nv1.3: {'all passed' if not fails else str(fails) + ' FAILED'}")
    return rc or (1 if fails else 0)


DASHBOARD_HTML = (DASHBOARD_HTML.replace("</style></head>", _EXT_CSS_V13 + "</style></head>", 1)
                  .replace("</body></html>", _EXT_JS_V13 + "</body></html>", 1))


if __name__ == "__main__":
    main()
