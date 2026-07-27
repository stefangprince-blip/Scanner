import time

import pytest

from catalyst_scanner import quotes, scanner, scoring, store


def make_scanner(tmp_path):
    return scanner.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "audit.db")),
    )


@pytest.mark.parametrize(
    "symbol",
    ["A", "BRK-B", "ACME-W", "SPAC-U", "RIGHT-R", "ABCDE"],
)
def test_listed_security_symbol_forms_are_preserved(symbol):
    assert scanner.Scanner._is_supported_market_symbol(symbol)


@pytest.mark.parametrize("symbol", ["", "TOO-LONG", "BAD$", "ABC---W"])
def test_invalid_security_symbol_forms_are_rejected(symbol):
    assert not scanner.Scanner._is_supported_market_symbol(symbol)


def test_execution_metrics_validate_spread_and_distance():
    result = scanner._execution_metrics(
        {"bid": 9.98, "ask": 10.02, "last": 10.0, "session_high": 10.5}
    )
    assert result["spread"] == pytest.approx(0.04)
    assert result["spread_pct"] == pytest.approx(0.4)
    assert result["distance_from_high"] == pytest.approx(4.76190476)
    assert result["spread_status"] == "valid"

    crossed = scanner._execution_metrics({"bid": 10.02, "ask": 9.98})
    assert crossed["spread"] is None
    assert crossed["spread_status"] == "invalid_crossed_quote"


def test_out_of_order_and_future_quotes_are_rejected(tmp_path):
    scn = make_scanner(tmp_path)
    now = time.time()
    first = scn._accept_quote_generation(
        "TEST", {"last": 10.0, "volume": 100, "last_trade_ts": now}
    )
    stale = scn._accept_quote_generation(
        "TEST", {"last": 9.0, "volume": 90, "last_trade_ts": now - 1}
    )
    future = scn._accept_quote_generation(
        "FUTR", {"last": 11.0, "volume": 120, "last_trade_ts": now + 301}
    )
    assert first["accepted_generation"] > 0
    assert stale["_rejected_generation"]
    assert future["_rejected_generation"]
    assert scn.stats["market_updates_dropped_stale"] == 1
    assert scn.stats["market_updates_dropped_future"] == 1


def test_score_ledger_reconciles_exactly():
    row = {
        "score": 75,
        "news_score": 75,
        "quick_news": True,
        "scanner_age_seconds": 20,
        "rvol": 4.2,
        "change_pct": 8.0,
        "change_pct_3m": 1.2,
        "change_pct_10m": 3.0,
        "volume": 2_000_000,
        "dollar_volume": 20_000_000,
        "float_shares": 8_000_000,
        "scan_volume_accel": 1200,
        "scan_change_accel": 0.2,
        "scan_volume_delta": 50_000,
        "scan_change_delta": 0.4,
        "scan_rvol_delta": 0.2,
        "session": "regular",
        "quote_age": 2,
    }
    ledger = scoring.scalp_score_ledger(row)
    assert sum(item["contribution"] for item in ledger["components"]) == pytest.approx(
        ledger["total"], abs=1e-9
    )
    assert scoring.scalp_score(row) == pytest.approx(ledger["total"])


def test_closed_market_rvol_uses_completed_session_not_receipt_clock(monkeypatch):
    monkeypatch.setattr(
        scanner,
        "_session_info",
        lambda: {"type": "closed", "elapsed_minutes": 0.0, "window_minutes": 0.0},
    )
    quote = {
        "volume": 2_000_000,
        "avg_volume": 1_000_000,
        "market_state": "CLOSED",
        # A weekend receipt timestamp must not inflate RVOL by 390x.
        "last_trade_ts": time.time(),
    }
    assert scanner._rvol_for_quote(quote) == pytest.approx(2.0)


def test_last_session_history_excludes_scanned_session_from_average(tmp_path, monkeypatch):
    scn = make_scanner(tmp_path)

    class Response:
        status_code = 200
        content = b"ok"

        @staticmethod
        def json():
            return {
                "chart": {
                    "result": [{
                        "meta": {
                            "regularMarketTime": 1_700_000_000,
                            "regularMarketDayHigh": 12.0,
                            "marketState": "CLOSED",
                        },
                        "indicators": {
                            "quote": [{"volume": [100.0] * 20 + [900.0]}]
                        },
                    }]
                }
            }

    monkeypatch.setattr(scn._http, "get", lambda *args, **kwargs: Response())
    result = scn._last_session_history("TEST")
    assert result["avg_volume"] == pytest.approx(100.0)
    assert result["avg_volume_sample_count"] == 20
    assert result["last_trade_ts"] == 1_700_000_000


@pytest.mark.parametrize(
    "quote,expected,session,baseline_type",
    [
        (
            {
                "market_state": "PRE", "last": 11.0,
                "previous_extended_close": 10.0, "prev_close": 9.5,
            },
            10.0, "premarket", "previous_extended_close",
        ),
        (
            {
                "market_state": "REGULAR", "last": 12.0,
                "previous_extended_close": 10.0, "prev_close": 9.5,
            },
            20.0, "regular", "previous_extended_close",
        ),
        (
            {
                "market_state": "POST", "last": 10.5,
                "regular_session_close": 10.0, "prev_close": 8.0,
            },
            5.0, "after_hours", "regular_session_close",
        ),
    ],
)
def test_dynamic_change_uses_session_specific_baseline(
    quote, expected, session, baseline_type
):
    result = scanner._dynamic_change_metrics(quote)
    assert result["change_pct"] == pytest.approx(expected)
    assert result["change_session"] == session
    assert result["change_baseline_type"] == baseline_type


def test_dynamic_change_falls_back_to_previous_regular_close():
    result = scanner._dynamic_change_metrics(
        {"market_state": "PRE", "last": 10.5, "prev_close": 10.0}
    )
    assert result["change_pct"] == pytest.approx(5.0)
    assert result["change_baseline_type"] == "previous_regular_close"
