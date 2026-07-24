#!/usr/bin/env python3
"""Entry point.

    python run.py                  # start the scanner + dashboard
    python run.py --check-feeds    # verify every feed URL, then exit
    python run.py --demo           # seed sample rows to see the board
    python run.py --no-quotes      # catalyst scoring only, no market data
"""
import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from catalyst_scanner import config, quotes
from catalyst_scanner.app import create_app
from catalyst_scanner.feeds import FeedManager
from catalyst_scanner.scanner import Scanner


def main():
    p = argparse.ArgumentParser(description="Small-cap catalyst scanner")
    p.add_argument("--port", type=int, default=5057)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--check-feeds", action="store_true",
                   help="test each feed URL and show tickers found, then exit")
    p.add_argument("--demo", action="store_true",
                   help="seed sample alerts so the board is populated")
    p.add_argument("--no-quotes", action="store_true",
                   help="skip market data (no universe filtering)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%H:%M:%S",
    )

    if "you@example.com" in config.USER_AGENT:
        logging.warning(
            "Set SCANNER_USER_AGENT to include a real contact email — "
            "SEC will return 403 without one."
        )

    if args.check_feeds:
        FeedManager().check()
        return

    provider = quotes.NullProvider() if args.no_quotes else quotes.build()
    scanner = Scanner(quote_provider=provider)
    if args.demo:
        scanner.seed_demo()
    scanner.start()

    app = create_app(scanner)
    print(f"\n  Catalyst Board  ->  http://{args.host}:{args.port}\n")
    try:
        app.run(host=args.host, port=args.port, debug=False,
                use_reloader=False, threaded=True)
    finally:
        scanner.stop()


if __name__ == "__main__":
    main()
