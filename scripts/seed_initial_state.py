"""
seed_initial_state.py — One-time script to populate the v2 tables
with the current IB Gateway state as "day zero".

Usage:
  python seed_initial_state.py            # connect to IB and seed
  python seed_initial_state.py --skip-ib  # use hardcoded values (no IB needed)
"""

import os
import sys
import time
import logging
import argparse
from queue import Queue, Empty
from datetime import datetime

# Add project root to path so we can import config / db_manager
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yfinance as yf

import config
import db_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("seed")


def seed_from_ib():
    """Connect to IB, read account + positions, seed DB."""
    from ib_connection import IBConnection

    event_queue = Queue()
    conn = IBConnection(event_queue)
    conn.connect_to_ib()
    conn.request_account_summary()
    conn.request_positions()
    conn.wait_for_reconciliation(timeout=20)
    time.sleep(2)

    cash = 0.0
    nlv = 0.0
    positions = {}

    try:
        while True:
            event = event_queue.get_nowait()
            etype = event.get("event_type")
            if etype == "ACCOUNT_SUMMARY":
                if event["tag"] == "TotalCashValue":
                    cash = float(event["value"])
                elif event["tag"] == "NetLiquidation":
                    nlv = float(event["value"])
            elif etype == "POSITION_DATA":
                sym = event["symbol"]
                qty = event["quantity"]
                if qty > 0:
                    positions[sym] = {
                        "quantity": int(qty),
                        "average_cost": float(event["average_cost"]),
                    }
    except Empty:
        pass

    conn.disconnect()
    return cash, nlv, positions


def seed_hardcoded():
    """Fallback: use the known paper account state."""
    return 1010077.87, 1016339.30, {
        "AAPL": {"quantity": 15, "average_cost": 234.85},
        "F": {"quantity": 3, "average_cost": 11.80},
    }


def fetch_current_prices(symbols: list) -> dict:
    """Get latest close prices from Yahoo."""
    prices = {}
    for sym in symbols:
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period="5d")
            if not hist.empty:
                prices[sym] = float(hist["Close"].iloc[-1])
        except Exception:
            logger.warning("Could not fetch price for %s", sym)
    return prices


def main():
    parser = argparse.ArgumentParser(description="Seed initial state from IB")
    parser.add_argument("--skip-ib", action="store_true",
                        help="Use hardcoded values instead of connecting to IB")
    args = parser.parse_args()

    if args.skip_ib:
        cash, nlv, positions = seed_hardcoded()
        logger.info("Using hardcoded values (--skip-ib)")
    else:
        cash, nlv, positions = seed_from_ib()
        logger.info("Read from IB: cash=%.2f NLV=%.2f positions=%s", cash, nlv, list(positions.keys()))

    # Fetch current prices
    current_prices = fetch_current_prices(list(positions.keys()))
    logger.info("Current prices: %s", current_prices)

    # Upsert positions
    total_pos_value = 0.0
    total_unrealized = 0.0
    for sym, pos in positions.items():
        cur_price = current_prices.get(sym, pos["average_cost"])
        market_val = pos["quantity"] * cur_price
        unrealized = (cur_price - pos["average_cost"]) * pos["quantity"]
        total_pos_value += market_val
        total_unrealized += unrealized

        db_manager.upsert_position(
            symbol=sym,
            quantity=pos["quantity"],
            avg_cost=pos["average_cost"],
            current_price=cur_price,
            strategy_type="",
            entry_date=datetime.utcnow(),
            realized_pnl=0.0,
        )
        logger.info("Seeded position: %s qty=%d avg=%.2f cur=%.2f unrealized=%.2f",
                     sym, pos["quantity"], pos["average_cost"], cur_price, unrealized)

    # Save account snapshot
    db_manager.save_account_snapshot(
        net_liquidation=nlv,
        free_cash=cash,
        total_positions_value=total_pos_value,
        total_unrealized_pnl=total_unrealized,
        total_realized_pnl=0.0,
        num_positions=len(positions),
    )
    logger.info("Saved account snapshot — NLV=%.2f cash=%.2f", nlv, cash)

    # Also save legacy portfolio_state
    positions_dict = {
        sym: {
            "quantity": pos["quantity"],
            "average_cost": pos["average_cost"],
            "current_price": current_prices.get(sym, pos["average_cost"]),
        }
        for sym, pos in positions.items()
    }
    db_manager.save_portfolio_state(
        free_cash=cash,
        total_equity=nlv,
        positions=positions_dict,
    )
    logger.info("Seeding complete!")


if __name__ == "__main__":
    main()
