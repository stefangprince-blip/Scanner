"""Persistent alert store with a rolling TTL window, plus a quote cache.

SQLite so a restart mid-session doesn't wipe the board. The 4-hour window is
enforced on read and on a periodic prune, so a stalled prune thread can never
leave stale rows on screen.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id           TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    headline     TEXT NOT NULL,
    url          TEXT,
    source       TEXT,
    published    REAL,
    first_seen   REAL NOT NULL,
    score        INTEGER NOT NULL,
    tags         TEXT,
    dilution     TEXT,
    distress     TEXT,
    body         TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_seen ON alerts(first_seen);
CREATE INDEX IF NOT EXISTS idx_alerts_ticker ON alerts(ticker);
"""

_NORM_RE = re.compile(r"[^a-z0-9 ]+")
_STOP = re.compile(r"\b(?:announces|announced|reports|inc|corp|ltd|the|a|an)\b")


def alert_id(ticker: str, headline: str) -> str:
    """Stable ID that collapses the same story republished across wires."""
    norm = _NORM_RE.sub(" ", (headline or "").lower())
    norm = _STOP.sub(" ", norm)
    norm = " ".join(norm.split())[:120]
    return hashlib.sha1(f"{ticker.upper()}|{norm}".encode()).hexdigest()[:16]


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or config.DB_PATH
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # ticker -> quote dict, refreshed on the quote thread
        self.quotes: dict[str, dict] = {}
        self._quote_lock = threading.Lock()

    # -- alerts ------------------------------------------------------------
    def add(self, alert: dict) -> bool:
        """Insert an alert. Returns True if it was new."""
        aid = alert_id(alert["ticker"], alert["headline"])
        now = time.time()
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM alerts WHERE id = ?", (aid,))
            if cur.fetchone():
                return False
            self._conn.execute(
                "INSERT INTO alerts (id, ticker, headline, url, source, published,"
                " first_seen, score, tags, dilution, distress, body)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    alert["ticker"].upper(),
                    alert["headline"],
                    alert.get("url", ""),
                    alert.get("source", ""),
                    alert.get("published", now),
                    now,
                    int(alert.get("score", 0)),
                    json.dumps(alert.get("tags", [])),
                    json.dumps(alert.get("dilution", [])),
                    json.dumps(alert.get("distress", [])),
                    (alert.get("body", "") or "")[:600],
                ),
            )
            self._conn.commit()
        return True

    def active(self, ttl: int | None = None) -> list[dict]:
        ttl = config.ALERT_TTL_SECONDS if ttl is None else ttl
        cutoff = time.time() - ttl
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE first_seen >= ? ORDER BY first_seen DESC",
                (cutoff,),
            ).fetchall()
        out = []
        now = time.time()
        for r in rows:
            out.append({
                "id": r["id"],
                "ticker": r["ticker"],
                "headline": r["headline"],
                "url": r["url"],
                "source": r["source"],
                "published": r["published"],
                "first_seen": r["first_seen"],
                "age_seconds": now - r["first_seen"],
                "ttl_fraction": max(0.0, 1.0 - (now - r["first_seen"]) / ttl),
                "score": r["score"],
                "tags": json.loads(r["tags"] or "[]"),
                "dilution": json.loads(r["dilution"] or "[]"),
                "distress": json.loads(r["distress"] or "[]"),
                "body": r["body"] or "",
            })
        return out

    def active_tickers(self, ttl: int | None = None) -> list[str]:
        ttl = config.ALERT_TTL_SECONDS if ttl is None else ttl
        cutoff = time.time() - ttl
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT ticker FROM alerts WHERE first_seen >= ?",
                (cutoff,),
            ).fetchall()
        return [r["ticker"] for r in rows]

    def prune(self, ttl: int | None = None) -> int:
        ttl = config.ALERT_TTL_SECONDS if ttl is None else ttl
        cutoff = time.time() - ttl
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM alerts WHERE first_seen < ?", (cutoff,)
            )
            self._conn.commit()
            removed = cur.rowcount
        if removed:
            live = set(self.active_tickers(ttl))
            with self._quote_lock:
                for sym in list(self.quotes):
                    if sym not in live:
                        self.quotes.pop(sym, None)
        return removed

    # -- quotes ------------------------------------------------------------
    def set_quote(self, ticker: str, quote: dict) -> None:
        with self._quote_lock:
            self.quotes[ticker.upper()] = {**quote, "updated": time.time()}

    def get_quote(self, ticker: str) -> dict:
        with self._quote_lock:
            return dict(self.quotes.get(ticker.upper(), {}))

    def all_quotes(self) -> dict[str, dict]:
        with self._quote_lock:
            return {k: dict(v) for k, v in self.quotes.items()}

    # -- lifecycle helpers -------------------------------------------------
    def close(self) -> None:
        """Close the underlying sqlite connection."""
        try:
            if hasattr(self, '_conn') and self._conn:
                self._conn.close()
                self._conn = None
        except Exception:
            # best-effort close; don't raise during cleanup
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        # Ensure connection is closed when object is garbage-collected
        try:
            self.close()
        except Exception:
            pass
