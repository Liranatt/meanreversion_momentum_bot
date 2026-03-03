"""
ib_connection.py — IB Gateway / TWS wrapper using ib_insync.

Provides synchronous methods for:
  1. Connect / disconnect
  2. Account summary and positions
  3. Single and bracket order placement
"""

import logging
from ib_insync import IB, Stock, MarketOrder, LimitOrder, StopOrder, Order
import time
import config

logger = logging.getLogger(__name__)


class IBConnection:
    """Thin wrapper around ib_insync for daily-batch execution."""

    def __init__(self):
        self.ib = IB()

    # ── Connection ─────────────────────────────────────────

    def connect_to_ib(self, retries=3):
        logger.info("Connecting to IB at %s:%s (client %s) …",
                     config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID)
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                self.ib.connect(config.IB_HOST, config.IB_PORT,
                                clientId=config.IB_CLIENT_ID, timeout=15)
                logger.info("Connected to IB Gateway.")
                return
            except Exception as e:
                last_error = e
                logger.warning("Connection attempt %d/%d failed: %s", attempt, retries, e)
                if attempt < retries:
                    time.sleep(min(10, 2 ** attempt))
        raise ConnectionError(f"Could not connect to IB after {retries} attempts: {last_error}")

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()

    # ── Account data ───────────────────────────────────────

    def get_account_summary(self) -> dict:
        """Returns {'cash': float, 'nlv': float}."""
        result = {"cash": 0.0, "nlv": 0.0}
        for item in self.ib.accountSummary():
            if item.tag == "TotalCashValue":
                result["cash"] = float(item.value)
            elif item.tag == "NetLiquidation":
                result["nlv"] = float(item.value)
        return result

    def get_positions(self) -> dict:
        """Returns {symbol: {'quantity': int, 'average_cost': float}}."""
        positions = {}
        for pos in self.ib.positions():
            if pos.position > 0:
                positions[pos.contract.symbol] = {
                    "quantity": int(pos.position),
                    "average_cost": float(pos.avgCost),
                }
        return positions

    # ── Contract / Order helpers ───────────────────────────

    @staticmethod
    def create_contract(symbol, exchange="SMART", currency="USD"):
        return Stock(symbol, exchange, currency)

    @staticmethod
    def create_order(action, quantity, order_type="MKT", lmt_price=0.0, aux_price=0.0):
        if order_type == "MKT":
            return MarketOrder(action, quantity)
        elif order_type == "LMT":
            return LimitOrder(action, quantity, lmt_price)
        elif order_type == "STP":
            return StopOrder(action, quantity, aux_price)
        return Order(action=action, totalQuantity=quantity, orderType=order_type)

    # ── Order placement ────────────────────────────────────

    def place_new_order(self, contract, order):
        """Place a single order. Returns an ib_insync Trade object."""
        trade = self.ib.placeOrder(contract, order)
        logger.info("Placed order: %s %s %s",
                     order.action, order.totalQuantity, contract.symbol)
        return trade

    def place_bracket_order(self, symbol, quantity, last_price,
                            stop_loss_pct=config.STOP_LOSS_PCT,
                            take_profit_pct=config.TAKE_PROFIT_PCT):
        """Place bracket: BUY MKT parent + stop-loss + take-profit.
        Returns list of Trade objects [parent, take_profit, stop_loss].
        """
        contract = self.create_contract(symbol)
        stop_price = round(last_price * (1 - stop_loss_pct), 2)
        tp_price = round(last_price * (1 + take_profit_pct), 2)

        bracket = self.ib.bracketOrder("BUY", quantity, last_price, tp_price, stop_price)
        parent, tp_order, sl_order = bracket

        # Change parent from LMT to MKT
        parent.orderType = "MKT"
        parent.lmtPrice = 0

        trades = [self.ib.placeOrder(contract, o) for o in bracket]
        logger.info("Bracket for %s: SL@%.2f  TP@%.2f", symbol, stop_price, tp_price)
        return trades
