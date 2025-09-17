import time
from queue import Queue, Empty  # for our threads
from datetime import datetime
from strategy_mean_momentum import mean_momentum_strategy
from connection import Connection
import config

class bot():
    def __init__(self):
        self.event_queue = Queue()
        self.connection = Connection(self.event_queue)
        self.strategy = mean_momentum_strategy()

        self.cash_balance = 0.0  #cash for buying assests
        self.portfolio = {}  # positions_data + buy_date + stop_loss_price
        self.market_data = {}
        self.req_to_ticker = {}
        self.pnl_data = {'daily': 0.0, 'unrealized': 0.0, 'realized': 0.0}

    def connect_and_initialize(self):
        self.connection.Connect_to_IB() # connecct to InterActive Broker
        self.strategy.historical_data()  # download all the data from yahoo
        self.connection.request_account_summary()
        self.connection.subscribe_to_pnl_updates(config.ID_PAPER) # subscribing to pnl updates

        # initializing getting market data for all the tickers and saving the req_id for receiving the data
        for i, ticker in enumerate(self.strategy.tickers):
            if ticker in self.strategy.tickers_data:
                req_id = self.connection.request_market_data(ticker)
                self.req_to_ticker[req_id] = ticker
                self.market_data[ticker] = {'price': None, 'volume': None}
        if (i + i) % 40 == 0:
            print("pause for too many requests")
            time.sleep(1)

    def handle_events(self): #process all messages from IB
        try:
            while True:
                event = self.event_queue.get_nowait() # benefiting from the asynchonic of IB
                event_type = event.get('event_type')

                if event_type == 'FILL':
                    self.on_fill(event)
                elif event_type == 'ACCOUNT_SUMMARY':
                    if event['tag'] == 'TotalCashValue':
                        self.cash_balance = float(event['value'])
                elif event_type == 'POSITION_DATA':
                # This populates initial positions
                    if event['symbol'] not in self.portfolio:
                        self.portfolio[event['symbol']] = {}
                    self.portfolio[event['symbol']]['quantity'] = event['quantity']
                    self.portfolio[event['symbol']]['average_cost'] = event['average_cost']

                elif event_type == 'TICK_PRICE':
                    ticker = self.req_to_ticker.get(event['reqId'])
                    if ticker: self.market_data[ticker]['price'] = event['price']

                elif event_type == 'TICK_VOLUME':
                    ticker = self.req_to_ticker.get(event['reqId'])
                    if ticker:
                        self.market_data[ticker]['volume'] = event['volume']
                elif event_type == 'PNL_UPDATE':
                    self.pnl_data['daily'] = event['daily_pnl']
                    self.pnl_data['unrealized'] = event['unrealized_pnl']
                    self.pnl_data['realized'] = event['realized_pnl']
                elif event_type == 'ERROR':
                    print(f"API ERROR: {event}")
        except Empty:
            pass

    def on_fill(self, event):
        symbol = event['symbol']
        action = event['action'].upper()

        if action == "BUY":
            if symbol not in self.portfolio:
                self.portfolio[symbol] = {}
            self.portfolio[symbol]['quantity'] = event['quantity']
            self.portfolio[symbol]['average_cost'] = event['fill_price']
            self.portfolio[symbol]['buy_date'] = datetime.now()
            self.portfolio[symbol]['stop_loss_price'] = event['fill_price'] * 0.90 # initial stop loss
            # strategy type
            self.portfolio[symbol]['strategy_type'] = "momentum" if self.strategy.is_bullish() else "mean_reversion"

        elif action == "SELL":
            # If we sold a position, remove it from our portfolio
            if symbol in self.portfolio:
                del self.portfolio[symbol]

        self.connection.request_account_summary()


    def check_for_signals(self):
        print("Scanning for trading signals...")

        for ticker in self.strategy.tickers:
            data = self.market_data.get(ticker)
            if data is None or data.get('price') is None:
                continue

            current_price = data['price']

            if ticker not in self.portfolio:
                if self.strategy.get_buy_signal(ticker, current_price):
                    print(f"BUY SIGNAL for {ticker} at {current_price}")

                    investment = self.cash_balance * 0.1
                    quantity = int(investment / current_price)

                    if quantity > 0:
                        contract = self.connection.create_contract(ticker)
                        order = self.connection.create_order("BUY", quantity)
                        self.connection.place_new_order(contract, order)

            else:
                pos_data = self.portfolio[ticker]

                # potential stoploss update
                stop_loss_precentage = 0.10
                potential_new_stop = current_price * (1 - stop_loss_precentage)
                if potential_new_stop > pos_data.get('stop_loss_price', 0):
                    self.portfolio[ticker]['stop_loss_price'] = potential_new_stop

                # Calculate days held
                days_held = (datetime.now() - pos_data.get('buy_date', datetime.now())).days

                if self.strategy.get_sell_signal(ticker, current_price, pos_data, days_held):
                    print(f"SELL SIGNAL for {ticker} at {current_price}")
                    contract = self.connection.create_contract(ticker)
                    order = self.connection.create_order("SELL", pos_data['quantity'])
                    self.connection.place_new_order(contract, order)

    def run(self):
        self.connect_and_initialize()

        print("\n--- Waiting for market data to arrive... ---")
        while True:
            self.handle_events()
            tickers_with_data = [t for t, d in self.market_data.items() if d.get('price') is not None]
            if len(tickers_with_data) >= (len(self.strategy.tickers_data) * 0.8):
                print(f" Data received for {len(tickers_with_data)} tickers. Starting strategy.")
                break
            print(
                f"Waiting... Received data for {len(tickers_with_data)} of {len(self.strategy.tickers_data)} tickers.")
            time.sleep(2)

        print("\n" + "=" * 50)
        print("Initial state populated.")
        print(f"Current Cash: {self.cash_balance}")
        print(f"Current Positions: {self.portfolio}")
        print("=" * 50 + "\n")

        self.check_for_signals()

        print("\n--- Waiting 10 seconds for any order fills to be processed... ---")
        time.sleep(10)

        print("\n--- Processing final events ---")
        self.handle_events()

        print("\n" + "=" * 50)
        print("Final Run State:")
        print(f"Final Cash: {self.cash_balance}")
        print(f"Final Positions: {self.portfolio}")
        print(
            f"Final PnL -> Daily: {self.pnl_data['daily']}, Unrealized: {self.pnl_data['unrealized']}")
        print("=" * 50 + "\n")

        print("Run complete. Disconnecting.")
        self.connection.disconnect()

if __name__ == '__main__':
    bot = bot()
    bot.run()









    """דברים שצריך להוסיף: 
    1. להבין איך הלוגר של TWS עובד.
    3. להבין איך אני משלב ביחד postgres כדי שאני אוכל לשמור נתונים ולאסוף מידע
    ללמוד על עוד מדדים ואסטרטגיות
     לסדר את זה שאני מקבל מראש את כל הפוזיציות שיש לי מאתמול"""