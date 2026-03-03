import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import talib as ta
import logging

import config
import db_manager
import math

def safe_float(val):
    if val is None: return None
    v = float(val)
    return None if math.isnan(v) else v
    
logger = logging.getLogger(__name__)


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
        self.tickers = config.TICKERS  # single source of truth
    def historical_data(self, persist: bool = True):
        """Download historical data with incremental DB-aware updates.

        Logic:
        - If `persist` and `DATABASE_URL` are set and DB has >1 row for ticker,
          load DB rows and fetch only recent data (7d) from yfinance, then merge.
        - Otherwise perform the full 1-year fetch.
        - Persist merged result when `persist` is True.
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=365)

        # Download NASDAQ index full-year for market regime
        try:
            self.nasdaq100 = yf.download('^NDX', start=start_date, end=end_date)
        except Exception:
            logger.warning("Failed to download ^NDX; nasdaq100 will be empty")
            self.nasdaq100 = pd.DataFrame()

        for ticker in self.tickers:
            try:
                use_db = persist and config.DATABASE_URL
                count = 0
                if use_db:
                    try:
                        count = db_manager.get_market_data_count(ticker)
                    except Exception:
                        logger.warning("Could not get DB count for %s; falling back to full fetch", ticker)
                        count = 0

                ticker_df = pd.DataFrame()

                if count > 1:
                    # Load existing DB data
                    try:
                        db_df = db_manager.load_market_data(ticker)
                    except Exception:
                        logger.warning("Failed to load DB data for %s; fetching full year", ticker)
                        db_df = pd.DataFrame()

                    # Fetch only recent rows from yfinance
                    try:
                        recent_df = yf.download(ticker, period='7d')
                    except Exception:
                        logger.warning("Recent fetch failed for %s; using DB data only", ticker)
                        recent_df = pd.DataFrame()

                    if not db_df.empty and not recent_df.empty:
                        db_df.index = pd.to_datetime(db_df.index).normalize()
                        recent_df.index = pd.to_datetime(recent_df.index).normalize()
                        merged = pd.concat([db_df, recent_df])
                        merged = merged[~merged.index.duplicated(keep='last')].sort_index()
                        ticker_df = merged
                    elif not db_df.empty:
                        ticker_df = db_df
                    else:
                        ticker_df = recent_df
                else:
                    # First-run or DB unavailable: full-year download
                    ticker_df = yf.download(ticker, start=start_date, end=end_date)

                if not ticker_df.empty:
                    ticker_df = ticker_df.dropna()
                    self.tickers_data[ticker] = ticker_df
                    self.calculate_indicators(ticker, ticker_df)

                    if persist and config.DATABASE_URL:
                        try:
                            db_manager.save_market_data(ticker, ticker_df)
                        except Exception:
                            logger.warning("DB persist failed for %s — continuing", ticker)
                else:
                    logger.warning("Could not obtain data for %s (empty)", ticker)

            except Exception:
                logger.exception("Failed preparing historical data for %s", ticker)

        logger.info("Historical data setup complete — %d tickers loaded.", len(self.tickers_data))

    # ── helpers for the daily-batch flow ─────────────────────

    def get_latest_prices(self) -> dict:
        """Return {ticker: last_close_price} from the downloaded data.
        Replaces the old live-tick market_data dict.
        """
        prices = {}
        for ticker, df in self.tickers_data.items():
            if not df.empty:
                prices[ticker] = float(df['Close'].iloc[-1])
        return prices

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

    def compute_indicators(self, ticker: str, current_price: float) -> dict:
        """Return a dict of all indicator values and signal classifications
        for a single ticker.  Used by the Market Scanner feature.
        """
        if ticker not in self.tickers_data:
            return {
                "close_price": current_price,
                "sma_30": safe_float(sma),
                "upper_bb": safe_float(upper),
                "lower_bb": safe_float(lower),
                "rsi_14": round(safe_float(rsi), 2) if rsi is not None else None,
                "atr_signal": "N/A", "macd_signal": "N/A", "bb_signal": "N/A",
                "market_regime": "bull" if self.is_bullish() else "bear",
            }

        sma = self.SMA[ticker].iloc[-1] if ticker in self.SMA and not self.SMA[ticker].empty else None
        upper = self.upper_boilinger120[ticker].iloc[-1] if ticker in self.upper_boilinger120 and not self.upper_boilinger120[ticker].empty else None
        lower = self.lower_boilinger120[ticker].iloc[-1] if ticker in self.lower_boilinger120 and not self.lower_boilinger120[ticker].empty else None
        rsi = float(self.RSI[ticker].iloc[-1]) if ticker in self.RSI and not self.RSI[ticker].empty else None
        atr = float(self.ATR[ticker].iloc[-1]) if ticker in self.ATR and not self.ATR[ticker].empty else None

        return {
            "close_price": current_price,
            "sma_30": float(sma) if sma is not None else None,
            "upper_bb": float(upper) if upper is not None else None,
            "lower_bb": float(lower) if lower is not None else None,
            "rsi_14": round(rsi, 2) if rsi is not None else None,
            "atr_14": round(atr, 4) if atr is not None else None,
            "atr_signal": self.atr_signal(ticker),
            "macd_signal": self.MACD_signal(ticker),
            "bb_signal": self.boilinger_signal(current_price, ticker),
            "market_regime": "bull" if self.is_bullish() else "bear",
        }

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


