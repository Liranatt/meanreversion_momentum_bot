import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import talib as ta


class mean_momentum_strategy():
    def __init__(self):
        self.SMA = {}
        self.upper_boilinger120 = {}
        self.lower_boilinger120 = {}
        self.volume120 = {}
        self.MACD = {}
        self.tickers_data = {}
        self.ATR = {}
        self.RSI = {}
        self.nasdaq100 = None
        self.tickers = [
            "MSFT", "AAPL", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "AVGO",
            "TSLA", "COST", "AMD", "PEP", "ADBE", "NFLX", "QCOM", "LIN",
            "INTC", "AMAT", "CMCSA", "INTU", "TXN", "AMGN", "CSCO", "LRCX",
            "HON", "BKNG", "ADP", "SBUX", "ISRG", "VRTX"
        ]
    def historical_data(self):
        end_date = datetime.now()
        start_date = end_date - timedelta(days=365)

        tickers_to_download = self.tickers + ['^NDX']
        all_data = yf.download(tickers_to_download, start=start_date, end=end_date)

        for ticker in self.tickers:
            if ('Close', ticker) in all_data.columns:
                ticker_df = all_data.xs(ticker, level=1, axis=1).dropna()
                if not ticker_df.empty:
                    self.tickers_data[ticker] = ticker_df
                    self.calculate_indicators(ticker, ticker_df)
            else:
                print(f"Could not download data for {ticker}. Skipping.")

        self.nasdaq100 = all_data.xs('^NDX', level=1, axis=1).dropna()
        print("Setup complete.")

    def calculate_indicators(self, ticker: str, data: pd.DataFrame):
        self.SMA[ticker] = data['Close'].rolling(window=30).mean()

        self.upper_boilinger120[ticker] = self.SMA[ticker] + 2 * data['Close'].rolling(30).std()
        self.lower_boilinger120[ticker] = self.SMA[ticker] - 2 * data['Close'].rolling(30).std()
        self.RSI[ticker] = pd.Series(ta.RSI(data['Close'].values, timeperiod=14), index=data.index)

        high_prices = data['High'].values
        low_prices = data['Low'].values
        close_prices = data['Close'].values
        atr = ta.ATR(high_prices, low_prices, close_prices, timeperiod=14)
        self.ATR[ticker] = pd.Series(atr, index=data.index[-len(atr):])

        macd, macdsignal, macdhist = ta.MACD(data['Close'].values, fastperiod=24, slowperiod=52, signalperiod=18)
        self.MACD[ticker] = {
            "macd_line": pd.Series(macd, index=data.index[-len(macd):]),
            "signal_line": pd.Series(macdsignal, index=data.index[-len(macdsignal):]),
            "hist": pd.Series(macdhist, index=data.index[-len(macdhist):])
        }

    def MACD_signal(self, ticker: str) -> str:
        if ticker not in self.MACD or self.MACD[ticker]["macd_line"].shape[0] < 2:
            return "weak"

        macd_line = self.MACD[ticker]["macd_line"]
        signal_line = self.MACD[ticker]["signal_line"]

        last_macd = macd_line.iloc[-1]
        before_last_macd = macd_line.iloc[-2]

        last_signal = signal_line.iloc[-1]
        before_last_signal = signal_line.iloc[-2]

        if last_macd >= last_signal and before_last_macd <= before_last_signal:
            return "strong"

        if last_macd >= last_signal and before_last_macd >= before_last_signal:
            return "Medium"
        return "weak"

    def boilinger_signal(self, current_price: int, ticker: str) -> str:
        if ticker not in self.upper_boilinger120 or self.upper_boilinger120[ticker].empty:
            return "SMA"

        upper_band = self.upper_boilinger120[ticker].iloc[-1]
        lower_band = self.lower_boilinger120[ticker].iloc[-1]

        if current_price >= upper_band:
            return "up above"
        if current_price <= lower_band:
            return "low below"

        return "SMA"

    def atr_signal(self, ticker: str) -> str:
        if ticker not in self.ATR or self.ATR[ticker].shape[0] < 31:
            return "low"  # Not enough data

        last_atr = self.ATR[ticker].iloc[-1]
        atr_sma = self.ATR[ticker].rolling(window=30).mean().iloc[-1]

        if last_atr > (atr_sma * 1.5):
            return "high"

        return "low"

    def is_bullish(self) -> bool:
        sma_200 = self.nasdaq100['Close'].rolling(window=200).mean()
        last_close = self.nasdaq100['Close'].iloc[-1]
        last_sma = sma_200.iloc[-1]
        return last_close > last_sma

    def get_buy_signal(self, ticker: str, current_price: int) -> bool:
        if ticker not in self.tickers_data:
            return False
        atr_signal = self.atr_signal(ticker)
        bullish = self.is_bullish()
        macd_signal = self.MACD_signal(ticker)
        boilinger_signal = self.boilinger_signal(current_price, ticker)
        last_rsi = self.RSI[ticker].iloc[-1]

        if bullish:
            # if atr_signal == "high" and (macd_signal == "strong" or macd_signal == "medium") and (
                    # boilinger_signal == "up above"):
                # return True

            if atr_signal == "high" and (macd_signal == "strong" or macd_signal == "medium"):
                return True
        else:
            if boilinger_signal == "low below" and last_rsi < 40:
                return True

        #print(f"wont buy {ticker} bullmarket is {bullish} macd is {macd_signal},"
             # f" boilinger is {boilinger_signal} and atr is {atr_signal} and last rsi is {last_rsi} ")
        return False

    def get_sell_signal(self, ticker: str, current_price: float, position_data: dict, days_held: int) -> bool:
        if current_price <= position_data.get('stop_loss_price', current_price + 1):
            print(f"SELL SIGNAL (Stop Loss Hit) for {ticker}")
            return True
        is_bull_market = self.is_bullish()

        if is_bull_market:
            macd_signal = self.MACD_signal(ticker)
            if macd_signal == "weak" and self.RSI[ticker].iloc[-1] <= 70:
                print(f"SELL SIGNAL (Momentum Fading) for {ticker}")
                return True
        else:
            if ticker in self.SMA and not self.SMA[ticker].empty:
                profit_target = self.SMA[ticker].iloc[-1]
                if current_price >= profit_target:
                    print(f"SELL SIGNAL (Mean Reversion Profit Target Hit) for {ticker}")
                    return True

            if days_held >= 20:
                print(f"SELL SIGNAL (Time Stop) for {ticker}")
                return True
        return False
