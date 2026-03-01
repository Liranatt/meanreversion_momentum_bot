"""
backfill_market_data.py — One-time script to download ~1 year of daily OHLCV
for every NASDAQ-100 ticker and persist to algo_trading.market_data.

Usage:
    $env:DATABASE_URL='postgres://...'
    python backfill_market_data.py          # downloads 1y for all tickers
    python backfill_market_data.py --period 2y   # override period
    python backfill_market_data.py --tickers AAPL MSFT   # subset

Safe to re-run: uses ON CONFLICT … DO UPDATE (upsert).
"""
import argparse
import sys
import time
import logging

import yfinance as yf

import config
import db_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill")


def backfill(tickers: list[str], period: str = "1y"):
    """Download daily data for *tickers* from Yahoo Finance and upsert."""
    total = len(tickers)
    ok, fail = 0, 0

    for i, ticker in enumerate(tickers, 1):
        log.info("[%d/%d] Downloading %s (%s) …", i, total, ticker, period)
        try:
            df = yf.download(ticker, period=period, interval="1d", progress=False)
            if df is None or df.empty:
                log.warning("  ⚠  No data returned for %s — skipping.", ticker)
                fail += 1
                continue

            # Flatten MultiIndex columns if present (yfinance >= 0.2)
            if hasattr(df.columns, "droplevel") and isinstance(df.columns, __import__("pandas").MultiIndex):
                df.columns = df.columns.droplevel(1)

            db_manager.save_market_data(ticker, df)
            log.info("  ✓  Saved %d rows for %s", len(df), ticker)
            ok += 1
        except Exception:
            log.exception("  ✗  Failed for %s", ticker)
            fail += 1

        # small delay to avoid rate-limiting from Yahoo
        if i < total:
            time.sleep(0.3)

    log.info("Done — %d succeeded, %d failed out of %d tickers.", ok, fail, total)
    return ok, fail


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill market data")
    parser.add_argument("--period", default="1y", help="yfinance period (default: 1y)")
    parser.add_argument("--tickers", nargs="*", help="Override ticker list (default: config.TICKERS)")
    args = parser.parse_args()

    if not config.DATABASE_URL:
        log.error("DATABASE_URL is not set. Export it first.")
        sys.exit(2)

    tickers = args.tickers if args.tickers else config.TICKERS
    log.info("Backfilling %d tickers with period=%s …", len(tickers), args.period)

    ok, fail = backfill(tickers, args.period)
    sys.exit(1 if fail > 0 else 0)
