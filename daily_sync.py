"""
daily_sync.py — End-of-day pipeline (runs after US market close, ~4:30 PM ET).

Architecture:
  • yfinance is the PRIMARY data source for all stock data (OHLCV).
  • IB Gateway is used only for VERIFICATION of positions & cash.
  • All data lives in the Postgres DB — the bot is never blocked by IB.

Flow:
  1. Expire stale pending signals from yesterday
  2. Download latest OHLCV for all NASDAQ-100 stocks from yfinance → DB
  3. Load last known account state from DB (cash, NLV, positions)
  4. Connect to IB Gateway → get account summary + positions (verification)
  5. Reconcile: compare IB vs DB — if mismatch, update DB from IB (IB is truth)
  6. Update current prices for held positions from the yfinance data
  7. Generate buy / sell signals for ALL tickers (with explanations for every stock)
  8. Save account snapshot
  9. Compute performance metrics (Sharpe, max drawdown, etc.)
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
    """End-of-day pipeline: download data → verify with IB → generate signals."""

    # Retry / timeout constants for IB verification
    IB_MAX_RETRIES = 3
    IB_WAIT_TIMEOUT = 30          # seconds per attempt
    IB_DRAIN_LOOPS = 5            # number of drain iterations
    IB_DRAIN_INTERVAL = 1.0       # seconds between drain loops

    def __init__(self):
        self.event_queue: Queue = Queue()
        self.connection: IBConnection | None = None
        self.strategy = mean_momentum_strategy()

        # Account data — loaded from DB first, then verified/overridden by IB
        self.cash_balance: float = 0.0
        self.net_liquidation: float = 0.0
        self.ib_positions: dict = {}   # symbol → {quantity, average_cost}
        self.ib_connected: bool = False
        self.ib_verified: bool = False  # True when IB successfully verified

        # Internal IB queue values (separate from working values)
        self._ib_cash_from_queue: float = 0.0
        self._ib_nlv_from_queue: float = 0.0

    # ── 1. Expire stale signals ────────────────────────────

    def expire_stale_signals(self):
        logger.info("Step 1 — Expiring stale pending signals …")
        db_manager.expire_old_signals()

    # ── 2. Download market data from yfinance ──────────────

    def download_market_data(self):
        """Download latest OHLCV for ALL NASDAQ-100 tickers from yfinance.
        This is the primary data source — saved to algo_trading.market_data.
        Also computes all indicators (SMA, RSI, MACD, ATR, Bollinger).
        """
        logger.info("Step 2 — Downloading market data from yfinance for %d tickers …",
                     len(self.strategy.tickers))
        self.strategy.historical_data(persist=bool(config.DATABASE_URL))
        logger.info("Market data download complete — %d tickers loaded.",
                     len(self.strategy.tickers_data))

    # ── 3. Load account state from DB ──────────────────────

    def load_account_from_db(self):
        """Load the most recent non-zero account snapshot and positions from DB.
        This establishes our baseline before IB verification.
        """
        logger.info("Step 3 — Loading last known account state from DB …")

        # Load last good snapshot
        snapshot = db_manager.get_latest_account_snapshot()
        if snapshot:
            self.cash_balance = float(snapshot["free_cash"])
            self.net_liquidation = float(snapshot["net_liquidation"])
            logger.info("  DB snapshot: cash=%.2f  NLV=%.2f  (from %s)",
                        self.cash_balance, self.net_liquidation,
                        snapshot["snapshot_date"])
        else:
            logger.warning("  No previous account snapshot in DB — starting from zero.")

        # Load current positions from DB
        db_positions = db_manager.get_all_positions()
        logger.info("  DB has %d positions: %s",
                     len(db_positions),
                     [p["symbol"] for p in db_positions])

    # ── 4. Connect to IB and verify ────────────────────────

    def verify_with_ib(self):
        """Connect to IB Gateway to VERIFY account data and positions.
        If IB fails to connect, we continue with DB values and log a warning.
        IB data is NOT the primary source — it is a verification check.
        """
        logger.info("Step 4 — Connecting to IB Gateway for verification …")
        try:
            self.connection = IBConnection(self.event_queue)
            self.connection.connect_to_ib()
            self.ib_connected = True

            for attempt in range(1, self.IB_MAX_RETRIES + 1):
                logger.info("IB verification attempt %d/%d (timeout=%ds) …",
                            attempt, self.IB_MAX_RETRIES, self.IB_WAIT_TIMEOUT)
                self.connection.request_account_summary()
                self.connection.request_positions()
                reconciled = self.connection.wait_for_reconciliation(
                    timeout=self.IB_WAIT_TIMEOUT
                )
                self._drain_queue_robust()

                if self._ib_nlv_from_queue != 0.0 or self._ib_cash_from_queue != 0.0:
                    self.ib_verified = True
                    logger.info("IB verified — cash=%.2f  NLV=%.2f  positions=%s",
                                self._ib_cash_from_queue, self._ib_nlv_from_queue,
                                list(self.ib_positions.keys()))
                    break

                if not reconciled:
                    logger.warning("Attempt %d: IB verification timed out", attempt)
                else:
                    logger.warning("Attempt %d: reconciled but NLV/cash are 0.0", attempt)

                if attempt < self.IB_MAX_RETRIES:
                    backoff = 2 ** attempt
                    logger.info("Backing off %ds before retry …", backoff)
                    time.sleep(backoff)

            if not self.ib_verified:
                logger.warning(
                    "IB verification FAILED after %d attempts — "
                    "continuing with DB values (cash=%.2f, NLV=%.2f). "
                    "IB Gateway may not be authenticated.",
                    self.IB_MAX_RETRIES, self.cash_balance, self.net_liquidation,
                )

        except Exception as exc:
            logger.warning(
                "Could not connect to IB Gateway: %s — "
                "continuing with DB values (cash=%.2f, NLV=%.2f).",
                exc, self.cash_balance, self.net_liquidation,
            )
            self.ib_connected = False

    # ── 5. Reconcile IB vs DB — update DB if needed ────────

    def reconcile_and_update(self):
        """Compare IB positions/account with DB. If they differ, update DB
        from IB because IB is the source of truth for real money & positions.
        """
        if not self.ib_verified:
            logger.info("Step 5 — Skipping reconciliation (IB not verified)")
            return

        logger.info("Step 5 — Reconciling IB vs DB …")

        # ── 5a. Account values ──
        ib_cash = self._ib_cash_from_queue
        ib_nlv = self._ib_nlv_from_queue

        if abs(ib_cash - self.cash_balance) > 0.01 or abs(ib_nlv - self.net_liquidation) > 0.01:
            logger.warning(
                "Account MISMATCH — DB: cash=%.2f NLV=%.2f  |  IB: cash=%.2f NLV=%.2f  → Updating from IB",
                self.cash_balance, self.net_liquidation, ib_cash, ib_nlv,
            )
            self.cash_balance = ib_cash
            self.net_liquidation = ib_nlv
        else:
            logger.info("Account values MATCH — cash=%.2f  NLV=%.2f", self.cash_balance, self.net_liquidation)

        # ── 5b. Positions ──
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

                # Update DB positions from IB (IB is source of truth)
                logger.info("Updating DB positions from IB …")
                for sym, ib_pos in self.ib_positions.items():
                    db_pos = db_positions.get(sym)
                    strategy = db_pos["strategy_type"] if db_pos else ""
                    entry = db_pos["entry_date"] if db_pos else None
                    db_manager.upsert_position(
                        symbol=sym,
                        quantity=ib_pos["quantity"],
                        avg_cost=ib_pos["average_cost"],
                        current_price=ib_pos["average_cost"],
                        strategy_type=strategy,
                        entry_date=entry,
                        realized_pnl=0.0,
                    )

                # Remove positions in DB but not in IB
                for sym in db_positions:
                    if sym not in self.ib_positions:
                        logger.info("Position %s in DB but not in IB — removing", sym)
                        db_manager.remove_position(sym)
            else:
                logger.info("All %d positions MATCH between IB and DB", len(recon_entries))

    # ── 6. Update current prices for positions ─────────────

    def update_position_prices(self):
        """Update current prices for all HELD positions using yfinance data."""
        logger.info("Step 6 — Updating current prices for positions …")
        prices = self.strategy.get_latest_prices()
        positions = db_manager.get_all_positions()
        held_symbols = {p["symbol"] for p in positions}

        price_map = {}
        for sym in held_symbols:
            if sym in prices:
                price_map[sym] = prices[sym]
            else:
                # Try fetching individually as fallback
                try:
                    tk = yf.Ticker(sym)
                    hist = tk.history(period="1d")
                    if not hist.empty:
                        price_map[sym] = float(hist["Close"].iloc[-1])
                except Exception:
                    logger.warning("Could not fetch price for %s", sym)

        if price_map:
            db_manager.update_current_prices(price_map)
        logger.info("Updated prices for %d / %d held positions", len(price_map), len(held_symbols))

    # ── 7. Generate signals ────────────────────────────────

    def generate_signals(self):
        """Generate BUY/SELL signals for ALL tickers.
        Every ticker gets a scan result row explaining what happened:
        - BUY: signal triggered, order will be placed at next open
        - SELL: exit conditions met for held position
        - HOLD: why we are not buying (which signals failed)
        - NO_DATA: no price data available
        """
        logger.info("Step 7 — Generating trading signals for %d tickers …",
                     len(self.strategy.tickers))
        prices = self.strategy.get_latest_prices()
        positions = {p["symbol"]: p for p in db_manager.get_all_positions()}
        strategy_type = "momentum" if self.strategy.is_bullish() else "mean_reversion"
        bullish = self.strategy.is_bullish()
        regime_str = "Bull" if bullish else "Bear"
        signals_count = 0
        scan_rows = []
        today = datetime.utcnow().date()

        for ticker in self.strategy.tickers:
            current_price = prices.get(ticker)
            if current_price is None:
                scan_rows.append({
                    "scan_date": today, "symbol": ticker,
                    "close_price": None, "sma_30": None, "upper_bb": None,
                    "lower_bb": None, "rsi_14": None, "atr_14": None,
                    "atr_signal": None, "macd_signal": None, "bb_signal": None,
                    "market_regime": strategy_type.replace("_", " "),
                    "signal_result": "NO_DATA",
                    "rejection_reason": "No price data available from yfinance",
                    "is_held": ticker in positions,
                })
                continue

            # Compute all indicators for scanner
            ind = self.strategy.compute_indicators(ticker, current_price)

            if ticker not in positions:
                # ── BUY signal logic ──
                if self.strategy.get_buy_signal(ticker, current_price):
                    investment = max(
                        self.cash_balance * config.POSITION_SIZE_PCT,
                        config.MIN_INVESTMENT,
                    )
                    if investment > self.cash_balance:
                        investment = self.cash_balance * config.POSITION_SIZE_PCT

                    quantity = int(investment / current_price) if current_price > 0 else 0

                    if quantity > 0:
                        rsi_str = ""
                        if ind.get("rsi_14") is not None:
                            rsi_str = f" RSI={ind['rsi_14']:.1f}"
                        reason = (
                            f"{regime_str} market | "
                            f"ATR={ind['atr_signal']} "
                            f"MACD={ind['macd_signal']} "
                            f"BB={ind['bb_signal']}"
                            f"{rsi_str}"
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
                        logger.info("BUY signal: %s qty=%d @ %.2f — %s",
                                    ticker, quantity, current_price, reason)

                        scan_rows.append({
                            "scan_date": today, "symbol": ticker,
                            **ind, "signal_result": "BUY",
                            "rejection_reason": None, "is_held": False,
                        })
                    else:
                        # Signal triggered but not enough cash
                        scan_rows.append({
                            "scan_date": today, "symbol": ticker,
                            **ind, "signal_result": "HOLD",
                            "rejection_reason": (
                                f"BUY signal triggered but insufficient cash "
                                f"(available=${self.cash_balance:,.0f}, "
                                f"need ${current_price:,.0f} min per share)"
                            ),
                            "is_held": False,
                        })
                else:
                    # Build detailed rejection reason
                    reasons = []
                    if bullish:
                        if ind["atr_signal"] != "high":
                            reasons.append(f"ATR={ind['atr_signal']} (need high)")
                        macd_sig = ind["macd_signal"]
                        if macd_sig not in ("strong", "Medium"):
                            reasons.append(f"MACD={macd_sig} (need strong/Medium)")
                    else:
                        bb_sig = ind["bb_signal"]
                        if bb_sig != "low below":
                            reasons.append(f"BB={bb_sig} (need low below)")
                        if ind["rsi_14"] is not None and ind["rsi_14"] >= 40:
                            reasons.append(f"RSI={ind['rsi_14']:.1f} (need <40)")

                    rej = (f"{regime_str} market: " + " | ".join(reasons)
                           if reasons
                           else f"{regime_str} market: No entry conditions met")

                    scan_rows.append({
                        "scan_date": today, "symbol": ticker,
                        **ind, "signal_result": "HOLD",
                        "rejection_reason": rej, "is_held": False,
                    })
            else:
                # ── SELL signal logic (for held positions) ──
                pos = positions[ticker]
                hp = float(pos.get("highest_price") or pos["avg_cost"])
                pos_data = {
                    "stop_loss_price": hp * (1 - config.STOP_LOSS_PCT),
                    "average_cost": float(pos["avg_cost"]),
                    "quantity": pos["quantity"],
                }
                entry = pos.get("entry_date") or datetime.utcnow()
                now = datetime.utcnow()
                # Handle timezone-aware vs naive datetimes
                if hasattr(entry, 'tzinfo') and entry.tzinfo is not None:
                    entry = entry.replace(tzinfo=None)
                days_held = (now - entry).days if hasattr(entry, 'date') else 0

                if self.strategy.get_sell_signal(ticker, current_price, pos_data, days_held):
                    sell_triggers = []
                    if current_price <= pos_data["stop_loss_price"]:
                        sell_triggers.append("Stop-loss hit")
                    if bullish:
                        ms = self.strategy.MACD_signal(ticker)
                        rsi_val = self.strategy.RSI[ticker].iloc[-1] if ticker in self.strategy.RSI else 50
                        if ms == "weak" and rsi_val <= 70:
                            sell_triggers.append("Momentum fading")
                    else:
                        if ticker in self.strategy.SMA and not self.strategy.SMA[ticker].empty:
                            if current_price >= self.strategy.SMA[ticker].iloc[-1]:
                                sell_triggers.append("Mean-reversion target hit")
                        if days_held >= 20:
                            sell_triggers.append("Time stop (>=20 days)")

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
                    logger.info("SELL signal: %s qty=%d @ %.2f — %s",
                                ticker, pos["quantity"], current_price, reason)

                    scan_rows.append({
                        "scan_date": today, "symbol": ticker,
                        **ind, "signal_result": "SELL",
                        "rejection_reason": None, "is_held": True,
                    })
                else:
                    # Held but no sell trigger — explain P&L
                    pnl_pct = ((current_price - float(pos["avg_cost"])) / float(pos["avg_cost"])) * 100
                    scan_rows.append({
                        "scan_date": today, "symbol": ticker,
                        **ind, "signal_result": "HOLD",
                        "rejection_reason": (
                            f"Held position — no sell trigger. "
                            f"P&L={pnl_pct:+.1f}% | DaysHeld={days_held}"
                        ),
                        "is_held": True,
                    })

        # Persist scan results for ALL tickers
        if scan_rows:
            try:
                db_manager.save_scan_results(scan_rows)
                logger.info("Saved %d scan result rows", len(scan_rows))
            except Exception:
                logger.exception("Failed to save scan results (non-fatal)")

        logger.info(
            "Generated %d signals for tomorrow's open "
            "(regime=%s, cash=$%.0f, %d held positions)",
            signals_count, regime_str, self.cash_balance, len(positions),
        )

    # ── 8. Save account snapshot ───────────────────────────

    def save_snapshot(self):
        logger.info("Step 8 — Saving account snapshot …")
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

    # ── 9. Compute performance metrics ─────────────────────

    def compute_metrics(self):
        logger.info("Step 9 — Computing performance metrics …")
        import psycopg2
        url = config.DATABASE_URL.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url, sslmode="require")
        cur = conn.cursor()

        # Equity curve from account_snapshot — skip zero rows
        cur.execute("""
            SELECT net_liquidation FROM algo_trading.account_snapshot
            WHERE net_liquidation > 0
            ORDER BY recorded_at ASC;
        """)
        rows = cur.fetchall()
        if len(rows) < 2:
            cur.execute("""
                SELECT total_equity FROM algo_trading.portfolio_state
                WHERE total_equity > 0
                ORDER BY recorded_at ASC;
            """)
            rows = cur.fetchall()

        if len(rows) < 2:
            conn.close()
            logger.info("Not enough data for metrics yet (%d snapshots)", len(rows))
            return

        equities = np.array([float(r[0]) for r in rows if r[0] and float(r[0]) > 0])
        if len(equities) < 2:
            conn.close()
            return

        returns = np.diff(equities) / equities[:-1]
        sharpe = float((returns.mean() / returns.std()) * np.sqrt(252)) if returns.std() > 0 else 0.0
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

    # ── Queue draining (IB event processing) ───────────────

    def _drain_queue(self):
        """Drain all currently available events from the queue (single pass)."""
        try:
            while True:
                event = self.event_queue.get_nowait()
                self._process_event(event)
        except Empty:
            pass

    def _drain_queue_robust(self):
        """Drain events with multiple passes to catch late-arriving IB callbacks."""
        for i in range(self.IB_DRAIN_LOOPS):
            self._drain_queue()
            if self._ib_nlv_from_queue != 0.0 or self._ib_cash_from_queue != 0.0:
                break
            if i < self.IB_DRAIN_LOOPS - 1:
                time.sleep(self.IB_DRAIN_INTERVAL)

    def _process_event(self, event: dict):
        """Handle a single IB event."""
        etype = event.get("event_type")
        if etype == "ACCOUNT_SUMMARY":
            tag, val = event["tag"], event["value"]
            if tag == "TotalCashValue":
                self._ib_cash_from_queue = float(val)
            elif tag == "NetLiquidation":
                self._ib_nlv_from_queue = float(val)
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

    # ── Run ────────────────────────────────────────────────

    def run(self):
        logger.info("=" * 60)
        logger.info("Daily Sync — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("=" * 60)

        self.expire_stale_signals()       # 1
        self.download_market_data()       # 2 — yfinance (primary data)
        self.load_account_from_db()       # 3 — load from DB
        self.verify_with_ib()             # 4 — IB verification
        self.reconcile_and_update()       # 5 — update DB if IB differs
        self.update_position_prices()     # 6 — yfinance prices
        self.generate_signals()           # 7 — signals + explanations
        self.save_snapshot()              # 8
        self.compute_metrics()            # 9

        if self.connection:
            try:
                self.connection.disconnect()
            except Exception:
                pass

        logger.info("=" * 60)
        logger.info("Daily sync complete.  IB verified: %s", self.ib_verified)
        logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="End-of-day sync pipeline")
    # --skip-ib is kept for backward compat but deprecated
    parser.add_argument("--skip-ib", action="store_true",
                        help="(Deprecated — IB failure is now handled gracefully)")
    args = parser.parse_args()

    if args.skip_ib:
        logger.warning("--skip-ib is deprecated. IB connection is always attempted "
                        "but failures are handled gracefully using DB values.")

    sync = DailySync()
    sync.run()
