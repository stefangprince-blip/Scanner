import math
import time

from catalyst_scanner import config, news_provider, news_scoring, quotes, scanner, scoring, store
from catalyst_scanner.app import create_app


def article(headline, published, score=50, url="https://example.com/a", source="Reuters"):
    return {
        "id": headline[:8], "ticker": "ABCD", "headline": headline, "url": url,
        "source": source, "published": published, "first_seen": published,
        "score": score, "body": "", "tags": [], "dilution": [], "distress": [],
    }


def test_news_score_decays_and_exceptional_decays_slower():
    now = 2_000_000.0
    fresh = news_scoring.analyze("ABCD", [article("Product launch", now - 30, 60)], now=now)
    old = news_scoring.analyze("ABCD", [article("Product launch", now - 7200, 60)], now=now)
    assert fresh["score"] > old["score"] + 40
    exceptional = news_scoring.analyze(
        "ABCD", [article("FDA approval granted", now - 3600, 95)], now=now
    )
    weak = news_scoring.analyze(
        "ABCD", [article("General corporate update", now - 3600, 40)], now=now
    )
    assert exceptional["articles"][0]["freshnessWeight"] > weak["articles"][0]["freshnessWeight"]


def test_quick_news_is_deterministic_high_magnitude_negative_and_decays():
    now = 2_000_000.0
    news = {
        "headline": "ABCD files Chapter 11 bankruptcy",
        "url": "https://example.com/ch11",
        "source": "Reuters",
        "published": now - 30,
        "score": 4,
    }
    fresh = news_scoring.quick_signal("ABCD", news, now=now)
    old = news_scoring.quick_signal("ABCD", {**news, "published": now - 7200}, now=now)
    duplicate = news_scoring.quick_signal("ABCD", news, previous_id=fresh["articleId"], now=now)
    assert fresh["catalystStrength"] >= 90
    assert fresh["direction"] == "negative"
    assert fresh["score"] > old["score"]
    assert duplicate["isNew"] is False


def test_material_negative_news_has_high_strength_and_direction():
    result = news_scoring.analyze(
        "ABCD", [article("ABCD files Chapter 11 bankruptcy", time.time(), 4)]
    )
    assert result["articles"][0]["catalystStrength"] >= 90
    assert result["articles"][0]["direction"] == "negative"
    assert result["articles"][0]["score"]["finalScore"] >= 75
    assert result["articles"][0]["score"]["direction"] == "strong_negative"


def test_news_v2_contract_preserves_decimals_and_stale_significance():
    now = 2_000_000.0
    result = news_scoring.analyze(
        "ABCD", [article("ABCD receives major contract award", now - 86400, 82)], now=now
    )
    score = result["articles"][0]["score"]
    assert config.NEWS_SCORING_MODEL_VERSION == "news-score-v2"
    assert result["scoreVersion"] == "news-score-v2"
    assert isinstance(score["finalScore"], float)
    assert 0 < score["finalScore"] <= 100
    assert score["freshness"] >= 5
    assert result["scoreStatus"] == "ready"


def test_combined_news_is_not_diluted_by_weak_articles_and_can_be_mixed():
    now = time.time()
    rows = [
        article("ABCD receives FDA approval", now, 96, "https://example.com/primary", "FDA"),
        article("ABCD corporate update", now - 100, 20, "https://example.com/weak1", "Aggregator"),
        article("ABCD conference presentation", now - 200, 20, "https://example.com/weak2", "Aggregator"),
    ]
    result = news_scoring.analyze("ABCD", rows, now=now)
    assert result["combinedScore"] >= 70
    assert 0 <= result["combinedScore"] <= 100
    conflicting = news_scoring.analyze(
        "ABCD",
        [
            article("ABCD receives FDA approval", now, 95, "https://example.com/good", "FDA"),
            article("ABCD announces registered direct offering", now - 1, 90, "https://example.com/bad", "Reuters"),
        ],
        now=now,
    )
    assert conflicting["combinedDirection"] == "mixed"


def test_duplicate_and_unsafe_news_are_handled():
    now = time.time()
    rows = [
        article("FDA approval granted", now, 95, "https://example.com/release", "FDA"),
        article("FDA approval granted", now - 10, 95, "https://other.example/rewrite", "Aggregator"),
        article("New contract awarded", now - 20, 80, "javascript:alert(1)", "Reuters"),
    ]
    result = news_scoring.analyze("ABCD", rows, now=now)
    assert len(result["articles"]) == 1
    assert result["articles"][0]["url"].startswith("https://")
    assert result["score"] <= 100


def test_tracking_urls_and_syndicated_rewrites_are_one_article():
    now = time.time()
    rows = [
        article(
            "Vivakor Surpasses $1 Billion in Annualized Physical Crude Transactions",
            now - 3600,
            80,
            "https://example.com/release?utm_source=feed&tsrc=rss",
        ),
        article(
            "Vivakor Surpasses $1 Billion in Annualized Crude Oil Transactions with New Commercial Programs",
            now - 1800,
            80,
            "https://syndicator.example/story?id=22",
        ),
        article(
            "Vivakor Surpasses $1 Billion in Annualized Physical Crude Transactions",
            now - 3600,
            80,
            "https://example.com/release?different=tracking",
        ),
    ]
    result = news_scoring.analyze("VIVK", rows, now=now)
    assert len(result["articles"]) == 1
    assert result["aggregation"]["duplicateArticlesExcluded"] == 2


def test_missing_publication_time_uses_detection_time_and_labels_it():
    now = time.time()
    row = article("ABCD corporate update", now, 40)
    row["publicationTimeAvailable"] = False
    row["first_seen"] = now - 120
    result = news_scoring.analyze("ABCD", [row], now=now)
    scored = result["articles"][0]
    assert scored["publicationTimeAvailable"] is False
    assert scored["timestampQuality"] == "discovered_fallback"


def test_publication_time_drives_age_not_ingestion_time():
    now = time.time()
    row = article("New contract awarded", now - 3600, 80)
    row["first_seen"] = now
    result = news_scoring.analyze("ABCD", [row], now=now)
    assert 3599 <= result["articles"][0]["ageSeconds"] <= 3601


def test_news_api_and_detail_page_return_at_most_four(tmp_path):
    db = store.Store(str(tmp_path / "news.db"))
    scn = scanner.Scanner(quote_provider=quotes.NullProvider(), store=db)
    now = time.time()
    for i in range(6):
        db.add(article(f"Distinct contract news {i}", now - i, 80, f"https://example.com/{i}"))
    app = create_app(scn)
    client = app.test_client()
    response = client.get("/api/news/ABCD?limit=4")
    assert response.status_code == 200
    assert len(response.get_json()["articles"]) == 4
    page = client.get("/news/ABCD")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert 'aria-label="Close news window"' in html
    assert "window.close()" in html
    assert "Return to Scanner" in html
    assert "safe-area-inset-top" in html
    db.close()


def test_scanner_page_uses_internal_new_tab_news_link(tmp_path, monkeypatch):
    scn = scanner.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "page.db")),
    )
    app = create_app(scn)
    client = app.test_client()
    monkeypatch.setattr(
        scn.store,
        "latest_news",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("row refresh must not analyze detailed news")
        ),
    )
    assert client.get("/api/rows").status_code == 200
    html = client.get("/").get_data(as_text=True)
    assert 'class="news-link" data-ticker=' in html
    assert 'target="_blank" rel="noopener noreferrer"' in html
    assert "/api/news/" in html
    assert "(hover: hover) and (pointer: fine)" in html
    assert "a&&newsKeyboardIntent" in html
    assert 'class="score-toggle"' in html
    assert 'aria-expanded="${scoreExpanded}"' in html
    assert "Analyzing score factors…" in html
    assert "/api/active-filtered-symbols" in html
    assert "queueMicrotask(()=>syncActiveFilteredSymbols(allRows))" in html
    assert 'data-label="Age on scanner"' in html
    assert "data-scanner-entered" in html
    assert "scanner_age_seconds" in html
    assert "document.querySelectorAll('[data-scanner-entered]')" in html
    assert "News age:" in html
    scn.store.close()


def test_quick_news_queue_deduplicates_and_cached_decay_is_local(tmp_path):
    scn = scanner.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "quick.db")),
    )
    now = time.time()
    signal = news_scoring.quick_signal(
        "ABCD",
        {
            "headline": "ABCD receives FDA approval",
            "url": "https://example.com/fda",
            "source": "FDA",
            "published": now - 10,
            "score": 95,
        },
        now=now,
    )
    key = scn.security_identity("ABCD").cache_key
    scn._quick_news_cache[key] = signal
    scn._active_filtered_symbols = {"ABCD"}
    assert scn._queue_quick_news("ABCD") is False
    later = scn.quick_news_signal("ABCD", now=now + 30)
    assert later["score"] < signal["score"]
    row = {"ticker": "ABCD", "news_score": 0}
    scn._apply_quick_news_to_row(row, now + 30)
    assert row["quick_news"] is True
    assert row["news_score"] > 0
    scn.store.close()


def test_quick_news_scope_tracks_exact_filtered_lifecycle(tmp_path, monkeypatch):
    scn = scanner.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "active-filtered.db")),
    )
    queued = []
    monkeypatch.setattr(
        scn._quick_news_queue, "put_nowait", lambda symbol: queued.append(symbol)
    )
    first = scn.update_active_filtered_symbols(["AAA", "BBB"])
    assert first == {"active": 2, "added": 2, "retained": 0, "removed": 0, "queued": 2}
    assert queued == ["AAA", "BBB"]
    # In-flight deduplication prevents routine retained refreshes from requeueing.
    second = scn.update_active_filtered_symbols(["AAA", "BBB"])
    assert second["queued"] == 0
    removed = scn.update_active_filtered_symbols(["BBB"])
    assert removed["removed"] == 1
    assert scn._queue_quick_news("AAA") is False
    scn.store.close()


def test_active_filtered_endpoint_validates_and_syncs_symbols(tmp_path, monkeypatch):
    scn = scanner.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "active-endpoint.db")),
    )
    monkeypatch.setattr(scn, "_queue_quick_news", lambda _symbol: False)
    client = create_app(scn).test_client()
    result = client.post(
        "/api/active-filtered-symbols", json={"symbols": ["aaa", "BRK.B", "bad symbol"]}
    )
    assert result.status_code == 200
    assert result.get_json()["active"] == 2
    assert scn._active_filtered_symbols == {"AAA", "BRK.B"}
    scn.store.close()


def test_scanner_membership_age_grace_and_reentry(tmp_path, monkeypatch):
    scn = scanner.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "membership.db")),
    )
    monkeypatch.setattr(scn, "_queue_quick_news", lambda _symbol: False)
    base = time.time()
    scn.update_active_filtered_symbols(["ABCD"])
    entered = scn._scanner_memberships["ABCD"]["enteredFilteredScannerAt"]
    assert scn.scanner_membership("ABCD", now=entered)["scanner_age_seconds"] == 0
    assert scn.scanner_membership("ABCD", now=entered + 45)["scanner_age_state"] == "confirming"
    # Sorting/rerendering does not touch membership; a brief failure preserves entry.
    scn.update_active_filtered_symbols([])
    pending = scn._scanner_memberships["ABCD"]["pendingRemovalAt"]
    assert scn.scanner_membership("ABCD", now=pending - 0.1)["entered_filtered_scanner_at"] == entered
    scn.update_active_filtered_symbols(["ABCD"])
    assert scn._scanner_memberships["ABCD"]["enteredFilteredScannerAt"] == entered
    # A genuine exit expires the session; later re-entry receives a new timestamp.
    scn.update_active_filtered_symbols([])
    pending = scn._scanner_memberships["ABCD"]["pendingRemovalAt"]
    assert scn.scanner_membership("ABCD", now=pending + 0.1)["entered_filtered_scanner_at"] is None
    time.sleep(0.002)
    scn.update_active_filtered_symbols(["ABCD"])
    assert scn._scanner_memberships["ABCD"]["enteredFilteredScannerAt"] > entered
    scn.store.close()


def test_scanner_age_score_is_separate_from_news_age():
    common = {
        "news_score": 70, "published_age_seconds": 3600,
        "age_seconds": 3600, "quote_age": 1, "rvol": 2,
    }
    fresh = scoring.scalp_score({**common, "scanner_age_seconds": 0})
    confirming = scoring.scalp_score({**common, "scanner_age_seconds": 180})
    stale = scoring.scalp_score({**common, "scanner_age_seconds": 3600})
    assert confirming > fresh
    assert stale < confirming


def test_quick_news_score_is_not_double_decayed():
    row = {
        "quick_news": True,
        "news_score": 80,
        "published_age_seconds": 7200,
        "age_seconds": 0,
    }
    quick = scoring.scalp_score(row)
    legacy = scoring.scalp_score({**row, "quick_news": False})
    assert quick > legacy


def test_detailed_reconciliation_preserves_stable_quick_article_identity(tmp_path):
    scn = scanner.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "reconcile.db")),
    )
    now = time.time()
    news = {
        "headline": "ABCD receives FDA approval", "url": "https://example.com/fda",
        "source": "FDA", "published": now, "score": 95,
    }
    quick = news_scoring.quick_signal("ABCD", news, now=now)
    key = scn.security_identity("ABCD").cache_key
    scn._quick_news_cache[key] = quick
    scn.update_detailed_news_signal({
        "symbol": "ABCD",
        "articles": [{
            "id": "different-store-id", "headline": news["headline"], "url": news["url"],
            "publisher": news["source"], "publishedTimestamp": now,
            "publishedAt": "", "currentScore": 95, "catalystStrength": 95,
            "sourceConfidence": 1.0, "decayConstantSeconds": 5400,
            "direction": "positive", "catalystType": "Regulatory Approval",
        }],
    })
    assert scn._quick_news_cache[key]["articleId"] == quick["articleId"]
    assert scn._quick_news_cache[key]["isDuplicate"] is True
    scn.store.close()


def test_yahoo_symbol_normalization_and_identity_cache_key():
    first = news_provider.SecurityIdentity("BRK.B", "NYSE", "Berkshire Hathaway")
    renamed = news_provider.SecurityIdentity(
        "BRK.B", "NYSE", "Berkshire Hathaway", previous_symbols=("OLD",), cik="123"
    )
    assert news_provider.normalize_yahoo_symbol("BRK.B", "NYSE") == "BRK-B"
    assert "/BRK-B/news" in news_provider.yahoo_symbol_page_url(first)
    assert first.cache_key != renamed.cache_key


def test_yahoo_provider_preserves_publisher_and_discovery_metadata():
    class Response:
        status_code = 200
        content = b"""<rss><channel><item>
        <title>Company wins government contract</title>
        <link>https://www.reuters.com/markets/example</link>
        <pubDate>Sun, 26 Jul 2026 12:00:00 GMT</pubDate>
        </item></channel></rss>"""

        def raise_for_status(self):
            return None

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    provider = news_provider.YahooRssSymbolNewsProvider(Session())
    result = provider.get_latest_articles(news_provider.SecurityIdentity("ABCD"), 4)
    row = result["articles"][0]
    assert row["originalPublisher"] == "Reuters"
    assert row["discoverySource"] == "yahoo_finance"
    assert row["timestampQuality"] == "yahoo_displayed"


def test_score_details_are_lazy_cached_and_do_not_trigger_news(tmp_path, monkeypatch):
    scn = scanner.Scanner(
        quote_provider=quotes.NullProvider(),
        store=store.Store(str(tmp_path / "score-details.db")),
    )
    app = create_app(scn)
    client = app.test_client()
    calls = {"score": 0}
    original = scoring.score_explanation

    def counted(*args, **kwargs):
        calls["score"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(scoring, "score_explanation", counted)
    monkeypatch.setattr(
        scn.news_provider,
        "get_latest_articles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("score details must not request full news")
        ),
    )
    assert client.get("/api/rows").status_code == 200
    assert calls["score"] == 0
    row = {
        "ticker": "ABCD", "score": 84, "news_score": 72, "quick_news": True,
        "quick_news_preliminary": True, "rvol": 3.2, "scan_rvol_delta": 0.4,
        "scan_volume_accel": 12000, "scan_change_accel": 0.7,
        "change_pct": 8.0, "volume": 2_000_000, "avg_volume": 500_000,
        "quote_age": 2, "published_age_seconds": 720, "dilution": [], "distress": [],
    }
    first = client.post("/api/score-details/ABCD", json={"row": row})
    second = client.post("/api/score-details/ABCD", json={"row": row})
    assert first.status_code == 200
    assert first.get_json()["newsBasis"] == "quick"
    assert second.get_json()["cached"] is True
    assert calls["score"] == 1
    full = client.post("/api/score-details/ABCD", json={"row": row, "full": True})
    assert "allFactors" in full.get_json()
    assert calls["score"] == 2
    scn.store.close()
