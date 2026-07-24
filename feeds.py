"""Polls news feeds and turns entries into scored alerts.

Polling every 5 seconds only works because every request is conditional: we
send back the ETag / Last-Modified the wire gave us, and a 304 costs a few
hundred bytes with no body to parse. Feeds that answer 429 or 403 get an
exponential backoff so one angry host can't take the scanner down.
"""
from __future__ import annotations

import logging
import random
import threading
import time

import requests

from . import config, rssparse, scoring, tickers

log = logging.getLogger("scanner.feeds")


class Feed:
    def __init__(self, spec: dict):
        self.name = spec["name"]
        self.url = spec["url"]
        self.kind = spec.get("kind", "rss")
        self.weight = float(spec.get("weight", 1.0))
        self.interval = float(spec.get("poll_seconds", config.FEED_POLL_SECONDS))
        self.etag: str | None = None
        self.modified: str | None = None
        self.backoff = 0.0
        self.next_poll = 0.0
        self.last_status: str = "waiting"
        self.last_ok: float = 0.0
        self.error_count = 0
        self.entries_seen = 0
        self._seen_ids: set[str] = set()

    def due(self, now: float) -> bool:
        return now >= self.next_poll

    def schedule(self, now: float) -> None:
        wait = self.interval + random.uniform(0, config.FEED_JITTER_SECONDS)
        self.next_poll = now + wait + self.backoff

    def fetch(self, session: requests.Session) -> list[dict]:
        headers = {"User-Agent": config.USER_AGENT,
                   "Accept": "application/atom+xml, application/rss+xml, "
                             "application/xml;q=0.9, */*;q=0.8"}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.modified:
            headers["If-Modified-Since"] = self.modified

        try:
            resp = session.get(self.url, headers=headers, timeout=12)
        except requests.RequestException as exc:
            self.error_count += 1
            self.last_status = f"error: {type(exc).__name__}"
            self._grow_backoff()
            return []

        if resp.status_code == 304:
            self.last_status = "304 unchanged"
            self.last_ok = time.time()
            self.backoff = max(0.0, self.backoff * 0.5)
            return []

        if resp.status_code in (403, 429, 503):
            self.error_count += 1
            self.last_status = f"HTTP {resp.status_code} (backing off)"
            self._grow_backoff()
            return []

        if resp.status_code != 200:
            self.error_count += 1
            self.last_status = f"HTTP {resp.status_code}"
            return []

        self.etag = resp.headers.get("ETag") or self.etag
        self.modified = resp.headers.get("Last-Modified") or self.modified
        self.backoff = 0.0
        self.error_count = 0
        self.last_ok = time.time()

        entries = rssparse.parse(resp.content)
        self.last_status = f"200 · {len(entries)} entries"
        return entries

    def _grow_backoff(self) -> None:
        self.backoff = min(
            config.FEED_BACKOFF_MAX,
            max(config.FEED_BACKOFF_START, self.backoff * 2),
        )

    def new_entries(self, entries: list[dict]) -> list[dict]:
        """Filter out entries this feed has already handed us."""
        fresh = []
        for e in entries:
            key = e.get("raw_id") or e.get("link") or e.get("title", "")
            if key in self._seen_ids:
                continue
            self._seen_ids.add(key)
            fresh.append(e)
        if len(self._seen_ids) > 5000:
            self._seen_ids = set(list(self._seen_ids)[-2500:])
        return fresh

    def to_alerts(self, entries: list[dict], first_run: bool) -> list[dict]:
        alerts: list[dict] = []
        cutoff = time.time() - config.ALERT_TTL_SECONDS
        for e in entries:
            # On startup, seed the board with the last 4 hours but skip
            # anything older so we don't backfill yesterday's news.
            if e["published"] < cutoff:
                continue

            title, summary = e["title"], e.get("summary", "")
            if self.kind == "edgar":
                syms = tickers.from_edgar(title, e.get("link", ""), summary)
            else:
                syms = tickers.extract(f"{title} {summary}")
            if not syms:
                continue

            result = scoring.score(title, summary, self.weight)
            if result["junk"] or result["score"] < config.MIN_SCORE:
                continue

            for sym in syms:
                alerts.append({
                    "ticker": sym,
                    "headline": title,
                    "url": e.get("link", ""),
                    "source": self.name,
                    "published": e["published"],
                    "body": summary,
                    **{k: result[k] for k in
                       ("score", "tags", "dilution", "distress")},
                })
        return alerts


class FeedManager:
    """Owns every feed and runs them from one thread on a due-time loop."""

    def __init__(self, specs: list[dict] | None = None):
        self.feeds = [Feed(s) for s in (specs or config.FEEDS)]
        self.session = requests.Session()
        self._first_run = True
        self._lock = threading.Lock()

    def poll_due(self) -> list[dict]:
        now = time.time()
        alerts: list[dict] = []
        for feed in self.feeds:
            if not feed.due(now):
                continue
            entries = feed.fetch(self.session)
            feed.schedule(time.time())
            if entries:
                fresh = feed.new_entries(entries)
                feed.entries_seen += len(fresh)
                alerts.extend(feed.to_alerts(fresh, self._first_run))
        self._first_run = False
        return alerts

    def status(self) -> list[dict]:
        now = time.time()
        return [{
            "name": f.name,
            "status": f.last_status,
            "seconds_since_ok": round(now - f.last_ok, 1) if f.last_ok else None,
            "backoff": round(f.backoff, 1),
            "errors": f.error_count,
            "entries": f.entries_seen,
        } for f in self.feeds]

    def check(self) -> None:
        """One-shot connectivity report. Run this after editing FEEDS."""
        print(f"{'FEED':<18} {'RESULT':<26} TICKERS FOUND IN LATEST BATCH")
        print("-" * 78)
        for feed in self.feeds:
            entries = feed.fetch(self.session)
            syms: list[str] = []
            for e in entries[:25]:
                if feed.kind == "edgar":
                    syms += tickers.from_edgar(e["title"], e.get("link", ""),
                                               e.get("summary", ""))
                else:
                    syms += tickers.extract(f"{e['title']} {e.get('summary','')}")
            uniq = sorted(set(syms))
            preview = ", ".join(uniq[:8]) or "—"
            print(f"{feed.name:<18} {feed.last_status:<26} {preview}")
