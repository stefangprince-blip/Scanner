# Feeds module (copied from top-level feeds.py)
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
        self.spec = spec
        self.name = spec["name"]
        self.url = spec["url"]
        self.kind = spec.get("kind", "rss")
        self.weight = float(spec.get("weight", 1.0))
        self.interval = float(spec.get("poll_seconds", config.FEED_POLL_SECONDS))
        self.min_social = spec.get('min_social')
        self.max_entries = int(spec.get("max_entries", config.MAX_FEED_ITEMS_PER_FETCH) or config.MAX_FEED_ITEMS_PER_FETCH)
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
        headers = {
            "User-Agent": config.USER_AGENT,
            "Accept": "application/atom+xml, application/rss+xml, "
            "application/xml;q=0.9, */*;q=0.8",
        }
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.modified:
            headers["If-Modified-Since"] = self.modified

        try:
            # For JSON-based social endpoints we'll override parsing below
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

        # Handle JSON social feeds (reddit/stocktwits) specially
        if self.kind == 'reddit':
            try:
                j = resp.json()
                entries = []
                for item in (j.get('data', {}).get('children', []) or [])[:200]:
                    d = item.get('data', {})
                    entries.append(
                        {
                            'title': d.get('title') or d.get('link_title') or '',
                            'summary': (d.get('selftext') or '')[:800],
                            'link': 'https://www.reddit.com' + (d.get('permalink') or ''),
                            'published': float(d.get('created_utc') or time.time()),
                            'raw_id': d.get('id'),
                            'social_score': int(d.get('score') or 0),
                        }
                    )
                self.last_status = f"200 · {len(entries)} entries"
                # apply per-feed min_social if provided
                if self.min_social is not None:
                    entries = [e for e in entries if (e.get('social_score') or 0) >= int(self.min_social)]
                return self._trim_entries(entries)
            except Exception:
                self.last_status = 'error: reddit-parse'
                return []

        if self.kind == 'stocktwits':
            try:
                j = resp.json()
                entries = []
                for m in j.get('messages', [])[:200]:
                    # StockTwits messages include 'body', 'created_at', 'id', and 'symbols'
                    body = m.get('body') or ''
                    symbols = [s.get('symbol') for s in m.get('symbols', []) if s.get('symbol')]
                    created = m.get('created_at')
                    # created_at is ISO8601; try to parse to epoch
                    ts = time.time()
                    try:
                        from email.utils import parsedate_to_datetime

                        dt = parsedate_to_datetime(created)
                        ts = dt.timestamp()
                    except Exception:
                        try:
                            # fallback: parse common format
                            ts = time.mktime(time.strptime(created, '%Y-%m-%dT%H:%M:%SZ'))
                        except Exception:
                            ts = time.time()
                    social_score = 0
                    # likes/replies may be nested; try to extract a heuristic
                    try:
                        social_score = int(m.get('likes', {}).get('count', 0) or 0)
                    except Exception:
                        social_score = 0
                    link = m.get('id') and f"https://stocktwits.com/message/{m.get('id')}" or ''
                    title = (', '.join(symbols) + ': ' + (body[:120] or '')).strip()
                    entries.append(
                        {
                            'title': title,
                            'summary': body[:800],
                            'link': link,
                            'published': float(ts),
                            'raw_id': m.get('id'),
                            'social_score': social_score,
                        }
                    )
                self.last_status = f"200 · {len(entries)} entries"
                if self.min_social is not None:
                    entries = [e for e in entries if (e.get('social_score') or 0) >= int(self.min_social)]
                return self._trim_entries(entries)
            except Exception:
                self.last_status = 'error: stocktwits-parse'
                return []

        # Default: parse as RSS/Atom
        entries = rssparse.parse(resp.content)
        self.last_status = f"200 · {len(entries)} entries"
        return self._trim_entries(entries)

    def _grow_backoff(self) -> None:
        self.backoff = min(
            config.FEED_BACKOFF_MAX,
            max(config.FEED_BACKOFF_START, self.backoff * 2),
        )

    def _trim_entries(self, entries: list[dict]) -> list[dict]:
        if not entries:
            return []
        try:
            entries = sorted(entries, key=lambda e: float(e.get("published", 0) or 0), reverse=True)
        except Exception:
            pass
        if self.max_entries and self.max_entries > 0:
            entries = entries[: self.max_entries]
        return entries

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
            # Only consider items that are current enough for day-trading.
            # Keep it very tight: same calendar day and within the recent age window.
            try:
                now_ts = time.time()
                pub_ts = float(e.get("published", 0) or 0)
                age_hours = max(0.0, (now_ts - pub_ts) / 3600.0)
                if age_hours > config.MAX_NEWS_AGE_HOURS:
                    continue
                # Also require the item to be from the same local day to avoid older-news clutter.
                now_tm = time.localtime(now_ts)
                pub_tm = time.localtime(pub_ts)
                if not (now_tm.tm_year == pub_tm.tm_year and now_tm.tm_mon == pub_tm.tm_mon and now_tm.tm_mday == pub_tm.tm_mday):
                    continue
            except Exception:
                if e.get("published", 0) < cutoff:
                    continue

            title, summary = e["title"], e.get("summary", "")
            # Social feeds: skip low-score posts
            if self.kind in ('reddit', 'stocktwits'):
                if (e.get('social_score') or 0) < config.SOCIAL_MIN_SCORE:
                    continue

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
                alerts.append(
                    {
                        "ticker": sym,
                        "headline": title,
                        "url": e.get("link", ""),
                        "source": self.name,
                        "published": e["published"],
                        "body": summary,
                        **{
                            k: result[k]
                            for k in ("score", "tags", "dilution", "distress")
                        },
                    }
                )
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
        seen_keys: set[str] = set()  # dedupe across feeds by ticker+headline+url
        for feed in self.feeds:
            if not feed.due(now):
                continue
            entries = feed.fetch(self.session)
            feed.schedule(time.time())
            if entries:
                fresh = feed.new_entries(entries)
                feed.entries_seen += len(fresh)
                new_alerts = feed.to_alerts(fresh, self._first_run)
                for a in new_alerts:
                    # canonical key: ticker|normalized headline|url
                    ticker = (a.get('ticker') or '').strip().upper()
                    headline = (a.get('headline') or '').strip().lower()
                    url = a.get('url','') or ''
                    key = f"{ticker}|{headline}|{url}"
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    alerts.append(a)
        self._first_run = False
        return alerts

    def status(self) -> list[dict]:
        now = time.time()
        return [
            {
                "name": f.name,
                "status": f.last_status,
                "seconds_since_ok": round(now - f.last_ok, 1) if f.last_ok else None,
                "backoff": round(f.backoff, 1),
                "errors": f.error_count,
                "entries": f.entries_seen,
            }
            for f in self.feeds
        ]

    def check(self) -> None:
        """One-shot connectivity report. Run this after editing FEEDS."""
        print(f"{'FEED':<18} {'RESULT':<26} TICKERS FOUND IN LATEST BATCH")
        print("-" * 78)
        for feed in self.feeds:
            entries = feed.fetch(self.session)
            syms: list[str] = []
            for e in entries[:25]:
                if feed.kind == "edgar":
                    syms += tickers.from_edgar(
                        e["title"], e.get("link", ""), e.get("summary", "")
                    )
                else:
                    syms += tickers.extract(f"{e['title']} {e.get('summary','')}")
            uniq = sorted(set(syms))
            preview = ", ".join(uniq[:8]) or "—"
            print(f"{feed.name:<18} {feed.last_status:<26} {preview}")
