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

import re

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
    (r"\bdepartment\s+of\s+defense\b|\bu\.?s\.?\s+army\b|\bu\.?s\.?\s+navy\b"
     r"|\bair\s+force\b|\bdarpa\b|\bnasa\b", 30, "Gov contract"),
    (r"\bpurchase\s+order\b|\breceives?\s+order\b|\bfirst\s+order\b", 26, "Order"),
    (r"\bstrategic\s+partnership\b|\bpartnership\s+with\b", 24, "Partnership"),
    (r"\blicense\s+agreement\b|\blicensing\s+deal\b", 24, "Licensing"),
    (r"\brecord\s+(?:revenue|quarter|sales|bookings)\b", 28, "Record results"),
    (r"\braises?\s+(?:full[- ]year\s+)?guidance\b|\bincreases?\s+guidance\b",
     30, "Guidance raise"),
    (r"\bbeats?\s+(?:analyst\s+)?(?:estimates|expectations|consensus)\b",
     26, "Earnings beat"),
    (r"\buplist(?:ing|s|ed)?\s+to\s+(?:the\s+)?nasdaq\b|\bapproved\s+for\s+listing\b",
     30, "Uplisting"),
    (r"\bshare\s+(?:re)?purchase\s+program\b|\bbuyback\b", 24, "Buyback"),
    (r"\bshort\s+squeeze\b|\bshort\s+interest\b", 20, "Short interest"),
    (r"\bpatent\s+(?:granted|issued|allowance)\b|\bnotice\s+of\s+allowance\b",
     22, "Patent"),
    (r"\bjury\s+(?:verdict|awards)\b|\bwins?\s+(?:lawsuit|litigation|appeal)\b",
     26, "Legal win"),
]

TIER_C = [
    (r"\bletter\s+of\s+intent\b|\bmemorandum\s+of\s+understanding\b", 14, "LOI"),
    (r"\bcollaborat(?:ion|es|ing)\b", 12, "Collaboration"),
    (r"\bdistribution\s+agreement\b|\bsupply\s+agreement\b", 16, "Distribution"),
    (r"\bexpands?\s+into\b|\benters?\s+(?:the\s+)?\w+\s+market\b", 12, "Expansion"),
    (r"\blaunch(?:es|ed|ing)?\b", 12, "Launch"),
    (r"\bmilestone\s+payment\b|\bachieves?\s+milestone\b", 16, "Milestone"),
    (r"\binsider\s+(?:buying|purchase)\b|\bceo\s+purchases?\s+shares\b",
     20, "Insider buy"),
    (r"\bjoins?\s+(?:the\s+)?(?:russell|s&p)\b|\bindex\s+inclusion\b",
     20, "Index add"),
    (r"\bdividend\s+(?:increase|initiation)\b|\bspecial\s+dividend\b", 18, "Dividend"),
    (r"\bnamed\s+to\b|\bappoints?\b.*\b(?:ceo|cfo|president)\b", 8, "Management"),
]

# Money magnitudes inside a catalyst headline scale it up.
MONEY_RE = re.compile(
    r"\$\s?([\d,.]+)\s*(billion|million|bn|mm|m|b)\b", re.IGNORECASE
)

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
    (r"\bdeficiency\s+letter\b|\bnon[- ]compliance\s+with\s+nasdaq\b"
     r"|\bdelisting\b", "Delisting risk"),
    (r"\bclass\s+action\b|\bsecurities\s+fraud\b|\bsec\s+investigation\b",
     "Litigation"),
    (r"\brestat(?:es|ement|ing)\b", "Restatement"),
    (r"\bclinical\s+hold\b|\bfails?\s+to\s+meet\b|\bmissed\s+(?:the\s+)?"
     r"primary\s+endpoint\b|\bdiscontinu(?:es|ed|ing)\s+(?:the\s+)?(?:trial|study)\b",
     "Trial fail"),
    (r"\bcomplete\s+response\s+letter\b|\bcrl\b", "CRL"),
    (r"\bwithdraws?\s+guidance\b|\bcuts?\s+guidance\b|\blowers?\s+guidance\b",
     "Guidance cut"),
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
    for group in (TIER_A, TIER_B, TIER_C) for p, pts, tag in group
]
_DILUTION = [(re.compile(p, re.IGNORECASE), t) for p, t in DILUTION]
_DISTRESS = [(re.compile(p, re.IGNORECASE), t) for p, t in DISTRESS]


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


def score(headline: str, body: str = "", source_weight: float = 1.0) -> dict:
    """Return {score, tags, dilution, distress, junk} for one release."""
    headline = headline or ""
    body = body or ""

    # Flags are evaluated first and always reported, even when nothing bullish
    # matched — a bare offering scores zero but you still want it labelled if
    # you widen the filters or pull the row up by hand.
    dilution = [t for p, t in _DILUTION
                if p.search(headline) or p.search(body[:400])]
    distress = [t for p, t in _DISTRESS if p.search(headline)]

    if JUNK.search(headline):
        return {"score": 0, "tags": [], "dilution": dilution[:2],
                "distress": distress[:2], "junk": True}

    hits: list[tuple[int, str]] = []
    for pattern, points, tag in _COMPILED:
        if pattern.search(headline):
            hits.append((points, tag))
        elif body and pattern.search(body):
            hits.append((int(points * 0.4), tag))

    if not hits:
        return {"score": 0, "tags": [], "dilution": dilution[:2],
                "distress": distress[:2], "junk": False}

    # Strongest hit counts fully; each additional one contributes less, so
    # keyword-stuffed releases can't outrank a single real catalyst.
    hits.sort(key=lambda h: -h[0])
    raw = 0.0
    for i, (points, _) in enumerate(hits):
        raw += points * (0.45 ** i)

    raw *= _money_multiplier(headline)
    raw *= source_weight

    if dilution:
        raw *= 0.45          # damped, not deleted — you still want to see it
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
    value += max(0.0, 18.0 - age_min * 0.9)     # first ~20 minutes get a boost
    return value
