"""Quote + fundamentals providers.

Two rates of change, so two caches:

* Price / change / volume — refreshed every few seconds for tickers on board.
* Market cap / float / avg volume — static enough to fetch once per ticker per
  session. These are what actually gate the micro/small-cap filter, and
  re-pulling them every 5s is what gets you rate-limited.
"""
from __future__ import annotations

import logging
import threading
import time

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
        self._fund_cache: dict[str, dict] = {}
        self._fund_lock = threading.Lock()

    def fundamentals(self, ticker: str) -> dict:
        with self._fund_lock:
            cached = self._fund_cache.get(ticker)
        if cached and time.time() - cached.get("_at", 0) < 3600:
            return cached

        data: dict = {"_at": time.time()}
        try:
            tk = self._yf.Ticker(ticker)
            info = {}
            try:
                info = tk.get_info() or {}
            except Exception:
                info = getattr(tk, "info", {}) or {}
            data.update({
                "name": info.get("shortName") or info.get("longName"),
                "exchange": info.get("exchange"),
                "market_cap": info.get("marketCap"),
                "float_shares": info.get("floatShares"),
                "shares_out": info.get("sharesOutstanding"),
                "avg_volume": info.get("averageVolume10days")
                              or info.get("averageVolume"),
                "sector": info.get("sector"),
            })
        except Exception as exc:
            log.debug("fundamentals failed for %s: %s", ticker, exc)

        with self._fund_lock:
            self._fund_cache[ticker] = data
        return data

    def quote(self, ticker: str) -> dict:
        try:
            fi = self._yf.Ticker(ticker).fast_info
            last = fi.get("lastPrice") or fi.get("last_price")
            prev = fi.get("previousClose") or fi.get("previous_close")
            vol = fi.get("lastVolume") or fi.get("last_volume")
            chg = None
            if last and prev:
                chg = (last - prev) / prev * 100.0
            return {"last": last, "change_pct": chg, "volume": vol,
                    "prev_close": prev}
        except Exception as exc:
            log.debug("quote failed for %s: %s", ticker, exc)
            return {}

    def quotes(self, tickers: list[str]) -> dict[str, dict]:
        if not tickers:
            return {}
        out: dict[str, dict] = {}
        try:
            df = self._yf.download(
                tickers=" ".join(tickers), period="2d", interval="1d",
                progress=False, group_by="ticker", threads=True,
                auto_adjust=False,
            )
            for sym in tickers:
                try:
                    sub = df[sym] if len(tickers) > 1 else df
                    closes = sub["Close"].dropna()
                    vols = sub["Volume"].dropna()
                    if len(closes) >= 2:
                        last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
                        out[sym] = {
                            "last": last,
                            "prev_close": prev,
                            "change_pct": (last - prev) / prev * 100.0 if prev else None,
                            "volume": int(vols.iloc[-1]) if len(vols) else None,
                        }
                except Exception:
                    continue
        except Exception as exc:
            log.debug("batch quote failed: %s", exc)
        for sym in tickers:
            if sym not in out:
                out[sym] = self.quote(sym)
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
        if cached and time.time() - cached.get("_at", 0) < 3600:
            return cached
        data = {"_at": time.time()}
        try:
            raw = self._first(self.client.get_company_profile(
                category=self.category, symbol=ticker))
            data.update({
                "name": raw.get("name") or raw.get("companyName"),
                "exchange": raw.get("exchangeCode") or raw.get("exchange"),
                "market_cap": _num(raw.get("marketValue") or raw.get("marketCap")),
                "shares_out": _num(raw.get("totalShares")),
                "float_shares": _num(raw.get("outstandingShares")
                                     or raw.get("floatShares")),
                "sector": raw.get("sector"),
            })
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
        for i in range(0, len(tickers), 100):   # API caps symbols per call
            chunk = tickers[i:i + 100]
            try:
                raw = self.client.get_stock_snapshot(
                    category=self.category, symbols=",".join(chunk),
                    extend_hour_required=True, overnight_required=False,
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
                        "change_pct": chg * 100.0 if chg is not None
                                      else ((last - prev) / prev * 100.0
                                            if last and prev else None),
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
            log.warning("yfinance not installed — running without market data. "
                        "pip install yfinance")
            return NullProvider()
    if name == "webull":
        return WebullProvider(client=client)
    return NullProvider()
