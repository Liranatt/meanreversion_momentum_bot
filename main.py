"""
main.py — Daily-batch orchestrator for the mean-reversion / momentum bot.

Flow (runs once per day, typically via GitHub Actions after market close):
  1. Download daily OHLCV from Yahoo Finance  (strategy.historical_data)
  2. Connect to IB Gateway → reconcile positions & cash
  3. Scan for buy / sell signals using the EXISTING strategy logic
  4. Place bracket orders (BUY + STP + TP) or market sells through IB
  5. Wait for fills
  6. Persist portfolio state, trade logs, and metrics to Postgres
  7. Disconnect
"""

import time
import logging
import numpy as np
from queue import Queue, Empty
from datetime import datetime

from strategy_mean_momentum import mean_momentum_strategy
from ib_connection import IBConnection
import config
import db_manager

# ── Logging setup ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("bot")


class TradingBot:
    """Daily-batch trading bot."""

    def __init__(self):
        self.event_queue: Queue = Queue()
        self.connection = IBConnection(self.event_queue)
        self.strategy = mean_momentum_strategy()

        self.cash_balance: float = 0.0
        self.net_liquidation: float = 0.0
        self.portfolio: dict = {}           # symbol → {quantity, average_cost, buy_date, stop_loss_price, strategy_type}
        self.fills: list = []               # filled orders collected during this run

    # ── 1. Data download ───────────────────────────────────

    def download_data(self):
        """Fetch 1 year of daily OHLCV from Yahoo and persist to DB."""
        logger.info("Step 1 — Downloading daily data from Yahoo Finance …")
        self.strategy.historical_data(persist=bool(config.DATABASE_URL))

    # ── 2. IB reconciliation ──────────────────────────────

    def reconcile(self):
        """Connect to IB, pull current cash & positions, drain the queue."""
        logger.info("Step 2 — Connecting to IB for reconciliation …")
        self.connection.connect_to_ib()

        self.connection.request_account_summary()
        self.connection.request_positions()
        self.connection.wait_for_reconciliation(timeout=20)

        time.sleep(2)
        self._drain_queue()

        logger.info(
            "Reconciliation done — cash=%.2f  positions=%s",
            self.cash_balance, list(self.portfolio.keys()),
        )

    # ── 3. Signal scanning ─────────────────────────────────

    def scan_signals(self):
        """Iterate tickers and act on buy / sell signals."""
        logger.info("Step 3 — Scanning for trading signals …")
        prices = self.strategy.get_latest_prices()

        for ticker in self.strategy.tickers:
            current_price = prices.get(ticker)
            if current_price is None:
                continue

            if ticker not in self.portfolio:
                # ── BUY logic ──
                if self.strategy.get_buy_signal(ticker, current_price):
                    investment = max(
                        self.cash_balance * config.POSITION_SIZE_PCT,
                        config.MIN_INVESTMENT,
                    )
                    if investment > self.cash_balance:
                        investment = self.cash_balance * config.POSITION_SIZE_PCT
                    quantity = int(investment / current_price)
                    if quantity > 0:
                        logger.info("BUY SIGNAL — %s @ %.2f  qty=%d", ticker, current_price, quantity)
                        self.connection.place_bracket_order(
                            symbol=ticker,
                            quantity=quantity,
                            last_price=current_price,
                        )
            else:
                # ── SELL logic ──
                pos = self.portfolio[ticker]

                # Trailing stop update
                potential_stop = current_price * (1 - config.STOP_LOSS_PCT)
                if potential_stop > pos.get("stop_loss_price", 0):
                    self.portfolio[ticker]["stop_loss_price"] = potential_stop

                days_held = (datetime.now() - pos.get("buy_date", datetime.now())).days
                if self.strategy.get_sell_signal(ticker, current_price, pos, days_held):
                    logger.info("SELL SIGNAL — %s @ %.2f", ticker, current_price)
                    contract = self.connection.create_contract(ticker)
                    order = self.connection.create_order("SELL", int(pos["quantity"]))
                    self.connection.place_new_order(contract, order)

    # ── 4. Wait for fills ─────────────────────────────────

    def wait_for_fills(self, seconds: int = 15):
        logger.info("Step 4 — Waiting %ds for order fills …", seconds)
        time.sleep(seconds)
        self._drain_queue()

    # ── 5. Persist to DB ──────────────────────────────────

    def persist(self):
        """Save portfolio state, trade logs, and performance metrics."""
        if not config.DATABASE_URL:
            logger.warning("DATABASE_URL not set — skipping DB persistence.")
            return

        logger.info("Step 5 — Persisting data to Postgres …")

        # Portfolio snapshot
        positions_value = 0.0
        prices = self.strategy.get_latest_prices()
        for sym, pos in self.portfolio.items():
            p = prices.get(sym, pos.get("average_cost", 0))
            positions_value += pos.get("quantity", 0) * p
        total_equity = self.cash_balance + positions_value

        db_manager.save_portfolio_state(
            free_cash=self.cash_balance,
            total_equity=total_equity,
            positions=self.portfolio,
        )

        # Trade logs
        for fill in self.fills:
            strategy_type = "momentum" if self.strategy.is_bullish() else "mean_reversion"
            db_manager.log_trade(
                symbol=fill["symbol"],
                action=fill["action"],
                quantity=int(fill["quantity"]),
                price=fill["fill_price"],
                commission=config.COMMISSION,
                pnl=0.0,  # exact P&L computed at sell
                strategy_type=strategy_type,
                order_type="MKT",
                ib_order_id=fill.get("order_id"),
            )

        # Metrics — computed from portfolio_state history in DB
        try:
            self._compute_and_save_metrics()
        except Exception:
            logger.warning("Metrics computation skipped (not enough data yet).")

    def _compute_and_save_metrics(self):
        """Pull equity curve from DB, compute Sharpe & max drawdown, save."""
        import psycopg2
        url = config.DATABASE_URL.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url, sslmode="require")
        cur = conn.cursor()
        cur.execute("""
            SELECT total_equity
            FROM algo_trading.portfolio_state
            ORDER BY recorded_at ASC;
        """)
        rows = cur.fetchall()
        conn.close()

        if len(rows) < 2:
            return

        equities = np.array([float(r[0]) for r in rows])
        returns = np.diff(equities) / equities[:-1]
        sharpe = (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() > 0 else 0.0

        running_max = np.maximum.accumulate(equities)
        drawdowns = (equities - running_max) / running_max
        max_dd = float(drawdowns.min())

        total_return = float((equities[-1] / equities[0]) - 1)

        # Win rate from trades_log
        cur2_conn = psycopg2.connect(url, sslmode="require")
        cur2 = cur2_conn.cursor()
        cur2.execute("SELECT COUNT(*) FROM algo_trading.trades_log;")
        total_trades = cur2.fetchone()[0]
        cur2.execute("SELECT COUNT(*) FROM algo_trading.trades_log WHERE pnl > 0;")
        winning = cur2.fetchone()[0]
        cur2_conn.close()

        win_rate = (winning / total_trades * 100) if total_trades > 0 else 0.0

        db_manager.save_metrics(sharpe, max_dd, total_return, win_rate)

    # ── Queue draining ────────────────────────────────────

    def _drain_queue(self):
        """Process all pending events from IB."""
        try:
            while True:
                event = self.event_queue.get_nowait()
                etype = event.get("event_type")

                if etype == "FILL":
                    self._on_fill(event)
                elif etype == "ACCOUNT_SUMMARY":
                    tag, val = event["tag"], event["value"]
                    if tag == "TotalCashValue":
                        self.cash_balance = float(val)
                    elif tag == "NetLiquidation":
                        self.net_liquidation = float(val)
                elif etype == "POSITION_DATA":
                    sym = event["symbol"]
                    qty = event["quantity"]
                    if qty > 0:
                        self.portfolio.setdefault(sym, {})
                        self.portfolio[sym]["quantity"] = qty
                        self.portfolio[sym]["average_cost"] = event["average_cost"]
                        self.portfolio[sym].setdefault("buy_date", datetime.now())
                        self.portfolio[sym].setdefault(
                            "stop_loss_price",
                            event["average_cost"] * (1 - config.STOP_LOSS_PCT),
                        )
                    else:
                        self.portfolio.pop(sym, None)
                elif etype == "ERROR":
                    logger.error("IB API error: %s", event)
        except Empty:
            pass

    def _on_fill(self, event):
        symbol = event["symbol"]
        action = event["action"].upper()
        self.fills.append(event)

        if action == "BUY":
            self.portfolio.setdefault(symbol, {})
            self.portfolio[symbol]["quantity"] = event["quantity"]
            self.portfolio[symbol]["average_cost"] = event["fill_price"]
            self.portfolio[symbol]["buy_date"] = datetime.now()
            self.portfolio[symbol]["stop_loss_price"] = event["fill_price"] * (1 - config.STOP_LOSS_PCT)
            self.portfolio[symbol]["strategy_type"] = (
                "momentum" if self.strategy.is_bullish() else "mean_reversion"
            )
        elif action == "SELL":
            self.portfolio.pop(symbol, None)

    # ── Run ───────────────────────────────────────────────

    def run(self):
        logger.info("=" * 60)
        logger.info("Daily batch run — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("=" * 60)

        self.download_data()
        self.reconcile()
        self.scan_signals()
        self.wait_for_fills()
        self.persist()

        logger.info("=" * 60)
        logger.info("Run complete — cash=%.2f  positions=%s", self.cash_balance, list(self.portfolio.keys()))
        logger.info("=" * 60)

        self.connection.disconnect()


if __name__ == "__main__":
    bot = TradingBot()
    bot.run()