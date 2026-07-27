"""Score a headline for how hard it is likely to move a small cap, upward.

Design notes:

* The headline carries far more weight than the body. Wire bodies are padded
  with boilerplate that matches half the keyword list.
* Dilution language is scored separately and surfaced as its own flag rather
  than folded into the score. An offering priced off a good catalyst is still
  tradeable — you just need to see it before you click.
* Scores are capped and non-linear so a release that stuffs six buzzwords
  doesn't outrank a genuine FDA approval.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from math import log10

from . import config

SETTINGS_PATH = Path(__file__).with_name("settings.json")

TOGGLE_KEYS = {
    "Analyst upgrade": "analyst_upgrades",
    "Price target": "price_targets",
    "Strategic investment": "strategic_investments",
    "First commercial sale": "commercial_launches",
    "Commercial launch": "commercial_launches",
    "Major customer": "major_customer",
    "New contract": "new_contracts",
    "Production boost": "production_boost",
    "Order surge": "order_surge",
    "Earnings surprise": "earnings_surprise",
    "Tech breakthrough": "tech_breakthrough",
}

DEFAULT_TOGGLES = {
    "analyst_upgrades": True,
    "price_targets": True,
    "strategic_investments": True,
    "commercial_launches": True,
    "major_customer": True,
    "new_contracts": True,
    "production_boost": True,
    "order_surge": True,
    "earnings_surprise": True,
    "tech_breakthrough": True,
}

_TOGGLES = dict(DEFAULT_TOGGLES)


def _load_toggles() -> dict:
    try:
        if SETTINGS_PATH.exists():
            payload = json.loads(SETTINGS_PATH.read_text())
            if isinstance(payload, dict):
                return {k: bool(v) for k, v in payload.items() if k in DEFAULT_TOGGLES}
    except Exception:
        pass
    return dict(DEFAULT_TOGGLES)


def set_toggles(payload: dict | None = None) -> None:
    global _TOGGLES
    data = _load_toggles() if payload is None else payload
    normalized = {}
    for key, default in DEFAULT_TOGGLES.items():
        val = data.get(key, default)
        normalized[key] = bool(val)
    _TOGGLES = normalized


def get_toggles() -> dict:
    return dict(_TOGGLES)


# (pattern, points, tag) — points apply when matched in the headline.
# Body matches count for 40% of the headline value.
TIER_A = [
    (r"\bfda\s+approv(?:al|es|ed)\b", 50, "FDA approval"),
    (r"\bapprov(?:al|ed|es)\s+by\s+the\s+fda\b", 50, "FDA approval"),
    (r"\bto\s+be\s+acquired\b|\bacquisition\s+of\b.*\bby\b", 48, "M&A"),
    (r"\bdefinitive\s+(?:merger\s+)?agreement\b", 45, "M&A"),
    (r"\bmerger\s+agreement\b|\bagreement\s+to\s+merge\b", 45, "M&A"),
    (r"\btender\s+offer\b|\ball[- ]cash\s+transaction\b", 44, "M&A"),
    (r"\bto\s+acquire\b.*\bfor\s+\$[\d.]+\s*(?:billion|million)\b", 42, "M&A"),
    (r"\bmet\s+(?:the\s+)?primary\s+endpoint\b", 45, "Trial hit"),
    (r"\bstatistically\s+significant\b", 38, "Trial hit"),
    (r"\bbreakthrough\s+therapy\s+designation\b", 40, "FDA designation"),
    (r"\bpositive\s+topline\b|\btopline\s+.*\bpositive\b", 40, "Trial hit"),
    (r"\bphase\s*3\b.*\b(?:success|positive|met)\b", 42, "Trial hit"),
]

TIER_B = [
    (r"\b510\(k\)\s+clearance\b|\bfda\s+clearance\b", 32, "FDA clearance"),
    (r"\bemergency\s+use\s+authorization\b", 30, "EUA"),
    (r"\borphan\s+drug\s+designation\b", 28, "FDA designation"),
    (r"\bfast\s+track\s+designation\b", 28, "FDA designation"),
    (r"\bpriority\s+review\b", 30, "FDA designation"),
    (r"\bce\s+mark\b", 24, "CE mark"),
    (r"\bawarded\b.*\bcontract\b|\bcontract\s+award(?:ed)?\b", 32, "Contract"),
    (
        r"\bdepartment\s+of\s+defense\b|\bu\.?s\.?\s+army\b|\bu\.?s\.?\s+navy\b"
        r"|\bair\s+force\b|\bdarpa\b|\bnasa\b",
        30,
        "Gov contract",
    ),
    (r"\bpurchase\s+order\b|\breceives?\s+order\b|\bfirst\s+order\b", 26, "Order"),
    (r"\bstrategic\s+partnership\b|\bpartnership\s+with\b", 24, "Partnership"),
    (r"\blicense\s+agreement\b|\blicensing\s+deal\b", 24, "Licensing"),
    (r"\brecord\s+(?:revenue|quarter|sales|bookings)\b", 28, "Record results"),
    (
        r"\braises?\s+(?:full[- ]year\s+)?guidance\b|\bincreases?\s+guidance\b",
        30,
        "Guidance raise",
    ),
    (
        r"\bbeats?\s+(?:analyst\s+)?(?:estimates|expectations|consensus)\b",
        26,
        "Earnings beat",
    ),
    (
        r"\buplist(?:ing|s|ed)?\s+to\s+(?:the\s+)?nasdaq\b|"
        r"\bapproved\s+for\s+listing\b",
        30,
        "Uplisting",
    ),
    (r"\bshare\s+(?:re)?purchase\s+program\b|\bbuyback\b", 24, "Buyback"),
    (r"\bshort\s+squeeze\b|\bshort\s+interest\b", 20, "Short interest"),
    (
        r"\bpatent\s+(?:granted|issued|allowance)\b|\bnotice\s+of\s+allowance\b",
        22,
        "Patent",
    ),
    (
        r"\bjury\s+(?:verdict|awards)\b|\bwins?\s+(?:lawsuit|litigation|appeal)\b",
        26,
        "Legal win",
    ),
    # Additional positive catalysts
    (r"\bupgraded\s+to\s+(?:outperform|buy|overweight|strong\s+buy)\b|\bupgraded\b", 20, "Analyst upgrade"),
    (r"\btarget\s+price\s+(?:raised|increased)\b|\bnew\s+target\s+of\s+\$[\d,.]+\b", 18, "Price target"),
    (r"\bstrategic\s+investment\b|\binvests?\s+in\b|\bbacked\s+by\s+\w+\b", 26, "Strategic investment"),
    (r"\bfirst\s+commercial\s+sale\b|\bfirst\s+commercial\s+shipment\b|\bfirst\s+sale\b", 30, "First commercial sale"),
    (r"\bcommercial\s+launch\b|\blaunch(?:es|ed)\s+commercially\b|\bgoes\s+to\s+market\b", 28, "Commercial launch"),
    (r"\bmajor\s+customer\b|\bnamed\s+customer\b|\benterprise\s+customer\b", 26, "Major customer"),
]

TIER_C = [
    (r"\bletter\s+of\s+intent\b|\bmemorandum\s+of\s+understanding\b", 14, "LOI"),
    (r"\bcollaborat(?:ion|es|ing)\b", 12, "Collaboration"),
    (r"\bdistribution\s+agreement\b|\bsupply\s+agreement\b", 16, "Distribution"),
    (r"\bexpands?\s+into\b|\benters?\s+(?:the\s+)?\w+\s+market\b", 12, "Expansion"),
    (r"\blaunch(?:es|ed|ing)?\b", 12, "Launch"),
    (r"\bmilestone\s+payment\b|\bachieves?\s+milestone\b", 16, "Milestone"),
    (
        r"\binsider\s+(?:buying|purchase)\b|\bceo\s+purchases?\s+shares\b",
        20,
        "Insider buy",
    ),
    (r"\bjoins?\s+(?:the\s+)?(?:russell|s&p)\b|\bindex\s+inclusion\b", 20, "Index add"),
    (r"\bdividend\s+(?:increase|initiation)\b|\bspecial\s+dividend\b", 18, "Dividend"),
    (r"\bnamed\s+to\b|\bappoints?\b.*\b(?:ceo|cfo|president)\b", 8, "Management"),
    # Sector / business-cycle positive patterns
    (r"\bnew\s+contract\b|\bmajor\s+contract\b|\brenew(?:al|ed)\s+contract\b", 16, "New contract"),
    (r"\bproduction\s+increase\b|\bcapacity\s+expansion\b|\bexpands\s+production\b", 14, "Production boost"),
    (r"\borders\s+surge\b|\bbookings\s+surge\b|\bstrong\s+orders\b", 16, "Order surge"),
    (r"\bearnings\s+surprise\b|\bbeat\s+quarterly\s+estimates\b", 18, "Earnings surprise"),
    (r"\bnew\s+technology\b|\btechnology\s+breakthrough\b|\bproduct\s+launch\b", 14, "Tech breakthrough"),
]

# Money magnitudes inside a catalyst headline scale it up.
MONEY_RE = re.compile(r"\$\s?([\d,.]+)\s*(billion|million|bn|mm|m|b)\b", re.IGNORECASE)

# Dilution / distress language — flagged, and score-damped.
DILUTION = [
    (r"\bpublic\s+offering\b", "Offering"),
    (r"\bregistered\s+direct\s+offering\b", "Reg direct"),
    (r"\bprivate\s+placement\b", "Placement"),
    (r"\bpricing\s+of\b.*\boffering\b", "Priced offering"),
    (r"\bat[- ]the[- ]market\b|\batm\s+(?:program|facility|offering)\b", "ATM"),
    (r"\bconvertible\s+(?:note|debenture|preferred)\b", "Convertible"),
    (r"\bwarrant\s+(?:exercise|inducement|repricing)\b", "Warrants"),
    (r"\bshelf\s+registration\b|\bform\s+s-3\b", "Shelf"),
    (r"\breverse\s+(?:stock\s+)?split\b", "Reverse split"),
    (r"\bequity\s+line\s+of\s+credit\b|\bstandby\s+equity\b", "ELOC"),
]

DISTRESS = [
    (r"\bgoing\s+concern\b", "Going concern"),
    (r"\bchapter\s+11\b|\bbankruptcy\b|\breceivership\b", "Bankruptcy"),
    (
        r"\bdeficiency\s+letter\b|\bnon[- ]compliance\s+with\s+nasdaq\b"
        r"|\bdelisting\b",
        "Delisting risk",
    ),
    (
        r"\bclass\s+action\b|\bsecurities\s+fraud\b|\bsec\s+investigation\b",
        "Litigation",
    ),
    (r"\brestat(?:es|ement|ing)\b", "Restatement"),
    (
        r"\bclinical\s+hold\b|\bfails?\s+to\s+meet\b|\bmissed\s+(?:the\s+)?"
        r"primary\s+endpoint\b|"
        r"\bdiscontinu(?:es|ed|ing)\s+(?:the\s+)?(?:trial|study)\b",
        "Trial fail",
    ),
    (r"\bcomplete\s+response\s+letter\b|\bcrl\b", "CRL"),
    (
        r"\bwithdraws?\s+guidance\b|\bcuts?\s+guidance\b|\blowers?\s+guidance\b",
        "Guidance cut",
    ),
]

# Noise that should never reach the board.
JUNK = re.compile(
    r"\b(?:webinar|conference\s+call|to\s+present\s+at|to\s+participate\s+in"
    r"|investor\s+conference|earnings\s+call\s+scheduled|to\s+report\s+"
    r"(?:first|second|third|fourth)\s+quarter|annual\s+meeting\s+of\s+"
    r"stockholders|holiday\s+schedule|newsletter)\b",
    re.IGNORECASE,
)

_COMPILED = [
    (re.compile(p, re.IGNORECASE), pts, tag)
    for group in (TIER_A, TIER_B, TIER_C)
    for p, pts, tag in group
]


def _is_tag_enabled(tag: str, toggles: dict | None = None) -> bool:
    if toggles is None:
        toggles = _TOGGLES
    key = TOGGLE_KEYS.get(tag)
    if key is None:
        return True
    return bool(toggles.get(key, DEFAULT_TOGGLES.get(key, True)))
_DILUTION = [(re.compile(p, re.IGNORECASE), t) for p, t in DILUTION]
_DISTRESS = [(re.compile(p, re.IGNORECASE), t) for p, t in DISTRESS]

set_toggles(_load_toggles())


def _money_multiplier(text: str) -> float:
    """Bigger headline dollar figures mean a bigger reprice, up to a point."""
    best = 1.0
    for m in MONEY_RE.finditer(text):
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = m.group(2).lower()
        if unit in ("billion", "bn", "b"):
            amount *= 1000
        if amount >= 500:
            best = max(best, 1.35)
        elif amount >= 100:
            best = max(best, 1.25)
        elif amount >= 25:
            best = max(best, 1.15)
        elif amount >= 5:
            best = max(best, 1.07)
    return best


def score(headline: str, body: str = "", source_weight: float = 1.0, toggles: dict | None = None) -> dict:
    """Return {score, tags, dilution, distress, junk} for one release."""
    headline = headline or ""
    body = body or ""
    toggles = get_toggles() if toggles is None else toggles

    # Flags are evaluated first and always reported, even when nothing bullish
    # matched — a bare offering scores zero but you still want it labelled if
    # you widen the filters or pull the row up by hand.
    dilution = [t for p, t in _DILUTION if p.search(headline) or p.search(body[:400])]
    distress = [t for p, t in _DISTRESS if p.search(headline)]

    if JUNK.search(headline):
        return {
            "score": 0,
            "tags": [],
            "dilution": dilution[:2],
            "distress": distress[:2],
            "junk": True,
        }

    hits: list[tuple[int, str]] = []
    for pattern, points, tag in _COMPILED:
        if not _is_tag_enabled(tag, toggles):
            continue
        if pattern.search(headline):
            hits.append((points, tag))
        elif body and pattern.search(body):
            hits.append((int(points * 0.4), tag))

    if not hits:
        return {
            "score": 0,
            "tags": [],
            "dilution": dilution[:2],
            "distress": distress[:2],
            "junk": False,
        }

    # Strongest hit counts fully; each additional one contributes less, so
    # keyword-stuffed releases can't outrank a single real catalyst.
    hits.sort(key=lambda h: -h[0])
    raw = 0.0
    for i, (points, _) in enumerate(hits):
        raw += points * (0.45**i)

    raw *= _money_multiplier(headline)
    raw *= source_weight

    if dilution:
        raw *= 0.45  # damped, not deleted — you still want to see it
    if distress:
        raw *= 0.20

    # Dedup tags, preserving the order the strongest hits appeared in.
    tags: list[str] = []
    for _, tag in hits:
        if tag not in tags:
            tags.append(tag)

    return {
        "score": int(round(min(raw, 100))),
        "tags": tags[:4],
        "dilution": dilution[:2],
        "distress": distress[:2],
        "junk": False,
    }


def heat(row: dict) -> float:
    """Composite ranking: catalyst strength, then confirmation from the tape."""
    value = float(row.get("score", 0))
    chg = row.get("change_pct")
    if chg is not None:
        value += max(-20.0, min(45.0, float(chg) * 1.6))
    rvol = row.get("rvol")
    if rvol:
        value += min(25.0, (float(rvol) - 1.0) * 6.0)
    age_min = max(0.0, row.get("age_seconds", 0) / 60.0)
    value += max(0.0, 18.0 - age_min * 0.9)  # first ~20 minutes get a boost
    return value


def _strategy_weights(strategy_profile: str | None) -> dict:
    profiles = getattr(config, "SCALP_STRATEGY_PROFILES", {}) or {}
    default_key = getattr(config, "SCALP_STRATEGY_DEFAULT", "balanced")
    key = (strategy_profile or default_key or "balanced").strip().lower()
    if key not in profiles:
        key = default_key if default_key in profiles else "balanced"
    return dict(profiles.get(key, {}))


def _source_quality_boost(source: str) -> float:
    s = (source or "").lower()
    boost = 0.0
    if "sec" in s:
        boost += 8.0
    if "businesswire" in s or "globenewswire" in s or "pr newswire" in s:
        boost += 4.5
    elif "accesswire" in s or "newsfile" in s or "stocktitan" in s:
        boost += 3.0
    if "confirmed news" in s or "yahoo finance" in s:
        boost += 3.5
    if "streetinsider" in s or "seeking alpha" in s:
        boost += 1.5
    return min(12.0, boost)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _piecewise_score(value: float, points: list[tuple[float, float]]) -> float:
    """Piecewise linear interpolation for scalar->score mappings."""
    if not points:
        return 0.0
    ordered = sorted(points, key=lambda p: p[0])
    if value <= ordered[0][0]:
        return ordered[0][1]
    if value >= ordered[-1][0]:
        return ordered[-1][1]
    for i in range(1, len(ordered)):
        x0, y0 = ordered[i - 1]
        x1, y1 = ordered[i]
        if x0 <= value <= x1:
            if x1 == x0:
                return y1
            t = (value - x0) / (x1 - x0)
            return y0 + ((y1 - y0) * t)
    return ordered[-1][1]


_POSITIVE_MACRO = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\brate\s+cut\b",
        r"\bstimulus\b|\bsubsid(?:y|ies)\b|\bgrant\b",
        r"\bgovernment\s+contract\b|\bdefense\s+contract\b",
        r"\bfavorable\s+regulat(?:ion|ory)\b",
        r"\btrade\s+deal\b",
    ]
]
_NEGATIVE_MACRO = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\brate\s+hike\b",
        r"\btariff(?:s)?\b|\bsanction(?:s)?\b|\bexport\s+restriction(?:s)?\b",
        r"\bgeopolitical\s+tension(?:s)?\b|\bwar\b",
        r"\bshutdown\b|\bdefault\s+risk\b",
        r"\bpolitical\s+uncertainty\b|\bpolicy\s+risk\b",
    ]
]


def _news_quality_component(row: dict) -> float:
    source = str(row.get("source") or "")
    url = str(row.get("url") or "")
    tags = [str(t).lower() for t in (row.get("tags") or [])]
    has_article = bool(url and url != "#")
    has_confirmed = "confirmed news" in source.lower() or "yahoo finance" in source.lower()
    tag_quality = 0.0
    if any("fda" in t or "m&a" in t or "trial" in t for t in tags):
        tag_quality += 3.5
    if any("contract" in t or "partnership" in t or "guidance" in t for t in tags):
        tag_quality += 2.0
    quality = _source_quality_boost(source) * 0.55
    if has_article:
        quality += 2.0
    if has_confirmed:
        quality += 3.0
    quality += tag_quality
    return max(0.0, min(14.0, quality))


def _news_quality_multiplier(row: dict) -> float:
    source = str(row.get("source") or "").lower()
    tags = [str(t).lower() for t in (row.get("tags") or [])]
    headline = str(row.get("headline") or "").lower()
    if any("fda approval" in t for t in tags):
        return 1.30
    if any("m&a" in t for t in tags) or "acquir" in headline or "buyout" in headline:
        return 1.25
    if any("trial hit" in t for t in tags) or "phase 3" in headline:
        return 1.20
    if "conference" in headline or "to present at" in headline:
        return 0.90
    if "businesswire" in source or "globenewswire" in source or "pr newswire" in source:
        return 1.08
    if "accesswire" in source or "newsfile" in source:
        return 0.97
    return 1.00


def _source_reliability_multiplier(row: dict) -> float:
    """Credibility adjustment for the news component, not the tape component."""
    source = str(row.get("source") or "").lower()
    sources = {
        str(item).strip().lower()
        for item in (row.get("sources") or [])
        if str(item).strip()
    }
    if "sec" in source:
        value = 1.14
    elif any(
        name in source
        for name in ("businesswire", "globenewswire", "pr newswire")
    ):
        value = 1.08
    elif any(name in source for name in ("accesswire", "newsfile")):
        value = 1.01
    elif any(name in source for name in ("reddit", "stocktwits", "social")):
        value = 0.72
    elif any(name in source for name in ("seeking alpha", "streetinsider")):
        value = 0.92
    else:
        value = 0.96
    if len(sources) >= 2:
        value += min(0.08, 0.03 * (len(sources) - 1))
    if not row.get("url") or row.get("url") == "#":
        value -= 0.04
    return _clamp(value, 0.65, 1.20)


def _acceleration_component(row: dict) -> float:
    chg_accel = float(row.get("scan_change_accel") or 0.0)
    vol_accel = float(row.get("scan_volume_accel") or 0.0)
    rvol_delta = float(row.get("scan_rvol_delta") or 0.0)
    # Scale and cap acceleration signals so they inform rank without dominating.
    chg_term = max(-6.0, min(10.0, chg_accel * 4.0))
    vol_term = 0.0
    if vol_accel > 0:
        vol_term = min(9.0, log10(vol_accel + 1.0) * 2.3)
    elif vol_accel < 0:
        vol_term = max(-5.0, -log10(abs(vol_accel) + 1.0) * 1.7)
    rvol_term = max(-4.0, min(7.0, rvol_delta * 5.0))
    return chg_term + vol_term + rvol_term


def _macro_sentiment_component(row: dict) -> float:
    text = " ".join(
        [
            str(row.get("headline") or ""),
            str(row.get("body") or ""),
            str(row.get("source") or ""),
            " ".join(str(t) for t in (row.get("tags") or [])),
        ]
    )
    pos = sum(1 for p in _POSITIVE_MACRO if p.search(text))
    neg = sum(1 for p in _NEGATIVE_MACRO if p.search(text))
    raw = (pos * 2.0) - (neg * 2.5)
    return max(-8.0, min(8.0, raw))


def _market_regime_multiplier(row: dict) -> float:
    """Conservative regime multiplier until explicit SPY/QQQ/VIX feeds are wired."""
    sentiment = _macro_sentiment_component(row)
    multiplier = 1.0 + (sentiment / 100.0)  # ~0.92 to ~1.08
    session = str(row.get("session") or "").lower()
    if session == "closed":
        multiplier *= 0.97
    return _clamp(multiplier, 0.88, 1.12)


def _news_age_decay(age_seconds: float) -> float:
    age_min = max(0.0, age_seconds / 60.0)
    if age_min <= 0.5:
        return 1.00
    if age_min <= 2.0:
        return _piecewise_score(age_min, [(0.5, 1.00), (2.0, 0.95)])
    if age_min <= 5.0:
        return _piecewise_score(age_min, [(2.0, 0.95), (5.0, 0.90)])
    if age_min <= 10.0:
        return _piecewise_score(age_min, [(5.0, 0.90), (10.0, 0.75)])
    if age_min <= 20.0:
        return _piecewise_score(age_min, [(10.0, 0.75), (20.0, 0.55)])
    if age_min <= 60.0:
        return _piecewise_score(age_min, [(20.0, 0.55), (60.0, 0.35)])
    if age_min <= 120.0:
        return _piecewise_score(age_min, [(60.0, 0.35), (120.0, 0.20)])
    if age_min <= 240.0:
        return _piecewise_score(age_min, [(120.0, 0.20), (240.0, 0.10)])
    return 0.05


def _scanner_age_score(age_seconds: float, row: dict | None = None) -> float:
    age_seconds = max(0.0, age_seconds)
    fresh = float(config.SCANNER_AGE_FRESH_WINDOW_SECONDS)
    confirming = float(config.SCANNER_AGE_CONFIRMATION_WINDOW_SECONDS)
    stable = float(config.SCANNER_AGE_STABLE_WINDOW_SECONDS)
    entry_points = float(config.SCANNER_AGE_FRESH_ENTRY_POINTS)
    maximum = float(config.SCANNER_AGE_MAX_PERSISTENCE_POINTS)
    if age_seconds <= fresh:
        value = _piecewise_score(age_seconds, [(0, entry_points), (fresh, 84.0)])
    elif age_seconds <= confirming:
        value = _piecewise_score(age_seconds, [(fresh, 84.0), (confirming, maximum)])
    elif age_seconds <= stable:
        value = _piecewise_score(age_seconds, [(confirming, maximum), (stable, 88.0)])
    else:
        half_life = max(60.0, float(config.SCANNER_AGE_STALE_HALF_LIFE_SECONDS))
        value = 88.0 * (0.5 ** ((age_seconds - stable) / half_life))
    if row:
        confirmation = (
            float(row.get("scan_change_accel") or 0)
            + min(1.0, float(row.get("scan_rvol_delta") or 0))
        )
        if confirmation > 0:
            value *= 1.05
        elif confirmation < 0:
            value *= 0.88
    return _clamp(value, 5.0, maximum)


def _volume_acceleration_score(row: dict) -> float:
    prev_vol = float(row.get("volume") or 0.0) - float(row.get("scan_volume_delta") or 0.0)
    vol_delta = float(row.get("scan_volume_delta") or 0.0)
    if prev_vol <= 0:
        accel_ratio = 0.0
    else:
        accel_ratio = vol_delta / prev_vol
    accel_ratio = max(accel_ratio, float(row.get("scan_volume_accel") or 0.0) / max(prev_vol, 1.0))
    accel_pct = accel_ratio * 100.0
    return _clamp(
        _piecewise_score(
            accel_pct,
            [
                (0.0, 10.0),
                (10.0, 12.0),
                (25.0, 30.0),
                (50.0, 60.0),
                (100.0, 85.0),
                (200.0, 100.0),
            ],
        ),
        0.0,
        100.0,
    )


def _rvol_score(row: dict) -> float:
    rvol = max(0.0, float(row.get("rvol") or 0.0))
    return _clamp(
        _piecewise_score(
            rvol,
            [(1.0, 10.0), (2.0, 30.0), (3.0, 50.0), (5.0, 75.0), (8.0, 90.0), (10.0, 100.0)],
        ),
        0.0,
        100.0,
    )


def _price_momentum_score(row: dict) -> float:
    chg = float(row.get("change_pct") or 0.0)
    chg3 = float(row.get("change_pct_3m") or 0.0)
    chg10 = float(row.get("change_pct_10m") or 0.0)
    accel = float(row.get("scan_change_accel") or 0.0)
    base = _piecewise_score(
        abs(chg),
        [(0.0, 5.0), (1.0, 15.0), (3.0, 35.0), (6.0, 60.0), (10.0, 80.0), (20.0, 100.0)],
    )
    trend_bonus = 0.0
    if chg > 0 and chg3 >= 0 and chg10 >= 0:
        trend_bonus += 8.0
    if chg < 0 and chg3 <= 0 and chg10 <= 0:
        trend_bonus += 8.0
    accel_bonus = _clamp(accel * 10.0, -12.0, 12.0)
    return _clamp(base + trend_bonus + accel_bonus, 0.0, 100.0)


def _float_score(row: dict) -> float:
    flt = row.get("float_shares")
    try:
        fv = float(flt)
    except (TypeError, ValueError):
        return 45.0
    if fv < 5_000_000:
        return 100.0
    if fv < 10_000_000:
        return 90.0
    if fv < 20_000_000:
        return 75.0
    if fv < 50_000_000:
        return 50.0
    if fv < 100_000_000:
        return 20.0
    return 5.0


def _liquidity_score(row: dict) -> float:
    """Reward executable activity without making mega-volume dominate rank."""
    dollar_volume = float(row.get("dollar_volume") or 0.0)
    volume = float(row.get("volume") or 0.0)
    dollar_component = _piecewise_score(
        dollar_volume,
        [
            (0.0, 0.0),
            (250_000.0, 12.0),
            (1_000_000.0, 35.0),
            (5_000_000.0, 65.0),
            (20_000_000.0, 88.0),
            (75_000_000.0, 100.0),
        ],
    )
    share_component = _piecewise_score(
        volume,
        [
            (0.0, 0.0),
            (100_000.0, 15.0),
            (500_000.0, 45.0),
            (2_000_000.0, 75.0),
            (10_000_000.0, 100.0),
        ],
    )
    return _clamp((dollar_component * 0.7) + (share_component * 0.3), 0.0, 100.0)


def _tape_freshness(row: dict) -> float:
    """Confidence in quote-derived features during a live market session."""
    if str(row.get("session") or "").lower() == "closed":
        return 1.0
    raw_age = row.get("quote_age")
    if raw_age is None:
        return 0.70
    age = max(0.0, float(raw_age))
    return _clamp(
        _piecewise_score(
            age,
            [(0.0, 1.0), (10.0, 1.0), (30.0, 0.92), (90.0, 0.70), (180.0, 0.45)],
        ),
        0.35,
        1.0,
    )


def _candlestick_component(row: dict) -> float:
    """Score pattern confluence and alignment with the observed price direction."""
    pattern = _clamp(float(row.get("candlestick_score") or 0.0), -100.0, 100.0)
    confidence = _clamp(
        float(row.get("candlestick_confidence") or 0.0) / 100.0, 0.0, 1.0
    )
    change = float(row.get("change_pct_3m") or row.get("change_pct") or 0.0)
    if pattern == 0:
        return 35.0
    aligned = (pattern > 0 and change >= 0) or (pattern < 0 and change <= 0)
    directional = abs(pattern) * (0.75 + (0.25 * confidence))
    if aligned:
        directional += 12.0
    else:
        directional *= 0.58
    return _clamp(directional, 0.0, 100.0)


def _volatility_expansion_score(row: dict) -> float:
    accel = abs(float(row.get("scan_change_accel") or 0.0))
    vol_accel = abs(float(row.get("scan_volume_accel") or 0.0))
    rvol_delta = abs(float(row.get("scan_rvol_delta") or 0.0))
    chg3 = abs(float(row.get("change_pct_3m") or 0.0))
    chg10 = abs(float(row.get("change_pct_10m") or 0.0))
    score = 0.0
    score += _clamp(accel * 35.0, 0.0, 30.0)
    score += _clamp(log10(vol_accel + 1.0) * 8.0, 0.0, 30.0)
    score += _clamp(rvol_delta * 22.0, 0.0, 20.0)
    score += _clamp((chg3 + chg10) * 3.0, 0.0, 20.0)
    return _clamp(score, 0.0, 100.0)


def _order_flow_proxy_score(row: dict) -> float:
    vol_delta = float(row.get("scan_volume_delta") or 0.0)
    rvol_delta = float(row.get("scan_rvol_delta") or 0.0)
    chg_delta = float(row.get("scan_change_delta") or 0.0)
    score = 50.0
    score += _clamp(log10(max(0.0, vol_delta) + 1.0) * 6.0, 0.0, 25.0)
    score += _clamp(rvol_delta * 20.0, -15.0, 20.0)
    score += _clamp(chg_delta * 8.0, -15.0, 15.0)
    return _clamp(score, 0.0, 100.0)


def _technical_structure_score(row: dict) -> float:
    chg = float(row.get("change_pct") or 0.0)
    chg3 = float(row.get("change_pct_3m") or 0.0)
    chg10 = float(row.get("change_pct_10m") or 0.0)
    accel = float(row.get("scan_change_accel") or 0.0)
    score = 40.0
    if chg >= chg3 >= chg10 and chg > 0:
        score += 35.0
    elif chg <= chg3 <= chg10 and chg < 0:
        score += 35.0
    if accel > 0 and chg > 0:
        score += 12.0
    if accel < 0 and chg < 0:
        score += 12.0
    if abs(chg3) > abs(chg10) and abs(chg) > abs(chg3):
        score += 10.0
    return _clamp(score, 0.0, 100.0)


def _profile_weight_overrides(strategy_profile: str | None) -> dict[str, float]:
    base = {
        "news": 0.30,
        "age": 0.15,
        "volume_accel": 0.15,
        "rvol": 0.10,
        "price_momentum": 0.10,
        "float": 0.05,
        "volatility": 0.05,
        "order_flow": 0.05,
        "technical": 0.05,
        "liquidity": 0.07,
        "candlestick": 0.08,
    }
    profile = (strategy_profile or getattr(config, "SCALP_STRATEGY_DEFAULT", "balanced")).strip().lower()
    if profile == "news_first":
        base["news"] *= 1.22
        base["age"] *= 1.10
        base["volume_accel"] *= 0.82
        base["rvol"] *= 0.85
        base["price_momentum"] *= 0.85
    elif profile == "momentum_first":
        base["news"] *= 0.82
        base["age"] *= 0.90
        base["volume_accel"] *= 1.30
        base["rvol"] *= 1.28
        base["price_momentum"] *= 1.28
        base["volatility"] *= 1.18
        base["liquidity"] *= 1.12
        base["candlestick"] *= 1.18
    total = sum(base.values())
    if total <= 0:
        return base
    return {k: (v / total) for k, v in base.items()}


def scalp_score_ledger(row: dict, strategy_profile: str | None = None) -> dict:
    """Return the authoritative, exactly reconciling score component ledger."""
    news_age_seconds = max(
        0.0,
        float(row.get("published_age_seconds", row.get("age_seconds", 0.0)) or 0.0),
    )
    scanner_age_seconds = max(0.0, float(row.get("scanner_age_seconds") or 0.0))
    news_score_raw = _clamp(float(row.get("news_score", row.get("score", 0)) or 0.0), 0.0, 100.0)
    if row.get("quick_news"):
        # Quick-news already includes source confidence and publication-age
        # decay. Applying the legacy alert decay again would double-decay it.
        news_score = news_score_raw
    else:
        news_score = (
            news_score_raw
            * _news_age_decay(news_age_seconds)
            * _news_quality_multiplier(row)
            * _source_reliability_multiplier(row)
        )
    age_score = _scanner_age_score(scanner_age_seconds, row)
    vol_accel_score = _volume_acceleration_score(row)
    rvol_score = _rvol_score(row)
    momentum_score = _price_momentum_score(row)
    float_score = _float_score(row)
    vol_expansion_score = _volatility_expansion_score(row)
    order_flow_score = _order_flow_proxy_score(row)
    technical_score = _technical_structure_score(row)
    liquidity_score = _liquidity_score(row)
    candlestick_score = _candlestick_component(row)
    tape_freshness = _tape_freshness(row)

    weights = _profile_weight_overrides(strategy_profile)
    raw = {
        "Breaking News": weights["news"] * news_score,
        "Scanner Freshness": weights["age"] * age_score,
        "Volume Acceleration": tape_freshness * weights["volume_accel"] * vol_accel_score,
        "Relative Volume": tape_freshness * weights["rvol"] * rvol_score,
        "Price Momentum": tape_freshness * weights["price_momentum"] * momentum_score,
        "Float": tape_freshness * weights["float"] * float_score,
        "Volatility": tape_freshness * weights["volatility"] * vol_expansion_score,
        "Order Flow": tape_freshness * weights["order_flow"] * order_flow_score,
        "Technical Structure": tape_freshness * weights["technical"] * technical_score,
        "Liquidity": tape_freshness * weights["liquidity"] * liquidity_score,
        "Candlestick Patterns": tape_freshness * weights["candlestick"] * candlestick_score,
    }
    multiplier = _market_regime_multiplier(row)
    if row.get("dilution"):
        multiplier *= 0.82
    if row.get("distress"):
        multiplier *= 0.70
    components = [
        {"name": name, "contribution": value * multiplier}
        for name, value in raw.items()
    ]
    component_sum = sum(item["contribution"] for item in components)
    total = _clamp(component_sum, 0.0, 100.0)
    if component_sum > 0 and total != component_sum:
        scale = total / component_sum
        for item in components:
            item["contribution"] *= scale
    return {
        "components": components,
        "total": total,
        "tapeFreshness": tape_freshness,
        "combinedMultiplier": multiplier,
    }


def scalp_score(row: dict, strategy_profile: str | None = None) -> float:
    """Probability-style momentum score (0-100) for near-term significant moves."""
    return scalp_score_ledger(row, strategy_profile)["total"]


def score_explanation(row: dict, strategy_profile: str | None = None, full: bool = False) -> dict:
    """Build an on-demand deterministic explanation from existing row inputs."""
    ledger = scalp_score_ledger(row, strategy_profile)
    news_age_seconds = max(
        0.0, float(row.get("published_age_seconds", row.get("age_seconds", 0)) or 0)
    )
    scanner_age_seconds = max(0.0, float(row.get("scanner_age_seconds") or 0))
    raw_news = _clamp(float(row.get("news_score") or 0), 0, 100)
    news_value = raw_news if row.get("quick_news") else (
        raw_news * _news_age_decay(news_age_seconds)
        * _news_quality_multiplier(row) * _source_reliability_multiplier(row)
    )
    weights = _profile_weight_overrides(strategy_profile)
    values = {
        "Breaking News": news_value,
        "Scanner Freshness": _scanner_age_score(scanner_age_seconds, row),
        "Volume Acceleration": _volume_acceleration_score(row),
        "Relative Volume": _rvol_score(row),
        "Price Momentum": _price_momentum_score(row),
        "Float": _float_score(row),
        "Volatility": _volatility_expansion_score(row),
        "Order Flow": _order_flow_proxy_score(row),
        "Technical Structure": _technical_structure_score(row),
        "Liquidity": _liquidity_score(row),
        "Candlestick Patterns": _candlestick_component(row),
    }
    keys = {
        "Breaking News": "news", "Scanner Freshness": "age",
        "Volume Acceleration": "volume_accel", "Relative Volume": "rvol",
        "Price Momentum": "price_momentum", "Float": "float",
        "Volatility": "volatility", "Order Flow": "order_flow",
        "Technical Structure": "technical", "Liquidity": "liquidity",
        "Candlestick Patterns": "candlestick",
    }
    factors = []
    rising = {
        "Volume Acceleration": float(row.get("scan_volume_accel") or 0) > 0,
        "Relative Volume": float(row.get("scan_rvol_delta") or 0) > 0,
        "Price Momentum": float(row.get("scan_change_accel") or 0) > 0,
    }
    fading_names = {"Breaking News", "Scanner Freshness"}
    for name, value in values.items():
        contribution = round(weights[keys[name]] * value, 1)
        status = "rising" if rising.get(name) else "fading" if name in fading_names and contribution > 0 else "steady"
        factors.append({"name": name, "contribution": contribution, "status": status})
    penalties = []
    if row.get("dilution"):
        penalties.append({"name": "Dilution risk", "contribution": -round(float(row.get("score") or 0) * .18, 1)})
    if row.get("distress"):
        penalties.append({"name": "Distress risk", "contribution": -round(float(row.get("score") or 0) * .30, 1)})
    if row.get("rvol") is not None and float(row.get("rvol") or 0) < 1:
        penalties.append({"name": "Low relative volume", "contribution": -4.0})
    factors.sort(key=lambda item: abs(item["contribution"]), reverse=True)
    biggest = factors[0] if factors else {"name": "No dominant factor", "contribution": 0}
    strengthening = [f for f in factors if f["status"] == "rising" and f["contribution"] > 0][:3]
    fading = []
    if raw_news > 0:
        remaining = round(100 * (news_value / raw_news), 1)
        fading.append({
            "name": "News Freshness", "current": round(weights["news"] * news_value, 1),
            "peak": round(weights["news"] * raw_news, 1), "remainingPercent": remaining,
            "decaySpeed": "Rapidly decaying" if remaining < 50 else "Gradually decaying",
            "reason": f"The latest catalyst is {round(news_age_seconds / 60)} minutes old.",
        })
    confidence_reasons = []
    confidence = 55
    if row.get("quote_age") is not None and float(row["quote_age"]) < 30:
        confidence += 15; confidence_reasons.append("Fresh market quote")
    if row.get("rvol") is not None:
        confidence += 10; confidence_reasons.append("Relative-volume data available")
    if row.get("quick_news"):
        confidence += 8; confidence_reasons.append(
            "Detailed news cached" if not row.get("quick_news_preliminary") else "Preliminary quick-news signal"
        )
    confidence = min(95, confidence)
    headline = str(row.get("quick_news_headline") or row.get("headline") or "")
    summary = (
        f"The score is driven primarily by {biggest['name'].lower()}. "
        + ("Volume and price confirmation are strengthening." if strengthening else "Current confirmation factors are steady.")
        + (" News influence is decaying with time." if fading else "")
    )
    result = {
        "symbol": str(row.get("ticker") or "").upper(),
        "score": round(float(row.get("score") or 0)),
        "summary": summary,
        "primaryDriver": headline or biggest["name"],
        "biggestInfluence": biggest,
        "strengthening": strengthening,
        "fading": fading[:3],
        "negativeInfluences": penalties[:3],
        "confidence": confidence,
        "confidenceLabel": "High" if confidence >= 80 else "Medium" if confidence >= 60 else "Low",
        "confidenceReasons": confidence_reasons[:3],
        "newsBasis": "quick" if row.get("quick_news_preliminary") else "detailed_cached" if row.get("quick_news") else "none",
        "scannerTiming": {
            "ageSeconds": round(scanner_age_seconds),
            "state": row.get("scanner_age_state") or "fresh",
            "contribution": round(
                weights["age"] * _scanner_age_score(scanner_age_seconds, row), 1
            ),
        },
        "modelVersion": config.SCORE_DETAILS_MODEL_VERSION,
        "scoreLedger": {
            "components": [
                {**item, "contribution": round(item["contribution"], 4)}
                for item in ledger["components"]
            ],
            "total": round(ledger["total"], 4),
            "combinedMultiplier": round(ledger["combinedMultiplier"], 4),
            "tapeFreshness": round(ledger["tapeFreshness"], 4),
            "reconciles": abs(
                sum(item["contribution"] for item in ledger["components"])
                - ledger["total"]
            ) < 1e-9,
        },
    }
    metric_changes = row.get("metric_changes") or {}
    refresh_changes = {
        "trend": row.get("refresh_trend") or "Stable",
        "improving": [],
        "regressing": [],
        "unchanged": [],
    }
    for name, change in metric_changes.items():
        if not isinstance(change, dict):
            continue
        direction = change.get("direction", "unknown")
        item = {
            "name": name,
            "direction": direction,
            "previousValue": change.get("previousValue"),
            "currentValue": change.get("currentValue"),
            "reason": change.get("reason"),
        }
        if direction in refresh_changes:
            refresh_changes[direction].append(item)
    result["refreshChanges"] = refresh_changes
    if full:
        result["allFactors"] = factors
        result["diagnostics"] = {
            "strategyProfile": strategy_profile or config.SCALP_STRATEGY_DEFAULT,
            "marketState": row.get("market_state"),
            "candlestickScore": row.get("candlestick_score", 0),
            "quoteAgeSeconds": row.get("quote_age"),
            "note": "Full article analysis remains available only from the separate news control.",
        }
    return result
