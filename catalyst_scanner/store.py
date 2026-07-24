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
    sources      TEXT,
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
        # Ensure older DBs get the new 'sources' column
        try:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(alerts)").fetchall()]
            if 'sources' not in cols:
                self._conn.execute("ALTER TABLE alerts ADD COLUMN sources TEXT DEFAULT '[]'")
                self._conn.commit()
        except Exception:
            # If ALTER not permitted or fails, keep running — older DBs will still work
            pass
        # ticker -> quote dict, refreshed on the quote thread
        self.quotes: dict[str, dict] = {}
        self._quote_lock = threading.Lock()

    # -- alerts ------------------------------------------------------------
    def _rows_to_alerts(self, rows: list, ttl: int) -> list[dict]:
        out = []
        now = time.time()
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "ticker": r["ticker"],
                    "headline": r["headline"],
                    "url": r["url"],
                    "source": r["source"],
                    "sources": (json.loads(r["sources"] or "[]") if r["sources"] is not None else ([r["source"]] if r["source"] else [])),
                    "published": r["published"],
                    "first_seen": r["first_seen"],
                    "age_seconds": now - r["first_seen"],
                    "ttl_fraction": max(0.0, 1.0 - (now - r["first_seen"]) / ttl),
                    "score": r["score"],
                    "tags": json.loads(r["tags"] or "[]"),
                    "dilution": json.loads(r["dilution"] or "[]"),
                    "distress": json.loads(r["distress"] or "[]"),
                    "body": r["body"] or "",
                }
            )
        return out

    def add(self, alert: dict) -> bool:
        """Insert an alert. Returns True if it was new."""
        aid = alert_id(alert["ticker"], alert["headline"])
        now = time.time()
        with self._lock:
            cur = self._conn.execute("SELECT * FROM alerts WHERE id = ?", (aid,))
            row = cur.fetchone()
            if row:
                # Existing alert: merge sources, tags, dilution, distress and bump score if higher
                # Safely read existing row values (sqlite3.Row does not support .get())
                cols = list(row.keys())
                sources_val = row['sources'] if 'sources' in cols else None
                source_val = row['source'] if 'source' in cols else None
                try:
                    existing_sources = json.loads(sources_val) if sources_val is not None else ([source_val] if source_val else [])
                except Exception:
                    existing_sources = [source_val] if source_val else []
                new_source = alert.get('source') or ''
                if new_source and new_source not in existing_sources:
                    existing_sources.append(new_source)
                # Merge arrays for tags/dilution/distress
                try:
                    existing_tags = set(json.loads(row['tags'] or '[]')) if 'tags' in cols else set()
                except Exception:
                    existing_tags = set()
                try:
                    existing_dil = set(json.loads(row['dilution'] or '[]')) if 'dilution' in cols else set()
                except Exception:
                    existing_dil = set()
                try:
                    existing_dist = set(json.loads(row['distress'] or '[]')) if 'distress' in cols else set()
                except Exception:
                    existing_dist = set()
                new_tags = set(alert.get('tags', []))
                new_dil = set(alert.get('dilution', []))
                new_dist = set(alert.get('distress', []))
                merged_tags = list(existing_tags.union(new_tags))
                merged_dil = list(existing_dil.union(new_dil))
                merged_dist = list(existing_dist.union(new_dist))
                # Score: keep the max to reflect strongest signal observed
                existing_score = int(row['score'] or 0) if 'score' in cols else 0
                new_score = max(int(alert.get('score', 0)), existing_score)
                # Update the DB row with merged info
                self._conn.execute(
                    "UPDATE alerts SET sources = ?, source = ?, score = ?, tags = ?, dilution = ?, distress = ? WHERE id = ?",
                    (json.dumps(existing_sources), ', '.join(existing_sources), new_score, json.dumps(merged_tags), json.dumps(merged_dil), json.dumps(merged_dist), aid),
                )
                self._conn.commit()
                return False

            self._conn.execute(
                "INSERT INTO alerts (id, ticker, headline, url, source, sources, published,"
                " first_seen, score, tags, dilution, distress, body)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    alert["ticker"].upper(),
                    alert["headline"],
                    alert.get("url", ""),
                    alert.get("source", ""),
                    json.dumps([alert.get("source","")]) ,
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
        # Best-effort notification hook so external listeners can react in realtime.
        try:
            # Import locally to avoid creating an import-time dependency cycle
            from . import notifications

            # Send a lightweight alert payload to notification channels
            notif = {
                "id": aid,
                "ticker": alert.get("ticker", "").upper(),
                "headline": alert.get("headline", ""),
                "url": alert.get("url", ""),
                "source": alert.get("source", ""),
                "published": alert.get("published", now),
                "score": int(alert.get("score", 0)),
            }
            notifications.send_alert(notif)
        except Exception:
            # Keep store behavior robust even if notifications fail
            pass
        return True

    def active(self, ttl: int | None = None) -> list[dict]:
        ttl = config.ALERT_TTL_SECONDS if ttl is None else ttl
        cutoff = time.time() - ttl
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE first_seen >= ? ORDER BY first_seen DESC",
                (cutoff,),
            ).fetchall()
        return self._rows_to_alerts(rows, ttl)

    def active_latest_per_ticker(self, ttl: int | None = None) -> list[dict]:
        ttl = config.ALERT_TTL_SECONDS if ttl is None else ttl
        cutoff = time.time() - ttl
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT a.* FROM alerts a
                JOIN (
                    SELECT ticker, MAX(first_seen) AS max_seen
                    FROM alerts
                    WHERE first_seen >= ?
                    GROUP BY ticker
                ) latest
                  ON a.ticker = latest.ticker AND a.first_seen = latest.max_seen
                WHERE a.first_seen >= ?
                ORDER BY a.first_seen DESC
                """,
                (cutoff, cutoff),
            ).fetchall()
        return self._rows_to_alerts(rows, ttl)

    def active_tickers(self, ttl: int | None = None) -> list[str]:
        ttl = config.ALERT_TTL_SECONDS if ttl is None else ttl
        cutoff = time.time() - ttl
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT ticker FROM alerts WHERE first_seen >= ?",
                (cutoff,),
            ).fetchall()
        return [r["ticker"] for r in rows]

    def recent_tickers(self, lookback_seconds: int) -> list[str]:
        cutoff = time.time() - max(0, int(lookback_seconds))
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

    def quotes_snapshot(self) -> dict[str, dict]:
        with self._quote_lock:
            return {k: dict(v) for k, v in self.quotes.items()}

    def all_quotes(self) -> dict[str, dict]:
        with self._quote_lock:
            return {k: dict(v) for k, v in self.quotes.items()}

    # -- lifecycle helpers -------------------------------------------------
    def close(self) -> None:
        """Close the underlying sqlite connection."""
        try:
            if getattr(self, "_conn", None):
                try:
                    self._conn.close()
                except Exception:
                    pass
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
