# Quotes provider (copied from top-level quotes.py)
from __future__ import annotations

import logging
import re
import threading
import time
import requests

from . import config

log = logging.getLogger("scanner.quotes")


class QuoteProvider:
    """Interface. Implement these two methods to plug in any data source."""

    def fundamentals(self, ticker: str) -> dict:
        """{market_cap, float_shares, avg_volume, exchange, name} — cached."""
        raise NotImplementedError

    def quote(self, ticker: str) -> dict:
        """{last, change_pct, volume, bid, ask} — live."""
        raise NotImplementedError

    def quotes(self, tickers: list[str]) -> dict[str, dict]:
        """Batch fetch. Override when the API supports multi-symbol calls."""
        return {t: self.quote(t) for t in tickers}


class NullProvider(QuoteProvider):
    """No market data. Catalyst scoring only — useful for testing feeds."""

    def fundamentals(self, ticker):
        return {}

    def quote(self, ticker):
        return {}


class YFinanceProvider(QuoteProvider):
    """Works with no API key. Fine for a few dozen symbols at 5s."""

    def __init__(self):
        import yfinance  # imported lazily so the app runs without it

        self._yf = yfinance
        self._http = requests.Session()
        self._fund_cache: dict[str, dict] = {}
        self._fund_lock = threading.Lock()

    def fundamentals(self, ticker: str) -> dict:
        key = str(ticker or "").upper()
        if not key:
            return {}
        with self._fund_lock:
            cached = self._fund_cache.get(key)
        if cached and time.time() - cached.get("_at", 0) < config.FUNDAMENTALS_REFRESH_SECONDS:
            if (
                cached.get("market_cap") is not None
                and cached.get("float_shares") is not None
            ):
                return cached
            # cache is incomplete; force a refresh attempt

        data: dict = {"_at": time.time()}
        if cached:
            for k in (
                "name",
                "exchange",
                "market_cap",
                "float_shares",
                "shares_out",
                "avg_volume",
                "sector",
            ):
                if cached.get(k) is not None:
                    data[k] = cached.get(k)
        try:
            tk = self._yf.Ticker(key)
            try:
                fi = tk.fast_info
            except Exception:
                fi = {}
            data.update(
                {
                    "exchange": data.get("exchange")
                    or fi.get("exchange"),
                    "market_cap": fi.get("marketCap")
                    or fi.get("market_cap")
                    or data.get("market_cap"),
                    "avg_volume": fi.get("tenDayAverageVolume")
                    or fi.get("ten_day_average_volume")
                    or fi.get("threeMonthAverageVolume")
                    or fi.get("three_month_average_volume")
                    or data.get("avg_volume"),
                }
            )
            info: dict = {}
            try:
                info = tk.get_info() or {}
            except Exception:
                info = getattr(tk, "info", {}) or {}
            data.update(
                {
                    "name": info.get("shortName") or info.get("longName"),
                    "exchange": info.get("exchange") or data.get("exchange"),
                    "market_cap": info.get("marketCap") or data.get("market_cap"),
                    "float_shares": info.get("floatShares")
                    or info.get("sharesOutstanding")
                    or data.get("float_shares"),
                    "shares_out": info.get("sharesOutstanding") or data.get("shares_out"),
                    "avg_volume": info.get("averageVolume10days")
                    or info.get("averageVolume"),
                    "sector": info.get("sector"),
                }
            )
        except Exception as exc:
            log.debug("fundamentals failed for %s: %s", key, exc)

        if (
            data.get("market_cap") is None
            or data.get("float_shares") is None
            or data.get("exchange") is None
        ):
            finviz_data = self._finviz_fundamentals(key)
            for k, v in finviz_data.items():
                if v is not None and data.get(k) is None:
                    data[k] = v

        with self._fund_lock:
            self._fund_cache[key] = data
        return data

    @staticmethod
    def _parse_scaled_number(text: str | None) -> float | None:
        if not text:
            return None
        raw = str(text).strip().replace(",", "")
        if raw in {"-", "N/A", "NA"}:
            return None
        mult = 1.0
        if raw.endswith("B"):
            mult = 1_000_000_000.0
            raw = raw[:-1]
        elif raw.endswith("M"):
            mult = 1_000_000.0
            raw = raw[:-1]
        elif raw.endswith("K"):
            mult = 1_000.0
            raw = raw[:-1]
        try:
            return float(raw) * mult
        except ValueError:
            return None

    def _finviz_fundamentals(self, ticker: str) -> dict:
        def extract(label: str, html: str) -> str | None:
            pattern = re.compile(
                r"snapshot-td-label\">\s*"
                + re.escape(label)
                + r"\s*</div>.*?snapshot-td-content\"><b>([^<]+)</b>",
                re.IGNORECASE | re.DOTALL,
            )
            m = pattern.search(html)
            if not m:
                return None
            return str(m.group(1) or "").strip()

        try:
            resp = self._http.get(
                "https://finviz.com/quote.ashx",
                params={"t": ticker, "p": "d"},
                headers={"User-Agent": config.USER_AGENT},
                timeout=8,
            )
            if resp.status_code != 200 or not resp.text:
                return {}
            html = resp.text
            market_cap = extract("Market Cap", html)
            shs_float = extract("Shs Float", html)
            shs_out = extract("Shs Outstand", html)
            out = {
                "market_cap": self._parse_scaled_number(market_cap),
                "float_shares": self._parse_scaled_number(shs_float)
                or self._parse_scaled_number(shs_out),
                "shares_out": self._parse_scaled_number(shs_out),
                "exchange": None,
            }
            return out
        except Exception:
            return {}

    def quote(self, ticker: str) -> dict:
        key = str(ticker or "").upper()
        if not key:
            return {}
        try:
            resp = self._http.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{key}",
                params={"interval": "1m", "range": "1d", "includePrePost": "true"},
                headers={"User-Agent": config.USER_AGENT},
                timeout=6,
            )
            if resp.status_code == 200:
                payload = resp.json() if resp.content else {}
                result = (payload.get("chart", {}) or {}).get("result") or []
                if result:
                    r0 = result[0]
                    meta = r0.get("meta") or {}
                    q0 = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
                    vols = q0.get("volume") or []
                    closes = q0.get("close") or []

                    # Determine which trading period we're in from Yahoo's data
                    trading_periods = meta.get("tradingPeriods") or {}
                    current_tp = meta.get("currentTradingPeriod") or {}
                    market_state = meta.get("marketState") or "REGULAR"  # PRE, REGULAR, POST, POSTPOST, CLOSED

                    vol = None
                    if vols:
                        for item in reversed(vols):
                            if item is not None:
                                vol = int(item)
                                break
                    # NOTE: do NOT return per-minute bar avg as avg_volume —
                    # it's a per-minute figure (e.g. 5,000 shares/min) not
                    # daily avg volume (e.g. 1,000,000/day). Fundamentals
                    # provide the correct daily avg_volume via tenDayAverageVolume.

                    # Compute 3-min and 10-min change percentages from 1-min close bars
                    change_pct_3m = None
                    change_pct_10m = None
                    if closes:
                        valid_closes = [(i, float(c)) for i, c in enumerate(closes) if c is not None]
                        if len(valid_closes) >= 2:
                            last_idx, last_close = valid_closes[-1]
                            def _ref_at(n_bars_back):
                                target = last_idx - n_bars_back
                                for idx, c in reversed(valid_closes[:-1]):
                                    if idx <= target:
                                        return c
                                return None
                            ref3 = _ref_at(3)
                            ref10 = _ref_at(10)
                            if ref3 and ref3 != 0:
                                change_pct_3m = (last_close - ref3) / abs(ref3) * 100.0
                            if ref10 and ref10 != 0:
                                change_pct_10m = (last_close - ref10) / abs(ref10) * 100.0

                    # Pick the right price based on market state
                    reg_price = meta.get("regularMarketPrice")
                    prev = meta.get("chartPreviousClose") or meta.get("previousClose")

                    if market_state in ("PRE", "PREPRE"):
                        # Pre-market: use preMarketPrice if available
                        last = meta.get("preMarketPrice") or reg_price
                        chg_pct_raw = meta.get("preMarketChangePercent")
                        if chg_pct_raw is not None:
                            try:
                                # Yahoo returns preMarketChangePercent as a decimal ratio
                                # (e.g. 0.025 for 2.5%). Multiply by 100 to get percent.
                                chg = float(chg_pct_raw) * 100.0
                            except Exception:
                                chg = None
                        elif last is not None and prev:
                            try:
                                chg = (float(last) - float(prev)) / float(prev) * 100.0
                            except Exception:
                                chg = None
                        else:
                            chg = None
                        # Pre-market session volume from bars (regularMarketVolume is 0 until open)
                        session_vol = vol
                    elif market_state in ("POST", "POSTPOST"):
                        # After-hours: use postMarketPrice if available
                        last = meta.get("postMarketPrice") or reg_price
                        chg_pct_raw = meta.get("postMarketChangePercent")
                        if chg_pct_raw is not None:
                            try:
                                # Yahoo returns postMarketChangePercent as a decimal ratio too
                                chg = float(chg_pct_raw) * 100.0
                            except Exception:
                                chg = None
                        elif last is not None and prev:
                            try:
                                chg = (float(last) - float(prev)) / float(prev) * 100.0
                            except Exception:
                                chg = None
                        else:
                            chg = None
                        session_vol = meta.get("regularMarketVolume") or vol
                    else:
                        # Regular session
                        last = reg_price
                        chg = None
                        if last is not None and prev:
                            try:
                                chg = (float(last) - float(prev)) / float(prev) * 100.0
                            except Exception:
                                chg = None
                        session_vol = meta.get("regularMarketVolume") or vol

                    return {
                        "last": float(last) if last is not None else None,
                        "prev_close": float(prev) if prev is not None else None,
                        "change_pct": round(chg, 2) if chg is not None else None,
                        "volume": int(session_vol) if session_vol is not None else vol,
                        "change_pct_3m": round(change_pct_3m, 2) if change_pct_3m is not None else None,
                        "change_pct_10m": round(change_pct_10m, 2) if change_pct_10m is not None else None,
                        "exchange": meta.get("exchangeName") or meta.get("fullExchangeName"),
                        "market_state": market_state,
                    }
        except Exception:
            pass
        try:
            fi = self._yf.Ticker(key).fast_info
            last = fi.get("lastPrice") or fi.get("last_price")
            prev = fi.get("previousClose") or fi.get("previous_close")
            vol = fi.get("lastVolume") or fi.get("last_volume")
            avg_vol = fi.get("tenDayAverageVolume") or fi.get("ten_day_average_volume")
            chg = None
            if last and prev:
                chg = (last - prev) / prev * 100.0
            return {
                "last": last,
                "change_pct": chg,
                "volume": vol,
                "avg_volume": avg_vol,
                "prev_close": prev,
                "market_state": "REGULAR",
            }
        except Exception:
            return {}

    def quotes(self, tickers: list[str]) -> dict[str, dict]:
        if not tickers:
            return {}
        out: dict[str, dict] = {}
        for sym in tickers:
            key = str(sym or "").upper()
            out[key] = self.quote(key)
        return out


class WebullProvider(QuoteProvider):
    """Bridge to a Webull OpenAPI client.

    Pass any object exposing `get_stock_snapshot(category, symbols, ...)` and
    `get_company_profile(category, symbol)`. Point it at your existing adapter
    and this drops straight in.
    """

    def __init__(self, client=None, category: str = "US_STOCK"):
        self.client = client
        self.category = category
        self._fund_cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        if client is None:
            raise ValueError(
                "WebullProvider needs a client. Construct the scanner with "
                "quote_provider=WebullProvider(client=your_adapter)."
            )

    @staticmethod
    def _first(payload):
        if isinstance(payload, dict):
            for key in ("data", "items", "list", "result"):
                if key in payload:
                    return WebullProvider._first(payload[key])
            return payload
        if isinstance(payload, list) and payload:
            return payload[0]
        return {}

    def fundamentals(self, ticker: str) -> dict:
        with self._lock:
            cached = self._fund_cache.get(ticker)
        if cached and time.time() - cached.get("_at", 0) < config.FUNDAMENTALS_REFRESH_SECONDS:
            return cached
        data = {"_at": time.time()}
        try:
            raw = self._first(
                self.client.get_company_profile(category=self.category, symbol=ticker)
            )
            data.update(
                {
                    "name": raw.get("name") or raw.get("companyName"),
                    "exchange": raw.get("exchangeCode") or raw.get("exchange"),
                    "market_cap": _num(raw.get("marketValue") or raw.get("marketCap")),
                    "shares_out": _num(raw.get("totalShares")),
                    "float_shares": _num(
                        raw.get("outstandingShares") or raw.get("floatShares")
                    ),
                    "sector": raw.get("sector"),
                }
            )
        except Exception as exc:
            log.debug("webull fundamentals failed for %s: %s", ticker, exc)
        with self._lock:
            self._fund_cache[ticker] = data
        return data

    def quote(self, ticker: str) -> dict:
        return self.quotes([ticker]).get(ticker, {})

    def quotes(self, tickers: list[str]) -> dict[str, dict]:
        if not tickers:
            return {}
        out: dict[str, dict] = {}
        for i in range(0, len(tickers), 100):  # API caps symbols per call
            chunk = tickers[i : i + 100]
            try:
                raw = self.client.get_stock_snapshot(
                    category=self.category,
                    symbols=",".join(chunk),
                    extend_hour_required=True,
                    overnight_required=False,
                )
                rows = raw.get("data", raw) if isinstance(raw, dict) else raw
                for row in rows if isinstance(rows, list) else []:
                    sym = (row.get("symbol") or "").upper()
                    if not sym:
                        continue
                    last = _num(row.get("close") or row.get("price"))
                    prev = _num(row.get("preClose"))
                    chg = _num(row.get("changeRatio"))
                    out[sym] = {
                        "last": last,
                        "prev_close": prev,
                        "change_pct": (
                            chg * 100.0
                            if chg is not None
                            else (
                                (last - prev) / prev * 100.0 if last and prev else None
                            )
                        ),
                        "volume": _num(row.get("volume")),
                    }
            except Exception as exc:
                log.debug("webull snapshot failed: %s", exc)
        return out


def _num(value):
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def build(name: str | None = None, client=None) -> QuoteProvider:
    name = (name or config.QUOTE_PROVIDER or "none").lower()
    if name == "yfinance":
        try:
            return YFinanceProvider()
        except ImportError:
            log.warning(
                "yfinance not installed — running without market data. "
                "pip install yfinance"
            )
            return NullProvider()
    if name == "webull":
        return WebullProvider(client=client)
    return NullProvider()
