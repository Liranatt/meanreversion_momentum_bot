"""
daily_sync.py — End-of-day pipeline (runs after US market close, ~4:30 PM ET).

Flow:
  1. Expire stale pending signals from yesterday
  2. Connect to IB Gateway → get account summary + positions
  3. Compare IB positions with DB positions → log reconciliation
  4. Upsert positions from IB into DB (IB is source of truth)
  5. Download latest market data from Yahoo → save to DB
  6. Update current prices for all positions
  7. Generate buy / sell signals → save as PENDING for tomorrow's open
  8. Compute metrics (Sharpe, max drawdown, etc.)
  9. Save account snapshot + portfolio state
 10. Disconnect
"""

import sys
import time
import logging
import argparse
import numpy as np
from queue import Queue, Empty
from datetime import datetime, timedelta

import yfinance as yf

from strategy_mean_momentum import mean_momentum_strategy
from ib_connection import IBConnection
import config
import db_manager

# ── Logging ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("daily_sync")


class DailySync:
    """End-of-day reconciliation + signal generation pipeline."""

    def __init__(self, skip_ib: bool = False):
        self.skip_ib = skip_ib
        self.event_queue: Queue = Queue()
        self.connection: IBConnection | None = None
        self.strategy = mean_momentum_strategy()

        # IB account data
        self.cash_balance: float = 0.0
        self.net_liquidation: float = 0.0
        self.ib_positions: dict = {}  # symbol → {quantity, average_cost}

    # ── 1. Expire stale signals ────────────────────────────

    def expire_stale_signals(self):
        logger.info("Step 1 — Expiring stale pending signals …")
        db_manager.expire_old_signals()

    # ── 2. Connect to IB & reconcile ──────────────────────

    def connect_ib(self):
        if self.skip_ib:
            logger.info("Step 2 — Skipping IB connection (--skip-ib flag)")
            return
        logger.info("Step 2 — Connecting to IB Gateway …")
        self.connection = IBConnection(self.event_queue)
        self.connection.connect_to_ib()
        self.connection.request_account_summary()
        self.connection.request_positions()
        self.connection.wait_for_reconciliation(timeout=20)
        time.sleep(2)
        self._drain_queue()
        logger.info(
            "IB reconciled — cash=%.2f  NLV=%.2f  positions=%s",
            self.cash_balance, self.net_liquidation, list(self.ib_positions.keys()),
        )

    # ── 3. Reconcile IB vs DB ──────────────────────────────

    def reconcile_positions(self):
        logger.info("Step 3 — Reconciling IB vs DB positions …")
        db_positions = {p["symbol"]: p for p in db_manager.get_all_positions()}
        all_symbols = set(list(self.ib_positions.keys()) + list(db_positions.keys()))

        recon_entries = []
        for sym in sorted(all_symbols):
            ib = self.ib_positions.get(sym)
            db = db_positions.get(sym)

            if ib and db:
                status = "MATCH" if ib["quantity"] == db["quantity"] else "MISMATCH"
                recon_entries.append({
                    "symbol": sym,
                    "ib_quantity": ib["quantity"],
                    "db_quantity": db["quantity"],
                    "ib_avg_cost": ib["average_cost"],
                    "db_avg_cost": float(db["avg_cost"]),
                    "status": status,
                })
            elif ib and not db:
                recon_entries.append({
                    "symbol": sym,
                    "ib_quantity": ib["quantity"],
                    "db_quantity": 0,
                    "ib_avg_cost": ib["average_cost"],
                    "db_avg_cost": 0,
                    "status": "IB_ONLY",
                })
            elif db and not ib:
                recon_entries.append({
                    "symbol": sym,
                    "ib_quantity": 0,
                    "db_quantity": db["quantity"],
                    "ib_avg_cost": 0,
                    "db_avg_cost": float(db["avg_cost"]),
                    "status": "DB_ONLY",
                })

        if recon_entries:
            db_manager.log_reconciliation(recon_entries)
            mismatches = [e for e in recon_entries if e["status"] != "MATCH"]
            if mismatches:
                for m in mismatches:
                    logger.warning("RECON %s: %s (IB=%s DB=%s)",
                                   m["status"], m["symbol"],
                                   m.get("ib_quantity"), m.get("db_quantity"))
            else:
                logger.info("All %d positions match between IB and DB", len(recon_entries))

    # ── 4. Sync positions (IB is source of truth) ──────────

    def sync_positions_from_ib(self):
        logger.info("Step 4 — Syncing positions from IB to DB …")
        db_positions = {p["symbol"]: p for p in db_manager.get_all_positions()}

        # Upsert IB positions
        for sym, ib_pos in self.ib_positions.items():
            db_pos = db_positions.get(sym)
            strategy = db_pos["strategy_type"] if db_pos else ""
            entry = db_pos["entry_date"] if db_pos else None
            realized = 0.0  # don't overwrite accumulated realized
            db_manager.upsert_position(
                symbol=sym,
                quantity=ib_pos["quantity"],
                avg_cost=ib_pos["average_cost"],
                current_price=ib_pos["average_cost"],  # will be updated with Yahoo prices later
                strategy_type=strategy,
                entry_date=entry,
                realized_pnl=realized,
            )

        # Remove positions that are in DB but not in IB (fully sold)
        for sym in db_positions:
            if sym not in self.ib_positions:
                logger.info("Position %s no longer in IB — removing from DB", sym)
                db_manager.remove_position(sym)

    # ── 5. Download & persist market data ──────────────────

    def update_market_data(self):
        logger.info("Step 5 — Downloading latest market data …")
        self.strategy.historical_data(persist=bool(config.DATABASE_URL))
        logger.info("Market data download complete.")

    # ── 6. Update current prices ───────────────────────────

    def update_prices(self):
        logger.info("Step 6 — Updating current prices for positions …")
        prices = self.strategy.get_latest_prices()
        positions = db_manager.get_all_positions()
        held_symbols = {p["symbol"] for p in positions}

        price_map = {}
        for sym in held_symbols:
            if sym in prices:
                price_map[sym] = prices[sym]
            else:
                # Try fetching individually
                try:
                    tk = yf.Ticker(sym)
                    hist = tk.history(period="1d")
                    if not hist.empty:
                        price_map[sym] = float(hist["Close"].iloc[-1])
                except Exception:
                    logger.warning("Could not fetch price for %s", sym)

        if price_map:
            db_manager.update_current_prices(price_map)
        logger.info("Updated prices for %d positions", len(price_map))

    # ── 7. Generate signals ────────────────────────────────

    def generate_signals(self):
        logger.info("Step 7 — Generating trading signals …")
        prices = self.strategy.get_latest_prices()
        positions = {p["symbol"]: p for p in db_manager.get_all_positions()}
        strategy_type = "momentum" if self.strategy.is_bullish() else "mean_reversion"
        signals_count = 0
        scan_rows = []  # collect scan results for ALL tickers
        today = datetime.utcnow().date()

        for ticker in self.strategy.tickers:
            current_price = prices.get(ticker)
            if current_price is None:
                # No data for this ticker
                scan_rows.append({
                    "scan_date": today, "symbol": ticker,
                    "close_price": None, "sma_30": None, "upper_bb": None,
                    "lower_bb": None, "rsi_14": None, "atr_14": None,
                    "atr_signal": None, "macd_signal": None, "bb_signal": None,
                    "market_regime": strategy_type.replace("_", " "),
                    "signal_result": "NO_DATA", "rejection_reason": "No price data available",
                    "is_held": ticker in positions,
                })
                continue

            # Compute all indicators for scanner
            ind = self.strategy.compute_indicators(ticker, current_price)
            is_held = ticker in positions

            if ticker not in positions:
                # ── BUY signal logic ──
                if self.strategy.get_buy_signal(ticker, current_price):
                    investment = max(
                        self.cash_balance * config.POSITION_SIZE_PCT,
                        config.MIN_INVESTMENT,
                    )
                    if investment > self.cash_balance:
                        investment = self.cash_balance * config.POSITION_SIZE_PCT
                    quantity = int(investment / current_price)
                    if quantity > 0:
                        reason = (
                            f"{'Bull' if self.strategy.is_bullish() else 'Bear'} market | "
                            f"ATR={self.strategy.atr_signal(ticker)} "
                            f"MACD={self.strategy.MACD_signal(ticker)} "
                            f"Bollinger={self.strategy.boilinger_signal(current_price, ticker)} "
                            f"RSI={self.strategy.RSI[ticker].iloc[-1]:.1f}"
                        )
                        db_manager.save_pending_signal(
                            symbol=ticker,
                            signal_type="BUY",
                            quantity=quantity,
                            target_price=current_price,
                            strategy_type=strategy_type,
                            reason=reason,
                        )
                        signals_count += 1
                        logger.info("BUY signal: %s qty=%d @ %.2f — %s", ticker, quantity, current_price, reason)

                        scan_rows.append({
                            "scan_date": today, "symbol": ticker,
                            **ind, "signal_result": "BUY",
                            "rejection_reason": None, "is_held": False,
                        })
                    else:
                        scan_rows.append({
                            "scan_date": today, "symbol": ticker,
                            **ind, "signal_result": "HOLD",
                            "rejection_reason": "Insufficient cash for minimum order",
                            "is_held": False,
                        })
                else:
                    # Build rejection reason
                    bullish = self.strategy.is_bullish()
                    reasons = []
                    if bullish:
                        if ind["atr_signal"] != "high":
                            reasons.append(f"ATR={ind['atr_signal']} (need high)")
                        if ind["macd_signal"] not in ("strong", "Medium"):
                            reasons.append(f"MACD={ind['macd_signal']} (need strong/Medium)")
                    else:
                        if ind["bb_signal"] != "low below":
                            reasons.append(f"BB={ind['bb_signal']} (need low below)")
                        if ind["rsi_14"] is not None and ind["rsi_14"] >= 40:
                            reasons.append(f"RSI={ind['rsi_14']:.1f} (need <40)")

                    regime = "Bull" if bullish else "Bear"
                    rej = f"{regime} market: " + " | ".join(reasons) if reasons else f"{regime} market: No conditions met"

                    scan_rows.append({
                        "scan_date": today, "symbol": ticker,
                        **ind, "signal_result": "HOLD",
                        "rejection_reason": rej, "is_held": False,
                    })
            else:
                # ── SELL signal logic ──
                pos = positions[ticker]
                pos_data = {
                    "stop_loss_price": float(pos["avg_cost"]) * (1 - config.STOP_LOSS_PCT),
                    "average_cost": float(pos["avg_cost"]),
                    "quantity": pos["quantity"],
                }
                entry = pos.get("entry_date") or datetime.utcnow()
                days_held = (datetime.utcnow() - entry).days if hasattr(entry, 'days') or hasattr(entry, 'date') else 0

                if self.strategy.get_sell_signal(ticker, current_price, pos_data, days_held):
                    # Determine sell trigger for better reason
                    sell_triggers = []
                    if current_price <= pos_data["stop_loss_price"]:
                        sell_triggers.append("Stop-loss hit")
                    if self.strategy.is_bullish():
                        ms = self.strategy.MACD_signal(ticker)
                        rsi_val = self.strategy.RSI[ticker].iloc[-1] if ticker in self.strategy.RSI else 50
                        if ms == "weak" and rsi_val <= 70:
                            sell_triggers.append("Momentum fading")
                    else:
                        if ticker in self.strategy.SMA and not self.strategy.SMA[ticker].empty:
                            if current_price >= self.strategy.SMA[ticker].iloc[-1]:
                                sell_triggers.append("Mean-reversion target hit")
                        if days_held >= 20:
                            sell_triggers.append("Time stop (≥20 days)")

                    trigger_str = ", ".join(sell_triggers) if sell_triggers else "Sell conditions met"
                    reason = (
                        f"{trigger_str} | Price={current_price:.2f} "
                        f"AvgCost={float(pos['avg_cost']):.2f} DaysHeld={days_held}"
                    )
                    db_manager.save_pending_signal(
                        symbol=ticker,
                        signal_type="SELL",
                        quantity=pos["quantity"],
                        target_price=current_price,
                        strategy_type=strategy_type,
                        reason=reason,
                    )
                    signals_count += 1
                    logger.info("SELL signal: %s qty=%d @ %.2f", ticker, pos["quantity"], current_price)

                    scan_rows.append({
                        "scan_date": today, "symbol": ticker,
                        **ind, "signal_result": "SELL",
                        "rejection_reason": None, "is_held": True,
                    })
                else:
                    scan_rows.append({
                        "scan_date": today, "symbol": ticker,
                        **ind, "signal_result": "HOLD",
                        "rejection_reason": "Held — no sell trigger",
                        "is_held": True,
                    })

        # Persist scan results for ALL tickers
        if scan_rows:
            try:
                db_manager.save_scan_results(scan_rows)
                logger.info("Saved %d scan result rows", len(scan_rows))
            except Exception:
                logger.exception("Failed to save scan results (non-fatal)")

        logger.info("Generated %d signals for tomorrow's open", signals_count)

    # ── 8. Compute & save metrics ──────────────────────────

    def compute_metrics(self):
        logger.info("Step 8 — Computing performance metrics …")
        import psycopg2
        url = config.DATABASE_URL.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url, sslmode="require")
        cur = conn.cursor()

        # Equity curve from account_snapshot (prefer) or portfolio_state (fallback)
        cur.execute("""
            SELECT net_liquidation FROM algo_trading.account_snapshot
            ORDER BY recorded_at ASC;
        """)
        rows = cur.fetchall()
        if len(rows) < 2:
            cur.execute("""
                SELECT total_equity FROM algo_trading.portfolio_state
                ORDER BY recorded_at ASC;
            """)
            rows = cur.fetchall()

        if len(rows) < 2:
            conn.close()
            logger.info("Not enough data for metrics yet (%d snapshots)", len(rows))
            return

        equities = np.array([float(r[0]) for r in rows if r[0]])
        if len(equities) < 2:
            conn.close()
            return

        returns = np.diff(equities) / equities[:-1]
        sharpe = (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() > 0 else 0.0
        running_max = np.maximum.accumulate(equities)
        drawdowns = (equities - running_max) / running_max
        max_dd = float(drawdowns.min())
        total_return = float((equities[-1] / equities[0]) - 1)

        # Win rate
        cur.execute("SELECT COUNT(*) FROM algo_trading.trades_log;")
        total_trades = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM algo_trading.trades_log WHERE pnl > 0;")
        winning = cur.fetchone()[0]
        conn.close()

        win_rate = (winning / total_trades * 100) if total_trades > 0 else 0.0
        db_manager.save_metrics(sharpe, max_dd, total_return, win_rate)

    # ── 9. Save account snapshot ───────────────────────────

    def save_snapshot(self):
        logger.info("Step 9 — Saving account snapshot …")
        positions = db_manager.get_all_positions()
        total_pos_value = sum(float(p.get("market_value") or 0) for p in positions)
        total_unrealized = sum(float(p.get("unrealized_pnl") or 0) for p in positions)
        total_realized = sum(float(p.get("realized_pnl") or 0) for p in positions)
        nlv = self.net_liquidation or (self.cash_balance + total_pos_value)

        db_manager.save_account_snapshot(
            net_liquidation=nlv,
            free_cash=self.cash_balance,
            total_positions_value=total_pos_value,
            total_unrealized_pnl=total_unrealized,
            total_realized_pnl=total_realized,
            num_positions=len(positions),
        )

        # Also save to legacy portfolio_state for backward compat
        positions_dict = {
            p["symbol"]: {
                "quantity": p["quantity"],
                "average_cost": float(p["avg_cost"]),
                "current_price": float(p.get("current_price") or 0),
            }
            for p in positions
        }
        db_manager.save_portfolio_state(
            free_cash=self.cash_balance,
            total_equity=nlv,
            positions=positions_dict,
        )

    # ── Queue draining ────────────────────────────────────

    def _drain_queue(self):
        try:
            while True:
                event = self.event_queue.get_nowait()
                etype = event.get("event_type")
                if etype == "ACCOUNT_SUMMARY":
                    tag, val = event["tag"], event["value"]
                    if tag == "TotalCashValue":
                        self.cash_balance = float(val)
                    elif tag == "NetLiquidation":
                        self.net_liquidation = float(val)
                elif etype == "POSITION_DATA":
                    sym = event["symbol"]
                    qty = event["quantity"]
                    if qty > 0:
                        self.ib_positions[sym] = {
                            "quantity": int(qty),
                            "average_cost": float(event["average_cost"]),
                        }
                    else:
                        self.ib_positions.pop(sym, None)
                elif etype == "ERROR":
                    logger.error("IB error: %s", event)
        except Empty:
            pass

    # ── Run ────────────────────────────────────────────────

    def run(self):
        logger.info("=" * 60)
        logger.info("Daily Sync — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("=" * 60)

        self.expire_stale_signals()       # 1
        self.connect_ib()                 # 2
        if not self.skip_ib:
            self.reconcile_positions()    # 3
            self.sync_positions_from_ib() # 4
        self.update_market_data()         # 5
        self.update_prices()              # 6
        self.generate_signals()           # 7
        self.compute_metrics()            # 8
        self.save_snapshot()              # 9

        if self.connection:
            self.connection.disconnect()

        logger.info("=" * 60)
        logger.info("Daily sync complete.")
        logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="End-of-day sync pipeline")
    parser.add_argument("--skip-ib", action="store_true",
                        help="Skip IB Gateway connection (use DB positions only)")
    args = parser.parse_args()

    sync = DailySync(skip_ib=args.skip_ib)
    sync.run()
