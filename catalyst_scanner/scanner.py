from __future__ import annotations

"""Orchestration: background threads, filtering, and row assembly."""

import datetime
import logging
import re
import threading
import time
import csv
import zoneinfo
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

from . import config, quotes as quotes_mod, rssparse, scoring
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
        self._quote_cursor = 0
        self.stats = {
            "started": time.time(),
            "alerts_total": 0,
            "last_feed_poll": 0.0,
            "last_quote_poll": 0.0,
            "rejected_by_filter": 0,
        }
        self._fund_cache: dict[str, dict] = {}
        self._http = requests.Session()
        self._independent_symbols: list[str] = []
        self._independent_symbols_at = 0.0
        self._rvol_news_cache: dict[str, dict] = {}
        self._last_seen_volume: dict[str, float] = {}
        self._us_market_symbols: list[str] = []
        self._us_market_symbols_at = 0.0
        self._us_market_cursor = 0
        self._us_market_exchange: dict[str, str] = {}
        self._filtered_news_candidates: dict[str, dict] = {}
        self._filtered_news_last_check: dict[str, float] = {}
        self._quote_momentum: dict[str, dict] = {}
        self._active_news_last_check: dict[str, float] = {}
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
        log.info(
            "scanner started: %d feeds, provider=%s",
            len(self.feeds.feeds),
            type(self.provider).__name__,
        )

    def stop(self) -> None:
        self._stop.set()

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

    def _batched_quotes(self, symbols: list[str], chunk_size: int) -> dict[str, dict]:
        if not symbols:
            return {}
        if chunk_size <= 0 or len(symbols) <= chunk_size:
            return self.provider.quotes(symbols)
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i : i + chunk_size]
            try:
                out.update(self.provider.quotes(chunk))
            except Exception:
                for sym in chunk:
                    out.setdefault(sym, {})
        return out

    def _run_market_universe_scan(self) -> None:
        market_scan = self._us_market_scan_symbols()
        if market_scan:
            chunk_size = int(getattr(config, "US_MARKET_SCAN_CHUNK_SIZE", 250) or 250)
            market_live = self._batched_quotes(market_scan, chunk_size)
            for sym in market_scan:
                merged = self._merge_quote_with_fundamentals(sym, market_live.get(sym, {}))
                self.store.set_quote(sym, merged)
                self._maybe_add_us_market_catalyst(sym, merged)
                self._maybe_add_volume_catalyst(sym, merged)

        independent = self._independent_symbols_for_scan()
        if independent:
            independent_live = self._batched_quotes(independent, 200)
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

    def _run_filtered_results_scan(self) -> None:
        # _quote_symbols() now returns only currently-visible tickers + up to 20
        # unquoted new arrivals. No batching/rotation needed — just refresh them all.
        scan_symbols = self._quote_symbols()
        if scan_symbols:
            # Hard cap to prevent runaway in edge cases (e.g. hundreds of new symbols)
            max_symbols = int(getattr(config, "FILTERED_SCAN_MAX_SYMBOLS", 50) or 50)
            scan_symbols = scan_symbols[:max_symbols]
            chunk_size = max(1, int(getattr(config, "FILTERED_SCAN_CHUNK_SIZE", 20) or 20))
            live = self._batched_quotes(scan_symbols, chunk_size)
            for sym in scan_symbols:
                merged = self._merge_quote_with_fundamentals(sym, live.get(sym, {}))
                self.store.set_quote(sym, merged)
                self._maybe_add_volume_catalyst(sym, merged)
                self._maybe_add_volume_momentum_catalyst(sym, merged)
            self._run_active_symbol_news_deep_dives(scan_symbols)
        self._run_filtered_symbol_news_deep_dives()

    def _quote_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not _is_trading_window():
                    # Outside Mon–Fri 4 AM–8 PM ET; sleep and retry
                    self._stop.wait(30.0)
                    continue
                now = time.time()
                market_interval = max(5, int(getattr(config, "US_MARKET_SCAN_SECONDS", 20) or 20))
                filtered_interval = max(2, int(getattr(config, "FILTERED_SCAN_SECONDS", 3) or 3))
                with self._scan_lock:
                    if (now - self._last_filtered_scan_at) >= filtered_interval:
                        self._run_filtered_results_scan()
                        self._last_filtered_scan_at = now
                    if (now - self._last_market_scan_at) >= market_interval:
                        self._run_market_universe_scan()
                        self._last_market_scan_at = now
                self.stats["last_quote_poll"] = time.time()
            except Exception:
                log.exception("quote loop error")
            self._stop.wait(max(0.5, float(getattr(config, "QUOTE_REFRESH_SECONDS", 1) or 1)))

    def force_scan(self) -> dict:
        """Run an immediate scan cycle regardless of market session."""
        ran_filtered = 0
        ran_market = 0
        started_at = time.time()
        with self._scan_lock:
            self._run_filtered_results_scan()
            ran_filtered = 1
            self._run_market_universe_scan()
            ran_market = 1
            now = time.time()
            self._last_filtered_scan_at = now
            self._last_market_scan_at = now
            self.stats["last_quote_poll"] = now
        return {
            "forced": True,
            "started_at": started_at,
            "finished_at": time.time(),
            "scans": {
                "filtered_results": ran_filtered,
                "market_universe": ran_market,
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

    def _merge_quote_with_fundamentals(self, ticker: str, quote_data: dict) -> dict:
        merged = dict(quote_data or {})
        if not merged.get("exchange"):
            ex = self._us_market_exchange.get(ticker.upper())
            if ex:
                merged["exchange"] = ex
        for key, value in self._fundamentals(ticker).items():
            if value is not None or merged.get(key) is None:
                merged[key] = value
        # If the new quote is missing 3m/10m change data (chart API fell back to
        # fast_info), carry forward the previous stored values — they're only a
        # few seconds old and far better than showing '—' every other cycle.
        prev_stored = self.store.get_quote(ticker)
        for k in ("change_pct_3m", "change_pct_10m"):
            if merged.get(k) is None and prev_stored.get(k) is not None:
                # Only carry forward if the stored quote is recent (< 3 minutes)
                age = time.time() - float(prev_stored.get("updated") or 0)
                if age < 180:
                    merged[k] = prev_stored[k]
        merged = self._annotate_quote_momentum(ticker, merged)
        return merged

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

        rvol_now = _rvol(volume_now, avg_now)
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
        change_accel = (
            (change_delta - chg_delta_prev)
            if (isinstance(change_delta, float) and isinstance(chg_delta_prev, float))
            else None
        )
        volume_accel = (
            (volume_delta - vol_delta_prev)
            if (isinstance(volume_delta, float) and isinstance(vol_delta_prev, float))
            else None
        )

        out["scan_change_delta"] = change_delta
        out["scan_change_accel"] = change_accel
        out["scan_volume_delta"] = volume_delta
        out["scan_volume_accel"] = volume_accel
        out["scan_rvol_delta"] = rvol_delta
        out["scan_momentum_updated_at"] = now

        self._quote_momentum[sym] = {
            "change_pct": change_now if change_now is not None else change_prev,
            "volume": volume_now if volume_now is not None else volume_prev,
            "rvol": rvol_now if rvol_now is not None else rvol_prev,
            "change_delta": change_delta if change_delta is not None else chg_delta_prev,
            "volume_delta": volume_delta if volume_delta is not None else vol_delta_prev,
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

    def _load_us_market_symbols(self) -> list[str]:
        urls = [
            "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt",
            "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt",
        ]
        found: set[str] = set()
        exchange_map: dict[str, str] = {}
        def _canonical_symbol(raw: str) -> str:
            # Normalize class/share suffixes to a provider-friendly form
            # (for example BRK.B -> BRK-B).
            return raw.strip().upper().replace(".", "-").replace("/", "-")
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
                        symbol = _canonical_symbol(row.get("Symbol") or "")
                        exchange_name = "NASDAQ"
                        if (row.get("Test Issue") or "N").strip().upper() != "N":
                            continue
                    else:
                        symbol = _canonical_symbol(row.get("ACT Symbol") or "")
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
                    if not symbol:
                        continue
                    if not re.fullmatch(r"[A-Z]{1,5}(?:-[A-Z]{1,2})?", symbol):
                        continue
                    found.add(symbol)
                    if exchange_name:
                        exchange_map[symbol] = exchange_name
            except Exception:
                continue
        self._us_market_exchange = exchange_map
        return sorted(found)

    def _us_market_scan_symbols(self) -> list[str]:
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
        if not self._us_market_symbols:
            return []

        if getattr(config, "US_MARKET_SCAN_FULL_COVERAGE", False):
            return list(self._us_market_symbols)

        batch_size = int(getattr(config, "US_MARKET_SCAN_BATCH_SIZE", 120) or 120)
        start = self._us_market_cursor % len(self._us_market_symbols)
        out: list[str] = []
        for i in range(batch_size):
            out.append(self._us_market_symbols[(start + i) % len(self._us_market_symbols)])
        self._us_market_cursor = (start + batch_size) % len(self._us_market_symbols)
        return out

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
            rvol = _rvol(vol, avg)
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
        now = time.time()
        cached = self._rvol_news_cache.get(ticker)
        ttl = int(getattr(config, "RVOL_NEWS_RESEARCH_CACHE_SECONDS", 60) or 60)
        if cached and (now - float(cached.get("_at", 0))) < ttl:
            return cached.get("news")

        url = (
            "https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={ticker}&region=US&lang=en-US"
        )
        best: dict | None = None
        try:
            resp = self._http.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=10)
            if resp.status_code == 200:
                entries = rssparse.parse(resp.content)
                cutoff = now - (float(config.MAX_NEWS_AGE_HOURS) * 3600.0)
                for e in entries[:25]:
                    pub = float(e.get("published") or 0)
                    if pub and pub < cutoff:
                        continue
                    title = e.get("title", "")
                    summary = e.get("summary", "")
                    scored = scoring.score(title, summary, 1.0)
                    if scored.get("junk"):
                        continue
                    if int(scored.get("score", 0)) < int(config.MIN_SCORE):
                        continue
                    candidate = {
                        "headline": title,
                        "summary": summary[:220],
                        "url": e.get("link", "#"),
                        "source": "Yahoo Finance",
                        "score": int(scored.get("score", 0)),
                        "tags": list(scored.get("tags", [])),
                    }
                    if best is None or candidate["score"] > best["score"]:
                        best = candidate
        except Exception:
            best = None

        self._rvol_news_cache[ticker] = {"_at": now, "news": best}
        return best

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
    ) -> list[dict]:
        """Assemble the board: alerts joined to quotes, ranked by heat."""
        rows_by_ticker: dict[str, dict] = {}
        rejected = 0
        ttl = int(ttl_seconds) if ttl_seconds is not None else None
        quotes_by_ticker = self.store.quotes_snapshot()
        for alert in self.store.active_latest_per_ticker(ttl=ttl):
            q = quotes_by_ticker.get(alert["ticker"], {})

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
            sess = _session_info()
            rvol = _rvol(vol, avg_vol, session_type=sess["type"])
            dollar_volume = (q.get("last") * vol) if (q.get("last") and vol) else None
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
                "name": q.get("name"),
                "exchange": q.get("exchange"),
                "last": q.get("last"),
                "change_pct": q.get("change_pct"),
                "change_pct_3m": q.get("change_pct_3m"),
                "change_pct_10m": q.get("change_pct_10m"),
                "volume": vol,
                "dollar_volume": dollar_volume,
                "avg_volume": avg_vol,
                "rvol": round(rvol, 2) if rvol is not None else None,
                "market_cap": market_cap,
                "float_shares": float_shares,
                "quote_age": (time.time() - q["updated"]) if q.get("updated") else None,
                "scan_change_delta": q.get("scan_change_delta"),
                "scan_change_accel": q.get("scan_change_accel"),
                "scan_volume_delta": q.get("scan_volume_delta"),
                "scan_volume_accel": q.get("scan_volume_accel"),
                "scan_rvol_delta": q.get("scan_rvol_delta"),
                "filtered_reason": filtered_reason,
                "session": sess.get("type", "regular"),
                "market_state": q.get("market_state", "REGULAR"),
            }
            row["score"] = int(round(scoring.scalp_score(row, strategy_profile)))
            row["heat"] = round(scoring.heat(row), 1)
            existing = rows_by_ticker.get(row["ticker"])
            if existing is None or self._prefer_row(row, existing):
                rows_by_ticker[row["ticker"]] = row

        self.stats["rejected_by_filter"] = rejected
        rows = list(rows_by_ticker.values())
        rows.sort(key=lambda r: r["heat"], reverse=True)

        # Update the visible ticker list so the filtered scan focuses on
        # exactly these symbols for its next chart-API refresh cycle.
        visible = [r["ticker"] for r in rows if not r.get("filtered_reason")]
        with self._visible_tickers_lock:
            self._visible_tickers = visible

        return rows

    def health(self, ttl_seconds: int | None = None) -> dict:
        ttl = int(ttl_seconds) if ttl_seconds is not None else config.ALERT_TTL_SECONDS
        return {
            "uptime_seconds": round(time.time() - self.stats["started"], 1),
            "alerts_total": self.stats["alerts_total"],
            "alerts_active": len(self.store.active(ttl=ttl)),
            "tickers_active": len(self.store.active_tickers(ttl=ttl)),
            "rejected_by_filter": self.stats["rejected_by_filter"],
            "provider": type(self.provider).__name__,
            "ttl_hours": ttl / 3600,
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
