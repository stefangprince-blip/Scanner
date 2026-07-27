from __future__ import annotations

"""Orchestration: background threads, filtering, and row assembly."""

import datetime
import logging
import math
import re
import threading
import time
import queue
import csv
import zoneinfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO

import requests

_ET = zoneinfo.ZoneInfo("America/New_York")

# Trading window: Mon–Fri 4:00 AM – 8:00 PM ET
_PREMARKET_START_H = 4    # 4:00 AM ET
_REGULAR_OPEN_H = 9
_REGULAR_OPEN_M = 30
_REGULAR_CLOSE_H = 16     # 4:00 PM ET
_AFTERHOURS_END_H = 20    # 8:00 PM ET

# Session window lengths in minutes (used by the pace-of-day RVOL formula)
_PREMARKET_MINUTES = 330.0    # 4:00 AM → 9:30 AM
_REGULAR_MINUTES = 390.0      # 9:30 AM → 4:00 PM
_AFTERHOURS_MINUTES = 240.0   # 4:00 PM → 8:00 PM


def _session_info() -> dict:
    """Return the current trading session type and elapsed minutes within it.

    Returns a dict with:
      type: "premarket" | "regular" | "after_hours" | "closed"
      elapsed_minutes: float (>= 1.0 when not closed)
      window_minutes: total minutes in this session window
    """
    now = datetime.datetime.now(tz=_ET)
    weekday = now.weekday()  # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    if weekday >= 5:
        return {"type": "closed", "elapsed_minutes": 0.0, "window_minutes": 0.0}

    pm_start = now.replace(hour=_PREMARKET_START_H, minute=0, second=0, microsecond=0)
    reg_open = now.replace(hour=_REGULAR_OPEN_H, minute=_REGULAR_OPEN_M, second=0, microsecond=0)
    reg_close = now.replace(hour=_REGULAR_CLOSE_H, minute=0, second=0, microsecond=0)
    ah_end = now.replace(hour=_AFTERHOURS_END_H, minute=0, second=0, microsecond=0)

    if now < pm_start or now >= ah_end:
        return {"type": "closed", "elapsed_minutes": 0.0, "window_minutes": 0.0}
    if now < reg_open:
        elapsed = max(1.0, (now - pm_start).total_seconds() / 60.0)
        return {"type": "premarket", "elapsed_minutes": elapsed, "window_minutes": _PREMARKET_MINUTES}
    if now < reg_close:
        elapsed = max(1.0, (now - reg_open).total_seconds() / 60.0)
        return {"type": "regular", "elapsed_minutes": elapsed, "window_minutes": _REGULAR_MINUTES}
    elapsed = max(1.0, (now - reg_close).total_seconds() / 60.0)
    return {"type": "after_hours", "elapsed_minutes": elapsed, "window_minutes": _AFTERHOURS_MINUTES}


def _session_label(session_type: str) -> str:
    return {
        "premarket": "Pre-Market",
        "regular": "Regular Session",
        "after_hours": "Extended Hours",
        "closed": "Closed",
    }.get(session_type, "Closed")


def _is_trading_window() -> bool:
    """True Mon–Fri 4:00 AM – 8:00 PM ET (covers pre-market, regular, and after-hours)."""
    return _session_info()["type"] != "closed"


def _elapsed_session_minutes() -> float:
    """Return elapsed minutes in the current session window (clamped ≥ 1.0).

    During regular hours this matches the old behaviour.  During extended hours
    it reflects elapsed time since the start of that sub-session.
    """
    info = _session_info()
    return max(1.0, info["elapsed_minutes"])


def _rvol(volume, avg_daily_volume, elapsed_minutes: float | None = None,
          session_type: str | None = None) -> float | None:
    """Pace-of-day RVOL: projects the current per-minute rate to the session window
    and compares it to the historical daily average.

    Formula: (volume × window_minutes) / (elapsed_minutes × avg_daily_volume)

    The window adapts per session:
      • premarket   → 330 min  (4:00 AM – 9:30 AM)
      • regular     → 390 min  (9:30 AM – 4:00 PM)
      • after_hours → 240 min  (4:00 PM – 8:00 PM)
    """
    try:
        vol = float(volume)
        avg = float(avg_daily_volume)
        if vol <= 0 or avg <= 0:
            return None
    except (TypeError, ValueError):
        return None
    info = _session_info()
    st = session_type or info["type"]
    window = {
        "premarket": _PREMARKET_MINUTES,
        "regular": _REGULAR_MINUTES,
        "after_hours": _AFTERHOURS_MINUTES,
    }.get(st, _REGULAR_MINUTES)
    if elapsed_minutes is not None:
        elapsed = max(1.0, float(elapsed_minutes))
    else:
        elapsed = max(1.0, info["elapsed_minutes"]) if info["elapsed_minutes"] else 1.0
    return (vol * window) / (elapsed * avg)


def _coerce_epoch_seconds(raw) -> float | None:
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    # Some providers emit ms epochs.
    if ts > 10_000_000_000:
        ts /= 1000.0
    return ts


def _session_context_from_quote(quote: dict) -> tuple[str, float] | None:
    ts = _coerce_epoch_seconds((quote or {}).get("last_trade_ts"))
    if ts is None:
        return None
    dt = datetime.datetime.fromtimestamp(ts, tz=_ET)
    state = str((quote or {}).get("market_state") or "").upper()

    if state in {"PRE", "PREPRE"}:
        session_type = "premarket"
        window_minutes = _PREMARKET_MINUTES
        start = dt.replace(hour=_PREMARKET_START_H, minute=0, second=0, microsecond=0)
    elif state in {"POST", "POSTPOST"}:
        session_type = "after_hours"
        window_minutes = _AFTERHOURS_MINUTES
        start = dt.replace(hour=_REGULAR_CLOSE_H, minute=0, second=0, microsecond=0)
    elif state in {"REGULAR", "OPEN", "CLOSED"}:
        session_type = "regular"
        window_minutes = _REGULAR_MINUTES
        start = dt.replace(hour=_REGULAR_OPEN_H, minute=_REGULAR_OPEN_M, second=0, microsecond=0)
    else:
        minute_of_day = (dt.hour * 60) + dt.minute
        pre_start = _PREMARKET_START_H * 60
        reg_start = (_REGULAR_OPEN_H * 60) + _REGULAR_OPEN_M
        reg_end = _REGULAR_CLOSE_H * 60
        ah_end = _AFTERHOURS_END_H * 60
        if pre_start <= minute_of_day < reg_start:
            session_type = "premarket"
            window_minutes = _PREMARKET_MINUTES
            start = dt.replace(hour=_PREMARKET_START_H, minute=0, second=0, microsecond=0)
        elif reg_start <= minute_of_day < reg_end:
            session_type = "regular"
            window_minutes = _REGULAR_MINUTES
            start = dt.replace(hour=_REGULAR_OPEN_H, minute=_REGULAR_OPEN_M, second=0, microsecond=0)
        elif reg_end <= minute_of_day < ah_end:
            session_type = "after_hours"
            window_minutes = _AFTERHOURS_MINUTES
            start = dt.replace(hour=_REGULAR_CLOSE_H, minute=0, second=0, microsecond=0)
        else:
            # Outside session hours, use a conservative regular-session baseline.
            session_type = "regular"
            window_minutes = _REGULAR_MINUTES
            start = dt.replace(hour=_REGULAR_OPEN_H, minute=_REGULAR_OPEN_M, second=0, microsecond=0)

    elapsed = max(1.0, (dt - start).total_seconds() / 60.0)
    elapsed = min(window_minutes, elapsed)
    return session_type, elapsed


def _rvol_for_quote(quote: dict) -> float | None:
    info = _session_info()
    session_type = info["type"]
    elapsed_minutes = None
    if session_type == "closed":
        state = str((quote or {}).get("market_state") or "").upper()
        if state == "CLOSED":
            # A closed-market force scan represents the latest completed regular
            # session. Use full-day volume / prior-session average, not the
            # scanner's weekend/evening receipt clock.
            session_type, elapsed_minutes = "regular", _REGULAR_MINUTES
        else:
            context = _session_context_from_quote(quote or {})
            if context is not None:
                session_type, elapsed_minutes = context
    return _rvol(
        (quote or {}).get("volume"),
        (quote or {}).get("avg_volume"),
        elapsed_minutes=elapsed_minutes,
        session_type=session_type,
    )


def _execution_metrics(quote: dict) -> dict:
    """Validated spread and session-high distance from one accepted snapshot."""
    def number(name):
        try:
            value = float((quote or {}).get(name))
            return value if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    bid, ask, last = number("bid"), number("ask"), number("last")
    session_high = number("session_high")
    spread = spread_pct = None
    spread_status = "insufficient_data"
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        if ask >= bid:
            spread = ask - bid
            midpoint = (ask + bid) / 2.0
            spread_pct = (spread / midpoint) * 100.0 if midpoint > 0 else None
            spread_status = "valid"
        else:
            spread_status = "invalid_crossed_quote"
    distance = None
    distance_status = "insufficient_data"
    if session_high is not None and session_high > 0 and last is not None and last > 0:
        distance = max(0.0, ((session_high - last) / session_high) * 100.0)
        distance_status = "valid"
    return {
        "spread": spread,
        "spread_pct": spread_pct,
        "spread_status": spread_status,
        "distance_from_high": distance,
        "distance_from_high_status": distance_status,
    }


def _dynamic_change_metrics(quote: dict) -> dict:
    """Calculate the displayed move from the correct session baseline.

    Premarket and regular trading compare with the prior trading day's final
    extended-hours price when supplied, otherwise its official regular close.
    After-hours compares only with the current day's regular-session close.
    """
    def number(*names):
        for name in names:
            try:
                value = float((quote or {}).get(name))
                if math.isfinite(value) and value > 0:
                    return value
            except (TypeError, ValueError):
                continue
        return None

    state = str((quote or {}).get("market_state") or "").upper()
    last = number("last")
    baseline = None
    session = "regular"
    baseline_type = "previous_regular_close"
    if state in {"PRE", "PREPRE"}:
        session = "premarket"
        baseline = number("previous_extended_close", "prev_close")
        baseline_type = (
            "previous_extended_close"
            if number("previous_extended_close") is not None
            else "previous_regular_close"
        )
    elif state in {"POST", "POSTPOST"}:
        session = "after_hours"
        baseline = number("regular_session_close", "regular_close", "prev_close")
        baseline_type = "regular_session_close"
    elif state in {"REGULAR", "OPEN"}:
        session = "regular"
        baseline = number("previous_extended_close", "prev_close")
        baseline_type = (
            "previous_extended_close"
            if number("previous_extended_close") is not None
            else "previous_regular_close"
        )
    else:
        # Closed snapshots retain the provider's most recent completed-session
        # move, but recompute from raw prices whenever both are available.
        session = "closed"
        baseline = number("regular_session_close", "prev_close")
        baseline_type = "previous_regular_close"

    change = ((last - baseline) / baseline) * 100.0 if last and baseline else None
    if change is None:
        try:
            provider_change = float((quote or {}).get("change_pct"))
            change = provider_change if math.isfinite(provider_change) else None
        except (TypeError, ValueError):
            change = None
    return {
        "change_pct": change,
        "change_session": session,
        "change_baseline": baseline,
        "change_baseline_type": baseline_type,
        "change_status": "valid" if change is not None else "insufficient_data",
    }

from . import config, news_scoring, quotes as quotes_mod, rssparse, scoring
from .feeds import FeedManager
from .news_provider import (
    CompositeSymbolNewsProvider,
    SecurityIdentity,
    YahooRssSymbolNewsProvider,
)
from .store import Store

log = logging.getLogger("scanner")


class Scanner:
    def __init__(self, quote_provider=None, feed_specs=None, store=None):
        self.store = store or Store()
        self.feeds = FeedManager(feed_specs)
        self.provider = quote_provider or quotes_mod.build()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._quote_cursor = 0
        self._filtered_quote_cursor = 0
        self.stats = {
            "started": time.time(),
            "alerts_total": 0,
            "last_feed_poll": 0.0,
            "last_quote_poll": 0.0,
            "rejected_by_filter": 0,
            "us_market_universe_size": 0,
            "market_symbols_scanned_last": 0,
            "market_batches_last": 0,
            "market_coverage_completed": 0,
            "market_coverage_symbols": 0,
            "market_coverage_percent": 0.0,
            "filtered_scan_duration_seconds": 0.0,
            "filtered_scan_interval_actual_seconds": None,
            "filtered_scan_symbols_last": 0,
            "market_updates_accepted": 0,
            "market_updates_dropped_stale": 0,
            "market_updates_dropped_future": 0,
            "market_updates_duplicate": 0,
            "filter_symbols_evaluated_last": 0,
            "filter_symbols_missing_data_last": 0,
            "score_calculations_last": 0,
            "rows_build_duration_seconds": 0.0,
        }
        self._fund_cache: dict[str, dict] = {}
        self._http = requests.Session()
        self.news_provider = CompositeSymbolNewsProvider(
            self.store, YahooRssSymbolNewsProvider(self._http)
        )
        self._independent_symbols: list[str] = []
        self._independent_symbols_at = 0.0
        self._rvol_news_cache: dict[str, dict] = {}
        self._last_seen_volume: dict[str, float] = {}
        self._us_market_symbols: list[str] = []
        self._us_market_symbols_at = 0.0
        self._us_market_cursor = 0
        self._us_market_exchange: dict[str, str] = {}
        self._us_market_symbol_source_counts: dict[str, int] = {}
        persisted_quotes = self.store.quotes_snapshot()
        if len(persisted_quotes) >= 1000:
            self._us_market_symbols = sorted(persisted_quotes)
            self._us_market_symbols_at = time.time()
            self._us_market_exchange = {
                symbol: str(quote.get("exchange") or "")
                for symbol, quote in persisted_quotes.items()
                if quote.get("exchange")
            }
            self._us_market_symbol_source_counts = {
                "persisted_quote_universe": len(self._us_market_symbols)
            }
        self._market_coverage_seen: set[str] = set()
        self._market_resolved_symbols: set[str] = set()
        self._market_pending_retry: set[str] = set()
        self._market_retry_backoff_until = 0.0
        self._market_coverage_started_at = 0.0
        self._filtered_news_candidates: dict[str, dict] = {}
        self._filtered_news_last_check: dict[str, float] = {}
        self._filtered_scan_seen_at: dict[str, float] = {}
        self._quote_momentum: dict[str, dict] = {}
        self._extended_quote_cache: dict[str, tuple[float, dict]] = {}
        self._accepted_quote_generation: dict[str, dict] = {}
        self._market_generation = 0
        self._pattern_analysis: dict[str, dict] = {}
        self._pattern_lock = threading.Lock()
        self._active_news_last_check: dict[str, float] = {}
        self._quick_news_cache: dict[str, dict] = {}
        self._quick_news_inflight: set[str] = set()
        self._quick_news_lock = threading.Lock()
        self._quick_news_queue: queue.Queue[str] = queue.Queue(
            maxsize=int(getattr(config, "QUICK_NEWS_MAX_QUEUE", 300) or 300)
        )
        self._active_filtered_symbols: set[str] = set()
        self._active_filtered_lock = threading.Lock()
        self._scanner_memberships: dict[str, dict] = {}
        self._last_market_scan_at = 0.0
        self._last_filtered_scan_at = 0.0
        # Tickers currently visible in the scanner (set by rows()). The filtered
        # scan only fetches chart API data for these symbols so the 3s refresh
        # is fast even when hundreds of historical alerts are in the store.
        self._visible_tickers: list[str] = []
        self._visible_tickers_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._force_scan_lock = threading.Lock()
        self._force_scan_active = False
        self._last_closed_market_scan_at = 0.0

    def security_identity(self, ticker: str) -> SecurityIdentity:
        sym = str(ticker or "").upper().strip()
        quote = self.store.get_quote(sym)
        provider_ids: list[tuple[str, str]] = []
        for key in (
            "webull_ticker_id",
            "webullTickerId",
            "tickerId",
            "instrument_id",
            "instrumentId",
            "security_id",
            "securityId",
        ):
            value = str(quote.get(key) or "").strip()
            if value:
                provider_ids.append(("webull", value))
                break
        return SecurityIdentity(
            symbol=sym,
            exchange=str(quote.get("exchange") or self._us_market_exchange.get(sym) or ""),
            company_name=str(quote.get("name") or ""),
            previous_symbols=tuple(quote.get("previous_symbols") or ()),
            cik=str(quote.get("cik") or ""),
            provider_ids=tuple(provider_ids),
        )

    @staticmethod
    def _webull_identity_fields(symbol: str, quote: dict) -> dict:
        q = quote or {}
        security_id = (
            str(q.get("security_id") or q.get("securityId") or "").strip()
            or f"{symbol}|{str(q.get('exchange') or '').upper().strip()}"
        )
        return {
            "security_id": security_id,
            "security_type": str(q.get("security_type") or q.get("securityType") or "").strip(),
            "webull_ticker_id": str(
                q.get("webull_ticker_id")
                or q.get("webullTickerId")
                or q.get("tickerId")
                or q.get("instrument_id")
                or q.get("instrumentId")
                or ""
            ).strip(),
            "webull_exchange_code": str(
                q.get("webull_exchange_code")
                or q.get("webullExchangeCode")
                or ""
            ).strip(),
            "webull_instrument_url": str(
                q.get("webull_instrument_url")
                or q.get("webullInstrumentUrl")
                or ""
            ).strip(),
        }

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        for target, name in (
            (self._feed_loop, "feeds"),
            (self._quote_loop, "quotes"),
            (self._prune_loop, "prune"),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        if getattr(config, "QUICK_NEWS_ENABLED", True):
            for worker_no in range(max(1, int(getattr(config, "QUICK_NEWS_WORKERS", 2) or 2))):
                t = threading.Thread(
                    target=self._quick_news_loop,
                    name=f"quick-news-{worker_no + 1}",
                    daemon=True,
                )
                t.start()
                self._threads.append(t)
        if self._us_market_symbols:
            t = threading.Thread(
                target=self._refresh_market_universe_once,
                name="market-universe-refresh",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        log.info(
            "scanner started: %d feeds, provider=%s",
            len(self.feeds.feeds),
            type(self.provider).__name__,
        )
        if getattr(config, "CLOSED_MARKET_BOOTSTRAP_ENABLED", True):
            t = threading.Thread(
                target=self._bootstrap_closed_market_snapshot,
                name="closed-market-bootstrap",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def _refresh_market_universe_once(self) -> None:
        """Refresh listing directories without blocking persisted-universe scans."""
        try:
            loaded = self._load_us_market_symbols()
            if loaded:
                self._us_market_symbols = loaded
                self._us_market_symbols_at = time.time()
                if self._us_market_cursor >= len(loaded):
                    self._us_market_cursor = 0
                log.info("refreshed U.S. listed universe: %d symbols", len(loaded))
        except Exception:
            log.exception("background market-universe refresh failed")

    # -- loops -------------------------------------------------------------
    def _feed_loop(self) -> None:
        while not self._stop.is_set():
            try:
                for alert in self.feeds.poll_due():
                    if self.store.add(alert):
                        self.stats["alerts_total"] += 1
                        log.info(
                            "ALERT %-6s %3d  %s",
                            alert["ticker"],
                            alert["score"],
                            alert["headline"][:80],
                        )
                self.stats["last_feed_poll"] = time.time()
            except Exception:
                log.exception("feed loop error")
            self._stop.wait(1.0)

    def _quote_batch(self, symbols: list[str], batch_size_override: int | None = None) -> list[str]:
        if not symbols:
            return []
        batch_size = (
            int(batch_size_override)
            if batch_size_override is not None
            else int(config.QUOTE_BATCH_SIZE or 0)
        )
        if batch_size <= 0 or len(symbols) <= batch_size:
            return list(symbols)
        start = self._quote_cursor % len(symbols)
        batch = []
        for offset in range(batch_size):
            batch.append(symbols[(start + offset) % len(symbols)])
        self._quote_cursor = (start + batch_size) % len(symbols)
        return batch

    def _batched_quotes(
        self,
        symbols: list[str],
        chunk_size: int,
        workers: int = 1,
        minimal: bool = False,
        quote_fn=None,
    ) -> dict[str, dict]:
        if not symbols:
            return {}
        if quote_fn is None:
            quote_fn = self.provider.quotes_minimal if minimal else self.provider.quotes
        if chunk_size <= 0 or len(symbols) <= chunk_size:
            return quote_fn(symbols)
        if workers <= 1:
            out: dict[str, dict] = {}
            for i in range(0, len(symbols), chunk_size):
                chunk = symbols[i : i + chunk_size]
                try:
                    out.update(quote_fn(chunk))
                except Exception:
                    for sym in chunk:
                        out.setdefault(sym, {})
            return out
        chunks = [symbols[i : i + chunk_size] for i in range(0, len(symbols), chunk_size)]
        out: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(quote_fn, chunk): chunk for chunk in chunks}
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    out.update(future.result() or {})
                except Exception:
                    for sym in chunk:
                        out.setdefault(sym, {})
        return out

    def _chart_quote_snapshot(self, ticker: str) -> dict:
        key = str(ticker or "").upper().strip()
        if not key:
            return {}
        cached = self._extended_quote_cache.get(key)
        if cached and time.time() - cached[0] < 15:
            return dict(cached[1])
        try:
            resp = self._http.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{key}",
                params={"interval": "5m", "range": "5d", "includePrePost": "true"},
                headers={"User-Agent": config.USER_AGENT},
                timeout=8,
            )
            if resp.status_code != 200 or not resp.content:
                return {}
            payload = resp.json()
            result = ((payload.get("chart") or {}).get("result") or [])
            if not result:
                return {}
            r0 = result[0]
            meta = r0.get("meta") or {}
            q0 = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
            timestamps = r0.get("timestamp") or []

            def _num(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            market_state = str(meta.get("marketState") or "REGULAR").upper()
            reg_price = _num(meta.get("regularMarketPrice"))
            prev = _num(meta.get("chartPreviousClose") or meta.get("previousClose"))
            pre_price = _num(meta.get("preMarketPrice"))
            post_price = _num(meta.get("postMarketPrice"))
            if market_state in {"PRE", "PREPRE"}:
                last = pre_price if pre_price is not None else reg_price
            elif market_state in {"POST", "POSTPOST"}:
                last = post_price if post_price is not None else reg_price
            else:
                last = reg_price
            if last is None:
                # Off-hours snapshots sometimes omit regularMarketPrice but still
                # provide previous close; use it as the latest known value.
                last = prev
            chg = None
            if last is not None and prev not in (None, 0):
                chg = ((last - prev) / prev) * 100.0

            volume = None
            for value in reversed(q0.get("volume") or []):
                parsed = _num(value)
                if parsed is not None:
                    volume = int(parsed)
                    break
            if volume is None:
                reg_vol = _num(meta.get("regularMarketVolume"))
                if reg_vol is not None:
                    volume = int(reg_vol)

            last_trade_ts = _coerce_epoch_seconds(
                meta.get("regularMarketTime")
                or meta.get("postMarketTime")
                or meta.get("preMarketTime")
            )
            previous_extended_close = None
            closes = q0.get("close") or []
            valid_bars = [
                (float(ts), float(close))
                for ts, close in zip(timestamps, closes)
                if ts is not None and close is not None
            ]
            if valid_bars:
                latest_date = datetime.datetime.fromtimestamp(
                    valid_bars[-1][0], tz=_ET
                ).date()
                previous_bars = [
                    (ts, close) for ts, close in valid_bars
                    if datetime.datetime.fromtimestamp(ts, tz=_ET).date() < latest_date
                ]
                if previous_bars:
                    previous_extended_close = previous_bars[-1][1]
            result = {
                "last": last,
                "prev_close": prev,
                "previous_extended_close": previous_extended_close,
                "regular_session_close": (
                    reg_price if market_state in {"POST", "POSTPOST"} else None
                ),
                "change_pct": round(chg, 2) if chg is not None else None,
                "volume": volume,
                "exchange": meta.get("exchangeName") or meta.get("fullExchangeName"),
                "market_state": market_state,
                "last_trade_ts": last_trade_ts,
            }
            self._extended_quote_cache[key] = (time.time(), result)
            if len(self._extended_quote_cache) > 500:
                oldest = min(self._extended_quote_cache, key=lambda symbol: self._extended_quote_cache[symbol][0])
                self._extended_quote_cache.pop(oldest, None)
            return result
        except Exception:
            return {}

    @staticmethod
    def _parse_numeric(value) -> float | None:
        if value in (None, "", "-", "N/A", "n/a"):
            return None
        raw = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
        try:
            return float(raw)
        except ValueError:
            return None

    def _bulk_nasdaq_snapshot(self) -> dict[str, dict]:
        """Fetch a full U.S. stock snapshot in one request.

        Nasdaq's screener endpoint returns cross-exchange symbols with last sale,
        percent/net change, and volume, which is far more reliable than firing
        thousands of per-symbol quote requests during force scans.
        """
        try:
            resp = self._http.get(
                "https://api.nasdaq.com/api/screener/stocks",
                params={"tableonly": "true", "download": "true"},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
                },
                timeout=max(
                    10,
                    int(getattr(config, "US_MARKET_BULK_TIMEOUT_SECONDS", 30) or 30),
                ),
            )
            if resp.status_code != 200 or not resp.content:
                return {}
            payload = resp.json()
            rows = (((payload or {}).get("data") or {}).get("rows")) or []
            out: dict[str, dict] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                sym = self._canonical_symbol(str(row.get("symbol") or ""))
                if not sym or not self._is_supported_market_symbol(sym):
                    continue
                last = self._parse_numeric(row.get("lastsale"))
                if last is None:
                    continue
                net_change = self._parse_numeric(row.get("netchange"))
                pct_change = self._parse_numeric(row.get("pctchange"))
                prev = None
                if net_change is not None:
                    prev = last - net_change
                market_cap = self._parse_numeric(row.get("marketCap"))
                vol = self._parse_numeric(row.get("volume"))
                out[sym] = {
                    "name": row.get("name"),
                    "last": last,
                    "prev_close": prev,
                    "change_pct": pct_change,
                    "volume": int(vol) if vol is not None else None,
                    "market_cap": market_cap,
                    "market_state": (
                        "REGULAR" if _session_info()["type"] == "regular" else "CLOSED"
                    ),
                }
            return out
        except Exception:
            return {}

    def _last_session_history(self, ticker: str) -> dict:
        """Fetch the latest completed-session timestamp and 20-day volume baseline.

        This refinement is used only for broad-filter candidates during an explicit
        full pass. It avoids treating the force-scan receipt time as a trade time.
        """
        try:
            resp = self._http.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"interval": "1d", "range": "1mo", "includePrePost": "false"},
                headers={"User-Agent": config.USER_AGENT},
                timeout=8,
            )
            if resp.status_code != 200 or not resp.content:
                return {}
            result = ((resp.json().get("chart") or {}).get("result") or [])
            if not result:
                return {}
            r0 = result[0]
            meta = r0.get("meta") or {}
            volumes = ((r0.get("indicators") or {}).get("quote") or [{}])[0].get("volume") or []
            valid = [float(value) for value in volumes if value is not None and float(value) > 0]
            # The last bar is the scanned session. RVOL compares it with prior
            # completed sessions, never with itself.
            history = valid[-21:-1] if len(valid) >= 2 else []
            avg_volume = sum(history) / len(history) if len(history) >= 5 else None
            return {
                "avg_volume": avg_volume,
                "avg_volume_sample_count": len(history),
                "avg_volume_as_of": meta.get("regularMarketTime"),
                "last_trade_ts": meta.get("regularMarketTime"),
                "session_high": meta.get("regularMarketDayHigh"),
                "market_state": meta.get("marketState") or (
                    "CLOSED" if not _is_trading_window() else "REGULAR"
                ),
            }
        except Exception:
            return {}

    def _enrich_full_pass_candidates(self, market_live: dict[str, dict]) -> None:
        """Add RVOL history only to broad-filter candidates missing a baseline."""
        candidates = []
        min_change = float(getattr(config, "US_MARKET_CATALYST_MIN_CHANGE_PCT", 5.0))
        min_volume = float(getattr(config, "US_MARKET_SCAN_MIN_VOLUME", 350_000))
        for symbol, quote in market_live.items():
            if quote.get("avg_volume") is None:
                cached_avg = self.store.get_quote(symbol).get("avg_volume")
                if cached_avg is not None:
                    quote["avg_volume"] = cached_avg
            try:
                qualifies = (
                    float(quote.get("change_pct")) > min_change
                    and float(quote.get("volume")) >= min_volume
                    and quote.get("avg_volume") is None
                )
            except (TypeError, ValueError):
                qualifies = False
            if qualifies:
                candidates.append(symbol)
        if not candidates:
            self.stats["full_pass_rvol_candidates"] = 0
            return
        workers = min(12, max(1, int(getattr(config, "US_MARKET_SCAN_WORKERS", 5))))
        enriched = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._last_session_history, symbol): symbol
                for symbol in candidates
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    history = future.result() or {}
                except Exception:
                    history = {}
                if history.get("avg_volume"):
                    market_live[symbol].update(history)
                    enriched += 1
        self.stats["full_pass_rvol_candidates"] = len(candidates)
        self.stats["full_pass_rvol_enriched"] = enriched

    def _enrich_session_change_candidates(self, market_live: dict[str, dict]) -> None:
        """Refine broad movers with current session prices and extended baseline."""
        if _session_info()["type"] == "closed":
            self.stats["session_change_candidates"] = 0
            return
        threshold = max(
            0.0,
            float(getattr(config, "US_MARKET_CATALYST_MIN_CHANGE_PCT", 5.0)) - 2.0,
        )
        candidates = []
        for symbol, quote in market_live.items():
            try:
                if (
                    float(quote.get("change_pct")) >= threshold
                    and float(quote.get("volume") or 0) >= 100_000
                ):
                    candidates.append(symbol)
            except (TypeError, ValueError):
                continue
        # Keep previously filtered symbols current even if their broad snapshot
        # has moved close to the threshold.
        with self._active_filtered_lock:
            candidates.extend(self._active_filtered_symbols)
        candidates = list(dict.fromkeys(candidates))
        refined = 0
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(self._chart_quote_snapshot, symbol): symbol
                for symbol in candidates
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    update = future.result() or {}
                except Exception:
                    update = {}
                if update.get("last") is not None:
                    for key, value in update.items():
                        if key in {"volume", "avg_volume"}:
                            continue
                        if value is not None:
                            market_live.setdefault(symbol, {})[key] = value
                    refined += 1
        self.stats["session_change_candidates"] = len(candidates)
        self.stats["session_change_refined"] = refined

    def _scan_market_symbols(self, symbols: list[str], full_data: bool = False) -> None:
        if not symbols:
            return
        chunk_size = int(getattr(config, "US_MARKET_SCAN_CHUNK_SIZE", 250) or 250)
        workers = int(getattr(config, "US_MARKET_SCAN_WORKERS", 3) or 3)
        if full_data:
            market_live = self._bulk_nasdaq_snapshot()
            self._enrich_full_pass_candidates(market_live)
            self._enrich_session_change_candidates(market_live)
            # The bulk screener is the most efficient first pass, but it can
            # omit special listings. Fetch only those gaps through batched
            # minimal quotes—never candles or per-symbol chart history.
            missing_from_bulk = [
                sym for sym in symbols if not (market_live.get(sym) or {}).get("last")
            ]
            fallback_limit = max(
                0,
                int(
                    getattr(config, "US_MARKET_FULL_PASS_GAP_FALLBACK_LIMIT", 500)
                    or 0
                ),
            )
            fallback_symbols = missing_from_bulk[:fallback_limit]
            if fallback_symbols:
                market_live.update(
                    self._batched_quotes(
                        fallback_symbols,
                        chunk_size,
                        workers=max(1, workers),
                        minimal=True,
                    )
                )
            fallback_resolved = sum(
                1 for symbol in fallback_symbols
                if (market_live.get(symbol) or {}).get("last") is not None
            )
            if fallback_symbols and fallback_resolved == 0:
                self._market_retry_backoff_until = time.time() + max(
                    30,
                    int(getattr(config, "US_MARKET_RETRY_BACKOFF_SECONDS", 300) or 300),
                )
            else:
                self._market_retry_backoff_until = 0.0
            resolved: set[str] = set()
            for sym in symbols:
                merged = self._merge_quote_with_fundamentals(
                    sym,
                    market_live.get(sym, {}),
                    include_fundamentals=False,
                )
                self.store.set_quote(sym, merged)
                self._maybe_add_us_market_catalyst(sym, merged)
                self._maybe_add_volume_catalyst(sym, merged)
                if merged.get("last") is not None:
                    resolved.add(sym)
            self._market_resolved_symbols = resolved
            self._market_pending_retry = set(symbols) - resolved
            self.stats["market_bulk_gaps_last"] = len(missing_from_bulk)
            self.stats["market_bulk_gap_fallback_last"] = len(fallback_symbols)
            self.stats["market_bulk_gap_fallback_resolved_last"] = fallback_resolved
            self.stats["market_quotes_resolved_last"] = len(resolved)
            self.stats["market_quotes_missing_last"] = len(symbols) - len(resolved)
            self.stats["market_quote_availability_percent"] = round(
                len(resolved) * 100.0 / max(1, len(symbols)), 2
            )
            return
        market_live = self._batched_quotes(
            symbols,
            chunk_size,
            workers=max(1, workers),
            minimal=True,
        )
        resolved: set[str] = set()
        for sym in symbols:
            merged = self._merge_quote_with_fundamentals(
                sym,
                market_live.get(sym, {}),
                include_fundamentals=False,
            )
            self.store.set_quote(sym, merged)
            self._maybe_add_us_market_catalyst(sym, merged)
            self._maybe_add_volume_catalyst(sym, merged)
            if merged.get("last") is not None:
                resolved.add(sym)
        self._market_resolved_symbols.update(resolved)
        self._market_pending_retry.difference_update(resolved)
        self._market_pending_retry.update(set(symbols) - resolved)
        self.stats["market_quotes_resolved_last"] = len(resolved)
        self.stats["market_quotes_missing_last"] = len(symbols) - len(resolved)
        self.stats["market_quote_availability_percent"] = round(
            len(self._market_resolved_symbols) * 100.0
            / max(1, len(self._us_market_symbols)),
            2,
        )

    def _scan_independent_symbols(self) -> int:
        independent = self._independent_symbols_for_scan()
        if not independent:
            return 0
        independent_live = self._batched_quotes(independent, 200, minimal=True)
        for sym in independent:
            merged = self._merge_quote_with_fundamentals(sym, independent_live.get(sym, {}))
            self.store.set_quote(sym, merged)
            self._maybe_add_volume_catalyst(
                sym,
                merged,
                source=config.RVOL_INDEPENDENT_SOURCE,
                max_float=config.RVOL_INDEPENDENT_FLOAT_MAX,
            )
            self._maybe_add_volume_activity_catalyst(sym, merged)
            self._maybe_add_volume_momentum_catalyst(sym, merged)
            self._queue_filtered_symbol_for_news(sym, merged)
        return 1

    def _run_market_universe_scan(self, full_pass: bool = False) -> dict:
        started_at = time.time()
        batch_count = 0
        symbol_count = 0
        if full_pass:
            market_scan = self._us_market_scan_universe()
            if market_scan:
                # Full pass prioritizes completeness over speed.
                self._scan_market_symbols(market_scan, full_data=True)
                batch_count = 1
                symbol_count = len(market_scan)
        else:
            batches = self._us_market_scan_batches(full_pass=False)
            for market_scan in batches:
                if not market_scan:
                    continue
                self._scan_market_symbols(market_scan)
                batch_count += 1
                symbol_count += len(market_scan)
        independent_ran = self._scan_independent_symbols()
        self.stats["market_symbols_scanned_last"] = symbol_count
        self.stats["market_batches_last"] = batch_count
        self.stats["us_market_universe_size"] = len(self._us_market_symbols)
        self.stats["us_market_symbol_sources"] = dict(self._us_market_symbol_source_counts)
        universe_size = len(self._us_market_symbols)
        if full_pass and symbol_count:
            self._market_coverage_seen.clear()
            self._market_coverage_started_at = 0.0
            self.stats["market_coverage_symbols"] = universe_size
            self.stats["market_coverage_percent"] = 100.0
            self.stats["market_coverage_completed"] = int(
                self.stats.get("market_coverage_completed", 0)
            ) + 1
            self.stats["market_last_full_coverage_at"] = time.time()
            self.stats["market_last_full_coverage_seconds"] = time.time() - started_at
        elif symbol_count and universe_size:
            if not self._market_coverage_seen:
                self._market_coverage_started_at = started_at
            for batch in batches:
                self._market_coverage_seen.update(batch)
            covered = min(universe_size, len(self._market_coverage_seen))
            self.stats["market_coverage_symbols"] = covered
            self.stats["market_coverage_percent"] = round(
                covered * 100.0 / universe_size, 1
            )
            if covered >= universe_size:
                self.stats["market_coverage_completed"] = int(
                    self.stats.get("market_coverage_completed", 0)
                ) + 1
                self.stats["market_last_full_coverage_at"] = time.time()
                self.stats["market_last_full_coverage_seconds"] = (
                    time.time() - self._market_coverage_started_at
                )
                self._market_coverage_seen.clear()
                self._market_coverage_started_at = 0.0
        return {
            "market_batches": batch_count,
            "market_symbols": symbol_count,
            "independent_scans": independent_ran,
        }

    def _run_filtered_results_scan(self) -> None:
        # _quote_symbols() now returns only currently-visible tickers + up to 20
        # unquoted new arrivals. No batching/rotation needed — just refresh them all.
        scan_symbols = self._quote_symbols()
        if scan_symbols:
            # Hard cap to prevent runaway in edge cases (e.g. hundreds of new symbols)
            max_symbols = int(getattr(config, "FILTERED_SCAN_MAX_SYMBOLS", 50) or 50)
            if len(scan_symbols) > max_symbols:
                total_symbols = len(scan_symbols)
                start = self._filtered_quote_cursor % total_symbols
                scan_symbols = [
                    scan_symbols[(start + offset) % total_symbols]
                    for offset in range(max_symbols)
                ]
                self._filtered_quote_cursor = (start + max_symbols) % total_symbols
            self.stats["filtered_scan_symbols_last"] = len(scan_symbols)
            chunk_size = max(1, int(getattr(config, "FILTERED_SCAN_CHUNK_SIZE", 20) or 20))
            # Lightweight quote endpoint only. Intraday candle history is
            # fetched exclusively by the hover/chart HTTP routes.
            session_type = _session_info()["type"]
            if session_type in {"premarket", "after_hours"}:
                # The market-wide bulk feed does not expose extended-hours
                # baselines. Refresh only filtered symbols from extended-hours
                # bars and cache them briefly.
                live = self._batched_quotes(
                    scan_symbols,
                    chunk_size=10,
                    workers=min(8, max(1, int(getattr(config, "US_MARKET_SCAN_WORKERS", 5)))),
                    quote_fn=lambda symbols: {
                        symbol: self._chart_quote_snapshot(symbol) for symbol in symbols
                    },
                )
            else:
                live = self._batched_quotes(scan_symbols, chunk_size, minimal=True)
            now = time.time()
            cutoff = now - (24 * 60 * 60)
            stale = [k for k, seen_at in self._filtered_scan_seen_at.items() if seen_at < cutoff]
            for k in stale:
                self._filtered_scan_seen_at.pop(k, None)
            for sym in scan_symbols:
                self._filtered_scan_seen_at[sym.upper()] = now
                # Fundamentals are slow-changing and maintained by their own
                # long-lived cache. Never issue per-symbol fundamentals requests
                # from this latency-critical refresh tier.
                merged = self._merge_quote_with_fundamentals(
                    sym, live.get(sym, {}), include_fundamentals=False
                )
                self.store.set_quote(sym, merged)
                self._maybe_add_volume_catalyst(sym, merged)
                self._maybe_add_volume_momentum_catalyst(sym, merged)
        else:
            self.stats["filtered_scan_symbols_last"] = 0

    def _execute_filtered_results_scan(self) -> None:
        """Run and time the user-cadence result refresh."""
        started = time.time()
        previous = float(self.stats.get("filtered_scan_started_at") or 0.0)
        self.stats["filtered_scan_started_at"] = started
        if previous:
            self.stats["filtered_scan_interval_actual_seconds"] = started - previous
        try:
            self._run_filtered_results_scan()
        finally:
            finished = time.time()
            self.stats["filtered_scan_finished_at"] = finished
            self.stats["filtered_scan_duration_seconds"] = finished - started

    def _quote_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not _is_trading_window():
                    now = time.time()
                    closed_refresh_seconds = int(
                        getattr(config, "CLOSED_MARKET_SCAN_SECONDS", 0) or 0
                    )
                    if (
                        closed_refresh_seconds > 0
                        and (now - self._last_closed_market_scan_at)
                        >= max(60, closed_refresh_seconds)
                    ):
                        with self._scan_lock:
                            self._run_market_universe_scan()
                            self._execute_filtered_results_scan()
                            now = time.time()
                            self._last_market_scan_at = now
                            self._last_filtered_scan_at = now
                            self.stats["last_quote_poll"] = now
                            self._last_closed_market_scan_at = now
                    self._stop.wait(30.0)
                    continue
                now = time.time()
                market_interval = max(5, int(getattr(config, "US_MARKET_SCAN_SECONDS", 20) or 20))
                filtered_interval = max(2, int(getattr(config, "FILTERED_SCAN_SECONDS", 3) or 3))
                with self._scan_lock:
                    if (now - self._last_filtered_scan_at) >= filtered_interval:
                        self._execute_filtered_results_scan()
                        self._last_filtered_scan_at = float(
                            self.stats.get("filtered_scan_started_at") or time.time()
                        )
                    if (now - self._last_market_scan_at) >= market_interval:
                        self._run_market_universe_scan()
                        self._last_market_scan_at = now
                self.stats["last_quote_poll"] = time.time()
            except Exception:
                log.exception("quote loop error")
            self._stop.wait(max(0.5, float(getattr(config, "QUOTE_REFRESH_SECONDS", 1) or 1)))

    def _bootstrap_closed_market_snapshot(self) -> None:
        if _is_trading_window():
            return
        try:
            with self._scan_lock:
                self._run_market_universe_scan(full_pass=True)
                self._execute_filtered_results_scan()
                now = time.time()
                self._last_market_scan_at = now
                self._last_filtered_scan_at = now
                self.stats["last_quote_poll"] = now
                self._last_closed_market_scan_at = now
        except Exception:
            log.exception("closed-market bootstrap scan failed")

    def force_scan(self) -> dict:
        """Run an immediate scan cycle regardless of market session."""
        ran_filtered = 0
        ran_market = 0
        started_at = time.time()
        session = _session_info()
        closed_market_snapshot = session.get("type") == "closed"
        with self._scan_lock:
            self._execute_filtered_results_scan()
            ran_filtered = 1
            market_result = self._run_market_universe_scan(full_pass=True)
            ran_market = 1
            now = time.time()
            self._last_filtered_scan_at = now
            self._last_market_scan_at = now
            if closed_market_snapshot:
                self._last_closed_market_scan_at = now
            self.stats["last_quote_poll"] = now
        return {
            "forced": True,
            "market_session": session.get("type", "closed"),
            "market_session_label": _session_label(session.get("type", "closed")),
            "closed_market_snapshot": closed_market_snapshot,
            "started_at": started_at,
            "finished_at": time.time(),
            "scans": {
                "filtered_results": ran_filtered,
                "market_universe": ran_market,
                "market_batches": int(market_result.get("market_batches", 0)),
                "market_symbols": int(market_result.get("market_symbols", 0)),
                "independent_scans": int(market_result.get("independent_scans", 0)),
            },
        }

    def trigger_force_scan(self) -> bool:
        """Start force_scan in the background. Returns False if already running."""
        with self._force_scan_lock:
            if self._force_scan_active:
                return False
            self._force_scan_active = True

        def _run():
            try:
                self.force_scan()
            finally:
                with self._force_scan_lock:
                    self._force_scan_active = False

        t = threading.Thread(target=_run, name="force-scan", daemon=True)
        t.start()
        return True

    def force_scan_running(self) -> bool:
        with self._force_scan_lock:
            return self._force_scan_active

    def set_pattern_analysis(self, ticker: str, analysis: dict) -> None:
        """Cache chart evidence so the normal row score can use reviewed patterns."""
        symbol = str(ticker or "").upper().strip()
        if not symbol:
            return
        with self._pattern_lock:
            self._pattern_analysis[symbol] = {
                "candlestick_score": int(analysis.get("candlestick_score") or 0),
                "candlestick_bias": str(analysis.get("bias") or "neutral"),
                "candlestick_confidence": int(analysis.get("confidence") or 0),
                "candlestick_patterns": list(analysis.get("matched_patterns") or [])[:8],
                "candlestick_updated": time.time(),
            }

    def _pattern_snapshot(self) -> dict[str, dict]:
        cutoff = time.time() - 180
        with self._pattern_lock:
            stale = [
                symbol for symbol, data in self._pattern_analysis.items()
                if float(data.get("candlestick_updated") or 0) < cutoff
            ]
            for symbol in stale:
                self._pattern_analysis.pop(symbol, None)
            return {symbol: dict(data) for symbol, data in self._pattern_analysis.items()}

    def _quote_symbols(self) -> list[str]:
        """Return the symbols the filtered scan should refresh with chart API data.

        Priority 1: tickers currently visible on the scanner (updated by rows()).
        Priority 2: tickers with active alerts but no stored price yet — these
                    need at least one quote so they can be filter-evaluated.
        Universe tickers that already have a quote are handled by the universe scan.
        """
        with self._visible_tickers_lock:
            visible = list(self._visible_tickers)

        visible_set = set(visible)
        snap = self.store.quotes_snapshot()

        # If no visible tickers yet (app just started / first boot), fall back to
        # the most recent active alert tickers so the board populates quickly.
        if not visible:
            for ticker in self.store.active_tickers():
                if ticker not in visible_set:
                    visible.append(ticker)
                    visible_set.add(ticker)
                    if len(visible) >= 30:
                        break

        # Also include newly-arrived tickers that have no quote yet (max 20).
        # This ensures new symbols get their initial data quickly.
        unquoted: list[str] = []
        for ticker in self.store.active_tickers():
            if ticker in visible_set:
                continue
            q = snap.get(ticker, {})
            if q.get("last") is None:
                unquoted.append(ticker)
                if len(unquoted) >= 20:
                    break

        return visible + unquoted

    def _merge_quote_with_fundamentals(
        self,
        ticker: str,
        quote_data: dict,
        include_fundamentals: bool = True,
    ) -> dict:
        prev_stored = self.store.get_quote(ticker)
        merged = self._accept_quote_generation(ticker, quote_data or {})
        if merged.pop("_rejected_generation", False):
            return prev_stored
        if not merged.get("exchange"):
            ex = self._us_market_exchange.get(ticker.upper())
            if not ex:
                ex = prev_stored.get("exchange")
            if ex:
                merged["exchange"] = ex
        if include_fundamentals:
            for key, value in self._fundamentals(ticker).items():
                if value is not None or merged.get(key) is None:
                    merged[key] = value
        else:
            for key in ("name", "market_cap", "float_shares", "shares_out", "avg_volume", "sector"):
                if merged.get(key) is None and prev_stored.get(key) is not None:
                    merged[key] = prev_stored.get(key)
        # Avoid wiping usable quote fields when a batched quote request misses
        # a symbol (common during large-universe scans).
        quote_keys = (
            "last",
            "prev_close",
            "change_pct",
            "change_pct_3m",
            "change_pct_10m",
            "volume",
            "market_state",
            "last_trade_ts",
            "previous_extended_close",
            "regular_session_close",
            "scan_change_delta",
            "scan_change_accel",
            "scan_volume_delta",
            "scan_volume_accel",
            "scan_rvol_delta",
            "scan_momentum_updated_at",
        )
        for key in quote_keys:
            if merged.get(key) is None and prev_stored.get(key) is not None:
                merged[key] = prev_stored.get(key)
        # If the new quote is missing 3m/10m change data (chart API fell back to
        # fast_info), carry forward the previous stored values — they're only a
        # few seconds old and far better than showing '—' every other cycle.
        for k in ("change_pct_3m", "change_pct_10m"):
            if merged.get(k) is None and prev_stored.get(k) is not None:
                # Only carry forward if the stored quote is recent (< 3 minutes)
                age = time.time() - float(prev_stored.get("updated") or 0)
                if age < 180:
                    merged[k] = prev_stored[k]
        merged.update(_dynamic_change_metrics(merged))
        merged = self._annotate_quote_momentum(ticker, merged)
        return merged

    def _accept_quote_generation(self, ticker: str, quote_data: dict) -> dict:
        """Validate ordering once before filters, metrics, scoring, or display.

        Provider timestamps are authoritative when present. A late or implausibly
        future update is rejected, while identical timestamp/value snapshots are
        treated as duplicates so they cannot create false acceleration or flashes.
        """
        symbol = str(ticker or "").upper()
        incoming = dict(quote_data or {})
        now = time.time()
        provider_ts = _coerce_epoch_seconds(incoming.get("last_trade_ts"))
        previous = self._accepted_quote_generation.get(symbol)
        if provider_ts is not None and provider_ts > now + 300:
            self.stats["market_updates_dropped_future"] += 1
            return {"_rejected_generation": True}
        if previous and provider_ts is not None and previous.get("provider_timestamp") is not None:
            prior_ts = float(previous["provider_timestamp"])
            if provider_ts < prior_ts:
                self.stats["market_updates_dropped_stale"] += 1
                return {"_rejected_generation": True}
            core = tuple(incoming.get(k) for k in ("last", "change_pct", "volume", "bid", "ask"))
            if provider_ts == prior_ts and core == previous.get("core"):
                self.stats["market_updates_duplicate"] += 1
                return {"_rejected_generation": True}
        self._market_generation += 1
        generation = self._market_generation
        incoming.update({
            "provider_timestamp": provider_ts,
            "received_at": now,
            "accepted_generation": generation,
        })
        self._accepted_quote_generation[symbol] = {
            "provider_timestamp": provider_ts,
            "received_at": now,
            "generation": generation,
            "core": tuple(incoming.get(k) for k in ("last", "change_pct", "volume", "bid", "ask")),
        }
        self.stats["market_updates_accepted"] += 1
        return incoming

    def _annotate_quote_momentum(self, ticker: str, quote: dict) -> dict:
        sym = ticker.upper()
        now = time.time()
        prev = self._quote_momentum.get(sym, {})
        out = dict(quote)
        try:
            change_now = float(out.get("change_pct")) if out.get("change_pct") is not None else None
        except (TypeError, ValueError):
            change_now = None
        try:
            volume_now = float(out.get("volume")) if out.get("volume") is not None else None
        except (TypeError, ValueError):
            volume_now = None
        try:
            avg_now = float(out.get("avg_volume")) if out.get("avg_volume") is not None else None
        except (TypeError, ValueError):
            avg_now = None

        rvol_now = _rvol_for_quote(out)
        change_prev = prev.get("change_pct")
        volume_prev = prev.get("volume")
        rvol_prev = prev.get("rvol")
        vol_delta_prev = prev.get("volume_delta")
        chg_delta_prev = prev.get("change_delta")

        change_delta = (
            (change_now - change_prev)
            if (change_now is not None and isinstance(change_prev, float))
            else None
        )
        volume_delta = (
            (volume_now - volume_prev)
            if (volume_now is not None and isinstance(volume_prev, float))
            else None
        )
        rvol_delta = (
            (rvol_now - rvol_prev)
            if (rvol_now is not None and isinstance(rvol_prev, float))
            else None
        )
        elapsed = max(0.5, now - float(prev.get("updated") or now))
        change_velocity = change_delta / elapsed if isinstance(change_delta, float) else None
        prior_change_velocity = prev.get("change_velocity")
        change_accel = (
            (change_velocity - prior_change_velocity) / elapsed
            if isinstance(change_velocity, float) and isinstance(prior_change_velocity, float)
            else None
        )
        # Cumulative session volume must never generate a negative trade rate.
        volume_rate = max(0.0, volume_delta) / elapsed if isinstance(volume_delta, float) else None
        prior_volume_rate = prev.get("volume_rate")
        volume_accel = (
            (volume_rate - prior_volume_rate) / elapsed
            if isinstance(volume_rate, float) and isinstance(prior_volume_rate, float)
            else None
        )

        out["scan_change_delta"] = change_delta
        out["scan_change_accel"] = change_accel
        out["scan_volume_delta"] = volume_delta
        out["scan_volume_accel"] = volume_accel
        out["scan_rvol_delta"] = rvol_delta
        out["scan_momentum_updated_at"] = now
        out["scan_sample_seconds"] = elapsed if prev else None
        out["price_velocity_pct_per_second"] = change_velocity
        out["volume_rate_shares_per_second"] = volume_rate

        self._quote_momentum[sym] = {
            "change_pct": change_now if change_now is not None else change_prev,
            "volume": volume_now if volume_now is not None else volume_prev,
            "rvol": rvol_now if rvol_now is not None else rvol_prev,
            "change_delta": change_delta if change_delta is not None else chg_delta_prev,
            "volume_delta": volume_delta if volume_delta is not None else vol_delta_prev,
            "change_velocity": change_velocity if change_velocity is not None else prior_change_velocity,
            "volume_rate": volume_rate if volume_rate is not None else prior_volume_rate,
            "updated": now,
        }
        return out

    @staticmethod
    def _is_major_exchange(exchange: str | None) -> bool:
        if not exchange:
            return False
        raw = str(exchange).upper()
        normalized = re.sub(r"[^A-Z]", "", raw)
        majors = {re.sub(r"[^A-Z]", "", x.upper()) for x in config.US_MAJOR_EXCHANGES}
        return normalized in majors

    @staticmethod
    def _canonical_symbol(raw: str) -> str:
        # Normalize class/share suffixes to a provider-friendly form
        # (for example BRK.B -> BRK-B).
        return raw.strip().upper().replace(".", "-").replace("/", "-")

    @staticmethod
    def _is_supported_market_symbol(symbol: str) -> bool:
        # Listing directories already identify and exclude test issues. Preserve
        # legitimate five-letter listings and provider-normalized class, preferred,
        # warrant, unit, and right suffixes instead of guessing security type from
        # the final character of a ticker.
        return bool(re.fullmatch(r"[A-Z]{1,5}(?:-[A-Z]{1,2})?", symbol))

    @staticmethod
    def _canonical_exchange(raw: str | None) -> str | None:
        if not raw:
            return None
        token = re.sub(r"[^A-Z]", "", str(raw).upper())
        mapping = {
            "NASDAQ": "NASDAQ",
            "NMS": "NASDAQ",
            "NGM": "NASDAQ",
            "NYSE": "NYSE",
            "NYQ": "NYSE",
            "NYSEARCA": "NYSE",
            "ARCA": "NYSE",
            "NYSEMKT": "AMEX",
            "NYSEAMERICAN": "AMEX",
            "AMEX": "AMEX",
            "ASE": "AMEX",
            "BATS": "BATS",
            "BZX": "BATS",
            "CBOEBZX": "BATS",
            "CBOEBYX": "BATS",
            "CBOEEDGX": "BATS",
            "CBOEEDGA": "BATS",
            "BYX": "BATS",
            "EDGX": "BATS",
            "EDGA": "BATS",
            "IEX": "IEXG",
            "IEXG": "IEXG",
            "IEXEXCHANGE": "IEXG",
        }
        return mapping.get(token)

    def _track_symbol_source(self, source_counts: dict[str, int], source: str) -> None:
        source_counts[source] = int(source_counts.get(source, 0)) + 1

    def _load_us_market_symbols(self) -> list[str]:
        found: set[str] = set()
        exchange_map: dict[str, str] = {}
        source_counts: dict[str, int] = {}
        def _add_symbol(symbol_raw: str, source_label: str, exchange_name: str | None = None) -> None:
            symbol = self._canonical_symbol(symbol_raw or "")
            if not symbol:
                return
            if not self._is_supported_market_symbol(symbol):
                return
            found.add(symbol)
            self._track_symbol_source(source_counts, source_label)
            canonical_exchange = self._canonical_exchange(exchange_name)
            if canonical_exchange and not exchange_map.get(symbol):
                exchange_map[symbol] = canonical_exchange

        urls = (
            "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt",
            "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt",
        )
        for url in urls:
            try:
                resp = self._http.get(
                    url, headers={"User-Agent": config.USER_AGENT}, timeout=15
                )
                if resp.status_code != 200:
                    continue
                reader = csv.DictReader(StringIO(resp.text), delimiter="|")
                for row in reader:
                    if "File Creation Time" in row:
                        continue
                    if url.endswith("nasdaqlisted.txt"):
                        symbol = row.get("Symbol") or ""
                        exchange_name = "NASDAQ"
                        if (row.get("Test Issue") or "N").strip().upper() != "N":
                            continue
                    else:
                        symbol = row.get("ACT Symbol") or ""
                        if (row.get("Test Issue") or "N").strip().upper() != "N":
                            continue
                        exch_code = (row.get("Exchange") or "").strip().upper()
                        # N=NYSE, A=NYSE American, P=NYSE Arca, Z=BATS, V=IEX
                        if exch_code and exch_code not in {"N", "A", "P", "Z", "V"}:
                            continue
                        exchange_name = {
                            "N": "NYSE",
                            "A": "AMEX",
                            "P": "NYSE",
                            "Z": "BATS",
                            "V": "IEXG",
                        }.get(exch_code, "")
                    _add_symbol(symbol, "nasdaq_trader", exchange_name)
            except Exception:
                continue
        # Secondary source: SEC company tickers with exchange metadata.
        # This captures symbols that occasionally lag in directory files.
        sec_urls = (
            "https://www.sec.gov/files/company_tickers_exchange.json",
            "https://www.sec.gov/files/company_tickers.json",
        )
        for sec_url in sec_urls:
            try:
                resp = self._http.get(
                    sec_url, headers={"User-Agent": config.USER_AGENT}, timeout=15
                )
                if resp.status_code != 200:
                    continue
                payload = resp.json() if resp.content else {}
                rows = []
                if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                    fields = payload.get("fields") or []
                    rows = [dict(zip(fields, row)) for row in payload.get("data", [])]
                elif isinstance(payload, dict):
                    rows = payload.values()
                elif isinstance(payload, list):
                    rows = payload
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    raw_symbol = str(row.get("ticker") or row.get("symbol") or "")
                    sec_exchange = (
                        row.get("exchange")
                        or row.get("exchangeName")
                        or row.get("exch")
                    )
                    # The metadata-free SEC fallback includes OTC and other
                    # registered issuers. It may confirm an existing listing,
                    # but must not expand the exchange-listed universe.
                    if not self._canonical_exchange(sec_exchange):
                        symbol = self._canonical_symbol(raw_symbol)
                        if symbol not in found:
                            continue
                    _add_symbol(raw_symbol, "sec_company_tickers", sec_exchange)
            except Exception:
                continue
        # Exchange file fallback source (updated nightly).
        exchange_txt_sources = (
            (
                "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/nasdaq/nasdaq_tickers.txt",
                "NASDAQ",
            ),
            (
                "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/nyse/nyse_tickers.txt",
                "NYSE",
            ),
            (
                "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/amex/amex_tickers.txt",
                "AMEX",
            ),
        )
        for txt_url, exchange_name in exchange_txt_sources:
            try:
                resp = self._http.get(
                    txt_url, headers={"User-Agent": config.USER_AGENT}, timeout=15
                )
                if resp.status_code != 200 or not resp.text:
                    continue
                for line in resp.text.splitlines():
                    _add_symbol(line.strip(), "exchange_txt_backup", exchange_name)
            except Exception:
                continue
        # Index constituents are merged explicitly so S&P 500 and Russell names are
        # always searched even when a feed/source lags.
        index_csv_sources = (
            (
                "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
                "sp500_index",
            ),
            (
                "https://raw.githubusercontent.com/kelseyp99/stock-ai-scanner/48f5743a5c8557d3724c8196ac520a22803469ab/backend/data/indexes/russell2000.csv",
                "russell2000_index",
            ),
        )
        for csv_url, source_label in index_csv_sources:
            try:
                resp = self._http.get(
                    csv_url, headers={"User-Agent": config.USER_AGENT}, timeout=15
                )
                if resp.status_code != 200 or not resp.text:
                    continue
                reader = csv.DictReader(StringIO(resp.text))
                for row in reader:
                    if not isinstance(row, dict):
                        continue
                    _add_symbol(
                        row.get("Symbol")
                        or row.get("symbol")
                        or row.get("ticker")
                        or "",
                        source_label,
                    )
            except Exception:
                continue
        self._us_market_exchange = exchange_map
        self._us_market_symbol_source_counts = source_counts
        return sorted(found)

    def _us_market_scan_universe(self) -> list[str]:
        if not getattr(config, "US_MARKET_SCAN_ENABLED", False):
            return []
        now = time.time()
        refresh_after = 6 * 60 * 60
        if (
            not self._us_market_symbols
            or (now - self._us_market_symbols_at) > refresh_after
        ):
            loaded = self._load_us_market_symbols()
            if loaded:
                self._us_market_symbols = loaded
                self._us_market_symbols_at = now
                if self._us_market_cursor >= len(self._us_market_symbols):
                    self._us_market_cursor = 0
        return list(self._us_market_symbols)

    def _us_market_scan_symbols(self) -> list[str]:
        symbols = self._us_market_scan_universe()
        if not symbols:
            return []
        if getattr(config, "US_MARKET_SCAN_FULL_COVERAGE", False):
            return list(symbols)
        base_batch = int(getattr(config, "US_MARKET_SCAN_BATCH_SIZE", 120) or 120)
        scan_interval = max(5, int(getattr(config, "US_MARKET_SCAN_SECONDS", 20) or 20))
        target_seconds = max(
            scan_interval,
            int(getattr(config, "US_MARKET_SCAN_TARGET_FULL_COVERAGE_SECONDS", 300) or 300),
        )
        # Use only cycles whose scheduled start fits inside the coverage target.
        # A ceil here can silently turn (for example) a 300-second target at a
        # 40-second cadence into an actual 320-second full-universe pass.
        target_cycles = max(1, target_seconds // scan_interval)
        coverage_batch = max(1, (len(symbols) + target_cycles - 1) // target_cycles)
        batch_size = min(len(symbols), max(base_batch, coverage_batch))
        start = self._us_market_cursor % len(symbols)
        out: list[str] = []
        for i in range(batch_size):
            out.append(symbols[(start + i) % len(symbols)])
        self._us_market_cursor = (start + batch_size) % len(symbols)
        retry_limit = max(
            0,
            int(getattr(config, "US_MARKET_SCAN_RETRY_SYMBOLS_PER_CYCLE", 100) or 0),
        )
        if (
            retry_limit
            and self._market_pending_retry
            and time.time() >= self._market_retry_backoff_until
        ):
            present = set(out)
            for symbol in sorted(self._market_pending_retry):
                if symbol not in present:
                    out.append(symbol)
                    present.add(symbol)
                    if len(present) - batch_size >= retry_limit:
                        break
        return out

    def _us_market_scan_batches(self, full_pass: bool = False) -> list[list[str]]:
        symbols = self._us_market_scan_universe()
        if not symbols:
            return []
        if not full_pass and not getattr(config, "US_MARKET_SCAN_FULL_COVERAGE", False):
            batch = self._us_market_scan_symbols()
            return [batch] if batch else []
        batch_size = max(1, int(getattr(config, "US_MARKET_SCAN_BATCH_SIZE", 120) or 120))
        start = self._us_market_cursor % len(symbols)
        ordered = symbols[start:] + symbols[:start]
        self._us_market_cursor = (start + len(ordered)) % len(symbols)
        return [ordered[i : i + batch_size] for i in range(0, len(ordered), batch_size)]

    def _independent_symbols_for_scan(self) -> list[str]:
        if not getattr(config, "RVOL_INDEPENDENT_ENABLED", False):
            return []
        now = time.time()
        interval = int(getattr(config, "RVOL_INDEPENDENT_SCAN_SECONDS", 60) or 60)
        if self._independent_symbols and (now - self._independent_symbols_at) < interval:
            return list(self._independent_symbols)

        found: set[str] = set(self._finviz_screen_symbols())
        for screen in getattr(config, "RVOL_INDEPENDENT_SCREENS", []):
            url = (
                "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
                f"?count={int(config.RVOL_INDEPENDENT_SYMBOL_LIMIT)}&scrIds={screen}"
            )
            try:
                resp = self._http.get(
                    url,
                    headers={"User-Agent": config.USER_AGENT},
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                result = ((payload or {}).get("finance", {}).get("result") or [])
                quotes = result[0].get("quotes", []) if result else []
                for row in quotes:
                    sym = (row.get("symbol") or "").upper().strip()
                    if sym and sym.isascii() and sym.replace(".", "").isalnum():
                        found.add(sym)
            except Exception:
                continue

        ordered = sorted(found)
        limit = int(getattr(config, "RVOL_INDEPENDENT_SYMBOL_LIMIT", 250) or 250)
        self._independent_symbols = ordered[:limit]
        self._independent_symbols_at = now
        return list(self._independent_symbols)

    @staticmethod
    def _extract_finviz_symbols(html: str) -> list[str]:
        matches = re.findall(r"quote\.ashx\?t=([A-Z][A-Z0-9.]*)", html or "")
        out: list[str] = []
        seen: set[str] = set()
        for sym in matches:
            s = sym.upper().strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    def _finviz_screen_symbols(self) -> list[str]:
        limit = int(getattr(config, "RVOL_INDEPENDENT_SYMBOL_LIMIT", 250) or 250)
        found: list[str] = []
        seen: set[str] = set()
        for start in range(1, limit + 1, 20):
            url = (
                "https://finviz.com/screener.ashx"
                "?v=111&f=sh_float_u20,ta_relvol_o7"
                f"&r={start}"
            )
            try:
                resp = self._http.get(
                    url,
                    headers={"User-Agent": config.USER_AGENT},
                    timeout=10,
                )
                if resp.status_code != 200:
                    break
                page_symbols = self._extract_finviz_symbols(resp.text)
                if not page_symbols:
                    break
                for sym in page_symbols:
                    if sym in seen:
                        continue
                    seen.add(sym)
                    found.append(sym)
                    if len(found) >= limit:
                        return found
                if len(page_symbols) < 20:
                    break
            except Exception:
                break
        return found

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
        if cached and time.time() - cached.get("_at", 0) < config.FUNDAMENTALS_REFRESH_SECONDS:
            if cached.get("market_cap") is not None and cached.get("float_shares") is not None:
                return cached
        fresh = self.provider.fundamentals(ticker) or {}
        data = dict(cached or {})
        for key, value in fresh.items():
            if value is not None:
                data[key] = value
        data["_at"] = time.time()
        self._fund_cache[ticker] = data
        return data

    def _maybe_add_volume_catalyst(
        self,
        ticker: str,
        quote: dict,
        source: str | None = None,
        max_float: float | None = None,
    ) -> None:
        vol = quote.get("volume")
        avg = quote.get("avg_volume")
        if not vol or not avg:
            return
        try:
            rvol = _rvol_for_quote(quote)
            if rvol is None:
                return
        except (TypeError, ValueError, ZeroDivisionError):
            return
        float_shares = quote.get("float_shares")
        if (
            max_float is not None
            and float_shares is not None
            and float(float_shares) > float(max_float)
        ):
            return
        if rvol < float(config.RVOL_CATALYST_THRESHOLD):
            return
        news = self._research_rvol_news(ticker.upper())
        base_score = max(int(config.MIN_SCORE), int(config.RVOL_CATALYST_SCORE))
        bonus = int(config.RVOL_NEWS_BONUS_SCORE) if news else 0
        score = max(base_score + bonus, int(news.get("score", 0)) + bonus if news else 0)
        tags = [f"RVOL >= {config.RVOL_CATALYST_THRESHOLD:.1f}x"]
        if news:
            tags += list(news.get("tags", []))[:2]
            tags.append("Confirmed news")
        headline = (
            news.get("headline")
            if news
            else f"{ticker.upper()} relative volume spike (>= {config.RVOL_CATALYST_THRESHOLD:.1f}x)"
        )
        body = (
            f"Relative volume catalyst detected: {rvol:.2f}x "
            f"(volume {int(vol):,} vs avg {int(avg):,})."
        )
        if news and news.get("summary"):
            body = f"{body} Confirmed news: {news.get('summary')}"
        self.store.add(
            {
                "ticker": ticker.upper(),
                "headline": headline,
                "url": news.get("url", "#") if news else "#",
                "source": (
                    f"{source or config.RVOL_CATALYST_SOURCE} + {news.get('source')}"
                    if news
                    else (source or config.RVOL_CATALYST_SOURCE)
                ),
                "published": time.time(),
                "body": body,
                "score": score,
                "tags": tags,
                "dilution": [],
                "distress": [],
            }
        )

    def _research_rvol_news(self, ticker: str) -> dict | None:
        if not getattr(config, "RVOL_NEWS_RESEARCH_ENABLED", False):
            return None
        sym = str(ticker or "").upper().strip()
        if not sym:
            return None
        if sym not in self._filtered_scan_seen_at:
            # Only run news research after a ticker has passed through the
            # filtered scanner path. This keeps market-wide scans lightweight.
            return None
        now = time.time()
        identity = self.security_identity(sym)
        cache_key = identity.cache_key
        cached = self._rvol_news_cache.get(cache_key)
        ttl = int(getattr(config, "RVOL_NEWS_RESEARCH_CACHE_SECONDS", 60) or 60)
        if cached and (now - float(cached.get("_at", 0))) < ttl:
            return cached.get("news")

        best: dict | None = None
        try:
            candidate = self.news_provider.get_quick_news(identity)
            if candidate:
                title = candidate.get("headline", "")
                summary = candidate.get("body") or candidate.get("summary") or ""
                scored = scoring.score(title, summary, 1.0)
                pub = float(candidate.get("published") or 0)
                cutoff = now - (float(config.MAX_NEWS_AGE_HOURS) * 3600.0)
                if (
                    not scored.get("junk")
                    and (not pub or pub >= cutoff)
                    and int(scored.get("score", 0)) >= int(config.MIN_SCORE)
                ):
                    best = {
                        **candidate,
                        "summary": summary[:220],
                        "score": int(scored.get("score", 0)),
                        "tags": list(scored.get("tags", [])),
                    }
        except Exception:
            best = None

        self._rvol_news_cache[cache_key] = {"_at": now, "news": best}
        return best

    def _queue_quick_news(self, ticker: str) -> bool:
        """Queue one nonblocking quick lookup unless this symbol is still fresh."""
        if not getattr(config, "QUICK_NEWS_ENABLED", True):
            return False
        sym = str(ticker or "").upper().strip()
        if not sym:
            return False
        with self._active_filtered_lock:
            if sym not in self._active_filtered_symbols:
                return False
        cache_key = self.security_identity(sym).cache_key
        now = time.time()
        ttl = int(getattr(config, "QUICK_NEWS_CACHE_SECONDS", 60) or 60)
        with self._quick_news_lock:
            cached = self._quick_news_cache.get(cache_key)
            if cached and now - float(cached.get("checkedAt") or 0) < ttl:
                return False
            if cache_key in self._quick_news_inflight:
                return False
            self._quick_news_inflight.add(cache_key)
        try:
            self._quick_news_queue.put_nowait(sym)
            return True
        except queue.Full:
            with self._quick_news_lock:
                self._quick_news_inflight.discard(cache_key)
            return False

    def _quick_news_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sym = self._quick_news_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            cache_key = (sym,)
            try:
                with self._active_filtered_lock:
                    if sym not in self._active_filtered_symbols:
                        continue
                cache_key = self.security_identity(sym).cache_key
                with self._quick_news_lock:
                    previous = dict(self._quick_news_cache.get(cache_key) or {})
                news = self._research_rvol_news(sym)
                signal = news_scoring.quick_signal(
                    sym, news, str(previous.get("articleId") or "")
                )
                with self._active_filtered_lock:
                    if sym not in self._active_filtered_symbols:
                        continue
                if signal.get("articleId") == previous.get("articleId"):
                    signal["detectedAt"] = previous.get("detectedAt")
                    signal["isNew"] = False
                    signal["isDuplicate"] = True
                with self._quick_news_lock:
                    self._quick_news_cache[cache_key] = signal
                self.stats["quick_news_checks"] = int(
                    self.stats.get("quick_news_checks", 0)
                ) + 1
            except Exception:
                log.exception("quick news lookup failed for %s", sym)
            finally:
                with self._quick_news_lock:
                    self._quick_news_inflight.discard(cache_key)
                self._quick_news_queue.task_done()

    def update_active_filtered_symbols(self, symbols: list[str]) -> dict:
        """Apply the browser's completed filter result as the news work scope."""
        normalized = {
            str(symbol or "").upper().strip()
            for symbol in symbols
            if str(symbol or "").strip()
        }
        now = time.time()
        grace = max(
            0.0, float(getattr(config, "FILTER_EXIT_GRACE_SECONDS", 3) or 0)
        )
        with self._active_filtered_lock:
            previous = set(self._active_filtered_symbols)
            self._active_filtered_symbols = normalized
            for symbol in normalized:
                membership = self._scanner_memberships.get(symbol)
                if membership is None or (
                    membership.get("pendingRemovalAt")
                    and now >= float(membership["pendingRemovalAt"])
                ):
                    self._scanner_memberships[symbol] = {
                        "securityId": "|".join(map(str, self.security_identity(symbol).cache_key)),
                        "symbol": symbol,
                        "enteredFilteredScannerAt": now,
                        "pendingRemovalAt": None,
                    }
                else:
                    membership["pendingRemovalAt"] = None
            for symbol in previous - normalized:
                membership = self._scanner_memberships.get(symbol)
                if membership and not membership.get("pendingRemovalAt"):
                    membership["pendingRemovalAt"] = now + grace
        added = normalized - previous
        removed = previous - normalized
        retained = normalized & previous
        queued = 0
        # New entries are queued first. Retained entries enqueue only if their
        # identity cache has expired; _queue_quick_news performs that check.
        for symbol in sorted(added):
            queued += int(self._queue_quick_news(symbol))
        for symbol in sorted(retained):
            queued += int(self._queue_quick_news(symbol))
        self.stats["active_filtered_symbols"] = len(normalized)
        self.stats["quick_news_removed_last"] = len(removed)
        return {
            "active": len(normalized),
            "added": len(added),
            "retained": len(retained),
            "removed": len(removed),
            "queued": queued,
        }

    def scanner_membership(self, ticker: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        symbol = str(ticker or "").upper()
        with self._active_filtered_lock:
            membership = self._scanner_memberships.get(symbol)
            if not membership:
                return {
                    "entered_filtered_scanner_at": None,
                    "scanner_age_seconds": 0,
                    "scanner_age_state": "fresh",
                }
            pending = membership.get("pendingRemovalAt")
            if pending and now >= float(pending):
                self._scanner_memberships.pop(symbol, None)
                return {
                    "entered_filtered_scanner_at": None,
                    "scanner_age_seconds": 0,
                    "scanner_age_state": "fresh",
                }
            entered = float(membership["enteredFilteredScannerAt"])
        age_seconds = max(0, int(now - entered))
        state = (
            "fresh" if age_seconds < config.SCANNER_AGE_FRESH_WINDOW_SECONDS
            else "confirming" if age_seconds < config.SCANNER_AGE_CONFIRMATION_WINDOW_SECONDS
            else "stable" if age_seconds < config.SCANNER_AGE_STABLE_WINDOW_SECONDS
            else "decaying"
        )
        return {
            "entered_filtered_scanner_at": entered,
            "scanner_age_seconds": age_seconds,
            "scanner_age_state": state,
        }

    def quick_news_signal(self, ticker: str, now: float | None = None) -> dict | None:
        """Return cached quick news with decay recomputed and no network work."""
        now = time.time() if now is None else now
        cache_key = self.security_identity(ticker).cache_key
        with self._quick_news_lock:
            signal = dict(self._quick_news_cache.get(cache_key) or {})
        if not signal or not signal.get("hasNews"):
            return None
        stale = int(getattr(config, "QUICK_NEWS_STALE_SECONDS", 180) or 180)
        if now - float(signal.get("checkedAt") or 0) > stale:
            return None
        age = max(0.0, now - float(signal.get("publishedTimestamp") or now))
        freshness = math.exp(
            -age / max(1.0, float(signal.get("decayConstantSeconds") or 1800))
        )
        base = (
            float(signal.get("catalystStrength") or 0)
            * float(signal.get("sourceConfidence") or 0)
            * freshness
        )
        detected = float(signal.get("detectedAt") or 0)
        boost = 0.0
        if detected:
            boost = (
                base
                * float(getattr(config, "QUICK_NEWS_NEW_HEADLINE_MULTIPLIER", 0.25) or 0)
                * math.exp(
                    -max(0.0, now - detected)
                    / max(
                        1.0,
                        float(
                            getattr(
                                config,
                                "QUICK_NEWS_NEW_HEADLINE_DECAY_SECONDS",
                                120,
                            )
                            or 120
                        ),
                    )
                )
            )
        signal["freshnessWeight"] = round(freshness, 5)
        signal["score"] = round(min(100.0, base), 2)
        signal["newNewsBoost"] = round(min(100.0 - signal["score"], boost), 2)
        return signal

    def update_detailed_news_signal(self, payload: dict) -> None:
        """Let an interacted-with full analysis refine subsequent quick scoring."""
        articles = list(payload.get("articles") or [])
        if not articles:
            return
        article = max(articles, key=lambda item: float(item.get("currentScore") or 0))
        sym = str(payload.get("symbol") or "").upper()
        if not sym:
            return
        now = time.time()
        cache_key = self.security_identity(sym).cache_key
        identity = news_scoring.quick_signal(
            sym,
            {
                "headline": article.get("headline"),
                "url": article.get("url"),
                "source": article.get("publisher"),
                "published": article.get("publishedTimestamp"),
                "score": article.get("catalystStrength"),
            },
            now=now,
        )
        with self._quick_news_lock:
            previous = dict(self._quick_news_cache.get(cache_key) or {})
            self._quick_news_cache[cache_key] = {
                "symbol": sym, "hasNews": True, "headline": article.get("headline"),
                "url": article.get("url"), "publisher": article.get("publisher"),
                "publishedAt": article.get("publishedAt"),
                "publishedTimestamp": article.get("publishedTimestamp"),
                "catalystType": article.get("catalystType"),
                "catalystStrength": article.get("catalystStrength", 0),
                "sourceConfidence": article.get("sourceConfidence", 0),
                "decayConstantSeconds": article.get("decayConstantSeconds", 1800),
                "direction": str(article.get("direction") or "neutral").lower(),
                "articleId": identity.get("articleId"), "isNew": False,
                "isDuplicate": identity.get("articleId") == previous.get("articleId"),
                "detectedAt": previous.get("detectedAt"), "checkedAt": now,
                "modelVersion": config.NEWS_SCORING_MODEL_VERSION,
                "fromDetailedAnalysis": True,
            }

    def _apply_quick_news_to_row(self, row: dict, now: float) -> None:
        signal = self.quick_news_signal(row.get("ticker", ""), now=now)
        if not signal:
            row["quick_news"] = False
            return
        row.update(
            {
                "quick_news": True,
                "quick_news_preliminary": not bool(signal.get("fromDetailedAnalysis")),
                "quick_news_score": signal.get("score", 0),
                "quick_news_boost": signal.get("newNewsBoost", 0),
                "quick_news_direction": signal.get("direction", "neutral"),
                "quick_news_headline": signal.get("headline", ""),
                "quick_news_publisher": signal.get("publisher", ""),
                "quick_news_published": signal.get("publishedTimestamp"),
                "quick_news_article_id": signal.get("articleId"),
            }
        )
        row["news_score"] = round(
            min(
                100.0,
                (
                    float(signal.get("score") or 0)
                    + float(signal.get("newNewsBoost") or 0)
                )
                * float(getattr(config, "QUICK_NEWS_WEIGHT", 1.0) or 1.0),
            ),
            1,
        )

    def _queue_filtered_symbol_for_news(self, ticker: str, quote: dict) -> None:
        if not getattr(config, "FILTERED_SYMBOL_NEWS_DEEP_DIVE_ENABLED", False):
            return
        reason = ""
        last = quote.get("last")
        if last is None:
            reason = "no data"
        passes, universe_reason = self._passes_universe(quote)
        if not passes:
            reason = universe_reason or reason or "universe filter"
        if not reason:
            return
        try:
            volume = float(quote.get("volume") or 0.0)
        except (TypeError, ValueError):
            volume = 0.0
        min_volume = float(
            getattr(config, "FILTERED_SYMBOL_NEWS_DEEP_DIVE_MIN_VOLUME", 0) or 0
        )
        if volume < min_volume:
            return
        sym = ticker.upper()
        self._filtered_news_candidates[sym] = {
            "ticker": sym,
            "reason": reason,
            "volume": volume,
            "updated": time.time(),
        }

    def _run_active_symbol_news_deep_dives(self, symbols: list[str]) -> None:
        if not getattr(config, "ACTIVE_SYMBOL_NEWS_DEEP_DIVE_ENABLED", False):
            return
        now = time.time()
        recheck_seconds = int(
            getattr(config, "ACTIVE_SYMBOL_NEWS_DEEP_DIVE_RECHECK_SECONDS", 60) or 60
        )
        max_per_cycle = int(
            getattr(config, "ACTIVE_SYMBOL_NEWS_DEEP_DIVE_MAX_PER_CYCLE", 40) or 40
        )
        bonus = int(getattr(config, "ACTIVE_SYMBOL_NEWS_DEEP_DIVE_BONUS_SCORE", 8) or 8)
        source_name = str(
            getattr(
                config,
                "ACTIVE_SYMBOL_NEWS_DEEP_DIVE_SOURCE",
                "Active Symbol News Deep Dive",
            )
        )
        ranked_symbols: list[tuple[float, str]] = []
        quotes = self.store.quotes_snapshot()
        for sym_raw in symbols:
            sym = str(sym_raw or "").upper()
            if not sym:
                continue
            q = quotes.get(sym, {})
            vol = float(q.get("volume") or 0.0)
            dv = float((q.get("last") or 0.0) * (q.get("volume") or 0.0))
            ranked_symbols.append((dv if dv > 0 else vol, sym))
        ranked_symbols.sort(reverse=True)
        processed = 0
        for _, sym in ranked_symbols:
            last_checked = float(self._active_news_last_check.get(sym, 0.0))
            if (now - last_checked) < recheck_seconds:
                continue
            news = self._research_rvol_news(sym)
            self._active_news_last_check[sym] = now
            processed += 1
            if not news:
                if processed >= max_per_cycle:
                    break
                continue
            self.store.add(
                {
                    "ticker": sym,
                    "headline": news.get("headline") or f"{sym} active-symbol deep-dive news",
                    "url": news.get("url", "#"),
                    "source": source_name,
                    "published": time.time(),
                    "body": f"Active scanner deep-dive refresh. {news.get('summary', '')}".strip(),
                    "score": max(int(config.MIN_SCORE), int(news.get("score", 0)) + bonus),
                    "tags": ["Active deep-dive"] + list(news.get("tags", []))[:2],
                    "dilution": [],
                    "distress": [],
                }
            )
            if processed >= max_per_cycle:
                break

    def _run_filtered_symbol_news_deep_dives(self) -> None:
        if not getattr(config, "FILTERED_SYMBOL_NEWS_DEEP_DIVE_ENABLED", False):
            return
        now = time.time()
        max_per_cycle = int(
            getattr(config, "FILTERED_SYMBOL_NEWS_DEEP_DIVE_MAX_PER_CYCLE", 20) or 20
        )
        recheck_seconds = int(
            getattr(config, "FILTERED_SYMBOL_NEWS_DEEP_DIVE_RECHECK_SECONDS", 300) or 300
        )
        candidates = sorted(
            self._filtered_news_candidates.values(),
            key=lambda item: float(item.get("volume") or 0.0),
            reverse=True,
        )
        processed = 0
        for candidate in candidates:
            sym = str(candidate.get("ticker") or "").upper()
            if not sym:
                continue
            last_checked = float(self._filtered_news_last_check.get(sym, 0.0))
            if (now - last_checked) < recheck_seconds:
                continue
            news = self._research_rvol_news(sym)
            self._filtered_news_last_check[sym] = now
            processed += 1
            if news:
                bonus = int(
                    getattr(config, "FILTERED_SYMBOL_NEWS_DEEP_DIVE_BONUS_SCORE", 0) or 0
                )
                self.store.add(
                    {
                        "ticker": sym,
                        "headline": news.get("headline")
                        or f"{sym} filtered-symbol deep-dive news",
                        "url": news.get("url", "#"),
                        "source": getattr(
                            config,
                            "FILTERED_SYMBOL_NEWS_DEEP_DIVE_SOURCE",
                            "Filtered Symbol News Deep Dive",
                        ),
                        "published": time.time(),
                        "body": (
                            f"Deep-dive news check while filtered ({candidate.get('reason')}). "
                            f"{news.get('summary', '')}"
                        ).strip(),
                        "score": max(
                            int(config.MIN_SCORE),
                            int(news.get("score", 0)) + bonus,
                        ),
                        "tags": ["Filtered deep-dive"] + list(news.get("tags", []))[:2],
                        "dilution": [],
                        "distress": [],
                    }
                )
            if processed >= max_per_cycle:
                break

    def _maybe_add_volume_momentum_catalyst(self, ticker: str, quote: dict) -> None:
        if not getattr(config, "VOLUME_MOMENTUM_ENABLED", False):
            return
        last = quote.get("last")
        vol = quote.get("volume")
        if last is None or not vol:
            return
        try:
            current_vol = float(vol)
        except (TypeError, ValueError):
            return
        if current_vol < float(config.VOLUME_MOMENTUM_MIN_VOLUME):
            self._last_seen_volume[ticker] = current_vol
            return

        previous = self._last_seen_volume.get(ticker)
        self._last_seen_volume[ticker] = current_vol
        if previous is None or current_vol <= previous:
            return

        growth = current_vol / max(previous, 1.0)
        delta = current_vol - previous
        if growth < float(config.VOLUME_MOMENTUM_MIN_GROWTH):
            return
        if delta < float(config.VOLUME_MOMENTUM_MIN_DELTA):
            return

        news = self._research_rvol_news(ticker.upper())
        base_score = max(int(config.MIN_SCORE), int(config.VOLUME_MOMENTUM_SCORE))
        bonus = int(config.VOLUME_MOMENTUM_NEWS_BONUS_SCORE) if news else 0
        score = max(base_score + bonus, int(news.get("score", 0)) + bonus if news else 0)
        tags = [
            f"Vol >= {int(config.VOLUME_MOMENTUM_MIN_VOLUME):,}",
            "Volume rising",
        ]
        if news:
            tags += list(news.get("tags", []))[:2]
            tags.append("Confirmed news")
        headline = (
            news.get("headline")
            if news
            else f"{ticker.upper()} volume accelerating between scans"
        )
        body = (
            f"Volume momentum detected: {int(previous):,} -> {int(current_vol):,} "
            f"({growth:.2f}x scan-over-scan)."
        )
        if news and news.get("summary"):
            body = f"{body} Confirmed news: {news.get('summary')}"
        self.store.add(
            {
                "ticker": ticker.upper(),
                "headline": headline,
                "url": news.get("url", "#") if news else "#",
                "source": (
                    f"{config.VOLUME_MOMENTUM_SOURCE} + {news.get('source')}"
                    if news
                    else config.VOLUME_MOMENTUM_SOURCE
                ),
                "published": time.time(),
                "body": body,
                "score": score,
                "tags": tags,
                "dilution": [],
                "distress": [],
            }
        )

    def _maybe_add_volume_activity_catalyst(self, ticker: str, quote: dict) -> None:
        if not getattr(config, "VOLUME_ACTIVITY_ENABLED", False):
            return
        last = quote.get("last")
        vol = quote.get("volume")
        if last is None or not vol:
            return
        try:
            current_vol = float(vol)
        except (TypeError, ValueError):
            return
        if current_vol < float(config.VOLUME_ACTIVITY_MIN_VOLUME):
            return
        previous = self._last_seen_volume.get(ticker)
        trend = ""
        if previous is not None and current_vol > previous:
            trend = f" (rising from {int(previous):,})"
        self.store.add(
            {
                "ticker": ticker.upper(),
                "headline": f"{ticker.upper()} high daily volume activity{trend}",
                "url": "#",
                "source": config.VOLUME_ACTIVITY_SOURCE,
                "published": time.time(),
                "body": f"Volume watchlist candidate: {int(current_vol):,} shares traded.",
                "score": max(int(config.MIN_SCORE), int(config.VOLUME_ACTIVITY_SCORE)),
                "tags": [f"Volume >= {int(config.VOLUME_ACTIVITY_MIN_VOLUME):,}"],
                "dilution": [],
                "distress": [],
            }
        )

    def _maybe_add_us_market_catalyst(self, ticker: str, quote: dict) -> None:
        if not getattr(config, "US_MARKET_SCAN_ENABLED", False):
            return
        last = quote.get("last")
        vol = quote.get("volume")
        exchange = quote.get("exchange")
        if last is None or not vol:
            return
        try:
            price = float(last)
            current_vol = float(vol)
        except (TypeError, ValueError):
            return
        change_pct = quote.get("change_pct")
        try:
            if change_pct is None or float(change_pct) <= float(
                getattr(config, "US_MARKET_CATALYST_MIN_CHANGE_PCT", 5.0)
            ):
                return
        except (TypeError, ValueError):
            return
        rvol = _rvol_for_quote(quote)
        if rvol is None or rvol <= float(
            getattr(config, "US_MARKET_CATALYST_MIN_RVOL", 0.75)
        ):
            return
        if price < float(getattr(config, "US_MARKET_SCAN_MIN_PRICE", 0.4)):
            return
        if current_vol < float(getattr(config, "US_MARKET_SCAN_MIN_VOLUME", 350_000)):
            return
        if getattr(config, "US_MAJOR_EXCHANGE_ONLY", False) and exchange:
            if not self._is_major_exchange(exchange):
                return
        dollar_volume = price * current_vol
        if dollar_volume < float(getattr(config, "US_MARKET_SCAN_MIN_DOLLAR_VOLUME", 1_500_000)):
            return
        previous = self._last_seen_volume.get(ticker)
        trend = ""
        if previous is not None and current_vol > previous:
            trend = f" with rising volume ({int(previous):,}->{int(current_vol):,})"
        self.store.add(
            {
                "ticker": ticker.upper(),
                "headline": f"{ticker.upper()} elevated U.S. market activity{trend}",
                "url": "#",
                "source": config.US_MARKET_SCAN_SOURCE,
                "published": time.time(),
                "body": (
                    f"Major exchange activity: price ${price:.2f}, "
                    f"volume {int(current_vol):,}, dollar volume ${int(dollar_volume):,}."
                ),
                "score": max(int(config.MIN_SCORE), int(getattr(config, "US_MARKET_SCAN_SCORE", 18))),
                "tags": ["US major exchange", "Scalp activity"],
                "dilution": [],
                "distress": [],
            }
        )


    # -- read side ---------------------------------------------------------
    @staticmethod
    def _passes_universe(quote: dict) -> tuple[bool, str]:
        """Micro/small-cap gate. Unknown fundamentals are configurable."""
        cap = quote.get("market_cap")
        price = quote.get("last")
        flt = quote.get("float_shares")
        exchange = quote.get("exchange")

        if cap is None and price is None:
            return (config.KEEP_UNKNOWN_FUNDAMENTALS, "no data")
        if getattr(config, "US_MAJOR_EXCHANGE_ONLY", False) and exchange:
            if not Scanner._is_major_exchange(exchange):
                return False, "non-major exchange"
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

    @staticmethod
    def _prefer_row(candidate: dict, current: dict) -> bool:
        candidate_filtered = bool(candidate.get("filtered_reason"))
        current_filtered = bool(current.get("filtered_reason"))
        if candidate_filtered != current_filtered:
            return not candidate_filtered
        candidate_heat = float(candidate.get("heat") or 0.0)
        current_heat = float(current.get("heat") or 0.0)
        if candidate_heat != current_heat:
            return candidate_heat > current_heat
        candidate_score = int(candidate.get("score") or 0)
        current_score = int(current.get("score") or 0)
        if candidate_score != current_score:
            return candidate_score > current_score
        return float(candidate.get("age_seconds") or 0.0) < float(
            current.get("age_seconds") or 0.0
        )

    def rows(
        self,
        include_filtered: bool = False,
        strategy_profile: str | None = None,
        ttl_seconds: int | None = None,
        change_min: float | None = None,
        rvol_min: float | None = None,
    ) -> list[dict]:
        """Assemble the board: alerts joined to quotes, ranked by heat."""
        build_started = time.perf_counter()
        rows_by_ticker: dict[str, dict] = {}
        rejected = 0
        score_calculations = 0
        now = time.time()
        ttl = int(ttl_seconds) if ttl_seconds is not None else None
        sess = _session_info()
        active_alerts = self.store.active_latest_per_ticker(ttl=ttl)
        alert_symbols = {str(alert.get("ticker") or "").upper() for alert in active_alerts}
        quotes_by_ticker = self.store.quotes_snapshot(
            change_min=change_min,
            include_symbols=alert_symbols,
        )
        patterns_by_ticker = self._pattern_snapshot()
        self.stats["filter_symbols_evaluated_last"] = len(quotes_by_ticker)
        self.stats["filter_symbols_missing_data_last"] = sum(
            1 for quote in quotes_by_ticker.values() if quote.get("last") is None
        )
        with self._visible_tickers_lock:
            score_focus = set(self._visible_tickers)
        for alert in active_alerts:
            q = quotes_by_ticker.get(alert["ticker"], {})
            pattern = patterns_by_ticker.get(alert["ticker"], {})
            try:
                published_age_seconds = max(
                    0.0,
                    now - float(alert.get("published") or alert.get("first_seen") or now),
                )
            except (TypeError, ValueError):
                published_age_seconds = float(alert.get("age_seconds") or 0.0)

            # Hide symbols with no price data (could be untradeable or delisted).
            # For a news-first dashboard, keep news-only rows visible when the quote
            # provider cannot supply price data (for example NullProvider / no quote
            # service). That way the board still surfaces fresh catalysts even when
            # market data is temporarily unavailable.
            last = q.get("last")
            distress = alert.get("distress") or []
            filtered_reason = ""
            filtered = False
            allow_news_only = isinstance(self.provider, quotes_mod.NullProvider)
            if last is None:
                if not allow_news_only:
                    filtered = True
                    filtered_reason = "no data"
            if any("delist" in (d or "").lower() for d in distress):
                filtered = True
                filtered_reason = "delisting risk"

            passes, reason = self._passes_universe(q)
            if not passes:
                filtered = True
                filtered_reason = reason or filtered_reason

            if filtered and not include_filtered:
                rejected += 1
                continue

            avg_vol = q.get("avg_volume")
            vol = q.get("volume")
            rvol = _rvol_for_quote(q)
            if change_min is not None:
                try:
                    if q.get("change_pct") is None or float(q["change_pct"]) <= change_min:
                        continue
                except (TypeError, ValueError):
                    continue
            if rvol_min is not None and (rvol is None or rvol <= rvol_min):
                continue
            dollar_volume = (q.get("last") * vol) if (q.get("last") and vol) else None
            execution = _execution_metrics(q)
            market_cap = q.get("market_cap")
            shares_out = q.get("shares_out")
            if market_cap is None and q.get("last") and shares_out:
                try:
                    market_cap = float(q.get("last")) * float(shares_out)
                except (TypeError, ValueError):
                    market_cap = None
            float_shares = q.get("float_shares") or shares_out
            source_text = str(alert.get("source", ""))
            if (
                "Relative Volume Scanner" in source_text
                or "Independent RVOL Scanner" in source_text
            ):
                if rvol is None or rvol < float(config.RVOL_CATALYST_THRESHOLD):
                    continue

            row = {
                **alert,
                "news_score": int(alert.get("score", 0) or 0),
                "published_age_seconds": published_age_seconds,
                "name": q.get("name"),
                "exchange": q.get("exchange"),
                "last": q.get("last"),
                "change_pct": q.get("change_pct"),
                "change_session": q.get("change_session"),
                "change_baseline": q.get("change_baseline"),
                "change_baseline_type": q.get("change_baseline_type"),
                "change_status": q.get("change_status"),
                "change_pct_3m": q.get("change_pct_3m"),
                "change_pct_10m": q.get("change_pct_10m"),
                "volume": vol,
                "dollar_volume": dollar_volume,
                "avg_volume": avg_vol,
                "rvol": round(rvol, 2) if rvol is not None else None,
                "market_cap": market_cap,
                "float_shares": float_shares,
                "quote_age": (now - q["updated"]) if q.get("updated") else None,
                "provider_timestamp": q.get("provider_timestamp") or q.get("last_trade_ts"),
                "received_at": q.get("received_at"),
                "accepted_generation": q.get("accepted_generation"),
                "metric_status": {
                    "rvol": "valid" if rvol is not None else "insufficient_data",
                    "spread": execution["spread_status"],
                    "distance_from_high": execution["distance_from_high_status"],
                },
                **execution,
                "scan_change_delta": q.get("scan_change_delta"),
                "scan_change_accel": q.get("scan_change_accel"),
                "scan_volume_delta": q.get("scan_volume_delta"),
                "scan_volume_accel": q.get("scan_volume_accel"),
                "scan_rvol_delta": q.get("scan_rvol_delta"),
                "filtered_reason": filtered_reason,
                "session": sess.get("type", "regular"),
                "market_state": q.get("market_state", "REGULAR"),
                **self._webull_identity_fields(alert["ticker"], q),
                **pattern,
            }
            row.update(self.scanner_membership(row["ticker"], now=now))
            self._apply_quick_news_to_row(row, now)
            if not score_focus or row["ticker"] in score_focus:
                row["score"] = int(round(scoring.scalp_score(row, strategy_profile)))
                score_calculations += 1
            else:
                # Lightweight fallback for non-focused symbols to keep compute low.
                row["score"] = int(row.get("news_score", 0) or 0)
            row["heat"] = round(scoring.heat(row), 1)
            existing = rows_by_ticker.get(row["ticker"])
            if existing is None or self._prefer_row(row, existing):
                rows_by_ticker[row["ticker"]] = row

        # Include quote-only rows for full-universe visibility so scanner output can
        # be filtered directly from current market data even when no news alert exists.
        for ticker, q in quotes_by_ticker.items():
            symbol = str(ticker or "").upper()
            pattern = patterns_by_ticker.get(symbol, {})
            if not symbol or symbol in rows_by_ticker:
                continue
            last = q.get("last")
            if last is None:
                continue
            filtered_reason = ""
            passes, reason = self._passes_universe(q)
            if not passes:
                filtered_reason = reason or "filtered"
            if filtered_reason and not include_filtered:
                rejected += 1
                continue
            vol = q.get("volume")
            avg_vol = q.get("avg_volume")
            rvol = _rvol_for_quote(q)
            if change_min is not None:
                try:
                    if q.get("change_pct") is None or float(q["change_pct"]) <= change_min:
                        continue
                except (TypeError, ValueError):
                    continue
            if rvol_min is not None and (rvol is None or rvol <= rvol_min):
                continue
            dollar_volume = (last * vol) if (last and vol) else None
            execution = _execution_metrics(q)
            market_cap = q.get("market_cap")
            shares_out = q.get("shares_out")
            if market_cap is None and last and shares_out:
                try:
                    market_cap = float(last) * float(shares_out)
                except (TypeError, ValueError):
                    market_cap = None
            float_shares = q.get("float_shares") or shares_out
            updated_at = float(q.get("updated") or now)
            age_seconds = max(0.0, now - updated_at)
            row = {
                "id": f"market-quote-{symbol}",
                "ticker": symbol,
                "headline": f"{symbol} U.S. market quote snapshot",
                "url": "#",
                "source": config.US_MARKET_SCAN_SOURCE,
                "sources": [config.US_MARKET_SCAN_SOURCE],
                "published": updated_at,
                "first_seen": updated_at,
                "age_seconds": age_seconds,
                "published_age_seconds": age_seconds,
                "ttl_fraction": 1.0,
                "news_score": 0,
                "name": q.get("name"),
                "exchange": q.get("exchange"),
                "last": last,
                "change_pct": q.get("change_pct"),
                "change_session": q.get("change_session"),
                "change_baseline": q.get("change_baseline"),
                "change_baseline_type": q.get("change_baseline_type"),
                "change_status": q.get("change_status"),
                "change_pct_3m": q.get("change_pct_3m"),
                "change_pct_10m": q.get("change_pct_10m"),
                "volume": vol,
                "dollar_volume": dollar_volume,
                "avg_volume": avg_vol,
                "rvol": round(rvol, 2) if rvol is not None else None,
                "market_cap": market_cap,
                "float_shares": float_shares,
                "quote_age": age_seconds,
                "provider_timestamp": q.get("provider_timestamp") or q.get("last_trade_ts"),
                "received_at": q.get("received_at"),
                "accepted_generation": q.get("accepted_generation"),
                "metric_status": {
                    "rvol": "valid" if rvol is not None else "insufficient_data",
                    "spread": execution["spread_status"],
                    "distance_from_high": execution["distance_from_high_status"],
                },
                **execution,
                "scan_change_delta": q.get("scan_change_delta"),
                "scan_change_accel": q.get("scan_change_accel"),
                "scan_volume_delta": q.get("scan_volume_delta"),
                "scan_volume_accel": q.get("scan_volume_accel"),
                "scan_rvol_delta": q.get("scan_rvol_delta"),
                "filtered_reason": filtered_reason,
                "session": sess.get("type", "regular"),
                "market_state": q.get("market_state", "REGULAR"),
                **pattern,
                "score": 0,
                "tags": ["US market quote"],
                "dilution": [],
                "distress": [],
                "body": "Quote-only row generated from full U.S. market scan coverage.",
                **self._webull_identity_fields(symbol, q),
            }
            row.update(self.scanner_membership(symbol, now=now))
            self._apply_quick_news_to_row(row, now)
            if not score_focus or symbol in score_focus:
                row["score"] = max(0, int(round(scoring.scalp_score(row, strategy_profile))))
                score_calculations += 1
            else:
                row["score"] = 0
            row["heat"] = round(scoring.heat(row), 1)
            rows_by_ticker[symbol] = row

        self.stats["rejected_by_filter"] = rejected
        rows = list(rows_by_ticker.values())
        rows.sort(key=lambda r: r["heat"], reverse=True)

        # Update the visible ticker list so the filtered scan focuses on
        # exactly these symbols for its next chart-API refresh cycle.
        visible = [r["ticker"] for r in rows if not r.get("filtered_reason")]
        with self._visible_tickers_lock:
            self._visible_tickers = visible
        self.stats["score_calculations_last"] = score_calculations
        self.stats["rows_build_duration_seconds"] = time.perf_counter() - build_started
        return rows

    def health(self, ttl_seconds: int | None = None) -> dict:
        ttl = int(ttl_seconds) if ttl_seconds is not None else config.ALERT_TTL_SECONDS
        session = _session_info()
        session_type = session.get("type", "closed")
        alerts_active, tickers_active = self.store.active_counts(ttl=ttl)
        filtered_interval = max(
            2, int(getattr(config, "FILTERED_SCAN_SECONDS", 3) or 3)
        )
        filtered_started = float(self.stats.get("filtered_scan_started_at") or 0.0)
        filtered_actual = self.stats.get("filtered_scan_interval_actual_seconds")
        now = time.time()
        filtered_overdue = bool(
            session_type != "closed"
            and filtered_started
            and (now - filtered_started) > max(filtered_interval + 2, filtered_interval * 1.5)
        )
        return {
            "uptime_seconds": round(time.time() - self.stats["started"], 1),
            "alerts_total": self.stats["alerts_total"],
            "alerts_active": alerts_active,
            "tickers_active": tickers_active,
            "rejected_by_filter": self.stats["rejected_by_filter"],
            "us_market_universe_size": int(self.stats.get("us_market_universe_size", 0)),
            "market_symbols_scanned_last": int(self.stats.get("market_symbols_scanned_last", 0)),
            "market_batches_last": int(self.stats.get("market_batches_last", 0)),
            "market_coverage_symbols": int(self.stats.get("market_coverage_symbols", 0)),
            "market_coverage_percent": float(self.stats.get("market_coverage_percent", 0.0)),
            "market_coverage_completed": int(self.stats.get("market_coverage_completed", 0)),
            "market_last_full_coverage_at": self.stats.get("market_last_full_coverage_at"),
            "market_last_full_coverage_seconds": self.stats.get(
                "market_last_full_coverage_seconds"
            ),
            "market_coverage_target_seconds": int(
                getattr(config, "US_MARKET_SCAN_TARGET_FULL_COVERAGE_SECONDS", 300)
                or 300
            ),
            "market_quotes_resolved_last": int(
                self.stats.get("market_quotes_resolved_last", 0)
            ),
            "market_quotes_missing_last": int(
                self.stats.get("market_quotes_missing_last", 0)
            ),
            "market_quote_availability_percent": float(
                self.stats.get("market_quote_availability_percent", 0.0)
            ),
            "market_quote_retry_pending": len(self._market_pending_retry),
            "market_bulk_gaps_last": int(self.stats.get("market_bulk_gaps_last", 0)),
            "market_bulk_gap_fallback_last": int(
                self.stats.get("market_bulk_gap_fallback_last", 0)
            ),
            "market_bulk_gap_fallback_resolved_last": int(
                self.stats.get("market_bulk_gap_fallback_resolved_last", 0)
            ),
            "full_pass_rvol_candidates": int(
                self.stats.get("full_pass_rvol_candidates", 0)
            ),
            "full_pass_rvol_enriched": int(
                self.stats.get("full_pass_rvol_enriched", 0)
            ),
            "session_change_candidates": int(
                self.stats.get("session_change_candidates", 0)
            ),
            "session_change_refined": int(
                self.stats.get("session_change_refined", 0)
            ),
            "market_quote_retry_backoff_seconds": round(
                max(0.0, self._market_retry_backoff_until - time.time()), 1
            ),
            "filtered_scan_interval_configured_seconds": filtered_interval,
            "filtered_scan_interval_actual_seconds": (
                round(float(filtered_actual), 3)
                if filtered_actual is not None
                else None
            ),
            "filtered_scan_duration_seconds": round(
                float(self.stats.get("filtered_scan_duration_seconds") or 0.0), 3
            ),
            "filtered_scan_symbols_last": int(
                self.stats.get("filtered_scan_symbols_last", 0)
            ),
            "filter_symbols_evaluated_last": int(
                self.stats.get("filter_symbols_evaluated_last", 0)
            ),
            "filter_symbols_missing_data_last": int(
                self.stats.get("filter_symbols_missing_data_last", 0)
            ),
            "score_calculations_last": int(
                self.stats.get("score_calculations_last", 0)
            ),
            "rows_build_duration_seconds": round(
                float(self.stats.get("rows_build_duration_seconds", 0.0)), 4
            ),
            "market_generation": self._market_generation,
            "market_updates_accepted": int(self.stats.get("market_updates_accepted", 0)),
            "market_updates_dropped_stale": int(
                self.stats.get("market_updates_dropped_stale", 0)
            ),
            "market_updates_dropped_future": int(
                self.stats.get("market_updates_dropped_future", 0)
            ),
            "market_updates_duplicate": int(
                self.stats.get("market_updates_duplicate", 0)
            ),
            "filtered_scan_started_at": filtered_started or None,
            "filtered_scan_overdue": filtered_overdue,
            "active_filtered_symbols": int(
                self.stats.get("active_filtered_symbols", 0)
            ),
            "quick_news_queue_depth": self._quick_news_queue.qsize(),
            "quick_news_checks": int(self.stats.get("quick_news_checks", 0)),
            "force_scan_running": self.force_scan_running(),
            "quotes_cached": self.store.quote_count(),
            "us_market_symbol_sources": dict(self.stats.get("us_market_symbol_sources", {})),
            "provider": type(self.provider).__name__,
            "ttl_hours": ttl / 3600,
            "market_session": session_type,
            "market_session_label": _session_label(session_type),
            "is_trading_window": session_type != "closed",
            "live_scan_hours_et": "Mon-Fri 04:00-20:00",
            "feeds": self.feeds.status(),
        }

    # -- demo --------------------------------------------------------------
    def seed_demo(self) -> None:
        """Load sample headlines so the board is populated without waiting."""
        samples = [
            (
                "Cellect Biotech Announces FDA Approval of ARX-4 for Relapsed AML",
                "Cellect Biotechnology (NASDAQ: CLBT) today announced FDA approval.",
                "GlobeNewswire",
            ),
            (
                "Nuvectra Signs "
                "$210 Million Contract Award with U.S. Department of Defense",
                "Nuvectra Corp (NYSE American: NVTR) received the award.",
                "BusinessWire",
            ),
            (
                "Applied UV Sciences to be Acquired by Halma plc "
                "in All-Cash Transaction",
                "Applied UV (NASDAQ: AUVI) entered a definitive merger agreement.",
                "PR Newswire",
            ),
            (
                "Greenland Acquisition Reports Record Revenue, "
                "Raises Full-Year Guidance",
                "Greenland (NASDAQ: GLAC) reported record quarterly revenue.",
                "ACCESSWIRE",
            ),
            (
                "Siebert Financial Announces Pricing of $12.0 Million Public Offering",
                "Siebert (NASDAQ: SIEB) priced an underwritten public offering.",
                "GlobeNewswire",
            ),
            (
                "Protara Therapeutics Announces Positive Topline Phase 2 Results",
                "Protara (NASDAQ: TARA) met the primary endpoint.",
                "GlobeNewswire",
            ),
        ]
        for title, body, source in samples:
            from . import tickers as tk

            syms = tk.extract(f"{title} {body}")
            result = scoring.score(title, body, 1.05)
            for sym in syms:
                self.store.add(
                    {
                        "ticker": sym,
                        "headline": title,
                        "url": "#",
                        "source": source,
                        "published": time.time(),
                        "body": body,
                        **{
                            k: result[k]
                            for k in ("score", "tags", "dilution", "distress")
                        },
                    }
                )
