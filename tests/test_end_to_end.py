import os
import time
import json

import pytest

from catalyst_scanner import config, rssparse, tickers, scoring, store, scanner as sc, quotes
import catalyst_scanner.app as app_mod
from catalyst_scanner.app import create_app


def test_rss_and_atom_parsing():
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Wire</title>
    <item><title>Acme Corp (NASDAQ: ACME) Announces FDA Approval</title>
    <link>https://x.com/1</link><description>&lt;p&gt;Body text here&lt;/p&gt;</description>
    <pubDate>Wed, 22 Jul 2026 13:05:00 GMT</pubDate><guid>g1</guid></item></channel></rss>"""
    e = rssparse.parse(rss)
    assert len(e) == 1
    assert e[0]["title"].startswith("Acme Corp")
    assert e[0]["summary"] == "Body text here"

    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
    <entry><title>8-K - BIOTECH INC (0001234567) (Filer)</title>
    <link rel="alternate" href="https://www.sec.gov/Archives/edgar/data/1234567/x.htm"/>
    <summary>filing</summary><updated>2026-07-22T13:05:00-04:00</updated><id>urn:x</id></entry></feed>"""
    a = rssparse.parse(atom)
    assert len(a) == 1
    assert a[0]["link"].startswith("https://www.sec.gov")


def test_ticker_extraction_cases():
    cases = [
        ("Acme (NASDAQ: ACME) wins deal", ["ACME"]),
        ("Foo Corp (Nasdaq:FOO) reports", ["FOO"]),
        ("Bar Inc. (NYSE American: BR) announces", ["BR"]),
        ("Baz (OTCQB: BAZZZ) update", ["BAZZZ"]),
        ("Deal between (NASDAQ: AAA) and (NASDAQ: BBB)", ["AAA", "BBB"]),
        ("Combo (NASDAQ: CCC, DDD) merge", ["CCC", "DDD"]),
        ("Watch $TSLA today", ["TSLA"]),
        ("No tickers in this headline at all", []),
    ]
    for text, want in cases:
        got = tickers.extract(text)
        assert got == want


def test_scoring_and_flags():
    r = scoring.score("Cellect Announces FDA Approval of ARX-4")
    assert r["score"] >= 45
    assert "FDA approval" in r["tags"]

    r = scoring.score("Applied UV to be Acquired by Halma plc in All-Cash Transaction for $85 Million")
    assert r["score"] >= 45

    r = scoring.score("Company Announces Pricing of $12.0 Million Public Offering")
    assert r["dilution"] != []

    r2 = scoring.score("Company Announces Record Revenue and Raises Guidance")
    r3 = scoring.score("Company Announces Record Revenue, Raises Guidance and Pricing of Public Offering")
    assert r3["score"] < r2["score"]

    r = scoring.score("Company to Present at the Investor Conference Next Week")
    assert r["junk"] and r["score"] == 0


def test_news_rows_still_show_when_quotes_are_unavailable(tmp_path):
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "news_only.db")),
    )
    try:
        assert scn.store.add(
            {
                "ticker": "NEWSY",
                "headline": "NEWSY announces a major contract",
                "url": "https://example.com",
                "source": "Test feed",
                "published": time.time(),
                "body": "NEWSY announces a major contract award",
                "score": 35,
                "tags": ["New contract"],
                "dilution": [],
                "distress": [],
            }
        )
        rows = scn.rows()
        assert any(r["ticker"] == "NEWSY" for r in rows)
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_rvol_catalyst_adds_alert_at_threshold(tmp_path):
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "rvol.db")),
    )
    try:
        scn._maybe_add_volume_catalyst(
            "RVOL",
            {
                "last": 1.25,
                "volume": 700000,
                "avg_volume": 100000,
            },
        )
        scn.store.set_quote(
            "RVOL",
            {
                "last": 1.25,
                "volume": 700000,
                "avg_volume": 100000,
            },
        )
        rows = scn.rows(include_filtered=True)
        assert any(r["ticker"] == "RVOL" for r in rows)
        r = next(r for r in rows if r["ticker"] == "RVOL")
        assert "RVOL >= 7.0x" in r["tags"]
        assert r["source"] == "Relative Volume Scanner"
        assert r["score"] >= 15
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_rvol_catalyst_not_added_below_threshold(tmp_path):
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "rvol_low.db")),
    )
    try:
        scn._maybe_add_volume_catalyst(
            "LOWV",
            {
                "last": 1.25,
                "volume": 699999,
                "avg_volume": 100000,
            },
        )
        rows = scn.rows(include_filtered=True)
        assert not any(r["ticker"] == "LOWV" for r in rows)
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_independent_rvol_requires_low_float(tmp_path):
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "rvol_float.db")),
    )
    try:
        scn._maybe_add_volume_catalyst(
            "HIGHF",
            {
                "last": 1.25,
                "volume": 800000,
                "avg_volume": 100000,
                "float_shares": 25_000_000,
            },
            source="Independent RVOL Scanner",
            max_float=20_000_000,
        )
        scn._maybe_add_volume_catalyst(
            "LOWF",
            {
                "last": 1.25,
                "volume": 800000,
                "avg_volume": 100000,
                "float_shares": 15_000_000,
            },
            source="Independent RVOL Scanner",
            max_float=20_000_000,
        )
        scn.store.set_quote(
            "LOWF",
            {
                "last": 1.25,
                "volume": 800000,
                "avg_volume": 100000,
                "float_shares": 15_000_000,
            },
        )
        rows = scn.rows(include_filtered=True)
        assert not any(r["ticker"] == "HIGHF" for r in rows)
        assert any(r["ticker"] == "LOWF" for r in rows)
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_extract_finviz_symbols_parses_unique_tickers():
    html = """
    <a href='quote.ashx?t=ABCD'>ABCD</a>
    <a href='quote.ashx?t=EFGH'>EFGH</a>
    <a href='quote.ashx?t=ABCD'>ABCD</a>
    """
    got = sc.Scanner._extract_finviz_symbols(html)
    assert got == ["ABCD", "EFGH"]


def test_rows_are_deduplicated_per_ticker(tmp_path):
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "dedupe_rows.db")),
    )
    try:
        assert scn.store.add(
            {
                "ticker": "DUPL",
                "headline": "DUPL announces collaboration update",
                "url": "#",
                "source": "Wire A",
                "published": time.time(),
                "body": "initial alert",
                "score": 18,
                "tags": ["Collaboration"],
                "dilution": [],
                "distress": [],
            }
        )
        assert scn.store.add(
            {
                "ticker": "DUPL",
                "headline": "DUPL receives major contract award",
                "url": "#",
                "source": "Wire B",
                "published": time.time(),
                "body": "stronger alert",
                "score": 45,
                "tags": ["Contract"],
                "dilution": [],
                "distress": [],
            }
        )
        rows = scn.rows(include_filtered=True)
        dupl_rows = [r for r in rows if r["ticker"] == "DUPL"]
        assert len(dupl_rows) == 1
        assert dupl_rows[0]["news_score"] == 45
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_invalid_rvol_source_row_is_suppressed(tmp_path):
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "rvol_suppress.db")),
    )
    try:
        assert scn.store.add(
            {
                "ticker": "BADR",
                "headline": "BADR relative volume spike (>= 7.0x)",
                "url": "#",
                "source": "Independent RVOL Scanner",
                "published": time.time(),
                "body": "stale row with no quote confirmation",
                "score": 32,
                "tags": ["RVOL >= 7.0x"],
                "dilution": [],
                "distress": [],
            }
        )
        rows = scn.rows(include_filtered=True)
        assert not any(r["ticker"] == "BADR" for r in rows)
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_rvol_catalyst_score_boosts_when_news_confirmed(tmp_path, monkeypatch):
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "rvol_news_boost.db")),
    )
    try:
        monkeypatch.setattr(
            scn,
            "_research_rvol_news",
            lambda _ticker: {
                "headline": "BOOST receives FDA approval",
                "summary": "FDA approval confirmed.",
                "url": "https://example.com/news",
                "source": "Yahoo Finance",
                "score": 52,
                "tags": ["FDA approval"],
            },
        )
        scn._maybe_add_volume_catalyst(
            "BOOST",
            {"last": 2.0, "volume": 800000, "avg_volume": 100000},
            source="Relative Volume Scanner",
        )
        scn.store.set_quote(
            "BOOST",
            {"last": 2.0, "volume": 800000, "avg_volume": 100000},
        )
        rows = scn.rows(include_filtered=True)
        r = next(r for r in rows if r["ticker"] == "BOOST")
        assert r["score"] >= (config.RVOL_CATALYST_SCORE + config.RVOL_NEWS_BONUS_SCORE)
        assert "Confirmed news" in r["tags"]
        assert "Yahoo Finance" in r["source"]
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_volume_momentum_catalyst_adds_on_increasing_scan_volume(tmp_path):
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "volume_momentum.db")),
    )
    try:
        scn._maybe_add_volume_momentum_catalyst(
            "VMOM",
            {"last": 2.0, "volume": 510_000},
        )
        scn._maybe_add_volume_momentum_catalyst(
            "VMOM",
            {"last": 2.0, "volume": 560_000},
        )
        scn.store.set_quote(
            "VMOM",
            {"last": 2.0, "volume": 560_000},
        )
        rows = scn.rows(include_filtered=True)
        assert any(r["ticker"] == "VMOM" for r in rows)
        r = next(r for r in rows if r["ticker"] == "VMOM")
        assert "Volume Momentum Scanner" in r["source"]
        assert "Volume rising" in r["tags"]
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_volume_momentum_requires_increase(tmp_path):
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "volume_momentum_noadd.db")),
    )
    try:
        scn._maybe_add_volume_momentum_catalyst(
            "VMDN",
            {"last": 2.0, "volume": 510_000},
        )
        scn._maybe_add_volume_momentum_catalyst(
            "VMDN",
            {"last": 2.0, "volume": 509_000},
        )
        rows = scn.rows(include_filtered=True)
        assert not any(r["ticker"] == "VMDN" for r in rows)
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_volume_activity_adds_for_500k_plus_without_news(tmp_path):
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "volume_activity.db")),
    )
    try:
        scn._maybe_add_volume_activity_catalyst(
            "VACT",
            {"last": 3.0, "volume": 600_000},
        )
        scn.store.set_quote(
            "VACT",
            {"last": 3.0, "volume": 600_000},
        )
        rows = scn.rows(include_filtered=True)
        assert any(r["ticker"] == "VACT" for r in rows)
        r = next(r for r in rows if r["ticker"] == "VACT")
        assert "Volume Activity Scanner" in r["source"]
        assert "Volume >= 500,000" in r["tags"]
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_scalp_score_profiles_weight_news_and_momentum_differently():
    news_row = {
        "news_score": 80,
        "change_pct": 2.0,
        "rvol": 1.4,
        "dollar_volume": 1_500_000,
        "float_shares": 40_000_000,
        "age_seconds": 300,
        "source": "Yahoo Finance",
        "dilution": [],
        "distress": [],
    }
    momentum_row = {
        "news_score": 0,
        "change_pct": 8.0,
        "rvol": 8.0,
        "dollar_volume": 9_000_000,
        "float_shares": 35_000_000,
        "age_seconds": 240,
        "source": "Volume Momentum Scanner",
        "dilution": [],
        "distress": [],
    }
    assert scoring.scalp_score(news_row, "news_first") >= scoring.scalp_score(
        news_row, "momentum_first"
    )
    assert scoring.scalp_score(momentum_row, "momentum_first") >= scoring.scalp_score(
        momentum_row, "news_first"
    )


def test_scalp_score_boosts_brand_new_tickers():
    base = {
        "news_score": 32,
        "change_pct": 1.8,
        "rvol": 2.4,
        "dollar_volume": 3_400_000,
        "float_shares": 50_000_000,
        "source": "Yahoo Finance",
        "dilution": [],
        "distress": [],
    }
    new_row = {**base, "age_seconds": 10}
    old_row = {**base, "age_seconds": 1200}
    assert scoring.scalp_score(new_row, "balanced") > scoring.scalp_score(
        old_row, "balanced"
    )


def test_scalp_score_rewards_accelerating_tape():
    base = {
        "news_score": 25,
        "change_pct": 1.2,
        "rvol": 1.8,
        "dollar_volume": 2_600_000,
        "float_shares": 60_000_000,
        "age_seconds": 180,
        "source": "Yahoo Finance",
        "dilution": [],
        "distress": [],
    }
    accelerating = {
        **base,
        "scan_change_accel": 0.9,
        "scan_volume_accel": 220000,
        "scan_rvol_delta": 0.7,
    }
    fading = {
        **base,
        "scan_change_accel": -0.7,
        "scan_volume_accel": -180000,
        "scan_rvol_delta": -0.4,
    }
    assert scoring.scalp_score(accelerating, "momentum_first") > scoring.scalp_score(
        fading, "momentum_first"
    )


def test_scalp_score_accounts_for_macro_sentiment():
    base = {
        "news_score": 24,
        "change_pct": 1.0,
        "rvol": 1.6,
        "dollar_volume": 2_200_000,
        "float_shares": 80_000_000,
        "age_seconds": 240,
        "dilution": [],
        "distress": [],
        "scan_change_accel": 0.0,
        "scan_volume_accel": 0.0,
        "scan_rvol_delta": 0.0,
    }
    positive = {
        **base,
        "headline": "Company wins major government contract after rate cut optimism",
        "body": "Federal stimulus grant supports deployment.",
        "source": "BusinessWire",
    }
    negative = {
        **base,
        "headline": "Company warns on tariffs and geopolitical tensions",
        "body": "Policy risk and sanctions pressure demand outlook.",
        "source": "BusinessWire",
    }
    assert scoring.scalp_score(positive, "balanced") > scoring.scalp_score(
        negative, "balanced"
    )


def test_rvol_pace_of_day_formula():
    """RVOL must be time-weighted (pace-of-day), not a naive vol/avg ratio."""
    from catalyst_scanner.scanner import _rvol

    avg = 1_000_000  # 1M average daily volume

    # Regular session window = 390 min (9:30 AM - 4:00 PM)
    # At 30 min: a stock that has already traded its FULL daily avg (1M shares)
    # is running at 13x pace: (1M × 390) / (30 × 1M) = 13x
    r30 = _rvol(1_000_000, avg, elapsed_minutes=30, session_type="regular")
    assert r30 is not None
    assert abs(r30 - 13.0) < 0.1, f"expected 13.0x at 30min regular, got {r30:.2f}x"

    # At 30 min: a stock with 100K shares vs 1M avg is only at 1.3x pace
    # Expected at 30 min = 1M × (30/390) = 76,923 shares; 100K/76.9K ≈ 1.3x
    r30_low = _rvol(100_000, avg, elapsed_minutes=30, session_type="regular")
    assert r30_low is not None
    assert abs(r30_low - 1.3) < 0.05, f"expected 1.3x at 30min low vol, got {r30_low:.2f}x"

    # At 390 min (end of day): converges to naive ratio
    r390 = _rvol(1_000_000, avg, elapsed_minutes=390, session_type="regular")
    assert r390 is not None
    assert abs(r390 - 1.0) < 0.01, f"expected 1.0x at EOD, got {r390:.2f}x"

    # Pre-market session window = 330 min (4:00 AM - 9:30 AM)
    # At 60 min in pre-market: 500K shares → (500K × 330) / (60 × 1M) = 2.75x
    r_pre = _rvol(500_000, avg, elapsed_minutes=60, session_type="premarket")
    assert r_pre is not None
    assert abs(r_pre - 2.75) < 0.05, f"expected 2.75x at 60min premarket, got {r_pre:.2f}x"

    # After-hours session window = 240 min (4:00 PM - 8:00 PM)
    # At 30 min after close: 1M shares → (1M × 240) / (30 × 1M) = 8.0x
    r_ah = _rvol(1_000_000, avg, elapsed_minutes=30, session_type="after_hours")
    assert r_ah is not None
    assert abs(r_ah - 8.0) < 0.05, f"expected 8.0x at 30min after-hours, got {r_ah:.2f}x"

    # None is returned for invalid inputs
    assert _rvol(0, avg) is None
    assert _rvol(None, avg) is None
    assert _rvol(1000, 0) is None



def test_session_info_types():
    """_session_info must return a valid session type dict."""
    from catalyst_scanner.scanner import _session_info, _is_trading_window
    info = _session_info()
    assert info["type"] in ("premarket", "regular", "after_hours", "closed")
    assert isinstance(info["elapsed_minutes"], float)
    assert isinstance(info["window_minutes"], float)
    # _is_trading_window must agree with the session type
    expected_open = info["type"] != "closed"
    assert _is_trading_window() == expected_open


def test_rvol_uses_last_tick_timing_when_market_is_closed(monkeypatch):
    import datetime
    import zoneinfo

    et = zoneinfo.ZoneInfo("America/New_York")
    last_tick = datetime.datetime(2026, 7, 24, 16, 0, tzinfo=et).timestamp()
    monkeypatch.setattr(
        sc,
        "_session_info",
        lambda: {"type": "closed", "elapsed_minutes": 0.0, "window_minutes": 0.0},
    )

    quote = {
        "volume": 1_000_000,
        "avg_volume": 1_000_000,
        "market_state": "CLOSED",
        "last_trade_ts": last_tick,
    }
    rvol = sc._rvol_for_quote(quote)
    assert rvol is not None
    assert abs(rvol - 1.0) < 0.01


def test_closed_market_force_scan_does_not_overstate_rvol_alerts(tmp_path, monkeypatch):
    import datetime
    import zoneinfo

    et = zoneinfo.ZoneInfo("America/New_York")
    last_tick = datetime.datetime(2026, 7, 24, 16, 0, tzinfo=et).timestamp()
    monkeypatch.setattr(
        sc,
        "_session_info",
        lambda: {"type": "closed", "elapsed_minutes": 0.0, "window_minutes": 0.0},
    )

    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "closed_force_scan_rvol.db")),
    )
    try:
        scn._maybe_add_volume_catalyst(
            "EODV",
            {
                "last": 2.0,
                "volume": 1_000_000,
                "avg_volume": 1_000_000,
                "market_state": "CLOSED",
                "last_trade_ts": last_tick,
            },
        )
        assert not any(a.get("ticker") == "EODV" for a in scn.store.active())
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_momentum_tracking_change_delta(tmp_path):
    """_annotate_quote_momentum must compute change_delta without NameError."""
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "mom.db")),
    )
    try:
        q1 = {"last": 5.0, "change_pct": 2.0, "volume": 300_000, "avg_volume": 100_000}
        q2 = {"last": 5.5, "change_pct": 3.5, "volume": 400_000, "avg_volume": 100_000}

        out1 = scn._annotate_quote_momentum("TSYM", q1)
        # First call: no previous state, all deltas are None
        assert out1.get("scan_change_delta") is None
        assert out1.get("scan_volume_delta") is None
        assert out1.get("scan_rvol_delta") is None

        out2 = scn._annotate_quote_momentum("TSYM", q2)
        # Second call: deltas must be computed correctly
        assert out2.get("scan_change_delta") is not None
        assert abs(out2["scan_change_delta"] - 1.5) < 0.01, (
            f"change_delta should be ~1.5, got {out2['scan_change_delta']}"
        )
        assert out2.get("scan_volume_delta") == pytest.approx(100_000, abs=1)
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_us_market_catalyst_is_added(tmp_path):
    """_maybe_add_us_market_catalyst must fire when conditions are met."""
    import catalyst_scanner.config as cfg

    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "mkt.db")),
    )
    orig_enabled = cfg.US_MARKET_SCAN_ENABLED
    try:
        cfg.US_MARKET_SCAN_ENABLED = True
        cfg.US_MARKET_SCAN_MIN_PRICE = 0.4
        cfg.US_MARKET_SCAN_MIN_VOLUME = 300_000
        cfg.US_MARKET_SCAN_MIN_DOLLAR_VOLUME = 1_000_000
        cfg.US_MAJOR_EXCHANGE_ONLY = True

        quote = {
            "last": 4.0,
            "volume": 500_000,
            "avg_volume": 200_000,
            "exchange": "NASDAQ",
            "market_cap": 40_000_000,
        }
        scn._maybe_add_us_market_catalyst("MKTX", quote)
        rows = scn.rows(include_filtered=True, ttl_seconds=3600)
        assert any(r["ticker"] == "MKTX" for r in rows), "market catalyst alert should appear"
        r = next(r for r in rows if r["ticker"] == "MKTX")
        assert r["source"] == cfg.US_MARKET_SCAN_SOURCE
    finally:
        cfg.US_MARKET_SCAN_ENABLED = orig_enabled
        try:
            scn.store.close()
        except Exception:
            pass


def test_source_quality_boost_covers_all_wires():
    """All recognised wire services should get non-zero boosts."""
    from catalyst_scanner.scoring import _source_quality_boost

    assert _source_quality_boost("SEC 8-K") > 0
    assert _source_quality_boost("BusinessWire") > 0
    assert _source_quality_boost("GlobeNewswire") > 0
    assert _source_quality_boost("PR Newswire") > 0
    assert _source_quality_boost("ACCESSWIRE") > 0
    assert _source_quality_boost("Newsfile") > 0
    assert _source_quality_boost("StockTitan") > 0
    assert _source_quality_boost("Confirmed News + Yahoo Finance") > 0
    # Unknown source → zero boost, but should not raise
    assert _source_quality_boost("Unknown Random Blog") == 0.0
    # SEC gets maximum credibility
    assert _source_quality_boost("SEC 8-K") >= 8.0


def test_store_demo_and_flask_api(tmp_path):
    db1 = tmp_path / "t_pytest.db"
    st = store.Store(str(db1))
    try:
        a1 = {
            "ticker": "ABCD",
            "headline": "ABCD Announces FDA Approval",
            "score": 50,
            "tags": ["FDA approval"],
            "dilution": [],
            "distress": [],
            "source": "X",
            "published": time.time(),
        }
        assert st.add(a1) is True
        assert st.add(a1) is False
        assert len(st.active()) == 1
        assert st.active()[0]["ttl_fraction"] > 0.0

        scn = sc.Scanner(quote_provider=quotes.NullProvider(), store=store.Store(str(tmp_path / "t2_pytest.db")))
        scn.seed_demo()
        rows = scn.rows()
        assert len(rows) >= 5
        app = create_app(scn)
        c = app.test_client()
        resp = c.get("/api/rows")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["count"] >= 5
        assert payload["ttl_seconds"] > 0
        resp2 = c.get("/")
        assert resp2.status_code == 200
        assert b"Catalyst" in resp2.data
    finally:
        try:
            st.close()
        except Exception:
            pass
        try:
            scn.store.close()
        except Exception:
            pass
        for p in (db1, tmp_path / "t2_pytest.db"):
            if p.exists():
                p.unlink()


def test_shared_filters_are_persisted_and_profile_is_global(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", settings_path)
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "shared_filters.db")),
    )
    try:
        app = create_app(scn)
        c = app.test_client()

        payload = {
            "scalp_profile_current": "news_first",
            "watchlist_preset": "biotech_catalyst",
            "shared_filters": {
                "f-score": "30",
                "f-news": "yes",
                "sort_key": "score",
                "sort_dir": "desc",
                "watchlist": "biotech_catalyst",
                "include_filtered": True,
            },
        }
        post_resp = c.post("/api/settings", json=payload)
        assert post_resp.status_code == 200
        post_data = post_resp.get_json()
        assert post_data["scalp_profile_current"] == "news_first"
        assert post_data["watchlist_preset"] == "biotech_catalyst"
        assert post_data["shared_filters"]["f-score"] == "30"
        assert post_data["shared_filters"]["include_filtered"] is True

        get_data = c.get("/api/settings").get_json()
        assert get_data["scalp_profile_current"] == "news_first"
        assert get_data["shared_filters"]["sort_key"] == "score"

        # /api/rows should use the globally persisted profile, not per-request overrides.
        rows_data = c.get("/api/rows?profile=balanced").get_json()
        assert rows_data["profile"] == "news_first"
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_candlestick_pattern_analysis_detects_bullish_engulfing():
    candles = [
        {"o": 10.2, "h": 10.3, "l": 9.7, "c": 9.8},   # bearish
        {"o": 9.75, "h": 10.5, "l": 9.7, "c": 10.45},  # bullish engulfing
    ]
    result = app_mod.analyze_candlestick_patterns(candles)
    names = {p["name"] for p in result["matched_patterns"]}
    assert "Bullish Engulfing" in names
    assert result["candlestick_score"] > 0
    assert result["bias"] == "bullish"


def test_get_chart_candles_uses_short_range_for_small_windows(monkeypatch):
    calls = []
    sample = [{"o": 1.0, "h": 1.1, "l": 0.9, "c": 1.0} for _ in range(400)]

    def fake_fetch(_ticker, interval, data_range):
        calls.append((interval, data_range))
        return sample

    app_mod._chart_cache.clear()
    monkeypatch.setattr(app_mod, "_fetch_raw_yahoo_candles", fake_fetch)

    candles_short, used_short = app_mod._get_chart_candles("ABCD", "1m", "2h")
    assert used_short == "1m"
    assert candles_short is not None
    assert calls and calls[0] == ("1m", "1d")

    calls.clear()
    candles_long, used_long = app_mod._get_chart_candles("ABCD", "1m", "24h")
    assert used_long == "1m"
    assert candles_long is not None
    assert calls and calls[0] == ("1m", "5d")


def test_api_candlestick_patterns_uses_hover_time_chart_data(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", settings_path)

    stub_candles = [
        {"o": 10.2, "h": 10.3, "l": 9.7, "c": 9.8},
        {"o": 9.75, "h": 10.5, "l": 9.7, "c": 10.45},
    ]

    def fake_get_chart_candles(ticker, interval, requested_window="30m"):
        assert ticker == "ABCD"
        assert interval == "3m"
        assert requested_window == "30m"
        return stub_candles, "3m"

    monkeypatch.setattr(app_mod, "_get_chart_candles", fake_get_chart_candles)

    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "chart_patterns.db")),
    )
    try:
        app = create_app(scn)
        c = app.test_client()
        resp = c.get("/api/candlestick-patterns?ticker=ABCD&interval=3m")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["ticker"] == "ABCD"
        assert payload["used_interval"] == "3m"
        assert payload["candles_analyzed"] == 2
        assert payload["candlestick_score"] > 0
        assert any(p["name"] == "Bullish Engulfing" for p in payload["matched_patterns"])
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_api_candlestick_patterns_applies_requested_window(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", settings_path)
    stub_candles = [{"o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1} for _ in range(300)]

    def fake_get_chart_candles(ticker, interval, requested_window="30m"):
        assert ticker == "ABCD"
        assert interval == "5m"
        assert requested_window == "2h"
        return stub_candles, "5m"

    monkeypatch.setattr(app_mod, "_get_chart_candles", fake_get_chart_candles)
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "chart_patterns_window.db")),
    )
    try:
        app = create_app(scn)
        c = app.test_client()
        resp = c.get("/api/candlestick-patterns?ticker=ABCD&interval=5m&window=2h")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["used_interval"] == "5m"
        assert payload["requested_window"] == "2h"
        assert payload["candles_analyzed"] == 24
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_api_chart_label_uses_requested_window(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", settings_path)
    stub_candles = [{"o": 10.0, "h": 10.2, "l": 9.8, "c": 10.1} for _ in range(120)]

    monkeypatch.setattr(
        app_mod,
        "_get_chart_candles",
        lambda _ticker, _interval, _window="30m": (stub_candles, "3m"),
    )
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "chart_window_label.db")),
    )
    try:
        app = create_app(scn)
        c = app.test_client()
        resp = c.get("/api/chart?ticker=ABCD&interval=3m&window=24h")
        assert resp.status_code == 200
        assert b"Last 24h" in resp.data
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_force_scan_runs_both_scan_paths(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", settings_path)
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "force_scan.db")),
    )
    calls = {"filtered": 0, "market": 0, "full_pass": None}

    def fake_filtered():
        calls["filtered"] += 1

    def fake_market(full_pass=False):
        calls["market"] += 1
        calls["full_pass"] = full_pass
        return {"market_batches": 7, "market_symbols": 420, "independent_scans": 1}

    monkeypatch.setattr(scn, "_run_filtered_results_scan", fake_filtered)
    monkeypatch.setattr(scn, "_run_market_universe_scan", fake_market)
    result = scn.force_scan()
    assert result["forced"] is True
    assert result["scans"]["filtered_results"] == 1
    assert result["scans"]["market_universe"] == 1
    assert result["scans"]["market_batches"] == 7
    assert result["scans"]["market_symbols"] == 420
    assert result["scans"]["independent_scans"] == 1
    assert calls["filtered"] == 1
    assert calls["market"] == 1
    assert calls["full_pass"] is True
    try:
        scn.store.close()
    except Exception:
        pass


def test_market_scan_batches_cover_all_symbols_before_restart(tmp_path):
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "market_batches.db")),
    )
    try:
        orig_enabled = config.US_MARKET_SCAN_ENABLED
        config.US_MARKET_SCAN_ENABLED = True
        scn._us_market_symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        scn._us_market_symbols_at = time.time()
        scn._us_market_cursor = 2
        orig_batch = config.US_MARKET_SCAN_BATCH_SIZE
        config.US_MARKET_SCAN_BATCH_SIZE = 2
        try:
            batches = scn._us_market_scan_batches(full_pass=True)
        finally:
            config.US_MARKET_SCAN_BATCH_SIZE = orig_batch
            config.US_MARKET_SCAN_ENABLED = orig_enabled
        flattened = [sym for batch in batches for sym in batch]
        assert set(flattened) == {"AAA", "BBB", "CCC", "DDD", "EEE"}
        assert len(flattened) == 5
        assert len(batches) == 3
        assert flattened[:2] == ["CCC", "DDD"]
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_api_force_scan_triggers_background_scan(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", settings_path)
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "force_scan_api.db")),
    )
    started = {"count": 0}

    def fake_trigger():
        started["count"] += 1
        return True

    monkeypatch.setattr(scn, "trigger_force_scan", fake_trigger)
    monkeypatch.setattr(scn, "force_scan_running", lambda: True)
    try:
        app = create_app(scn)
        c = app.test_client()
        resp = c.post("/api/force-scan")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["ok"] is True
        assert payload["started"] is True
        assert payload["running"] is True
        assert started["count"] == 1
    finally:
        try:
            scn.store.close()
        except Exception:
            pass


def test_mobile_chart_route_renders_live_refresh_page(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(app_mod, "SETTINGS_PATH", settings_path)
    scn = sc.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "mobile_chart_page.db")),
    )
    try:
        app = create_app(scn)
        c = app.test_client()
        resp = c.get("/mobile-chart?ticker=ABCD&interval=5m&window=2h")
        assert resp.status_code == 200
        body = resp.data
        assert b"ABCD Live Chart" in body
        assert b"id=\"close-tab\"" in body
        assert b"data-win=\"30m\"" in body
        assert b"data-win=\"2h\"" in body
        assert b"data-win=\"24h\"" in body
        assert b"/api/chart?ticker=" in body
        assert b"/api/candlestick-patterns?ticker=" in body
        assert b"&window=" in body
        assert b"setInterval(refreshData, 3000)" in body
    finally:
        try:
            scn.store.close()
        except Exception:
            pass
