"""
Live pipeline diagnostic: verifies that change_pct_3m / change_pct_10m
flow correctly from Yahoo chart API through the store to rows().

Run directly:  python tests/test_pipeline_live.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Silence push notifications
import catalyst_scanner.push as push_mod
push_mod.send_alert_push = lambda *a, **kw: None

from catalyst_scanner import quotes as q_mod
from catalyst_scanner import store as store_mod
from catalyst_scanner import scanner as sc_mod


def run():
    print("=== Pipeline Live Diagnostic ===\n")

    # 1. Check session
    info = sc_mod._session_info()
    print(f"Session:        {info['type']} | elapsed={info['elapsed_minutes']:.1f}m")
    print(f"Trading window: {sc_mod._is_trading_window()}\n")

    # 2. quote() per symbol — individual chart API path
    provider = q_mod.YFinanceProvider()
    symbols = ["AAPL", "TSLA"]
    print("--- quote() results (chart API) ---")
    for sym in symbols:
        q = provider.quote(sym)
        c3 = q.get("change_pct_3m")
        c10 = q.get("change_pct_10m")
        last = q.get("last")
        ms = q.get("market_state")
        print(f"  {sym}: last={last}  change_pct_3m={c3}  change_pct_10m={c10}  market_state={ms}")
    print()

    # 3. Simulate filtered scan: store → merge → retrieve
    store = store_mod.Store(":memory:")
    for sym in symbols:
        store.add({
            "ticker": sym,
            "headline": "diag test",
            "url": "#",
            "source": "Diag",
            "published": time.time(),
            "body": "",
            "score": 50,
            "tags": [],
            "dilution": [],
            "distress": [],
        })

    print("--- Simulating _run_filtered_results_scan ---")
    live = provider.quotes(symbols)
    for sym in symbols:
        q = live.get(sym, {})
        merged = {
            **q,
            "avg_volume": 60_000_000,
            "market_cap": 3_000_000_000_000,
            "float_shares": 15_000_000_000,
        }
        store.set_quote(sym, merged)
        print(f"  Stored {sym}: 3m={merged.get('change_pct_3m')}  10m={merged.get('change_pct_10m')}")

    snap = store.quotes_snapshot()
    print()
    print("--- Quotes snapshot ---")
    for sym in symbols:
        q = snap.get(sym, {})
        print(f"  {sym}: 3m={q.get('change_pct_3m')}  10m={q.get('change_pct_10m')}  updated={q.get('updated')}")

    print()
    print("--- rows() output ---")
    sc = sc_mod.Scanner(quote_provider=provider, store=store)
    for row in sc.rows():
        print(f"  {row['ticker']}: 3m={row.get('change_pct_3m')}  10m={row.get('change_pct_10m')}  last={row.get('last')}")

    print("\n=== Done ===")


if __name__ == "__main__":
    run()
