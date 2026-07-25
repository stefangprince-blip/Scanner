"""Flask front end for the catalyst scanner."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Flask, jsonify, render_template, request

from . import config
from .scanner import Scanner
from . import scoring as scoring_mod

log = logging.getLogger("scanner.app")

SETTINGS_PATH = Path(__file__).with_name("settings.json")
VALID_SCALP_PROFILES = {"balanced", "news_first", "momentum_first"}
VALID_WATCHLIST_PRESETS = {"all", "small_cap_momentum", "biotech_catalyst", "large_cap_liquidation"}


def _bounded_int(value, default: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _ui_refresh_seconds(settings: dict | None = None) -> int:
    return _filtered_scan_seconds(settings)


def _filtered_scan_seconds(settings: dict | None = None) -> int:
    settings = settings or _load_settings()
    raw = settings.get("filtered_scan_seconds", settings.get("ui_refresh_seconds"))
    return _bounded_int(raw, int(config.FILTERED_SCAN_SECONDS), 2, 120)


def _us_market_scan_seconds(settings: dict | None = None) -> int:
    settings = settings or _load_settings()
    return _bounded_int(
        settings.get("us_market_scan_seconds"),
        int(config.US_MARKET_SCAN_SECONDS),
        5,
        600,
    )


def _alert_ttl_seconds(settings: dict | None = None) -> int:
    settings = settings or _load_settings()
    return _bounded_int(
        settings.get("alert_ttl_seconds"),
        int(config.ALERT_TTL_SECONDS),
        10 * 60,
        48 * 60 * 60,
    )


def _load_settings() -> dict:
    try:
        if SETTINGS_PATH.exists():
            return json.loads(SETTINGS_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_settings(payload: dict) -> None:
    try:
        SETTINGS_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True))
    except Exception:
        pass


def _normalized_scalp_profile(value) -> str:
    if value in VALID_SCALP_PROFILES:
        return value
    return config.SCALP_STRATEGY_DEFAULT


def _normalized_watchlist_preset(value) -> str:
    if value in VALID_WATCHLIST_PRESETS:
        return value
    return "all"


def _sanitize_shared_filters(value) -> dict:
    if not isinstance(value, dict):
        return {}
    clean = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            clean[key] = item
    return clean


def _fetch_raw_yahoo_candles(ticker: str, interval: str, data_range: str) -> list[dict]:
    params = urlencode({"interval": interval, "range": data_range})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?{params}"
    req = Request(url, headers={"User-Agent": config.USER_AGENT})
    with urlopen(req, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = (payload.get("chart", {}).get("result") or [])
    if not result:
        return []
    quote = ((result[0].get("indicators") or {}).get("quote") or [])
    if not quote:
        return []
    q0 = quote[0]
    raw_open = q0.get("open") or []
    raw_high = q0.get("high") or []
    raw_low = q0.get("low") or []
    raw_close = q0.get("close") or []
    candles: list[dict] = []
    count = min(len(raw_open), len(raw_high), len(raw_low), len(raw_close))
    for i in range(count):
        o = raw_open[i]
        h = raw_high[i]
        l = raw_low[i]
        c = raw_close[i]
        if o is None or h is None or l is None or c is None:
            continue
        candles.append({"o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    return candles


def _aggregate_3m(one_minute: list[dict]) -> list[dict]:
    if len(one_minute) < 3:
        return []
    blocks: list[dict] = []
    for i in range(0, len(one_minute), 3):
        group = one_minute[i:i + 3]
        if len(group) < 3:
            continue
        blocks.append(
            {
                "o": group[0]["o"],
                "h": max(x["h"] for x in group),
                "l": min(x["l"] for x in group),
                "c": group[-1]["c"],
            }
        )
    return blocks


def _get_chart_candles(ticker: str, requested_interval: str) -> tuple[list[dict] | None, str | None]:
    requested_interval = requested_interval if requested_interval in {"1m", "3m", "5m"} else "1m"
    fallback_order = [requested_interval] + [i for i in ("1m", "3m", "5m") if i != requested_interval]
    candles_per_30m = {"1m": 30, "3m": 10, "5m": 6}
    for interval in fallback_order:
        try:
            if interval == "1m":
                base = _fetch_raw_yahoo_candles(ticker, "1m", "1d")
                candidate = base[-candles_per_30m["1m"]:]
            elif interval == "3m":
                base = _fetch_raw_yahoo_candles(ticker, "1m", "1d")
                candidate = _aggregate_3m(base)
                candidate = candidate[-candles_per_30m["3m"]:]
            else:
                base = _fetch_raw_yahoo_candles(ticker, "5m", "5d")
                candidate = base[-candles_per_30m["5m"]:]
        except Exception:
            continue
        if len(candidate) < 2:
            continue
        return candidate, interval
    return None, None


def analyze_candlestick_patterns(candles: list[dict]) -> dict:
    n = len(candles)
    if n < 2:
        return {
            "candlestick_score": 0,
            "bias": "neutral",
            "matched_patterns": [],
            "bullish_patterns": 0,
            "bearish_patterns": 0,
            "supported_patterns": [],
        }

    supported_patterns = [
        "Bullish Engulfing",
        "Bearish Engulfing",
        "Piercing Line",
        "Dark Cloud Cover",
        "Bullish Harami",
        "Bearish Harami",
        "Tweezer Bottom",
        "Tweezer Top",
        "Morning Star",
        "Evening Star",
        "Three White Soldiers",
        "Three Black Crows",
        "Three Inside Up",
        "Three Inside Down",
        "Three Outside Up",
        "Three Outside Down",
        "Rising Three Methods",
        "Falling Three Methods",
    ]

    def body(c):
        return abs(c["c"] - c["o"])

    def rng(c):
        return max(c["h"] - c["l"], 1e-9)

    def bull(c):
        return c["c"] > c["o"]

    def bear(c):
        return c["c"] < c["o"]

    recent = candles[-min(20, n):]
    avg_body = sum(body(c) for c in recent) / max(1, len(recent))
    long_body = max(avg_body * 1.1, 1e-6)
    small_body = max(avg_body * 0.6, 1e-6)
    matches = []
    seen_names = set()

    def add_match(name: str, bias: str, score: int, bars_ago: int, description: str):
        if name in seen_names:
            return
        seen_names.add(name)
        matches.append(
            {
                "name": name,
                "bias": bias,
                "score": score,
                "bars_ago": bars_ago,
                "description": description,
            }
        )

    start_i = max(1, n - 12)
    for i in range(n - 1, start_i - 1, -1):
        c0 = candles[i]
        c1 = candles[i - 1] if i - 1 >= 0 else None
        c2 = candles[i - 2] if i - 2 >= 0 else None
        c3 = candles[i - 3] if i - 3 >= 0 else None
        c4 = candles[i - 4] if i - 4 >= 0 else None
        bars_ago = n - 1 - i

        if c1 is not None:
            if (
                bear(c1)
                and bull(c0)
                and c0["o"] <= c1["c"]
                and c0["c"] >= c1["o"]
                and body(c0) >= body(c1) * 0.9
            ):
                add_match("Bullish Engulfing", "bullish", 14, bars_ago, "Bull candle fully engulfs prior bear body.")
            if (
                bull(c1)
                and bear(c0)
                and c0["o"] >= c1["c"]
                and c0["c"] <= c1["o"]
                and body(c0) >= body(c1) * 0.9
            ):
                add_match("Bearish Engulfing", "bearish", -14, bars_ago, "Bear candle fully engulfs prior bull body.")

            c1_mid = (c1["o"] + c1["c"]) / 2.0
            if bear(c1) and body(c1) >= long_body and bull(c0) and c0["c"] > c1_mid and c0["c"] < c1["o"]:
                add_match("Piercing Line", "bullish", 12, bars_ago, "Recovery candle closes above midpoint of prior bear candle.")
            if bull(c1) and body(c1) >= long_body and bear(c0) and c0["c"] < c1_mid and c0["c"] > c1["o"]:
                add_match("Dark Cloud Cover", "bearish", -12, bars_ago, "Selloff candle closes below midpoint of prior bull candle.")

            if (
                bear(c1)
                and body(c1) >= long_body
                and body(c0) <= small_body
                and c0["o"] >= min(c1["o"], c1["c"])
                and c0["c"] <= max(c1["o"], c1["c"])
            ):
                add_match("Bullish Harami", "bullish", 8, bars_ago, "Small inside candle after bearish expansion.")
            if (
                bull(c1)
                and body(c1) >= long_body
                and body(c0) <= small_body
                and c0["o"] <= max(c1["o"], c1["c"])
                and c0["c"] >= min(c1["o"], c1["c"])
            ):
                add_match("Bearish Harami", "bearish", -8, bars_ago, "Small inside candle after bullish expansion.")

            low_tol = max(c0["c"], c1["c"], c0["o"], c1["o"]) * 0.0015
            if abs(c0["l"] - c1["l"]) <= low_tol and bull(c0):
                add_match("Tweezer Bottom", "bullish", 9, bars_ago, "Two-session low rejection at similar lows.")
            if abs(c0["h"] - c1["h"]) <= low_tol and bear(c0):
                add_match("Tweezer Top", "bearish", -9, bars_ago, "Two-session high rejection at similar highs.")

        if c2 is not None and c1 is not None:
            c2_mid = (c2["o"] + c2["c"]) / 2.0
            if (
                bear(c2)
                and body(c2) >= long_body
                and body(c1) <= small_body
                and bull(c0)
                and c0["c"] > c2_mid
            ):
                add_match("Morning Star", "bullish", 16, bars_ago, "Three-candle reversal: bear impulse, pause, bull reclaim.")
            if (
                bull(c2)
                and body(c2) >= long_body
                and body(c1) <= small_body
                and bear(c0)
                and c0["c"] < c2_mid
            ):
                add_match("Evening Star", "bearish", -16, bars_ago, "Three-candle reversal: bull impulse, pause, bear reclaim.")

            if (
                bear(c2)
                and body(c1) <= small_body
                and bull(c0)
                and c0["c"] > c2["o"]
            ):
                add_match("Three Inside Up", "bullish", 11, bars_ago, "Bullish harami confirmation with upside break.")
            if (
                bull(c2)
                and body(c1) <= small_body
                and bear(c0)
                and c0["c"] < c2["o"]
            ):
                add_match("Three Inside Down", "bearish", -11, bars_ago, "Bearish harami confirmation with downside break.")

            if (
                bear(c2)
                and bull(c1)
                and c1["o"] <= c2["c"]
                and c1["c"] >= c2["o"]
                and bull(c0)
                and c0["c"] > c1["c"]
            ):
                add_match("Three Outside Up", "bullish", 13, bars_ago, "Engulfing reversal confirmed by follow-through.")
            if (
                bull(c2)
                and bear(c1)
                and c1["o"] >= c2["c"]
                and c1["c"] <= c2["o"]
                and bear(c0)
                and c0["c"] < c1["c"]
            ):
                add_match("Three Outside Down", "bearish", -13, bars_ago, "Engulfing reversal confirmed by follow-through.")

            if (
                bull(c2)
                and bull(c1)
                and bull(c0)
                and c2["c"] < c1["c"] < c0["c"]
                and c1["o"] >= c2["o"]
                and c0["o"] >= c1["o"]
            ):
                add_match("Three White Soldiers", "bullish", 18, bars_ago, "Three strong bullish closes with stair-step continuation.")
            if (
                bear(c2)
                and bear(c1)
                and bear(c0)
                and c2["c"] > c1["c"] > c0["c"]
                and c1["o"] <= c2["o"]
                and c0["o"] <= c1["o"]
            ):
                add_match("Three Black Crows", "bearish", -18, bars_ago, "Three strong bearish closes with stair-step continuation.")

        if c4 is not None and c3 is not None and c2 is not None and c1 is not None:
            if (
                bull(c4)
                and body(c4) >= long_body
                and bear(c3)
                and bear(c2)
                and bear(c1)
                and c3["h"] <= c4["h"]
                and c2["h"] <= c4["h"]
                and c1["h"] <= c4["h"]
                and c3["l"] >= c4["l"]
                and c2["l"] >= c4["l"]
                and c1["l"] >= c4["l"]
                and bull(c0)
                and c0["c"] > c4["h"]
            ):
                add_match("Rising Three Methods", "bullish", 20, bars_ago, "Bull trend pauses with contained pullback then breaks higher.")
            if (
                bear(c4)
                and body(c4) >= long_body
                and bull(c3)
                and bull(c2)
                and bull(c1)
                and c3["h"] <= c4["h"]
                and c2["h"] <= c4["h"]
                and c1["h"] <= c4["h"]
                and c3["l"] >= c4["l"]
                and c2["l"] >= c4["l"]
                and c1["l"] >= c4["l"]
                and bear(c0)
                and c0["c"] < c4["l"]
            ):
                add_match("Falling Three Methods", "bearish", -20, bars_ago, "Bear trend pauses with contained bounce then breaks lower.")

    matches.sort(key=lambda p: (p["bars_ago"], -abs(p["score"])))
    total_score = sum(p["score"] for p in matches)
    total_score = max(-100, min(100, total_score))
    bullish = sum(1 for p in matches if p["bias"] == "bullish")
    bearish = sum(1 for p in matches if p["bias"] == "bearish")
    if total_score > 4:
        bias = "bullish"
    elif total_score < -4:
        bias = "bearish"
    else:
        bias = "neutral"
    return {
        "candlestick_score": total_score,
        "bias": bias,
        "matched_patterns": matches,
        "bullish_patterns": bullish,
        "bearish_patterns": bearish,
        "supported_patterns": supported_patterns,
    }


def create_app(scanner: Scanner) -> Flask:
    app = Flask(__name__)
    app.config["SCANNER"] = scanner
    settings = _load_settings()
    config.FILTERED_SCAN_SECONDS = _filtered_scan_seconds(settings)
    config.UI_REFRESH_SECONDS = config.FILTERED_SCAN_SECONDS
    config.US_MARKET_SCAN_SECONDS = _us_market_scan_seconds(settings)
    config.ALERT_TTL_SECONDS = _alert_ttl_seconds(settings)

    @app.route("/")
    def index():
        settings = _load_settings()
        refresh_seconds = _ui_refresh_seconds(settings)
        us_scan_seconds = _us_market_scan_seconds(settings)
        ttl_seconds = _alert_ttl_seconds(settings)
        return render_template(
            "index.html",
            refresh_ms=refresh_seconds * 1000,
            us_scan_seconds=us_scan_seconds,
            ttl_hours=ttl_seconds / 3600,
            min_score=config.MIN_SCORE,
        )

    @app.route("/api/rows")
    def api_rows():
        include_filtered = request.args.get("all") == "1"
        settings = _load_settings()
        refresh_seconds = _ui_refresh_seconds(settings)
        filtered_scan_seconds = _filtered_scan_seconds(settings)
        us_scan_seconds = _us_market_scan_seconds(settings)
        ttl_seconds = _alert_ttl_seconds(settings)
        strategy_profile = _normalized_scalp_profile(settings.get("scalp_profile_current"))
        rows = scanner.rows(
            include_filtered=include_filtered,
            strategy_profile=strategy_profile,
            ttl_seconds=ttl_seconds,
        )
        return jsonify(
            {
                "rows": rows,
                "count": len(rows),
                "ttl_seconds": ttl_seconds,
                "refresh_seconds": refresh_seconds,
                "filtered_scan_seconds": filtered_scan_seconds,
                "us_market_scan_seconds": us_scan_seconds,
                "health": scanner.health(ttl_seconds=ttl_seconds),
                "profile": strategy_profile or config.SCALP_STRATEGY_DEFAULT,
            }
        )

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        settings = _load_settings()
        settings["filtered_scan_seconds"] = _filtered_scan_seconds(settings)
        settings["ui_refresh_seconds"] = settings["filtered_scan_seconds"]
        settings["us_market_scan_seconds"] = _us_market_scan_seconds(settings)
        settings["scalp_profile_current"] = _normalized_scalp_profile(
            settings.get("scalp_profile_current")
        )
        settings["watchlist_preset"] = _normalized_watchlist_preset(settings.get("watchlist_preset"))
        settings["shared_filters"] = _sanitize_shared_filters(settings.get("shared_filters"))
        if request.method == "POST":
            try:
                payload = request.get_json(force=True) or {}
            except Exception:
                payload = {}
            if "ui_refresh_seconds" in payload:
                payload["filtered_scan_seconds"] = payload.get("ui_refresh_seconds")
            if "filtered_scan_seconds" in payload:
                payload["filtered_scan_seconds"] = _bounded_int(
                    payload.get("filtered_scan_seconds"),
                    int(config.FILTERED_SCAN_SECONDS),
                    2,
                    120,
                )
                payload["ui_refresh_seconds"] = payload["filtered_scan_seconds"]
            if "us_market_scan_seconds" in payload:
                payload["us_market_scan_seconds"] = _bounded_int(
                    payload.get("us_market_scan_seconds"),
                    int(config.US_MARKET_SCAN_SECONDS),
                    5,
                    600,
                )
            if "alert_ttl_seconds" in payload:
                payload["alert_ttl_seconds"] = _bounded_int(
                    payload.get("alert_ttl_seconds"),
                    int(config.ALERT_TTL_SECONDS),
                    10 * 60,
                    48 * 60 * 60,
                )
            if "scalp_profile_current" in payload:
                payload["scalp_profile_current"] = _normalized_scalp_profile(
                    payload.get("scalp_profile_current")
                )
            if "watchlist_preset" in payload:
                payload["watchlist_preset"] = _normalized_watchlist_preset(
                    payload.get("watchlist_preset")
                )
            if "shared_filters" in payload:
                payload["shared_filters"] = _sanitize_shared_filters(payload.get("shared_filters"))
            settings.update(payload)
            _save_settings(settings)
            config.FILTERED_SCAN_SECONDS = _filtered_scan_seconds(settings)
            config.UI_REFRESH_SECONDS = config.FILTERED_SCAN_SECONDS
            config.US_MARKET_SCAN_SECONDS = _us_market_scan_seconds(settings)
            config.ALERT_TTL_SECONDS = _alert_ttl_seconds(settings)
            # make new settings take effect immediately for the current process
            app.config["SCORING_TOGGLES"] = settings
            scoring_mod.set_toggles(settings)
        settings["filtered_scan_seconds"] = _filtered_scan_seconds(settings)
        settings["ui_refresh_seconds"] = settings["filtered_scan_seconds"]
        settings["us_market_scan_seconds"] = _us_market_scan_seconds(settings)
        settings["scalp_profile_current"] = _normalized_scalp_profile(
            settings.get("scalp_profile_current")
        )
        settings["watchlist_preset"] = _normalized_watchlist_preset(settings.get("watchlist_preset"))
        settings["shared_filters"] = _sanitize_shared_filters(settings.get("shared_filters"))
        return jsonify(settings)

    @app.route("/api/health")
    def api_health():
        settings = _load_settings()
        ttl_seconds = _alert_ttl_seconds(settings)
        return jsonify(scanner.health(ttl_seconds=ttl_seconds))

    @app.route("/api/force-scan", methods=["POST"])
    def api_force_scan():
        settings = _load_settings()
        ttl_seconds = _alert_ttl_seconds(settings)
        try:
            started = scanner.trigger_force_scan()
            return jsonify(
                {
                    "ok": True,
                    "started": started,
                    "running": scanner.force_scan_running(),
                    "health": scanner.health(ttl_seconds=ttl_seconds),
                }
            )
        except Exception:
            log.exception("force scan failed")
            return jsonify({"ok": False, "error": "force scan failed"}), 500

    @app.after_request
    def no_cache(resp):
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.before_request
    def _load_scoring_toggles():
        settings = _load_settings()
        app.config.setdefault("SCORING_TOGGLES", settings)
        scoring_mod.set_toggles(settings)

    # Service worker: serve a small JS file at /sw.js so browsers can register for
    # push notifications without requiring a physical static/ file on disk.
    @app.route('/sw.js')
    def service_worker():
        sw = r"""
self.addEventListener('push', function(event) {
  let data = {};
  try { data = event.data.json(); } catch(e) { data = {title: 'Scanner', body: event.data && event.data.text()}; }
  const title = data.title || (data.ticker ? `${data.ticker} ${data.score||''}` : 'Scanner');
  const options = {
    body: data.body || data.headline || '',
    data: data,
    tag: data.id || undefined,
    renotify: true,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  const url = event.notification.data && event.notification.data.url ? event.notification.data.url : '/';
  event.waitUntil(clients.matchAll({type: 'window'}).then(windowClients => {
    for (let i=0;i<windowClients.length;i++){
      const client = windowClients[i];
      if (client.url === url && 'focus' in client) return client.focus();
    }
    if (clients.openWindow) return clients.openWindow(url);
  }));
});
"""
        from flask import Response

        return Response(sw, mimetype='application/javascript')

    # Development helper: trigger a notification from an HTTP POST for testing.
    @app.route('/_dev/notify', methods=['POST'])
    def dev_notify():
        # Only allow local requests to call this
        if request.remote_addr not in ('127.0.0.1', '::1'):
            return ('forbidden', 403)
        try:
            payload = request.get_json(force=True)
        except Exception:
            payload = {}
        try:
            from . import notifications
            notifications.send_alert(payload or {'ticker': 'TEST', 'headline': 'Test alert', 'score': 50, 'id': 'test'})
            return ('ok', 200)
        except Exception:
            return ('error', 500)

    # Push endpoints: return public key and accept subscriptions
    @app.route('/push/keys')
    def push_keys():
        try:
            from . import push
            vap = push.ensure_vapid_keys()
            return jsonify({'publicKey': vap.get('public_key_b64', '')})
        except Exception:
            return jsonify({'publicKey': ''})

    @app.route('/push/subscribe', methods=['POST'])
    def push_subscribe():
        # Accept subscription object from client and store it for later pushes
        try:
            sub = request.get_json(force=True)
        except Exception:
            return ('bad request', 400)
        try:
            from . import push
            added = push.add_subscription(sub)
            return ('created' if added else 'exists', 201 if added else 200)
        except Exception:
            return ('error', 500)

    @app.route('/push/unsubscribe', methods=['POST'])
    def push_unsubscribe():
        try:
            body = request.get_json(force=True)
            endpoint = body.get('endpoint')
        except Exception:
            return ('bad request', 400)
        try:
            from . import push
            removed = push.remove_subscription(endpoint)
            return ('removed' if removed else 'notfound', 200)
        except Exception:
            return ('error', 500)

    @app.route('/api/chart')
    def api_chart():
        """Return a Yahoo intraday candlestick SVG for the requested ticker."""
        ticker = (request.args.get('ticker') or '').upper()
        if not ticker:
            return ('missing ticker', 400)

        requested_interval = (request.args.get('interval') or '1m').strip().lower()
        candles, used_interval = _get_chart_candles(ticker, requested_interval)

        if candles is not None and used_interval is not None:
            w = 360
            h = 170
            pad_l, pad_r, pad_t, pad_b = 8, 8, 18, 14
            hi = max(c["h"] for c in candles)
            lo = min(c["l"] for c in candles)
            span = hi - lo
            pad = span * 0.05 if span > 0 else max(0.01, hi * 0.002)
            y_hi = hi + pad
            y_lo = lo - pad
            y_span = (y_hi - y_lo) if y_hi != y_lo else 1.0
            plot_w = w - pad_l - pad_r
            plot_h = h - pad_t - pad_b

            def to_y(v: float) -> float:
                return pad_t + (y_hi - v) * plot_h / y_span

            n = len(candles)
            slot = plot_w / max(1, n)
            body_w = max(2.0, slot * 0.58)
            wick_x_offset = slot / 2.0
            rows = []
            for i, c in enumerate(candles):
                x0 = pad_l + i * slot
                cx = x0 + wick_x_offset
                o_y = to_y(c["o"])
                c_y = to_y(c["c"])
                h_y = to_y(c["h"])
                l_y = to_y(c["l"])
                top = min(o_y, c_y)
                bh = max(1.0, abs(c_y - o_y))
                color = "#2ee6a7" if c["c"] >= c["o"] else "#ff6e67"
                rows.append(
                    f"<line x1='{cx:.2f}' y1='{h_y:.2f}' x2='{cx:.2f}' y2='{l_y:.2f}' stroke='{color}' stroke-width='1.2'/>"
                )
                rows.append(
                    f"<rect x='{(cx - body_w/2):.2f}' y='{top:.2f}' width='{body_w:.2f}' height='{bh:.2f}' fill='{color}'/>"
                )

            last_close = candles[-1]["c"]
            label = f"{ticker} Yahoo {used_interval} - Last 30m"
            svg = (
                f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' viewBox='0 0 {w} {h}'>"
                "<rect width='100%' height='100%' fill='#0b1226' />"
                f"<text x='8' y='12' fill='#8ca3b8' font-size='10' font-family='IBM Plex Mono, monospace'>{label}</text>"
                f"<text x='{w-8}' y='12' text-anchor='end' fill='#d7dde7' font-size='10' font-family='IBM Plex Mono, monospace'>{last_close:.2f}</text>"
                + "".join(rows)
                + "</svg>"
            )
            from flask import Response

            return Response(svg, mimetype='image/svg+xml')
        # Fallback SVG
        svg_bad = """
<svg xmlns='http://www.w3.org/2000/svg' width='360' height='170' viewBox='0 0 360 170'>
  <rect width='100%' height='100%' fill='#222' />
  <text x='50%' y='50%' fill='#ccc' font-size='12' text-anchor='middle' alignment-baseline='middle'>No chart data</text>
</svg>
"""
        from flask import Response

        return Response(svg_bad, mimetype='image/svg+xml')

    @app.route('/api/candlestick-patterns')
    def api_candlestick_patterns():
        ticker = (request.args.get('ticker') or '').upper()
        if not ticker:
            return jsonify({"error": "missing ticker"}), 400
        requested_interval = (request.args.get('interval') or '1m').strip().lower()
        candles, used_interval = _get_chart_candles(ticker, requested_interval)
        if candles is None or used_interval is None:
            return jsonify(
                {
                    "ticker": ticker,
                    "requested_interval": requested_interval,
                    "used_interval": None,
                    "candles_analyzed": 0,
                    "candlestick_score": 0,
                    "bias": "neutral",
                    "matched_patterns": [],
                    "bullish_patterns": 0,
                    "bearish_patterns": 0,
                    "supported_patterns": [],
                }
            )
        analysis = analyze_candlestick_patterns(candles)
        return jsonify(
            {
                "ticker": ticker,
                "requested_interval": requested_interval,
                "used_interval": used_interval,
                "candles_analyzed": len(candles),
                **analysis,
            }
        )

    return app
