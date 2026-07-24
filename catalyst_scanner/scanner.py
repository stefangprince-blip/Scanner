from __future__ import annotations

"""Orchestration: background threads, filtering, and row assembly."""

import logging
import threading
import time

from . import config, quotes as quotes_mod, scoring
from .feeds import FeedManager
from .store import Store

log = logging.getLogger("scanner")


class Scanner:
    def __init__(self, quote_provider=None, feed_specs=None, store=None):
        self.store = store or Store()
        self.feeds = FeedManager(feed_specs)
        self.provider = quote_provider or quotes_mod.build()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self.stats = {
            "started": time.time(),
            "alerts_total": 0,
            "last_feed_poll": 0.0,
            "last_quote_poll": 0.0,
            "rejected_by_filter": 0,
        }
        self._fund_cache: dict[str, dict] = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        for target, name in ((self._feed_loop, "feeds"),
                             (self._quote_loop, "quotes"),
                             (self._prune_loop, "prune")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("scanner started: %d feeds, provider=%s",
                 len(self.feeds.feeds), type(self.provider).__name__)

    def stop(self) -> None:
        self._stop.set()

    # -- loops -------------------------------------------------------------
    def _feed_loop(self) -> None:
        while not self._stop.is_set():
            try:
                for alert in self.feeds.poll_due():
                    if self.store.add(alert):
                        self.stats["alerts_total"] += 1
                        log.info("ALERT %-6s %3d  %s",
                                 alert["ticker"], alert["score"],
                                 alert["headline"][:80])
                self.stats["last_feed_poll"] = time.time()
            except Exception:
                log.exception("feed loop error")
            self._stop.wait(1.0)

    def _quote_loop(self) -> None:
        while not self._stop.is_set():
            try:
                symbols = self.store.active_tickers()
                if symbols:
                    live = self.provider.quotes(symbols)
                    for sym in symbols:
                        merged = dict(live.get(sym, {}))
                        merged.update(self._fundamentals(sym))
                        self.store.set_quote(sym, merged)
                self.stats["last_quote_poll"] = time.time()
            except Exception:
                log.exception("quote loop error")
            self._stop.wait(config.QUOTE_REFRESH_SECONDS)

    def _prune_loop(self) -> None:
        while not self._stop.is_set():
            try:
                removed = self.store.prune()
                if removed:
                    log.info("pruned %d expired alerts", removed)
            except Exception:
                log.exception("prune loop error")
            self._stop.wait(60)

    def _fundamentals(self, ticker: str) -> dict:
        cached = self._fund_cache.get(ticker)
        if cached and time.time() - cached.get("_at", 0) < 3600:
            return cached
        data = self.provider.fundamentals(ticker) or {}
        data.setdefault("_at", time.time())
        self._fund_cache[ticker] = data
        return data

    # -- read side ---------------------------------------------------------
    @staticmethod
    def _passes_universe(quote: dict) -> tuple[bool, str]:
        """Micro/small-cap gate. Unknown fundamentals are configurable."""
        cap = quote.get("market_cap")
        price = quote.get("last")
        flt = quote.get("float_shares")

        if cap is None and price is None:
            return (config.KEEP_UNKNOWN_FUNDAMENTALS, "no data")
        if cap is not None:
            if config.MAX_MARKET_CAP and cap > config.MAX_MARKET_CAP:
                return False, "cap too large"
            if config.MIN_MARKET_CAP and cap < config.MIN_MARKET_CAP:
                return False, "cap too small"
        if price is not None:
            if config.MAX_PRICE and price > config.MAX_PRICE:
                return False, "price too high"
            if config.MIN_PRICE and price < config.MIN_PRICE:
                return False, "price too low"
        if flt and config.MAX_FLOAT and flt > config.MAX_FLOAT:
            return False, "float too large"
        return True, ""

    def rows(self, include_filtered: bool = False) -> list[dict]:
        """Assemble the board: alerts joined to quotes, ranked by heat."""
        rows: list[dict] = []
        rejected = 0
        for alert in self.store.active():
            q = self.store.get_quote(alert["ticker"])
            passes, reason = self._passes_universe(q)
            if not passes and not include_filtered:
                rejected += 1
                continue

            avg_vol = q.get("avg_volume")
            vol = q.get("volume")
            rvol = (vol / avg_vol) if (vol and avg_vol) else None

            row = {
                **alert,
                "name": q.get("name"),
                "exchange": q.get("exchange"),
                "last": q.get("last"),
                "change_pct": q.get("change_pct"),
                "volume": vol,
                "avg_volume": avg_vol,
                "rvol": round(rvol, 2) if rvol else None,
                "market_cap": q.get("market_cap"),
                "float_shares": q.get("float_shares"),
                "quote_age": (time.time() - q["updated"]) if q.get("updated") else None,
                "filtered_reason": reason if not passes else "",
            }
            row["heat"] = round(scoring.heat(row), 1)
            rows.append(row)

        self.stats["rejected_by_filter"] = rejected
        rows.sort(key=lambda r: r["heat"], reverse=True)
        return rows

    def health(self) -> dict:
        return {
            "uptime_seconds": round(time.time() - self.stats["started"], 1),
            "alerts_total": self.stats["alerts_total"],
            "alerts_active": len(self.store.active()),
            "tickers_active": len(self.store.active_tickers()),
            "rejected_by_filter": self.stats["rejected_by_filter"],
            "provider": type(self.provider).__name__,
            "ttl_hours": config.ALERT_TTL_SECONDS / 3600,
            "feeds": self.feeds.status(),
        }

    # -- demo --------------------------------------------------------------
    def seed_demo(self) -> None:
        """Load sample headlines so the board is populated without waiting."""
        samples = [
            ("Cellect Biotech Announces FDA Approval of ARX-4 for Relapsed AML",
             "Cellect Biotechnology (NASDAQ: CLBT) today announced FDA approval.",
             "GlobeNewswire"),
            ("Nuvectra Signs $210 Million Contract Award with U.S. Department of Defense",
             "Nuvectra Corp (NYSE American: NVTR) received the award.",
             "BusinessWire"),
            ("Applied UV Sciences to be Acquired by Halma plc in All-Cash Transaction",
             "Applied UV (NASDAQ: AUVI) entered a definitive merger agreement.",
             "PR Newswire"),
            ("Greenland Acquisition Reports Record Revenue, Raises Full-Year Guidance",
             "Greenland (NASDAQ: GLAC) reported record quarterly revenue.",
             "ACCESSWIRE"),
            ("Siebert Financial Announces Pricing of $12.0 Million Public Offering",
             "Siebert (NASDAQ: SIEB) priced an underwritten public offering.",
             "GlobeNewswire"),
            ("Protara Therapeutics Announces Positive Topline Phase 2 Results",
             "Protara (NASDAQ: TARA) met the primary endpoint.",
             "GlobeNewswire"),
        ]
        for title, body, source in samples:
            from . import tickers as tk
            syms = tk.extract(f"{title} {body}")
            result = scoring.score(title, body, 1.05)
            for sym in syms:
                self.store.add({
                    "ticker": sym, "headline": title, "url": "#",
                    "source": source, "published": time.time(), "body": body,
                    **{k: result[k] for k in
                       ("score", "tags", "dilution", "distress")},
                })
