"""
execute_signals.py — Market-open order execution.

Runs shortly after US market open (9:30 AM ET):
  1. Read all PENDING signals from the database
  2. Connect to IB Gateway
  3. Place orders for each signal (BUY → bracket order, SELL → market order)
  4. Wait for fills
  5. Mark signals as EXECUTED, log trades, update positions
  6. Disconnect
"""

import time
import logging
import argparse
from queue import Queue, Empty
from datetime import datetime

from ib_connection import IBConnection
import config
import db_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("execute_signals")


class SignalExecutor:
    """Execute pending signals via IB Gateway."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.event_queue: Queue = Queue()
        self.connection: IBConnection | None = None
        self.fills: list = []
        self.order_to_signal: dict = {}   # order_id → signal dict
        self.cash_balance: float = 0.0
        self.net_liquidation: float = 0.0

    def connect_ib(self):
        logger.info("Connecting to IB Gateway …")
        self.connection = IBConnection(self.event_queue)
        self.connection.connect_to_ib()

        # Get current account state
        self.connection.request_account_summary()
        self.connection.wait_for_reconciliation(timeout=15)
        time.sleep(2)
        self._drain_queue()
        logger.info("Connected — cash=%.2f  NLV=%.2f", self.cash_balance, self.net_liquidation)

    def execute_pending(self):
        """Read PENDING signals and place orders."""
        signals = db_manager.get_pending_signals()
        if not signals:
            logger.info("No pending signals to execute.")
            return

        logger.info("Found %d pending signals to execute:", len(signals))
        for s in signals:
            logger.info("  %s %d %s @ %.2f (%s)",
                        s["signal_type"], s["quantity"], s["symbol"],
                        float(s["target_price"]), s["reason"][:60] if s["reason"] else "")

        if self.dry_run:
            logger.info("DRY RUN — not placing any orders.")
            return

        for signal in signals:
            sym = signal["symbol"]
            qty = signal["quantity"]
            sig_type = signal["signal_type"]
            target_price = float(signal["target_price"])

            try:
                if sig_type == "BUY":
                    # Place bracket order (MKT buy + STP + TP)
                    order_id = self.connection.place_bracket_order(
                        symbol=sym,
                        quantity=qty,
                        last_price=target_price,
                    )
                    self.order_to_signal[order_id] = signal
                    logger.info("Placed BUY bracket for %s — parent order %d", sym, order_id)

                elif sig_type == "SELL":
                    # Place market sell
                    contract = self.connection.create_contract(sym)
                    order = self.connection.create_order("SELL", qty)
                    order_id = self.connection.place_new_order(contract, order)
                    self.order_to_signal[order_id] = signal
                    logger.info("Placed SELL MKT for %s — order %d", sym, order_id)

            except Exception:
                logger.exception("Failed to place order for %s", sym)

    def wait_for_fills(self, seconds: int = 30):
        """Wait for order fills and process them."""
        logger.info("Waiting %ds for fills …", seconds)
        deadline = time.time() + seconds
        while time.time() < deadline:
            self._drain_queue()
            time.sleep(1)

        # Process remaining events
        self._drain_queue()
        logger.info("Received %d fills", len(self.fills))

    def process_fills(self):
        """Log trades and update positions based on fills."""
        positions = {p["symbol"]: p for p in db_manager.get_all_positions()}

        for fill in self.fills:
            sym = fill["symbol"]
            action = fill["action"]
            qty = int(fill["quantity"])
            price = float(fill["fill_price"])
            order_id = fill.get("order_id")

            # Find matching signal
            signal = self.order_to_signal.get(order_id)
            strategy_type = signal["strategy_type"] if signal else ""
            signal_id = signal["id"] if signal else None

            # Compute P&L for sells
            pnl = 0.0
            if action == "SELL" and sym in positions:
                avg_cost = float(positions[sym]["avg_cost"])
                pnl = (price - avg_cost) * qty

            # Log the trade
            db_manager.log_trade(
                symbol=sym,
                action=action,
                quantity=qty,
                price=price,
                commission=config.COMMISSION,
                pnl=pnl,
                strategy_type=strategy_type,
                order_type="MKT",
                ib_order_id=order_id,
            )

            # Update positions
            if action == "BUY":
                db_manager.upsert_position(
                    symbol=sym,
                    quantity=qty,
                    avg_cost=price,
                    current_price=price,
                    strategy_type=strategy_type,
                    entry_date=datetime.utcnow(),
                )
            elif action == "SELL":
                if pnl != 0:
                    db_manager.accumulate_realized_pnl(sym, pnl)
                # Check if fully sold
                pos = positions.get(sym)
                if pos and qty >= pos["quantity"]:
                    db_manager.remove_position(sym)
                elif pos:
                    remaining = pos["quantity"] - qty
                    db_manager.upsert_position(
                        symbol=sym,
                        quantity=remaining,
                        avg_cost=float(pos["avg_cost"]),
                        current_price=price,
                        strategy_type=strategy_type,
                    )

            # Mark signal as executed
            if signal_id:
                db_manager.mark_signal_executed(signal_id, price, order_id)

            logger.info("Processed fill: %s %d %s @ %.2f  P&L=%.2f", action, qty, sym, price, pnl)

    def mark_unfilled_expired(self):
        """Any pending signals that didn't fill get marked expired."""
        filled_signal_ids = set()
        for fill in self.fills:
            order_id = fill.get("order_id")
            signal = self.order_to_signal.get(order_id)
            if signal:
                filled_signal_ids.add(signal["id"])

        # Remaining pending signals that weren't filled
        signals = db_manager.get_pending_signals()
        for s in signals:
            if s["id"] not in filled_signal_ids:
                logger.warning("Signal %d (%s %s) did not fill — leaving as PENDING for retry",
                               s["id"], s["signal_type"], s["symbol"])

    def _drain_queue(self):
        try:
            while True:
                event = self.event_queue.get_nowait()
                etype = event.get("event_type")
                if etype == "FILL":
                    self.fills.append(event)
                elif etype == "ACCOUNT_SUMMARY":
                    tag, val = event["tag"], event["value"]
                    if tag == "TotalCashValue":
                        self.cash_balance = float(val)
                    elif tag == "NetLiquidation":
                        self.net_liquidation = float(val)
                elif etype == "ERROR":
                    logger.error("IB error: %s", event)
        except Empty:
            pass

    def run(self):
        logger.info("=" * 60)
        logger.info("Signal Executor — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("=" * 60)

        if not self.dry_run:
            self.connect_ib()

        self.execute_pending()

        if not self.dry_run and self.order_to_signal:
            self.wait_for_fills()
            self.process_fills()
            self.mark_unfilled_expired()

        if self.connection:
            self.connection.disconnect()

        logger.info("=" * 60)
        logger.info("Signal execution complete — %d fills processed.", len(self.fills))
        logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Execute pending signals at market open")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show signals but don't place orders")
    args = parser.parse_args()

    executor = SignalExecutor(dry_run=args.dry_run)
    executor.run()
