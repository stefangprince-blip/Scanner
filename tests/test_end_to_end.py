import os
import time
import json

import pytest

from catalyst_scanner import rssparse, tickers, scoring, store, scanner as sc, quotes
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
        assert payload["ttl_seconds"] == 4 * 60 * 60
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
