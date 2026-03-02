"""
ib_connection.py — Lean IB Gateway / TWS wrapper for daily-batch execution.

Responsibilities:
  1. Connect to IB Gateway (or TWS)
  2. Reconcile: reqAccountSummary + reqPositions
  3. Place orders (market or bracket)
  4. Disconnect

Removed from the original connection.py:
  - reqMktData / tickPrice / tickSize  (no more real-time ticks)
  - reqPnL / pnl                       (we compute P&L ourselves)
"""

import threading
import logging
from queue import Queue

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.common import OrderId

import config

logger = logging.getLogger(__name__)


class IBConnection(EWrapper, EClient):
    """Thin wrapper around the IBKR Python API."""

    def __init__(self, event_queue: Queue):
        EClient.__init__(self, self)
        self.event_queue = event_queue

        self.next_order_id: int = 0
        self.next_reqId: int = 0
        self.active_orders: dict = {}

        # Synchronisation events
        self._positions_done = threading.Event()
        self._account_done = threading.Event()

    # ── Connection lifecycle ───────────────────────────────

    def connectAck(self):
        logger.info("IB connection acknowledged.")

    def connect_to_ib(self):
        """Connect, start the reader thread, and wait briefly for the
        nextValidId callback before returning."""
        logger.info(
            "Connecting to IB Gateway at %s:%s (client %s) …",
            config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID,
        )
        self.connect(config.IB_HOST, config.IB_PORT, clientId=config.IB_CLIENT_ID)
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        threading.Event().wait(2)  # allow handshake

    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.next_order_id = orderId
        self.next_reqId = orderId
        logger.info("Next valid order ID: %d", orderId)

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        super().error(reqId, errorCode, errorString)
        if errorCode < 2000:  # real errors (not warnings / info)
            self.event_queue.put({
                "event_type": "ERROR",
                "reqId": reqId,
                "code": errorCode,
                "message": errorString,
            })
            logger.error("IB ERROR %d: %s", errorCode, errorString)

    # ── Contract / Order helpers ───────────────────────────

    @staticmethod
    def create_contract(symbol: str, sec_type="STK", currency="USD", exchange="SMART") -> Contract:
        c = Contract()
        c.symbol = symbol
        c.secType = sec_type
        c.currency = currency
        c.exchange = exchange
        return c

    @staticmethod
    def create_order(action: str, quantity: int, order_type="MKT",
                     lmt_price=0.0, tif="DAY") -> Order:
        o = Order()
        o.action = action
        o.totalQuantity = quantity
        o.orderType = order_type
        o.lmtPrice = lmt_price
        o.tif = tif
        o.eTradeOnly = False
        o.firmQuoteOnly = False
        return o

    # ── Single order ───────────────────────────────────────

    def place_new_order(self, contract: Contract, order: Order) -> int:
        oid = self.next_order_id
        self.next_order_id += 1
        self.active_orders[oid] = {
            "symbol": contract.symbol,
            "action": order.action,
            "quantity": order.totalQuantity,
        }
        logger.info("Placing order %d: %s %d %s", oid, order.action, order.totalQuantity, contract.symbol)
        self.placeOrder(oid, contract, order)
        return oid

    # ── Bracket order (Parent + Stop-Loss + Take-Profit) ──

    def place_bracket_order(
        self,
        symbol: str,
        quantity: int,
        last_price: float,
        stop_loss_pct: float = config.STOP_LOSS_PCT,
        take_profit_pct: float = config.TAKE_PROFIT_PCT,
    ) -> int:
        """Submit a bracket order to IB:
        - Parent: BUY MKT
        - Child 1: SELL STP at last_price * (1 - stop_loss_pct)
        - Child 2: SELL LMT at last_price * (1 + take_profit_pct)
        Returns the parent order ID.
        """
        parent_id = self.next_order_id
        sl_id = parent_id + 1
        tp_id = parent_id + 2
        self.next_order_id += 3

        contract = self.create_contract(symbol)

        # Parent: Market Buy
        parent = self.create_order("BUY", quantity, "MKT")
        parent.orderId = parent_id
        parent.transmit = False  # hold until children are submitted

        # Child 1: Stop-Loss
        stop_price = round(last_price * (1 - stop_loss_pct), 2)
        sl_order = self.create_order("SELL", quantity, "STP")
        sl_order.orderId = sl_id
        sl_order.auxPrice = stop_price
        sl_order.parentId = parent_id
        sl_order.transmit = False

        # Child 2: Take-Profit
        tp_price = round(last_price * (1 + take_profit_pct), 2)
        tp_order = self.create_order("SELL", quantity, "LMT", lmt_price=tp_price)
        tp_order.orderId = tp_id
        tp_order.parentId = parent_id
        tp_order.transmit = True  # transmit entire bracket

        self.active_orders[parent_id] = {"symbol": symbol, "action": "BUY", "quantity": quantity}
        logger.info(
            "Bracket for %s: parent=%d  SL@%.2f  TP@%.2f",
            symbol, parent_id, stop_price, tp_price,
        )

        self.placeOrder(parent_id, contract, parent)
        self.placeOrder(sl_id, contract, sl_order)
        self.placeOrder(tp_id, contract, tp_order)

        return parent_id

    # ── Order status callback ─────────────────────────────

    def orderStatus(self, orderId, status, filled, remaining,
                    avgFillPrice, permId, parentId, lastFillPrice,
                    clientId, whyHeld, mktCapPrice):
        super().orderStatus(orderId, status, filled, remaining,
                            avgFillPrice, permId, parentId,
                            lastFillPrice, clientId, whyHeld, mktCapPrice)

        info = self.active_orders.get(orderId)
        if info:
            logger.info("Order %d (%s) status: %s", orderId, info["symbol"], status)
            if status == "Filled":
                self.event_queue.put({
                    "event_type": "FILL",
                    "symbol": info["symbol"],
                    "action": info["action"],
                    "quantity": filled,
                    "fill_price": avgFillPrice,
                    "order_id": orderId,
                })
                del self.active_orders[orderId]
            elif status in ("Cancelled", "ApiCancelled", "Inactive"):
                self.active_orders.pop(orderId, None)

    # ── Account / Positions reconciliation ─────────────────

    def request_account_summary(self):
        self._account_done.clear()
        rid = self.next_reqId; self.next_reqId += 1
        logger.info("Requesting account summary …")
        self.reqAccountSummary(rid, "All", "TotalCashValue,NetLiquidation")
        return rid

    def accountSummary(self, reqId, account, tag, value, currency):
        super().accountSummary(reqId, account, tag, value, currency)
        self.event_queue.put({
            "event_type": "ACCOUNT_SUMMARY",
            "tag": tag,
            "value": value,
        })

    def accountSummaryEnd(self, reqId: int):
        logger.info("Account summary received.")
        self.cancelAccountSummary(reqId)
        self._account_done.set()

    def request_positions(self):
        self._positions_done.clear()
        logger.info("Requesting existing positions …")
        self.reqPositions()

    def position(self, account, contract, position, avgCost):
        super().position(account, contract, position, avgCost)
        self.event_queue.put({
            "event_type": "POSITION_DATA",
            "symbol": contract.symbol,
            "quantity": position,
            "average_cost": avgCost,
        })

    def positionEnd(self):
        logger.info("Position data received.")
        self.cancelPositions()
        self._positions_done.set()

    def wait_for_reconciliation(self, timeout: float = 15) -> bool:
        """Block until both account summary and positions have been received.

        Returns True if BOTH events fired within *timeout* seconds,
        False if at least one timed out.
        """
        acct_ok = self._account_done.wait(timeout)
        pos_ok = self._positions_done.wait(timeout)
        if not acct_ok:
            logger.warning("Account summary did NOT arrive within %ss", timeout)
        if not pos_ok:
            logger.warning("Position data did NOT arrive within %ss", timeout)
        return acct_ok and pos_ok
