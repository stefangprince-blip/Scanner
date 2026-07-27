"""Configuration for the small-cap catalyst scanner.

Everything tunable lives here. Edit and restart.
"""

import os

# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------
UI_REFRESH_SECONDS = 3
FILTERED_SCAN_SECONDS = 3
US_MARKET_SCAN_SECONDS = 20
QUOTE_REFRESH_SECONDS = 1  # scheduler heartbeat for split scan intervals
QUOTE_BATCH_SIZE = 0  # generic batch setting (scanner can override per flow)
FILTERED_SCAN_CHUNK_SIZE = 20
FILTERED_SCAN_MAX_SYMBOLS = 120
FUNDAMENTALS_REFRESH_SECONDS = 900
US_MARKET_SCAN_ENABLED = True
US_MARKET_SCAN_BATCH_SIZE = 60
US_MARKET_SCAN_FULL_COVERAGE = False
US_MARKET_SCAN_TARGET_FULL_COVERAGE_SECONDS = 300
US_MARKET_SCAN_CHUNK_SIZE = 100
US_MARKET_SCAN_WORKERS = 5
US_MARKET_SCAN_RETRY_SYMBOLS_PER_CYCLE = 100
US_MARKET_FULL_PASS_GAP_FALLBACK_LIMIT = 500
US_MARKET_BULK_TIMEOUT_SECONDS = 30
US_MARKET_RETRY_BACKOFF_SECONDS = 300
US_MARKET_SCAN_SOURCE = "US Major Exchange Scanner"
CLOSED_MARKET_BOOTSTRAP_ENABLED = True
CLOSED_MARKET_SCAN_SECONDS = 0  # 0 disables periodic closed-market full refreshes
US_MARKET_SCAN_MIN_PRICE = 0.4
US_MARKET_SCAN_MIN_VOLUME = 350_000
US_MARKET_SCAN_MIN_DOLLAR_VOLUME = 1_500_000
US_MARKET_SCAN_SCORE = 18
US_MARKET_CATALYST_MIN_CHANGE_PCT = 5.0
US_MARKET_CATALYST_MIN_RVOL = 0.75
US_MAJOR_EXCHANGE_ONLY = True
US_MAJOR_EXCHANGES = {
    "NASDAQ",
    "NMS",
    "NGM",
    "NYQ",
    "NYSE",
    "ASE",
    "AMEX",
    "BATS",
    "IEXG",
}
RVOL_CATALYST_THRESHOLD = 7.0  # relative volume level that triggers a catalyst alert
RVOL_CATALYST_SCORE = 32  # base score used for RVOL catalyst alerts
RVOL_CATALYST_SOURCE = "Relative Volume Scanner"
RVOL_SCAN_LOOKBACK_SECONDS = 24 * 60 * 60  # include recent tickers for RVOL-only discovery
RVOL_INDEPENDENT_ENABLED = True
RVOL_INDEPENDENT_SOURCE = "Independent RVOL Scanner"
RVOL_INDEPENDENT_FLOAT_MAX = 20_000_000
RVOL_INDEPENDENT_SCAN_SECONDS = 120
RVOL_INDEPENDENT_SYMBOL_LIMIT = 25
RVOL_INDEPENDENT_SCREENS = [
    "most_actives",
    "day_gainers",
]
RVOL_NEWS_RESEARCH_ENABLED = True
RVOL_NEWS_RESEARCH_CACHE_SECONDS = 60
RVOL_NEWS_BONUS_SCORE = 18
FILTERED_SYMBOL_NEWS_DEEP_DIVE_ENABLED = True
FILTERED_SYMBOL_NEWS_DEEP_DIVE_SOURCE = "Filtered Symbol News Deep Dive"
FILTERED_SYMBOL_NEWS_DEEP_DIVE_MAX_PER_CYCLE = 20
FILTERED_SYMBOL_NEWS_DEEP_DIVE_RECHECK_SECONDS = 300
FILTERED_SYMBOL_NEWS_DEEP_DIVE_MIN_VOLUME = 100_000
FILTERED_SYMBOL_NEWS_DEEP_DIVE_BONUS_SCORE = 6
ACTIVE_SYMBOL_NEWS_DEEP_DIVE_ENABLED = True
ACTIVE_SYMBOL_NEWS_DEEP_DIVE_SOURCE = "Active Symbol News Deep Dive"
ACTIVE_SYMBOL_NEWS_DEEP_DIVE_RECHECK_SECONDS = 60
ACTIVE_SYMBOL_NEWS_DEEP_DIVE_BONUS_SCORE = 8
ACTIVE_SYMBOL_NEWS_DEEP_DIVE_MAX_PER_CYCLE = 40
VOLUME_MOMENTUM_ENABLED = True
VOLUME_MOMENTUM_MIN_VOLUME = 500_000
VOLUME_MOMENTUM_MIN_DELTA = 5_000
VOLUME_MOMENTUM_MIN_GROWTH = 1.0
VOLUME_MOMENTUM_SCORE = 22
VOLUME_MOMENTUM_SOURCE = "Volume Momentum Scanner"
VOLUME_MOMENTUM_NEWS_BONUS_SCORE = 10
VOLUME_ACTIVITY_ENABLED = True
VOLUME_ACTIVITY_SOURCE = "Volume Activity Scanner"
VOLUME_ACTIVITY_MIN_VOLUME = 500_000
VOLUME_ACTIVITY_SCORE = 16
SCALP_NEWS_WEIGHT = 1.55
SCALP_CHANGE_WEIGHT = 1.15
SCALP_RVOL_WEIGHT = 9.0
SCALP_DOLLAR_VOLUME_WEIGHT = 8.0
SCALP_FLOAT_WEIGHT = 8.0
SCALP_NEWS_QUALITY_WEIGHT = 9.0
SCALP_ACCELERATION_WEIGHT = 8.0
SCALP_SENTIMENT_WEIGHT = 6.0
SCALP_RECENCY_WINDOW_MINUTES = 45
SCALP_NEW_SYMBOL_WINDOW_SECONDS = 180
SCALP_NEW_SYMBOL_BONUS = 28.0
SCALP_STRATEGY_DEFAULT = "balanced"
SCALP_STRATEGY_PROFILES = {
    "balanced": {
        "news_weight": 1.55,
        "change_weight": 1.15,
        "rvol_weight": 9.0,
        "dollar_volume_weight": 8.0,
        "float_weight": 8.0,
        "news_quality_weight": 9.0,
        "acceleration_weight": 8.0,
        "sentiment_weight": 6.0,
        "recency_window_minutes": 45,
        "dilution_penalty": 14.0,
        "distress_penalty": 26.0,
    },
    "news_first": {
        "news_weight": 1.9,
        "change_weight": 0.85,
        "rvol_weight": 7.0,
        "dollar_volume_weight": 6.0,
        "float_weight": 6.0,
        "news_quality_weight": 11.5,
        "acceleration_weight": 6.0,
        "sentiment_weight": 7.0,
        "recency_window_minutes": 55,
        "dilution_penalty": 16.0,
        "distress_penalty": 28.0,
    },
    "momentum_first": {
        "news_weight": 1.25,
        "change_weight": 1.55,
        "rvol_weight": 12.0,
        "dollar_volume_weight": 10.0,
        "float_weight": 7.0,
        "news_quality_weight": 7.0,
        "acceleration_weight": 12.0,
        "sentiment_weight": 5.0,
        "recency_window_minutes": 35,
        "dilution_penalty": 11.0,
        "distress_penalty": 24.0,
    },
}
FEED_POLL_SECONDS = 45  # base poll interval per feed (conditional GET) - increased default for broader search/parse time
NEWS_DETAIL_CACHE_SECONDS = 45
NEWS_HOVER_INTENT_MS = 200
NEWS_SCORING_MODEL_VERSION = "news-score-v2"
QUICK_NEWS_ENABLED = True
QUICK_NEWS_CACHE_SECONDS = 60
QUICK_NEWS_STALE_SECONDS = 180
QUICK_NEWS_MAX_QUEUE = 300
QUICK_NEWS_WORKERS = 2
QUICK_NEWS_WEIGHT = 1.0
QUICK_NEWS_NEW_HEADLINE_MULTIPLIER = 0.25
QUICK_NEWS_NEW_HEADLINE_DECAY_SECONDS = 120
SCORE_DETAILS_CACHE_SECONDS = 60
SCORE_DETAILS_MODEL_VERSION = "score-details-v1"
ALLOW_MULTIPLE_EXPANDED_ROWS = True
FILTER_EXIT_GRACE_SECONDS = 3
SCANNER_AGE_FRESH_WINDOW_SECONDS = 30
SCANNER_AGE_CONFIRMATION_WINDOW_SECONDS = 180
SCANNER_AGE_STABLE_WINDOW_SECONDS = 600
SCANNER_AGE_FRESH_ENTRY_POINTS = 72.0
SCANNER_AGE_MAX_PERSISTENCE_POINTS = 100.0
SCANNER_AGE_STALE_HALF_LIFE_SECONDS = 600
NEWS_DECAY_EXCEPTIONAL_SECONDS = 90 * 60
NEWS_DECAY_STRONG_SECONDS = 60 * 60
NEWS_DECAY_MODERATE_SECONDS = 30 * 60
NEWS_DECAY_WEAK_SECONDS = 15 * 60
NEWS_DECAY_MINIMAL_SECONDS = 15 * 60
FEED_JITTER_SECONDS = 2.5  # random jitter so feeds don't fire in lockstep
MAX_FEED_ITEMS_PER_FETCH = 80  # cap how many feed items are processed per poll to keep the scanner snappy
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
MAX_MARKET_CAP = None
MIN_MARKET_CAP = None
MAX_PRICE = None
MIN_PRICE = None
MAX_FLOAT = None
MIN_SCORE = 12  # slightly lower floor to surface more potential movers

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
        "&company=&dateb=&owner=include&count=200&output=atom",
        "kind": "edgar",
        "weight": 1.15,
    },
    {
        "name": "SEC 425/SC TO",  # merger + tender offer communications
        "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=425"
        "&company=&dateb=&owner=include&count=100&output=atom",
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
    # Additional broader sources to increase coverage
    {
        "name": "MarketWatch",
        "url": "https://www.marketwatch.com/rss/topstories",
        "kind": "rss",
        "weight": 0.8,
    },
    {
        "name": "Yahoo Finance",
        "url": "https://finance.yahoo.com/rss/topstories",
        "kind": "rss",
        "weight": 0.9,
    },
    {
        "name": "Reuters Top",
        "url": "https://www.reuters.com/rssFeed/topNews",
        "kind": "rss",
        "weight": 0.9,
    },
    {
        "name": "Seeking Alpha",
        "url": "https://seekingalpha.com/feed.xml",
        "kind": "rss",
        "weight": 0.85,
    },
    {
        "name": "Nasdaq Press",
        "url": "https://www.nasdaq.com/feed/rssoutbound",
        "kind": "rss",
        "weight": 0.85,
    },
    {
        "name": "Investing.com",
        "url": "https://www.investing.com/rss/news.rss",
        "kind": "rss",
        "weight": 0.85,
    },
    {
        "name": "SEC 6-K",
        "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=6-K&company=&dateb=&owner=include&count=100&output=atom",
        "kind": "edgar",
        "weight": 1.05,
    },
    {
        "name": "StreetInsider",
        "url": "https://www.streetinsider.com/rss.php",
        "kind": "rss",
        "weight": 0.8,
    },
    # Social sources (Reddit subreddits as JSON endpoints)
    {
        "name": "Reddit: r/pennystocks",
        "url": "https://www.reddit.com/r/pennystocks/new.json",
        "kind": "reddit",
        "weight": 0.6,
        "min_social": 10,
    },
    {
        "name": "Reddit: r/stocks",
        "url": "https://www.reddit.com/r/stocks/new.json",
        "kind": "reddit",
        "weight": 0.6,
        "min_social": 6,
    },
    {
        "name": "Reddit: r/investing",
        "url": "https://www.reddit.com/r/investing/new.json",
        "kind": "reddit",
        "weight": 0.6,
        "min_social": 6,
    },
    # StockTwits trending messages (JSON)
    {
        "name": "StockTwits Trending",
        "url": "https://api.stocktwits.com/api/2/streams/trending.json",
        "kind": "stocktwits",
        "weight": 0.7,
        "min_social": 6,
    },
]


# --------------------------------------------------------------------------
# Social / noise filtering
# --------------------------------------------------------------------------
# Minimum social score (upvotes/likes) for social sources to be considered
SOCIAL_MIN_SCORE = int(os.getenv("SCANNER_SOCIAL_MIN_SCORE", "6"))

# --------------------------------------------------------------------------
# Scoring / news freshness
# --------------------------------------------------------------------------
# Only include alerts whose published time is within this many hours of now.
# For scalp/day-trading, keep it tight so stale headlines don't clutter the board.
MAX_NEWS_AGE_HOURS = int(os.getenv("SCANNER_MAX_NEWS_AGE_HOURS", "24"))

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
DB_PATH = os.getenv("SCANNER_DB", "scanner.db")
CACHE_DIR = os.getenv("SCANNER_CACHE", ".cache")
