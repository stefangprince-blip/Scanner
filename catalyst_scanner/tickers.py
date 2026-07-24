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
BARE_RE = re.compile(
    rf"\b(?:{EXCHANGES})\s*[:\-\u2013]\s*([A-Z]{{2,5}})\b"
)

CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")

# Words that look like tickers but aren't. Trimmed to things that genuinely
# show up inside exchange-style patterns or cashtags in wire copy.
BLOCKLIST = {
    "CEO", "CFO", "COO", "CTO", "USA", "USD", "CAD", "ETF", "IPO", "FDA",
    "SEC", "EPS", "GAAP", "IFRS", "NYSE", "OTC", "LLC", "INC", "LTD", "PLC",
    "CORP", "AI", "IT", "US", "EU", "UK", "AND", "THE", "FOR", "NEW", "NOT",
    "ALL", "ANY", "ITS", "OUR", "PDF", "FAQ", "TSX", "CSE", "NEO", "AMEX",
    "DOD", "NIH", "WHO", "EMA", "CE", "EUA", "IND", "NDA", "BLA", "PMA",
    "ATM", "SPAC", "PIPE", "LOI", "MOU", "MW", "GW", "KW", "OEM", "SAAS",
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
        text, re.IGNORECASE,
    ):
        for part in m.group(1).split(","):
            add(part)

    if not found:
        for m in BARE_RE.finditer(text):
            add(m.group(1))
    if not found:
        for m in CASHTAG_RE.finditer(text):
            add(m.group(1))

    return found[:3]   # a release naming 4+ tickers is an index piece, not news
