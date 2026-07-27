"""Pull tradeable tickers out of press-release text.

Precision beats recall here. A false ticker puts a dead row on the board and
costs you attention at exactly the wrong moment, so the parenthetical
"(NASDAQ: ABCD)" convention is the primary signal and everything else is
treated as a weaker fallback.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import requests

from . import config

EXCHANGES = (
    r"NASDAQ\s+(?:Capital|Global|Global\s+Select)\s+Market|NASDAQ|Nasdaq"
    r"|NYSE\s+American|NYSE\s+Arca|NYSE\s+MKT|NYSE|AMEX|NYSE\s+Amex"
    r"|OTCQB|OTCQX|OTC\s+Pink|OTCMKTS|OTC\s+Markets|OTC"
    r"|CBOE|BATS|NEO|CSE|TSXV|TSX"
)

# "(NASDAQ: ABCD)" / "(Nasdaq:ABCD)" / "(NYSE American: AB)" / "(OTCQB: ABCDE)"
PAREN_RE = re.compile(
    rf"\(\s*(?:{EXCHANGES})\s*[:\-\u2013]\s*([A-Z]{{1,5}}(?:\.[A-Z]{{1,2}})?)\s*[,\)]",
    re.IGNORECASE,
)

# Same, but without parentheses: "NASDAQ: ABCD" mid-sentence.
BARE_RE = re.compile(rf"\b(?:{EXCHANGES})\s*[:\-\u2013]\s*([A-Z]{{2,5}})\b")

CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
PLAIN_TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")

# Words that look like tickers but aren't. Trimmed to things that genuinely
# show up inside exchange-style patterns or cashtags in wire copy.
BLOCKLIST = {
    "CEO",
    "CFO",
    "COO",
    "CTO",
    "USA",
    "USD",
    "CAD",
    "ETF",
    "IPO",
    "FDA",
    "SEC",
    "EPS",
    "GAAP",
    "IFRS",
    "NYSE",
    "OTC",
    "LLC",
    "INC",
    "LTD",
    "PLC",
    "CORP",
    "AI",
    "IT",
    "US",
    "EU",
    "UK",
    "AND",
    "THE",
    "FOR",
    "NEW",
    "NOT",
    "ALL",
    "ANY",
    "ITS",
    "OUR",
    "PDF",
    "FAQ",
    "TSX",
    "CSE",
    "NEO",
    "AMEX",
    "DOD",
    "NIH",
    "WHO",
    "EMA",
    "CE",
    "EUA",
    "IND",
    "NDA",
    "BLA",
    "PMA",
    "ATM",
    "SPAC",
    "PIPE",
    "LOI",
    "MOU",
    "MW",
    "GW",
    "KW",
    "OEM",
    "SAAS",
}


def extract(text: str) -> list[str]:
    """Return tickers found in `text`, most-confident first, deduplicated."""
    if not text:
        return []

    found: list[str] = []

    def add(sym: str) -> None:
        sym = sym.strip().strip(".").upper()
        if not sym or sym in BLOCKLIST or sym in found:
            return
        if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z]{1,2})?", sym):
            return
        found.append(sym)

    for m in PAREN_RE.finditer(text):
        add(m.group(1))

    # "(NASDAQ: AAA, BBB)" — grab the trailing symbols in a shared paren group.
    for m in re.finditer(
        rf"\(\s*(?:{EXCHANGES})\s*[:\-]\s*([A-Z]{{1,5}}(?:\s*,\s*[A-Z]{{1,5}})+)\s*\)",
        text,
        re.IGNORECASE,
    ):
        for part in m.group(1).split(","):
            add(part)

    if not found:
        for m in BARE_RE.finditer(text):
            add(m.group(1))
    if not found:
        for m in CASHTAG_RE.finditer(text):
            add(m.group(1))

    if not found:
        for m in PLAIN_TICKER_RE.finditer(text):
            candidate = m.group(1)
            if len(candidate) < 2:
                continue
            if candidate in BLOCKLIST:
                continue
            # Avoid grabbing common English words that happen to be 2-5 letters.
            if candidate.lower() in {
                "this",
                "that",
                "with",
                "from",
                "into",
                "will",
                "have",
                "been",
                "were",
                "said",
                "today",
                "news",
                "analyst",
                "study",
                "phase",
                "report",
                "stock",
                "shares",
                "market",
                "price",
                "value",
                "trade",
                "trading",
                "company",
                "corporation",
                "group",
                "inc",
                "corp",
            }:
                continue
            add(candidate)

    return found[:3]  # a release naming 4+ tickers is an index piece, not news


# ---------------------------------------------------------------------------
# SEC CIK -> ticker map, for EDGAR entries that carry no exchange string.
# ---------------------------------------------------------------------------
_CIK_MAP: dict[str, str] = {}
_CIK_LOCK = threading.Lock()
_CIK_LOADED_AT = 0.0
_CIK_URL = "https://www.sec.gov/files/company_tickers.json"


def _cache_path() -> str:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    return os.path.join(config.CACHE_DIR, "company_tickers.json")


def load_cik_map(force: bool = False) -> dict[str, str]:
    """Load (and daily-refresh) the SEC's CIK->ticker table."""
    global _CIK_LOADED_AT
    with _CIK_LOCK:
        if _CIK_MAP and not force and time.time() - _CIK_LOADED_AT < 86400:
            return _CIK_MAP

        raw = None
        path = _cache_path()
        if (
            not force
            and os.path.exists(path)
            and time.time() - os.path.getmtime(path) < 86400
        ):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                raw = None

        if raw is None:
            try:
                resp = requests.get(
                    _CIK_URL,
                    timeout=20,
                    headers={"User-Agent": config.USER_AGENT},
                )
                resp.raise_for_status()
                raw = resp.json()
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(raw, fh)
            except Exception:
                # Fall back to a stale cache rather than losing the map entirely.
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as fh:
                            raw = json.load(fh)
                    except (OSError, ValueError):
                        raw = {}
                else:
                    raw = {}

        mapping: dict[str, str] = {}
        for row in (raw or {}).values():
            try:
                mapping[str(int(row["cik_str"]))] = str(row["ticker"]).upper()
            except (KeyError, TypeError, ValueError):
                continue
        _CIK_MAP.clear()
        _CIK_MAP.update(mapping)
        _CIK_LOADED_AT = time.time()
        return _CIK_MAP


CIK_IN_URL = re.compile(r"/data/(\d+)/", re.IGNORECASE)
CIK_IN_TEXT = re.compile(r"\(CIK\s+(\d+)\)", re.IGNORECASE)


def from_edgar(title: str, link: str, summary: str) -> list[str]:
    """Resolve an EDGAR filing entry to a ticker via its CIK."""
    blob = f"{title} {summary}"
    cik = None
    m = CIK_IN_URL.search(link or "")
    if m:
        cik = m.group(1)
    else:
        m = CIK_IN_TEXT.search(blob)
        if m:
            cik = m.group(1)
    if not cik:
        return extract(blob)
    sym = load_cik_map().get(str(int(cik)))
    return [sym] if sym else []
