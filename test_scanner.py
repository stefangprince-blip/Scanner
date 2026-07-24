import sys, os, time, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catalyst_scanner import rssparse, tickers, scoring, store, scanner as sc, quotes

fails: list[str] = []


def check(name, cond, detail=""):
    print(
        ("  PASS  " if cond else "  FAIL  ")
        + name
        + (f"   {detail}" if detail and not cond else "")
    )
    if not cond:
        fails.append(name)


print("\n--- RSS/Atom parsing ---")
rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Wire</title>
<item><title>Acme Corp (NASDAQ: ACME) Announces FDA Approval</title>
<link>https://x.com/1</link><description>&lt;p&gt;Body text here&lt;/p&gt;</description>
<pubDate>Wed, 22 Jul 2026 13:05:00 GMT</pubDate><guid>g1</guid></item></channel></rss>"""
e = rssparse.parse(rss)
check("RSS 2.0 item parsed", len(e) == 1)
check("RSS title clean", e[0]["title"].startswith("Acme Corp"))
check("RSS html stripped", e[0]["summary"] == "Body text here", e[0]["summary"])
check("RSS date parsed", abs(e[0]["published"] - 1784725500) < 90000, e[0]["published"])

atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>8-K - BIOTECH INC (0001234567) (Filer)</title>
<link rel="alternate" href="https://www.sec.gov/Archives/edgar/data/1234567/x.htm"/>
<summary>filing</summary><updated>2026-07-22T13:05:00-04:00</updated><id>urn:x</id></entry></feed>"""
a = rssparse.parse(atom)
check("Atom entry parsed", len(a) == 1)
check("Atom href link", a[0]["link"].startswith("https://www.sec.gov"), a[0]["link"])
check("Malformed xml safe", rssparse.parse(b"<not xml") == [])

print("\n--- ticker extraction ---")
cases = [
    ("Acme (NASDAQ: ACME) wins deal", ["ACME"]),
    ("Foo Corp (Nasdaq:FOO) reports", ["FOO"]),
    ("Bar Inc. (NYSE American: BR) announces", ["BR"]),
    ("Baz (OTCQB: BAZZZ) update", ["BAZZZ"]),
    ("Deal between (NASDAQ: AAA) and (NASDAQ: BBB)", ["AAA", "BBB"]),
    ("Combo (NASDAQ: CCC, DDD) merge", ["CCC", "DDD"]),
    ("The CEO of the USA ETF said", []),
    ("Watch $TSLA today", ["TSLA"]),
    ("No tickers in this headline at all", []),
]
for text, want in cases:
    got = tickers.extract(text)
    check(f"extract {text[:40]!r}", got == want, f"got {got} want {want}")

print("\n--- catalyst scoring ---")


def s(h, b="", w=1.0):
    return scoring.score(h, b, w)


r = s("Cellect Announces FDA Approval of ARX-4")
check("FDA approval scores high", r["score"] >= 45, str(r))
check("FDA approval tagged", "FDA approval" in r["tags"], str(r))

r = s("Applied UV to be Acquired by Halma plc in All-Cash Transaction for $85 Million")
check("M&A scores high", r["score"] >= 45, str(r))

r = s("Company Announces Pricing of $12.0 Million Public Offering")
check("offering flagged as dilution", r["dilution"] != [], str(r))

r2 = s("Company Announces Record Revenue and Raises Guidance")
r3 = s(
    "Company Announces Record Revenue, Raises Guidance and Pricing of Public Offering"
)
check(
    "dilution damps score", r3["score"] < r2["score"], f"{r3['score']} vs {r2['score']}"
)

r = s("Company to Present at the Investor Conference Next Week")
check("junk filtered", r["junk"] and r["score"] == 0, str(r))

r = s("Trial Fails to Meet Primary Endpoint in Phase 3 Study")
check("bad news flagged distress", r["distress"] != [], str(r))

r = s("Company Appoints New Chief Marketing Officer")
check("low-signal news scores low", r["score"] < 15, str(r))

big = s("Nuvectra Signs $210 Million Contract Award with U.S. Department of Defense")
small = s("Nuvectra Signs Contract Award with U.S. Department of Defense")
check(
    "dollar size lifts score",
    big["score"] > small["score"],
    f"{big['score']} vs {small['score']}",
)

stuffed = s(
    "Company Announces Launch, Collaboration, Expansion, Milestone and Partnership"
)
check(
    "keyword stuffing capped below real catalyst",
    stuffed["score"] < s("Company Announces FDA Approval")["score"],
    f"{stuffed['score']}",
)
check(
    "score never exceeds 100",
    s(
        "FDA Approval and Definitive Merger Agreement for $900 Million and Met Primary Endpoint"
    )["score"]
    <= 100,
)

print("\n--- store TTL + dedup ---")
import os

if os.path.exists("t.db"):
    os.remove("t.db")
st = store.Store("t.db")
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
check("first insert is new", st.add(a1) is True)
check("duplicate rejected", st.add(a1) is False)
a2 = dict(a1, headline="ABCD Inc. announces FDA approval", source="Y")
check("cross-wire duplicate collapsed", st.add(a2) is False)
check("active returns row", len(st.active()) == 1)
check("ttl_fraction near 1", st.active()[0]["ttl_fraction"] > 0.99)
check("expired rows excluded", len(st.active(ttl=0)) == 0)
check("prune removes expired", st.prune(ttl=0) == 1)
check("board empty after prune", st.active() == [])

print("\n--- end-to-end (demo mode, no network) ---")
scn = sc.Scanner(
    quote_provider=quotes.NullProvider(),
    store=store.Store("t2.db") if not os.path.exists("t2.db") else store.Store("t2.db"),
)
scn.seed_demo()
rows = scn.rows()
check("demo seeded rows", len(rows) >= 5, str(len(rows)))
check(
    "rows sorted by heat desc",
    all(rows[i]["heat"] >= rows[i + 1]["heat"] for i in range(len(rows) - 1)),
)
tick_map = {r["ticker"]: r for r in rows}
check("AUVI M&A present", "AUVI" in tick_map)
check(
    "SIEB offering flagged",
    tick_map.get("SIEB", {}).get("dilution"),
    str(tick_map.get("SIEB", {}).get("dilution")),
)
check("SIEB ranks below CLBT", tick_map["SIEB"]["heat"] < tick_map["CLBT"]["heat"])

print("\n--- flask api ---")
from catalyst_scanner.app import create_app

app = create_app(scn)
c = app.test_client()
resp = c.get("/api/rows")
check("/api/rows 200", resp.status_code == 200)
payload = resp.get_json()
check("api returns rows", payload["count"] >= 5)
check("api has ttl", payload["ttl_seconds"] == 14400)
check("api health has feeds", "feeds" in payload["health"])
resp = c.get("/")
check("dashboard renders", resp.status_code == 200 and b"Catalyst" in resp.data)
json.dumps(payload)  # must be serializable
check("payload json-serializable", True)

# Ensure DB handles are closed before attempting to remove files
try:
    st.close()
except Exception:
    pass
try:
    scn.store.close()
except Exception:
    pass
for f in ("t.db", "t2.db"):
    if os.path.exists(f):
        os.remove(f)

print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILURES: {fails}"))
if __name__ == "__main__":
    sys.exit(1 if fails else 0)
