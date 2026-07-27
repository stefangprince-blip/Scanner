"""On-demand, time-decaying scoring for a symbol's latest news."""

from __future__ import annotations

import hashlib
import math
import re
import time
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from . import config

_NEGATIVE = re.compile(
    r"\b(bankrupt|chapter 11|fraud|investigation|halt|recall|failed|miss(?:es|ed)?|"
    r"cuts? guidance|offering|dilution|delist|layoff|default|reverse stock split|"
    r"going concern|clinical failure|regulatory rejection|lawsuit)\b", re.I
)
_POSITIVE = re.compile(
    r"\b(approval|approved|award|contract|acquisition|acquire|merger|positive|"
    r"beat|raises? guidance|partnership|investment|patent victory)\b", re.I
)
_PRIMARY = re.compile(r"\b(sec|fda|investor relations|government|company release)\b", re.I)
_MAJOR = re.compile(
    r"\b(reuters|bloomberg|associated press|businesswire|globenewswire|"
    r"pr newswire|marketwatch|seeking alpha|stocktitan)\b", re.I
)
_EVENT_WORDS = re.compile(r"[^a-z0-9 ]+")
_EVENT_STOPWORDS = {
    "a", "an", "and", "at", "for", "from", "in", "inc", "ltd", "of", "on",
    "the", "to", "with", "plc", "corp", "corporation", "company", "announces",
    "announcement",
}


def safe_url(value: str) -> str | None:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return None
    return parsed.geturl() if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _canonical_url(value: str) -> str:
    parsed = urlparse(value)
    path = re.sub(r"/+$", "", parsed.path or "/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def _event_tokens(symbol: str, headline: str) -> set[str]:
    normalized = _EVENT_WORDS.sub(" ", headline.lower())
    ignored = _EVENT_STOPWORDS | {symbol.lower()}
    return {
        token for token in normalized.split()
        if (len(token) > 1 or token.isdigit()) and token not in ignored
    }


def _same_event(symbol: str, left: dict, right: dict) -> bool:
    left_url = _canonical_url(str(left.get("url") or ""))
    right_url = _canonical_url(str(right.get("url") or ""))
    if left_url and left_url == right_url:
        return True
    left_tokens = _event_tokens(symbol, str(left.get("headline") or ""))
    right_tokens = _event_tokens(symbol, str(right.get("headline") or ""))
    if not left_tokens or not right_tokens:
        return False
    left_numbers = {token for token in left_tokens if token.isdigit()}
    right_numbers = {token for token in right_tokens if token.isdigit()}
    if left_numbers != right_numbers:
        return False
    similarity = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    left_time = float(left.get("published") or left.get("first_seen") or 0)
    right_time = float(right.get("published") or right.get("first_seen") or 0)
    return similarity >= 0.40 and abs(left_time - right_time) <= 48 * 3600


def _classification(score: float) -> str:
    if score >= 90:
        return "Exceptional"
    if score >= 75:
        return "Strong"
    if score >= 55:
        return "Moderate"
    if score >= 30:
        return "Weak"
    return "Minimal"


def _source_confidence(source: str) -> tuple[float, bool]:
    if _PRIMARY.search(source or ""):
        return 1.0, True
    if _MAJOR.search(source or ""):
        return 0.94, False
    if re.search(r"yahoo|aggregator", source or "", re.I):
        return 0.72, False
    return 0.85, False


def _direction(text: str) -> str:
    positive, negative = bool(_POSITIVE.search(text)), bool(_NEGATIVE.search(text))
    return "mixed" if positive and negative else "negative" if negative else "positive" if positive else "neutral"


def _direction_label(direction: str, magnitude: float) -> str:
    if direction == "mixed":
        return "mixed"
    if direction == "neutral":
        return "neutral"
    strong = magnitude >= 75
    return f"strong_{direction}" if strong else direction


def _freshness_score(age_seconds: float, strength: float) -> float:
    """Trading freshness on 0-100, separate from lasting catalyst magnitude."""
    peak = 5 * 60
    half_life = (
        config.NEWS_DECAY_EXCEPTIONAL_SECONDS if strength >= 90
        else config.NEWS_DECAY_STRONG_SECONDS if strength >= 75
        else config.NEWS_DECAY_MODERATE_SECONDS if strength >= 55
        else config.NEWS_DECAY_WEAK_SECONDS
    )
    if age_seconds <= peak:
        return 100.0
    return max(5.0, min(100.0, 100.0 * math.pow(0.5, (age_seconds - peak) / half_life)))


def _relevance_score(symbol: str, row: dict, text: str) -> float:
    """Conservative identity relevance for already symbol-associated provider rows."""
    upper = text.upper()
    company = str(row.get("companyName") or row.get("name") or "").strip().lower()
    if re.search(rf"(?<![A-Z0-9]){re.escape(symbol.upper())}(?![A-Z0-9])", upper):
        return 100.0
    if company and len(company) >= 4 and company in text.lower():
        return 96.0
    # Yahoo association or a ticker-specific stored feed is evidence, but not
    # certainty; broad/automated commentary is discounted further.
    base = 82.0 if row.get("discoverySource") == "yahoo_finance" else 88.0
    if re.search(r"\b(stocks to watch|market roundup|why .* stock|price moved)\b", text, re.I):
        base -= 30.0
    return max(0.0, base)


def _article_components(symbol: str, row: dict, text: str, age: float, strength: float,
                        source_confidence: float, novelty: float) -> dict:
    relevance = _relevance_score(symbol, row, text)
    freshness = _freshness_score(age, strength)
    components = {
        "relevance": relevance,
        "catalystStrength": float(strength),
        "freshness": freshness,
        "sourceConfidence": float(source_confidence) * 100.0,
        "novelty": novelty,
    }
    magnitude = (
        relevance * 0.15
        + strength * 0.25
        + freshness * 0.45
        + components["sourceConfidence"] * 0.10
        + novelty * 0.05
    )
    return {**components, "finalScore": max(0.0, min(100.0, magnitude))}


def _catalyst_type(text: str) -> str:
    patterns = (
        (r"fda|approval", "Regulatory Approval"),
        (r"acqui|merger", "M&A"),
        (r"clinical|trial|phase [123]", "Clinical Data"),
        (r"contract|award", "Contract"),
        (r"earnings|guidance|revenue", "Financial Results"),
        (r"offering|dilution", "Financing"),
        (r"bankrupt|chapter 11", "Bankruptcy"),
    )
    for pattern, label in patterns:
        if re.search(pattern, text, re.I):
            return label
    return "Corporate News"


def _inferred_strength(text: str) -> int:
    tiers = (
        (r"fda (?:approval|approved)|definitive merger|to be acquired|chapter 11|bankrupt|fraud", 92),
        (r"positive (?:phase|clinical)|large contract|raises? guidance|patent victory|investigation|trading halt", 82),
        (r"product launch|new customer|partnership|conference presentation|earnings", 62),
        (r"corporate update|analyst|previously announced", 40),
    )
    for pattern, score in tiers:
        if re.search(pattern, text, re.I):
            return score
    return 18


def _decay_constant(strength: float) -> int:
    if strength >= 90:
        return config.NEWS_DECAY_EXCEPTIONAL_SECONDS
    if strength >= 75:
        return config.NEWS_DECAY_STRONG_SECONDS
    if strength >= 55:
        return config.NEWS_DECAY_MODERATE_SECONDS
    if strength >= 30:
        return config.NEWS_DECAY_WEAK_SECONDS
    return config.NEWS_DECAY_MINIMAL_SECONDS


def _age_label(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''} ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} hour{'s' if hours != 1 else ''} ago" + (
        f" {minutes} minute{'s' if minutes != 1 else ''} ago" if minutes else ""
    )


def analyze(symbol: str, rows: list[dict], now: float | None = None, limit: int = 4) -> dict:
    now = time.time() if now is None else now
    unique, seen_urls, seen_events = [], set(), set()
    ordered = sorted(rows, key=lambda r: float(r.get("published") or r.get("first_seen") or 0), reverse=True)
    for row in ordered:
        url = safe_url(row.get("url", ""))
        headline = str(row.get("headline") or "").strip()
        publisher = str(row.get("source") or "Unknown")
        published_value = row.get("published")
        publication_available = row.get("publicationTimeAvailable")
        if publication_available is None:
            publication_available = bool(published_value)
        published = float(published_value or row.get("first_seen") or now)
        if not publication_available:
            published = float(row.get("discoveredAt") or row.get("first_seen") or now)
        if published > now + 300:
            publication_available = False
            published = float(row.get("first_seen") or now)
        event_key = " ".join(_EVENT_WORDS.sub(" ", headline.lower()).split())[:100]
        url_key = _canonical_url(url) if url else ""
        if (
            not headline or not url or url_key in seen_urls or event_key in seen_events
            or any(_same_event(symbol, row, prior["_sourceRow"]) for prior in unique)
        ):
            continue
        seen_urls.add(url_key)
        seen_events.add(event_key)
        text = f"{headline} {row.get('body', '')} {' '.join(row.get('tags') or [])}"
        strength = max(0, min(100, max(int(row.get("score") or 0), _inferred_strength(text))))
        confidence, primary = _source_confidence(publisher)
        direction = _direction(text)
        age = max(0.0, now - published)
        decay = _decay_constant(strength)
        components = _article_components(
            symbol, row, text, age, strength, confidence, 100.0
        )
        current = components["finalScore"]
        direction_label = _direction_label(direction, current)
        direction_sign = -1 if direction == "negative" else 1 if direction == "positive" else 0
        score_contract = {
            **{key: round(value, 2) for key, value in components.items()},
            "directionalImpact": round(direction_sign * current, 2),
            "direction": direction_label,
            "confidence": round(
                (components["relevance"] * 0.55)
                + (components["sourceConfidence"] * 0.45), 2
            ),
            "explanation": (
                f"{_catalyst_type(text)} significance with "
                f"{round(components['freshness'])}% trading freshness and "
                f"{round(components['sourceConfidence'])}% source confidence."
            ),
            "status": "ready",
        }
        unique.append({
            "id": row.get("id") or hashlib.sha1(f"{symbol}|{event_key}".encode()).hexdigest()[:16],
            "headline": headline, "url": url, "publisher": publisher,
            "originalPublisher": row.get("originalPublisher") or publisher,
            "discoverySource": row.get("discoverySource") or "licensed_feed",
            "yahooFinanceUrl": row.get("yahooFinanceUrl"),
            "timestampQuality": (
                (row.get("timestampQuality") or "provider")
                if publication_available else "discovered_fallback"
            ),
            "publicationTimeAvailable": bool(publication_available),
            "discoveredAt": row.get("discoveredAt") or row.get("first_seen"),
            "publishedAt": datetime.fromtimestamp(published).astimezone().isoformat(),
            "publishedTimestamp": published, "ageSeconds": int(age), "ageLabel": _age_label(age),
            "catalystStrength": strength, "sourceConfidence": confidence,
            "relevanceConfidence": round(components["relevance"] / 100.0, 4),
            "freshnessWeight": round(components["freshness"] / 100.0, 5),
            "currentScore": round(current), "direction": direction,
            "catalystType": _catalyst_type(text), "isPrimarySource": primary,
            "decayConstantSeconds": decay, "score": score_contract,
            "_sourceRow": row,
        })
        if len(unique) >= limit:
            break
    unique.sort(key=lambda article: article["score"]["finalScore"], reverse=True)
    rank_weights = (1.0, 0.55, 0.30, 0.15)
    numerator = denominator = signed_total = 0.0
    for index, article in enumerate(unique):
        contract = article["score"]
        effective = (
            rank_weights[index]
            * (contract["relevance"] / 100.0)
            * (contract["sourceConfidence"] / 100.0)
        )
        numerator += contract["finalScore"] * effective
        signed_total += contract["directionalImpact"] * effective
        denominator += effective
        article["effectiveWeight"] = round(effective, 4)
        article.pop("_sourceRow", None)
    combined = max(0.0, min(100.0, numerator / denominator)) if denominator else 0.0
    signed_average = signed_total / denominator if denominator else 0.0
    has_positive = any(a["score"]["directionalImpact"] > 10 for a in unique)
    has_negative = any(a["score"]["directionalImpact"] < -10 for a in unique)
    if has_positive and has_negative:
        direction = "mixed"
    elif signed_average >= 10:
        direction = "strong_positive" if combined >= 75 else "positive"
    elif signed_average <= -10:
        direction = "strong_negative" if combined >= 75 else "negative"
    else:
        direction = "neutral"
    confidence = (
        sum(a["score"]["confidence"] * a["effectiveWeight"] for a in unique) / denominator
        if denominator else 0.0
    )
    score = round(combined)
    return {
        "securityId": symbol.upper(), "symbol": symbol.upper(),
        "score": score, "combinedScore": round(combined, 2),
        "classification": _classification(combined),
        "direction": direction.replace("_", " ").title(),
        "combinedDirection": direction, "confidence": round(confidence, 2),
        "calculatedAt": datetime.fromtimestamp(now).astimezone().isoformat(),
        "generatedAt": datetime.fromtimestamp(now).astimezone().isoformat(),
        "scoreVersion": config.NEWS_SCORING_MODEL_VERSION,
        "scoreStatus": "ready" if unique else "no_relevant_articles",
        "aggregation": {
            "rankWeights": list(rank_weights), "articlesConsidered": len(unique),
            "weightedNumerator": round(numerator, 4),
            "weightDenominator": round(denominator, 4),
            "duplicateArticlesExcluded": max(0, len(rows) - len(unique)),
        },
        "articles": unique,
    }


def quick_signal(symbol: str, news: dict | None, previous_id: str = "", now: float | None = None) -> dict:
    """Build the inexpensive, deterministic single-headline scanner signal."""
    now = time.time() if now is None else now
    if not news or not safe_url(news.get("url", "")):
        return {"symbol": symbol.upper(), "hasNews": False, "score": 0, "direction": "neutral",
                "isNew": False, "isDuplicate": False, "checkedAt": now}
    published = float(news.get("published") or now)
    headline = str(news.get("headline") or "")
    publisher = str(news.get("source") or "Unknown")
    text = f"{headline} {news.get('summary', '')}"
    strength = max(int(news.get("score") or 0), _inferred_strength(text))
    confidence, _primary = _source_confidence(publisher)
    age = max(0.0, now - published)
    decay = _decay_constant(strength)
    freshness = math.exp(-age / decay)
    normalized = "|".join((
        safe_url(news.get("url", "")).lower().rstrip("/"),
        " ".join(_EVENT_WORDS.sub(" ", headline.lower()).split())[:100],
        publisher.lower(), str(int(published)),
    ))
    article_id = hashlib.sha1(normalized.encode()).hexdigest()[:16]
    return {
        "symbol": symbol.upper(), "hasNews": True, "headline": headline,
        "url": safe_url(news.get("url", "")), "publisher": publisher,
        "publishedAt": datetime.fromtimestamp(published).astimezone().isoformat(),
        "publishedTimestamp": published, "catalystType": _catalyst_type(text),
        "catalystStrength": strength, "sourceConfidence": confidence,
        "freshnessWeight": round(freshness, 5),
        "score": round(max(0.0, min(100.0, strength * confidence * freshness))),
        "direction": _direction(text), "isNew": article_id != previous_id,
        "isDuplicate": article_id == previous_id, "articleId": article_id,
        "decayConstantSeconds": decay, "checkedAt": now,
        "detectedAt": now if article_id != previous_id else None,
        "modelVersion": config.NEWS_SCORING_MODEL_VERSION,
    }
