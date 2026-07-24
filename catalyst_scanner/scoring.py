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


def scalp_score(row: dict, strategy_profile: str | None = None) -> float:
    """Day-trade suitability score with news as the dominant factor."""
    weights = _strategy_weights(strategy_profile)
    news_score = float(row.get("news_score", row.get("score", 0)) or 0.0)
    news_component = max(0.0, min(100.0, news_score)) * float(weights.get("news_weight", getattr(config, "SCALP_NEWS_WEIGHT", 1.55)))

    chg = float(row.get("change_pct") or 0.0)
    chg_component = max(-18.0, min(26.0, chg * float(weights.get("change_weight", getattr(config, "SCALP_CHANGE_WEIGHT", 1.15)))))

    rvol = float(row.get("rvol") or 0.0)
    rvol_component = max(
        0.0,
        min(30.0, max(0.0, rvol - 1.0) * float(weights.get("rvol_weight", getattr(config, "SCALP_RVOL_WEIGHT", 9.0)))),
    )

    dollar_volume = float(row.get("dollar_volume") or 0.0)
    if dollar_volume > 0:
        dv_scale = max(0.0, min(1.0, (log10(dollar_volume) - 5.8) / 2.4))
    else:
        dv_scale = 0.0
    dv_component = dv_scale * float(weights.get("dollar_volume_weight", getattr(config, "SCALP_DOLLAR_VOLUME_WEIGHT", 8.0)))

    flt = row.get("float_shares")
    float_component = 0.0
    if flt:
        try:
            fv = float(flt)
            # Lower float gets more squeeze potential.
            if fv <= 25_000_000:
                float_component = float(weights.get("float_weight", getattr(config, "SCALP_FLOAT_WEIGHT", 8.0)))
            elif fv <= 75_000_000:
                float_component = float(weights.get("float_weight", getattr(config, "SCALP_FLOAT_WEIGHT", 8.0))) * 0.6
            elif fv <= 200_000_000:
                float_component = float(weights.get("float_weight", getattr(config, "SCALP_FLOAT_WEIGHT", 8.0))) * 0.25
        except (TypeError, ValueError):
            float_component = 0.0

    age_seconds = float(row.get("age_seconds") or 0.0)
    recency_window = float(weights.get("recency_window_minutes", getattr(config, "SCALP_RECENCY_WINDOW_MINUTES", 45)))
    recency_component = max(0.0, 12.0 - (age_seconds / 60.0) * (12.0 / max(5.0, recency_window)))
    new_symbol_window = float(getattr(config, "SCALP_NEW_SYMBOL_WINDOW_SECONDS", 180) or 180)
    new_symbol_bonus_max = float(getattr(config, "SCALP_NEW_SYMBOL_BONUS", 22.0) or 22.0)
    if age_seconds <= 0:
        new_symbol_component = new_symbol_bonus_max
    elif age_seconds < new_symbol_window:
        new_symbol_component = new_symbol_bonus_max * (
            1.0 - (age_seconds / max(1.0, new_symbol_window))
        )
    else:
        new_symbol_component = 0.0

    source_component = _source_quality_boost(str(row.get("source", "")))
    news_quality_component = _news_quality_component(row) * float(
        weights.get(
            "news_quality_weight",
            getattr(config, "SCALP_NEWS_QUALITY_WEIGHT", 9.0),
        )
        / 9.0
    )
    acceleration_component = _acceleration_component(row) * float(
        weights.get(
            "acceleration_weight",
            getattr(config, "SCALP_ACCELERATION_WEIGHT", 8.0),
        )
        / 8.0
    )
    sentiment_component = _macro_sentiment_component(row) * float(
        weights.get(
            "sentiment_weight",
            getattr(config, "SCALP_SENTIMENT_WEIGHT", 6.0),
        )
        / 6.0
    )

    penalty = 0.0
    if row.get("dilution"):
        penalty += float(weights.get("dilution_penalty", 14.0))
    if row.get("distress"):
        penalty += float(weights.get("distress_penalty", 26.0))

    total = (
        news_component
        + chg_component
        + rvol_component
        + dv_component
        + float_component
        + recency_component
        + new_symbol_component
        + source_component
        + news_quality_component
        + acceleration_component
        + sentiment_component
        - penalty
    )
    return max(0.0, min(100.0, total))
