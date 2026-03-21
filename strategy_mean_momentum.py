from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf


class mean_momentum_strategy:
    """Refactored existing strategy for daily batch processing.

    Core signal math is preserved from the original implementation:
    - SMA (30)
    - Bollinger Bands (30, 2 std)
    - RSI (14)
    - ATR (14)
    - MACD (24, 52, 18)
    """

    def __init__(self):
        self.SMA: dict[str, pd.Series] = {}
        self.upper_boilinger120: dict[str, pd.Series] = {}
        self.lower_boilinger120: dict[str, pd.Series] = {}
        self.volume120: dict[str, pd.Series] = {}
        self.MACD: dict[str, dict[str, pd.Series]] = {}
        self.tickers_data: dict[str, pd.DataFrame] = {}
        self.ATR: dict[str, pd.Series] = {}
        self.RSI: dict[str, pd.Series] = {}
        self.nasdaq100: pd.DataFrame | None = None
        self.tickers = [
            "MSFT", "AAPL", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "AVGO",
            "TSLA", "COST", "AMD", "PEP", "ADBE", "NFLX", "QCOM", "LIN",
            "INTC", "AMAT", "CMCSA", "INTU", "TXN", "AMGN", "CSCO", "LRCX",
            "HON", "BKNG", "ADP", "SBUX", "ISRG", "VRTX",
        ]

    def historical_data(self, lookback_days: int = 450):
        end_date = datetime.utcnow().date()
        start_date = end_date - timedelta(days=lookback_days)

        tickers_to_download = self.tickers + ["^NDX"]
        all_data = yf.download(
            tickers_to_download,
            start=start_date,
            end=end_date,
            interval="1d",
            auto_adjust=False,
            group_by="ticker",
            progress=False,
            threads=True,
        )

        for ticker in self.tickers:
            ticker_df = self._extract_ticker_frame(all_data, ticker)
            if ticker_df.empty:
                continue
            self.tickers_data[ticker] = ticker_df
            self.calculate_indicators(ticker, ticker_df)

        self.nasdaq100 = self._extract_ticker_frame(all_data, "^NDX")
        if self.nasdaq100.empty:
            raise RuntimeError("Could not download benchmark data (^NDX)")

    def calculate_indicators(self, ticker: str, data: pd.DataFrame):
        adj_close = data["Adj Close"].astype(float)

        # Preserve original SMA/Bollinger periods and structure.
        self.SMA[ticker] = adj_close.rolling(window=30, min_periods=30).mean()
        rolling_std = adj_close.rolling(window=30, min_periods=30).std(ddof=0)
        self.upper_boilinger120[ticker] = self.SMA[ticker] + 2 * rolling_std
        self.lower_boilinger120[ticker] = self.SMA[ticker] - 2 * rolling_std

        self.RSI[ticker] = self._rsi(adj_close, period=14)

        high_prices = data["High"].astype(float)
        low_prices = data["Low"].astype(float)
        close_prices = adj_close
        self.ATR[ticker] = self._atr(high_prices, low_prices, close_prices, period=14)

        macd_line, signal_line, hist = self._macd(close_prices, fast=24, slow=52, signal=18)
        self.MACD[ticker] = {
            "macd_line": macd_line,
            "signal_line": signal_line,
            "hist": hist,
        }

    def MACD_signal(self, ticker: str) -> str:
        if ticker not in self.MACD or self.MACD[ticker]["macd_line"].shape[0] < 2:
            return "weak"

        macd_line = self.MACD[ticker]["macd_line"].dropna()
        signal_line = self.MACD[ticker]["signal_line"].dropna()
        if macd_line.shape[0] < 2 or signal_line.shape[0] < 2:
            return "weak"

        last_macd = macd_line.iloc[-1]
        before_last_macd = macd_line.iloc[-2]
        last_signal = signal_line.iloc[-1]
        before_last_signal = signal_line.iloc[-2]

        if last_macd >= last_signal and before_last_macd <= before_last_signal:
            return "strong"
        if last_macd >= last_signal and before_last_macd >= before_last_signal:
            return "Medium"
        return "weak"

    def boilinger_signal(self, current_price: float, ticker: str) -> str:
        if ticker not in self.upper_boilinger120 or self.upper_boilinger120[ticker].dropna().empty:
            return "SMA"

        upper_band = self.upper_boilinger120[ticker].dropna().iloc[-1]
        lower_band = self.lower_boilinger120[ticker].dropna().iloc[-1]

        if current_price >= upper_band:
            return "up above"
        if current_price <= lower_band:
            return "low below"
        return "SMA"

    def atr_signal(self, ticker: str) -> str:
        atr_series = self.ATR.get(ticker)
        if atr_series is None or atr_series.dropna().shape[0] < 31:
            return "low"

        cleaned = atr_series.dropna()
        last_atr = cleaned.iloc[-1]
        atr_sma = cleaned.rolling(window=30, min_periods=30).mean().iloc[-1]

        if pd.isna(atr_sma):
            return "low"
        if last_atr > (atr_sma * 1.5):
            return "high"
        return "low"

    def is_bullish(self) -> bool:
        if self.nasdaq100 is None or self.nasdaq100.empty:
            return False
        ndx_adj = self.nasdaq100["Adj Close"].astype(float)
        sma_200 = ndx_adj.rolling(window=200, min_periods=200).mean()
        if sma_200.dropna().empty:
            return False
        return bool(ndx_adj.iloc[-1] > sma_200.iloc[-1])

    def get_buy_signal(self, ticker: str, current_price: float) -> bool:
        if ticker not in self.tickers_data:
            return False

        atr_signal = self.atr_signal(ticker)
        bullish = self.is_bullish()
        macd_signal = self.MACD_signal(ticker)
        bollinger_signal = self.boilinger_signal(current_price, ticker)

        rsi_series = self.RSI.get(ticker)
        if rsi_series is None or rsi_series.dropna().empty:
            return False
        last_rsi = rsi_series.dropna().iloc[-1]

        if bullish:
            if atr_signal == "high" and (macd_signal == "strong" or macd_signal == "Medium"):
                return True
        else:
            if bollinger_signal == "low below" and last_rsi < 40:
                return True

        return False

    def get_sell_signal(self, ticker: str, current_price: float, position_data: dict, days_held: int) -> bool:
        # Keep original stop-loss behavior only when stop-loss data exists.
        if "stop_loss_price" in position_data and current_price <= position_data["stop_loss_price"]:
            return True

        is_bull_market = self.is_bullish()

        if is_bull_market:
            macd_signal = self.MACD_signal(ticker)
            rsi_series = self.RSI.get(ticker)
            if rsi_series is not None and not rsi_series.dropna().empty:
                if macd_signal == "weak" and rsi_series.dropna().iloc[-1] <= 70:
                    return True
        else:
            sma_series = self.SMA.get(ticker)
            if sma_series is not None and not sma_series.dropna().empty:
                profit_target = sma_series.dropna().iloc[-1]
                if current_price >= profit_target:
                    return True
            if days_held >= 20:
                return True

        return False

    def latest_adjusted_close(self, ticker: str) -> float | None:
        frame = self.tickers_data.get(ticker)
        if frame is None or frame.empty:
            return None
        value = frame["Adj Close"].iloc[-1]
        return float(value) if pd.notna(value) else None

    @staticmethod
    def _extract_ticker_frame(all_data: pd.DataFrame, ticker: str) -> pd.DataFrame:
        if isinstance(all_data.columns, pd.MultiIndex):
            if ticker not in all_data.columns.get_level_values(0):
                return pd.DataFrame()
            frame = all_data[ticker].copy()
        else:
            frame = all_data.copy()

        required = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
        if not required.issubset(set(frame.columns)):
            return pd.DataFrame()
        return frame[["Open", "High", "Low", "Close", "Adj Close", "Volume"]].dropna()

    @staticmethod
    def _rsi(prices: pd.Series, period: int = 14) -> pd.Series:
        delta = prices.diff()
        gains = delta.clip(lower=0)
        losses = -delta.clip(upper=0)

        avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

        rs = avg_gain / avg_loss.replace(0, pd.NA)
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50)

    @staticmethod
    def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        prev_close = close.shift(1)
        tr_components = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        )
        true_range = tr_components.max(axis=1)
        return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    @staticmethod
    def _macd(prices: pd.Series, fast: int = 24, slow: int = 52, signal: int = 18):
        ema_fast = prices.ewm(span=fast, adjust=False, min_periods=fast).mean()
        ema_slow = prices.ewm(span=slow, adjust=False, min_periods=slow).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
        hist = macd_line - signal_line
        return macd_line, signal_line, hist
