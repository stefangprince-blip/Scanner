"""Configuration for the small-cap catalyst scanner.

Everything tunable lives here. Edit and restart.
"""
import os

# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------
UI_REFRESH_SECONDS = 5          # dashboard polls the API this often
QUOTE_REFRESH_SECONDS = 5       # live price/volume refresh for tickers on board
FEED_POLL_SECONDS = 5           # base poll interval per feed (conditional GET)
FEED_JITTER_SECONDS = 2.5       # random jitter so feeds don't fire in lockstep
ALERT_TTL_SECONDS = 4 * 60 * 60  # how long a headline stays on the scanner

# Backoff when a feed rate-limits us (429/403). Grows, then decays on success.
FEED_BACKOFF_START = 30
FEED_BACKOFF_MAX = 900

# --------------------------------------------------------------------------
# Identity — SEC *requires* a real contact in the User-Agent or it will 403 you.
# --------------------------------------------------------------------------
USER_AGENT = os.getenv(
    "SCANNER_USER_AGENT",
    "SmallCapCatalystScanner/1.0 (contact: you@example.com)",
)

# --------------------------------------------------------------------------
# Universe filters — the point is micro/small caps that can actually move.
# Set any bound to None to disable it.
# --------------------------------------------------------------------------
MAX_MARKET_CAP = 2_000_000_000   # $2B ceiling
MIN_MARKET_CAP = 3_000_000       # below this it's usually untradeable junk
MAX_PRICE = 50.00
MIN_PRICE = 0.15
MAX_FLOAT = 100_000_000          # low float moves fastest; None to disable
MIN_SCORE = 15                   # catalyst score floor to appear on the board

# Show alerts whose fundamentals we could not resolve? Keeping these True means
# brand-new tickers and IPOs still surface instead of being silently dropped.
KEEP_UNKNOWN_FUNDAMENTALS = True

# --------------------------------------------------------------------------
# Quote provider: "yfinance" (works out of the box) or "webull" (wire your
# existing OpenAPI adapter into quotes.WebullProvider) or "none".
# --------------------------------------------------------------------------
QUOTE_PROVIDER = os.getenv("SCANNER_QUOTES", "yfinance")

# --------------------------------------------------------------------------
# News feeds.
#
# `weight` scales the catalyst score — the regulated wires (SEC, BusinessWire,
# GlobeNewswire) carry more signal than aggregators that republish everything.
#
# Feed URLs drift. Run `python run.py --check-feeds` to see which are live.
# --------------------------------------------------------------------------
FEEDS = [
    {
        "name": "SEC 8-K",
        "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
               "&company=&dateb=&owner=include&count=100&output=atom",
        "kind": "edgar",
        "weight": 1.15,
    },
    {
        "name": "SEC 425/SC TO",   # merger + tender offer communications
        "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=425"
               "&company=&dateb=&owner=include&count=40&output=atom",
        "kind": "edgar",
        "weight": 1.25,
    },
    {
        "name": "GlobeNewswire",
        "url": "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/"
               "GlobeNewswire%20-%20News%20about%20Public%20Companies",
        "kind": "rss",
        "weight": 1.10,
    },
    {
        "name": "BusinessWire",
        "url": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRXQ==",
        "kind": "rss",
        "weight": 1.10,
    },
    {
        "name": "PR Newswire",
        "url": "https://www.prnewswire.com/rss/news-releases-list.rss",
        "kind": "rss",
        "weight": 1.00,
    },
    {
        "name": "ACCESSWIRE",
        "url": "https://www.accesswire.com/rss/latest.aspx",
        "kind": "rss",
        "weight": 0.95,
    },
    {
        "name": "Newsfile",
        "url": "https://www.newsfilecorp.com/rss",
        "kind": "rss",
        "weight": 0.90,
    },
    {
        "name": "StockTitan",
        "url": "https://www.stocktitan.net/rss",
        "kind": "rss",
        "weight": 0.95,
    },
]

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
DB_PATH = os.getenv("SCANNER_DB", "scanner.db")
CACHE_DIR = os.getenv("SCANNER_CACHE", ".cache")
