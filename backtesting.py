# new_backtester.py

import pandas as pd
from datetime import datetime
import yfinance as yf
import logging
import matplotlib.pyplot as plt
import numpy as np

from strategy_mean_momentum import mean_momentum_strategy

pd.options.mode.chained_assignment = None


class Backtester:


    def __init__(self, strategy_object, start_date, end_date, initial_capital=100000.0, commission=2.50,
                 trail_percentage=0.10):
        self.strategy = strategy_object
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.commission = commission
        self.trail_percentage = trail_percentage

        self.active_capital_base = self.initial_capital * 0.5
        self.passive_capital_base = self.initial_capital * 0.5
        self.cash = self.active_capital_base  # Cash for the active strategy
        self.qqq_shares = 0
        self.positions = pd.DataFrame({
            'symbol': pd.Series(dtype='str'),
            'quantity': pd.Series(dtype='int'),
            'buy_price': pd.Series(dtype='float'),
            'buy_date': pd.Series(dtype='datetime64[ns]'),
            'stop_loss_price': pd.Series(dtype='float')
        })
        self.trades_log = pd.DataFrame({
            'symbol': pd.Series(dtype='str'),
            'buy_date': pd.Series(dtype='datetime64[ns]'),
            'sell_date': pd.Series(dtype='datetime64[ns]'),
            'buy_price': pd.Series(dtype='float'),
            'sell_price': pd.Series(dtype='float'),
            'quantity': pd.Series(dtype='int'),
            'pnl': pd.Series(dtype='float')
        })

        self.all_ticker_data = {}
        self.all_benchmark_data = {}
        self.logger = self._setup_logger()
        self.tickers = self.strategy.tickers

    def _setup_logger(self):
        run_timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        log_filename = f'backtest_run_{run_timestamp}.log'
        logger = logging.getLogger('NewBacktesterLogger')
        logger.setLevel(logging.INFO)
        if logger.hasHandlers(): logger.handlers.clear()
        file_handler = logging.FileHandler(log_filename)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(file_handler)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(stream_handler)
        return logger

    def _download_full_historical_data(self):
        tickers_to_download = self.tickers + ['QQQ', '^NDX', '^GSPC']
        self.logger.info(f"Downloading all historical data for {len(tickers_to_download)} symbols...")
        all_data = yf.download(tickers_to_download, start=self.start_date, end=self.end_date)

        for ticker in self.tickers:
            if ('Close', ticker) in all_data.columns:
                self.all_ticker_data[ticker] = all_data.xs(ticker, level=1, axis=1).dropna()

        self.all_benchmark_data['QQQ'] = all_data.xs('QQQ', level=1, axis=1).dropna()
        self.all_benchmark_data['^NDX'] = all_data.xs('^NDX', level=1, axis=1).dropna()
        self.all_benchmark_data['^GSPC'] = all_data.xs('^GSPC', level=1, axis=1).dropna()
        self.logger.info("Full data download complete.")

    def _update_strategy_for_day(self, today):
        nasdaq_slice = self.all_benchmark_data['^NDX'].loc[:today]
        if not nasdaq_slice.empty:
            self.strategy.nasdaq100 = nasdaq_slice
        for ticker in self.tickers:
            if ticker in self.all_ticker_data:
                data_slice = self.all_ticker_data[ticker].loc[:today]
                if not data_slice.empty:
                    self.strategy.tickers_data[ticker] = data_slice
                    self.strategy.calculate_indicators(ticker, data_slice)

    def buy(self, ticker: str, price: float, date):
        investment_amount = 5000
        if self.cash * 0.1 > 5000:
            investment_amount = self.cash * 0.1
        if investment_amount >= self.cash:
            investment_amount = self.cash * 0.10
        quantity = int(investment_amount / price)
        cost = (quantity * price) + self.commission
        if self.cash > cost and quantity > 0:
            self.cash -= cost
            initial_stop_loss = price * (1 - self.trail_percentage)
            new_position = pd.DataFrame(
                [{'symbol': ticker, 'quantity': quantity, 'buy_price': price,
                  'buy_date': date, 'stop_loss_price': initial_stop_loss}])
            self.positions = pd.concat([self.positions, new_position], ignore_index=True)
            self.logger.info(f"{date.date()} - BUY: {quantity} of {ticker} at ${price}")

    def sell(self, ticker: str, current_price: float, date, position_index, reason: str):
        pos_data = self.positions.loc[position_index]
        revenue = (pos_data['quantity'] * current_price) - self.commission
        pnl = (current_price - pos_data['buy_price']) * pos_data['quantity'] - self.commission
        self.cash += revenue
        new_trade = pd.DataFrame([{'symbol': ticker, 'buy_date': pos_data['buy_date'], 'sell_date': date,
                                   'buy_price': pos_data['buy_price'], 'sell_price': current_price,
                                   'quantity': pos_data['quantity'], 'pnl': pnl}])
        self.trades_log = pd.concat([self.trades_log, new_trade], ignore_index=True)
        self.positions = self.positions.drop(position_index).reset_index(drop=True)
        self.logger.info(
            f"{date.date()} - SELL ({reason}): {pos_data['quantity']} of {ticker} at ${current_price:.2f} | P&L: ${pnl:.2f}")

    def run(self):
        self._download_full_historical_data()
        master_timeline = self.all_benchmark_data['^NDX'].index

        first_day_price = self.all_benchmark_data['QQQ']['Close'].iloc[0]
        self.qqq_shares = self.passive_capital_base / first_day_price
        self.logger.info(
            f"Allocating 50% of capital (${self.passive_capital_base:,.2f}) to passive QQQ holding ({self.qqq_shares:.2f} shares).")
        self.logger.info(f"Remaining 50% (${self.cash:,.2f}) allocated to active strategy.")

        equity_curve = []
        self.logger.info(f"--- Starting Simulation ({master_timeline[0].date()} to {master_timeline[-1].date()}) ---")

        for today in master_timeline:
            self._update_strategy_for_day(today)

            active_market_value = 0.0
            for index, pos in self.positions.iterrows():
                try:
                    current_price = self.all_ticker_data[pos['symbol']].loc[today]['Close']
                    potential_new_stop = current_price * (1 - self.trail_percentage)
                    if potential_new_stop > pos['stop_loss_price']:
                        self.positions.loc[index, 'stop_loss_price'] = potential_new_stop
                    active_market_value += pos['quantity'] * current_price
                except KeyError:
                    active_market_value += pos['quantity'] * pos['buy_price']

            qqq_price_today = self.all_benchmark_data['QQQ']['Close'].loc[today]
            passive_value = self.qqq_shares * qqq_price_today
            total_portfolio_value = self.cash + active_market_value + passive_value
            equity_curve.append({'date': today, 'value': total_portfolio_value})

            for ticker in self.tickers:
                try:
                    current_price = self.all_ticker_data[ticker].loc[today]['Close']
                except KeyError:
                    continue
                position_rows = self.positions[self.positions['symbol'] == ticker]
                if not position_rows.empty:
                    for index, pos in position_rows.iterrows():
                        if current_price <= pos['stop_loss_price']:
                            self.sell(ticker, current_price, today, index, reason="Trailing Stop")
                            break
                        days_held = (today - pos['buy_date']).days
                        if self.strategy.get_sell_signal(ticker, current_price, pos.to_dict(), days_held):
                            self.sell(ticker, current_price, today, index, reason="Strategy Signal")
                            break
                else:
                    if self.strategy.get_buy_signal(ticker, current_price):
                        self.buy(ticker, current_price, today)

        self.logger.info("--- Simulation Complete ---")
        self._process_results(equity_curve)

    def _process_results(self, equity_curve_data):
        self.logger.info("\n" + "=" * 50 + "\nBACKTEST RESULTS\n" + "=" * 50)
        equity_df = pd.DataFrame(equity_curve_data).set_index('date')

        last_day = equity_df.index[-1]
        for index, pos in self.positions.iterrows():
            last_price = self.all_ticker_data[pos['symbol']].loc[last_day]['Close']
            self.sell(pos['symbol'], last_price, last_day, index, reason="End of Simulation")

        last_day_qqq_price = self.all_benchmark_data['QQQ']['Close'].iloc[-1]
        final_passive_value = self.qqq_shares * last_day_qqq_price
        final_active_value = self.cash
        final_total_value = final_active_value + final_passive_value

        total_return = (final_total_value / self.initial_capital - 1) * 100

        nasdaq_prices = self.all_benchmark_data['^NDX']['Close'].loc[equity_df.index]
        sp500_prices = self.all_benchmark_data['^GSPC']['Close'].loc[equity_df.index]
        portfolio_returns = equity_df['value'].pct_change().dropna()
        nasdaq_returns = nasdaq_prices.pct_change().dropna()

        def calculate_sharpe(returns):
            return (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() != 0 else 0

        def calculate_max_drawdown(prices):
            return ((prices - prices.cummax()) / prices.cummax()).min()

        portfolio_sharpe = calculate_sharpe(portfolio_returns)
        nasdaq_sharpe = calculate_sharpe(nasdaq_returns)
        portfolio_max_drawdown = calculate_max_drawdown(equity_df['value'])
        nasdaq_max_drawdown = calculate_max_drawdown(nasdaq_prices)

        self.logger.info(f"Initial Total Capital:    ${self.initial_capital:,.2f}")
        self.logger.info(f"Final Total Portfolio Value: ${final_total_value:,.2f}")
        self.logger.info(f"Total Portfolio Return:   {total_return:.2f}%")

        active_pnl = final_active_value - self.active_capital_base
        passive_pnl = final_passive_value - self.passive_capital_base
        self.logger.info("\n--- Portfolio Breakdown ---")
        self.logger.info(f"Final Active Value:       ${final_active_value:,.2f} (P&L: ${active_pnl:,.2f})")
        self.logger.info(f"Final Passive Value (QQQ): ${final_passive_value:,.2f} (P&L: ${passive_pnl:,.2f})")

        self.logger.info("\n--- Risk & Return Metrics (Total Portfolio) ---")
        self.logger.info(f"Sharpe Ratio:             {portfolio_sharpe:.2f}  (NASDAQ 100: {nasdaq_sharpe:.2f})")
        self.logger.info(
            f"Max Drawdown:             {portfolio_max_drawdown * 100:.2f}% (NASDAQ 100: {nasdaq_max_drawdown * 100:.2f}%)")

        print("\n--- P&L Summary for Active Trades ---")
        if not self.trades_log.empty:
            pnl_by_ticker = self.trades_log.groupby('symbol')['pnl'].sum().sort_values(ascending=False)
            print(pnl_by_ticker.to_string())
            self.logger.info(f"\nTotal Net P&L from active trades: ${self.trades_log['pnl'].sum():,.2f}")
            self.logger.info(f"Total Active Trades Made: {len(self.trades_log)}")
            win_rate = (len(self.trades_log[self.trades_log['pnl'] > 0]) / len(self.trades_log) * 100)
            self.logger.info(f"Win Rate: {win_rate:.2f}%")

        plt.style.use('seaborn-v0_8-darkgrid')
        plt.figure(figsize=(14, 7))
        portfolio_pct = (equity_df['value'] / self.initial_capital - 1) * 100
        nasdaq_pct = (nasdaq_prices / nasdaq_prices.iloc[0] - 1) * 100
        sp500_pct = (sp500_prices / sp500_prices.iloc[0] - 1) * 100

        plt.plot(portfolio_pct, label=f'50/50 Portfolio (Sharpe: {portfolio_sharpe:.2f})', color='royalblue')
        plt.plot(nasdaq_pct, label=f'NASDAQ 100 (Sharpe: {nasdaq_sharpe:.2f})', color='orange', linestyle='--')
        plt.plot(sp500_pct, label='S&P 500', color='green', linestyle=':')

        plt.title('50/50 Portfolio vs. Benchmarks (Percentage Change)', fontsize=16)
        plt.ylabel('Percentage Change (%)')
        plt.xlabel('Date')
        plt.legend()
        plt.grid(True)
        plt.savefig('equity_curve.png')
        self.logger.info(f"\nEquity curve plot saved to equity_curve.png")
        plt.show()


if __name__ == '__main__':
    end_date = datetime.now()
    start_date = end_date - pd.DateOffset(years=5)

    strategy_instance = mean_momentum_strategy()

    bot = Backtester(
        strategy_object=strategy_instance,
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        initial_capital=100000.0,
        trail_percentage=0.10
    )

    bot.run()