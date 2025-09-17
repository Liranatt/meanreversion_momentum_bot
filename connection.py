import threading
from queue import Queue
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.common import TickerId, OrderId
from ibapi.ticktype import TickTypeEnum, TickType
from ibapi.order_state import OrderState


class Connection(EWrapper, EClient):
    def __init__(self, event_queue: Queue):
        EClient.__init__(self, self)
        self.next_order_id = 0
        self.port = 7497
        self.event_queue = event_queue
        self.requests = []
        self.next_reqId = 0
        self.active_orders = {}
        self.positions_event = threading.Event()
        self.tickers = [
            "MSFT", "AAPL", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "AVGO",
            "TSLA", "COST", "AMD", "PEP", "ADBE", "NFLX", "QCOM", "LIN",
            "INTC", "AMAT", "CMCSA", "INTU", "TXN", "AMGN", "CSCO", "LRCX",
            "HON", "BKNG", "ADP", "SBUX", "ISRG", "VRTX"
        ]

    def connectAck(self):
        print("connection successful")

    def Connect_to_IB(self):
        print("trying to connect to IB")
        self.connect("127.0.0.1", self.port, clientId=1)
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        threading.Event().wait(1)
        self.reqMarketDataType(3) # asking for delayed data

    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.next_order_id = orderId # for trades
        self.next_reqId = orderId # for data

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        super().error(reqId, errorCode, errorString)
        if errorCode < 2000:
            self.event_queue.put({'event_type': 'ERROR', 'reqId': reqId, 'code': errorCode, 'message': errorString})

    def create_contract(self, symbol, secType="STK", currency="USD", exchange="SMART"):
        contract = Contract()
        contract.symbol = symbol
        contract.secType = secType
        contract.currency = currency
        contract.exchange = exchange
        return contract

    def create_order(self, action, quantity, orderType="MKT",lmtPrice=0, tif="DAY"):
        order = Order()
        order.action = action
        order.totalQuantity = quantity
        order.lmtPrice = lmtPrice
        order.orderType = orderType
        order.tif = tif
        order.eTradeOnly = False
        order.firmQuoteOnly = False
        return order

    def place_new_order(self, contract: Contract, order: Order):
        order_id = self.next_order_id
        self.next_order_id += 1
        self.active_orders[order_id] = {"symbol": contract.symbol, "action": order.action,
                                        "quantity": order.totalQuantity}
        print(f"Placing Order {order_id}: {order.action} {order.totalQuantity} of {contract.symbol}")
        self.placeOrder(order_id, contract, order)

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId,
                    whyHeld, mktCapPrice):
        super().orderStatus(orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId,
                            whyHeld, mktCapPrice)
        if orderId in self.active_orders:
            print(
                f"Order Status Update - ID: {orderId}, Symbol: {self.active_orders[orderId]['symbol']}, Status: {status}")
            if status == "Filled":
                fill_event = {'event_type': 'FILL', 'symbol': self.active_orders[orderId]['symbol'],
                              'action': self.active_orders[orderId]['action'], 'quantity': filled,
                              'fill_price': avgFillPrice}
                self.event_queue.put(fill_event)
                del self.active_orders[orderId]
            elif status in ["Cancelled", "ApiCancelled", "Inactive"]:
                if orderId in self.active_orders: del self.active_orders[orderId]

    def request_account_summary(self):
        reqId = self.next_reqId
        self.next_reqId += 1
        print("Requesting account summary...")
        self.reqAccountSummary(reqId, "All", "TotalCashValue")

    def accountSummary(self, reqId, account, tag, value, currency):
        super().accountSummary(reqId, account, tag, value, currency)
        self.event_queue.put({'event_type': 'ACCOUNT_SUMMARY', 'tag': tag, 'value': value})

    def accountSummaryEnd(self, reqId: int):
        print("Account summary request finished.")
        self.cancelAccountSummary(reqId)

    def request_positions(self):
        print("Requesting existing positions...")
        self.reqPositions()

    def position(self, account, contract, position, avgCost):
        super().position(account, contract, position, avgCost)
        self.event_queue.put(
            {'event_type': 'POSITION_DATA', 'symbol': contract.symbol, 'quantity': position, 'average_cost': avgCost})

    def positionEnd(self):
        print("Position data request finished.")
        self.cancelPositions()

    def request_market_data(self, symbol: str) -> int:
        reqId = self.next_reqId;
        self.next_reqId += 1
        print(f"Requesting market data for {symbol} (Req ID: {reqId})")
        contract = self.create_contract(symbol)
        self.reqMktData(reqId, contract, '', False, False, [])
        return reqId

    def tickPrice(self, reqId, tickType, price, attrib):
        self.event_queue.put({'event_type': 'TICK_PRICE', 'reqId': reqId, 'price': price})

    def tickSize(self, reqId, tickType, size):
        self.event_queue.put({'event_type': 'TICK_VOLUME', 'reqId': reqId, 'volume': size})

    def subscribe_to_pnl_updates(self, account_id: str):
        reqId = self.next_reqId;
        self.next_reqId += 1
        print(f"Subscribing to PnL updates for account {account_id}...")
        self.reqPnL(reqId, account_id, "")

    def pnl(self, reqId: int, dailyPnL: float, unrealizedPnL: float, realizedPnL: float):
        super().pnl(reqId, dailyPnL, unrealizedPnL, realizedPnL)
        pnl_event = {
            'event_type': 'PNL_UPDATE',
            'daily_pnl': dailyPnL,
            'unrealized_pnl': unrealizedPnL,
            'realized_pnl': realizedPnL
        }
        self.event_queue.put(pnl_event)


