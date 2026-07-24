# Catalyst Board

A live scanner for breaking press releases on micro and small caps, ranked by how
hard the news is likely to move the stock. Rows appear seconds after the wire
publishes and stay on the board for 4 hours.

```
python run.py --demo        # see the board immediately with sample rows
python run.py --check-feeds # verify every feed URL before going live
python run.py               # go live  ->  http://127.0.0.1:5057
```

## Install

```bash
pip install flask requests yfinance
export SCANNER_USER_AGENT="CatalystScanner/1.0 (contact: you@yourdomain.com)"
python run.py
```

The user agent is not optional. SEC returns 403 to requests without a real
contact address, and you lose the two highest-signal feeds.

## How it works

Four threads, one job each.

**Feed poller** hits every wire on a 5-second cycle with jitter. Each request
sends back the `ETag` and `Last-Modified` the host gave us last time, so an
unchanged feed answers `304` with no body — that's what makes 5-second polling
sustainable instead of a fast route to a ban. Any feed answering 429 or 403 gets
exponential backoff up to 15 minutes and recovers on its own.

**Ticker extraction** keys on the `(NASDAQ: ABCD)` convention that every wire
follows, with cashtags and bare `NASDAQ: ABCD` as fallbacks and a blocklist for
the acronyms that look like tickers. SEC filings carry no exchange string, so
those resolve through the CIK→ticker table, cached daily.

**Scoring** is tiered keyword matching on the headline, with body matches worth
40%. The strongest match counts fully and each additional one contributes 45% of
the last, so a release stuffed with "partnership, milestone, expansion, launch"
cannot outrank a single FDA approval. Dollar figures in the headline scale the
result. Dilution and distress language are matched separately: an offering damps
the score to 45% and shows a violet badge rather than being silently dropped,
because a raise priced off good news is still tradeable if you can see it.

**Ranking** is catalyst score plus confirmation from the tape — percent change,
relative volume, and a decaying bonus for the first twenty minutes.

**Quotes** refresh every 5 seconds for tickers on the board only. Market cap and
float are cached hourly per ticker; refetching those every tick is what gets you
rate-limited.

State lives in SQLite, so a restart mid-session keeps the 4-hour window.

## Reading the board

- **Left edge colour** encodes catalyst score: teal is marginal, amber solid, red
  is a top-tier event.
- **Thin bar under the ticker** is that row's remaining life in the 4-hour
  window, draining in real time.
- **Amber tags** are catalyst types, **violet** is dilution, **struck-through
  grey** is distress language.
- New rows flash once and, above score 35, chirp if the sound toggle is on.

## Tuning

Everything is in `config.py`.

| Setting | Default | Notes |
|---|---|---|
| `ALERT_TTL_SECONDS` | 4h | how long rows stay |
| `FEED_POLL_SECONDS` | 5 | safe only because requests are conditional |
| `MAX_MARKET_CAP` | $2B | small-cap ceiling |
| `MAX_FLOAT` | 100M | low float moves fastest; `None` to disable |
| `MIN_SCORE` | 15 | score floor to reach the board |
| `KEEP_UNKNOWN_FUNDAMENTALS` | True | keeps new listings visible when data is missing |

Adding a keyword is a one-line edit to `TIER_A/B/C` in `scoring.py` — the tuple is
`(regex, points, label)`.

## Using Webull instead of yfinance

`quotes.WebullProvider` takes any client exposing `get_stock_snapshot` and
`get_company_profile`, which matches the OpenAPI shape you already have:

```python
from catalyst_scanner import quotes
from catalyst_scanner.scanner import Scanner

scanner = Scanner(quote_provider=quotes.WebullProvider(client=your_adapter))
scanner.start()
```

It batches 100 symbols per snapshot call and requests extended hours, which
matters because most of these releases hit pre-market or after the close.

## What this does and doesn't do

Worth being straight about the latency, because it determines how you use it.

Public RSS from the wires lags the actual release by roughly **15 to 60 seconds**,
sometimes more on GlobeNewswire. Algos reading the direct feeds are already
positioned by the time a row appears here. So this is a **context and
confirmation** tool — it tells you *why* something just moved and whether the
catalyst justifies continuation — not a tool that gets you in first.

If you want genuine first-mover latency you need a paid low-latency feed
(Benzinga Pro's news API is the usual choice at this level, sub-second). It drops
in as one more entry in `FEEDS` plus a small adapter in `feeds.py`; the scoring,
storage, and board logic are all source-agnostic.

Two other honest limits: scoring is keyword-based, so it reads language and not
meaning, and it will occasionally rank a well-worded nothing above a plainly
worded something. And feed URLs drift — `--check-feeds` exists because you should
re-verify them every few weeks rather than wondering why the board went quiet.

## Layout

```
config.py     feeds, thresholds, timing
rssparse.py   RSS 2.0 + Atom parser (stdlib, no feedparser dependency)
tickers.py    ticker extraction, SEC CIK map
scoring.py    catalyst tiers, dilution/distress flags, heat ranking
store.py      SQLite TTL store + quote cache
quotes.py     yfinance / Webull / null providers
feeds.py      conditional-GET poller with backoff
scanner.py    threads, universe filter, row assembly
app.py        Flask API
run.py        CLI
```
