"""
db_manager.py — Handles all writes to the algo_trading schema on Heroku Postgres.
Used by the daily-batch bot (main.py) to persist market data, portfolio state,
trade logs, and performance metrics.
"""
import os
import json
import logging
from datetime import datetime
from typing import Optional

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _get_connection():
    """Return a new psycopg2 connection using DATABASE_URL.
    Heroku sometimes provides postgres:// which psycopg2 needs as postgresql://
    """
    url = config.DATABASE_URL
    if not url:
        raise RuntimeError("DATABASE_URL is not set — cannot connect to Postgres.")
    url = url.replace("postgres://", "postgresql://", 1)
    conn = psycopg2.connect(url, sslmode="require")
    conn.autocommit = False
    return conn


def _set_schema(cur):
    """Ensure every query targets the algo_trading schema."""
    cur.execute("SET search_path TO algo_trading, public;")


# ---------------------------------------------------------------------------
# Market Data
# ---------------------------------------------------------------------------

def save_market_data(ticker: str, df):
    """Upsert daily OHLCV rows for *ticker* from a pandas DataFrame.
    Expected columns: Open, High, Low, Close, Adj Close / Close (fallback), Volume.
    Index must be DatetimeIndex.
    """
    if df.empty:
        return

    rows = []
    for date_idx, row in df.iterrows():
        adj = row.get("Adj Close", row.get("Close"))
        rows.append((
            ticker,
            date_idx.date() if hasattr(date_idx, "date") else date_idx,
            float(row["Open"]),
            float(row["High"]),
            float(row["Low"]),
            float(row["Close"]),
            float(adj),
            int(row["Volume"]) if row["Volume"] else 0,
        ))

    sql = """
        INSERT INTO algo_trading.market_data
            (ticker, date, open, high, low, close, adj_close, volume)
        VALUES %s
        ON CONFLICT (ticker, date) DO UPDATE SET
            open      = EXCLUDED.open,
            high      = EXCLUDED.high,
            low       = EXCLUDED.low,
            close     = EXCLUDED.close,
            adj_close = EXCLUDED.adj_close,
            volume    = EXCLUDED.volume;
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        execute_values(cur, sql, rows, page_size=500)
        conn.commit()
        logger.info("Saved %d market-data rows for %s", len(rows), ticker)
    except Exception:
        conn.rollback()
        logger.exception("Failed to save market data for %s", ticker)
        raise
    finally:
        conn.close()


def get_market_data_count(ticker: str) -> int:
    """Return number of rows in algo_trading.market_data for *ticker*."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute("SELECT COUNT(*) FROM algo_trading.market_data WHERE ticker = %s;", (ticker,))
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def load_market_data(ticker: str) -> pd.DataFrame:
    """Load market data for *ticker* from DB into a pandas DataFrame.

    Returns a DataFrame with DatetimeIndex and columns:
    Open, High, Low, Close, Adj Close, Volume
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute("""
            SELECT date, open, high, low, close, adj_close, volume
            FROM algo_trading.market_data
            WHERE ticker = %s
            ORDER BY date;
        """, (ticker,))
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame(rows, columns=cols)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')
        df = df.rename(columns={
            'open': 'Open', 'high': 'High', 'low': 'Low',
            'close': 'Close', 'adj_close': 'Adj Close', 'volume': 'Volume'
        })
        for c in ['Open', 'High', 'Low', 'Close', 'Adj Close']:
            df[c] = df[c].astype(float)
        df['Volume'] = df['Volume'].astype(int)
        return df
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Portfolio State
# ---------------------------------------------------------------------------

def save_portfolio_state(free_cash: float, total_equity: float, positions: dict):
    """Insert a daily snapshot of the portfolio."""
    sql = """
        INSERT INTO algo_trading.portfolio_state
            (recorded_at, free_cash, total_equity, positions_json)
        VALUES (%s, %s, %s, %s);
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute(sql, (
            datetime.utcnow(),
            round(free_cash, 2),
            round(total_equity, 2),
            json.dumps(positions, default=str),
        ))
        conn.commit()
        logger.info("Saved portfolio state — cash=%.2f equity=%.2f", free_cash, total_equity)
    except Exception:
        conn.rollback()
        logger.exception("Failed to save portfolio state")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Trades Log
# ---------------------------------------------------------------------------

def log_trade(
    symbol: str,
    action: str,
    quantity: int,
    price: float,
    commission: float = 0.0,
    pnl: float = 0.0,
    strategy_type: str = "",
    order_type: str = "MKT",
    ib_order_id: Optional[int] = None,
):
    """Record a single executed trade."""
    sql = """
        INSERT INTO algo_trading.trades_log
            (executed_at, symbol, action, quantity, price, commission, pnl,
             strategy_type, order_type, ib_order_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute(sql, (
            datetime.utcnow(),
            symbol,
            action.upper(),
            quantity,
            round(price, 4),
            round(commission, 2),
            round(pnl, 2),
            strategy_type,
            order_type,
            ib_order_id,
        ))
        conn.commit()
        logger.info("Logged trade: %s %d %s @ %.4f", action, quantity, symbol, price)
    except Exception:
        conn.rollback()
        logger.exception("Failed to log trade for %s", symbol)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def save_metrics(
    sharpe_ratio: float,
    max_drawdown: float,
    total_return: float,
    win_rate: float,
):
    """Insert a daily metrics snapshot."""
    sql = """
        INSERT INTO algo_trading.metrics
            (calculated_at, sharpe_ratio, max_drawdown, total_return, win_rate)
        VALUES (%s, %s, %s, %s, %s);
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute(sql, (
            datetime.utcnow(),
            round(float(sharpe_ratio), 4),
            round(float(max_drawdown), 4),
            round(float(total_return), 4),
            round(float(win_rate), 2),
        ))
        conn.commit()
        logger.info(
            "Saved metrics — sharpe=%.4f max_dd=%.4f return=%.4f win=%.2f%%",
            sharpe_ratio, max_drawdown, total_return, win_rate,
        )
    except Exception:
        conn.rollback()
        logger.exception("Failed to save metrics")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Positions (new v2)
# ---------------------------------------------------------------------------

def upsert_position(
    symbol: str,
    quantity: int,
    avg_cost: float,
    current_price: float = 0.0,
    strategy_type: str = "",
    entry_date: Optional[datetime] = None,
    realized_pnl: float = 0.0,
    highest_price: Optional[float] = None,
):
    """Insert or update a position row. Computes market_value & unrealized P&L."""
    market_value = quantity * current_price
    unrealized_pnl = (current_price - avg_cost) * quantity
    hp = highest_price if highest_price is not None else current_price
    sql = """
        INSERT INTO algo_trading.positions
            (symbol, quantity, avg_cost, current_price, highest_price, market_value,
             unrealized_pnl, realized_pnl, strategy_type, entry_date, last_updated)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE SET
            quantity        = EXCLUDED.quantity,
            avg_cost        = EXCLUDED.avg_cost,
            current_price   = EXCLUDED.current_price,
            highest_price   = GREATEST(algo_trading.positions.highest_price, EXCLUDED.highest_price),
            market_value    = EXCLUDED.market_value,
            unrealized_pnl  = EXCLUDED.unrealized_pnl,
            realized_pnl    = algo_trading.positions.realized_pnl + EXCLUDED.realized_pnl,
            strategy_type   = COALESCE(NULLIF(EXCLUDED.strategy_type,''), algo_trading.positions.strategy_type),
            entry_date      = COALESCE(EXCLUDED.entry_date, algo_trading.positions.entry_date),
            last_updated    = EXCLUDED.last_updated;
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute(sql, (
            symbol, quantity, round(avg_cost, 4), round(current_price, 4),
            round(hp, 4),
            round(market_value, 2), round(unrealized_pnl, 2),
            round(realized_pnl, 2), strategy_type,
            entry_date or datetime.utcnow(), datetime.utcnow(),
        ))
        conn.commit()
        logger.info("Upserted position: %s qty=%d avg=%.4f cur=%.4f high=%.4f", symbol, quantity, avg_cost, current_price, hp)
    except Exception:
        conn.rollback()
        logger.exception("Failed to upsert position for %s", symbol)
        raise
    finally:
        conn.close()


def remove_position(symbol: str):
    """Delete a position row (after full liquidation)."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute("DELETE FROM algo_trading.positions WHERE symbol = %s;", (symbol,))
        conn.commit()
        logger.info("Removed position: %s", symbol)
    except Exception:
        conn.rollback()
        logger.exception("Failed to remove position %s", symbol)
        raise
    finally:
        conn.close()


def update_current_prices(price_map: dict):
    """Bulk-update current_price, highest_price, market_value, unrealized_pnl for all positions."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        for symbol, price in price_map.items():
            cur.execute("""
                UPDATE algo_trading.positions
                SET current_price  = %s,
                    highest_price  = GREATEST(COALESCE(highest_price, 0), %s),
                    market_value   = quantity * %s,
                    unrealized_pnl = ((%s) - avg_cost) * quantity,
                    last_updated   = %s
                WHERE symbol = %s;
            """, (round(price, 4), round(price, 4), round(price, 4),
                  round(price, 4), datetime.utcnow(), symbol))
        conn.commit()
        logger.info("Updated current prices for %d symbols", len(price_map))
    except Exception:
        conn.rollback()
        logger.exception("Failed to update current prices")
        raise
    finally:
        conn.close()


def get_all_positions() -> list:
    """Return all position rows as a list of dicts."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute("""
            SELECT symbol, quantity, avg_cost, current_price, highest_price, market_value,
                   unrealized_pnl, realized_pnl, strategy_type, entry_date, last_updated
            FROM algo_trading.positions ORDER BY symbol;
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def accumulate_realized_pnl(symbol: str, pnl: float):
    """Add to the running realized P&L total for a position."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute("""
            UPDATE algo_trading.positions
            SET realized_pnl = realized_pnl + %s, last_updated = %s
            WHERE symbol = %s;
        """, (round(pnl, 2), datetime.utcnow(), symbol))
        conn.commit()
        logger.info("Accumulated realized P&L for %s: %.2f", symbol, pnl)
    except Exception:
        conn.rollback()
        logger.exception("Failed to accumulate P&L for %s", symbol)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Account Snapshots (new v2)
# ---------------------------------------------------------------------------

def save_account_snapshot(
    net_liquidation: float,
    free_cash: float,
    total_positions_value: float,
    total_unrealized_pnl: float,
    total_realized_pnl: float,
    num_positions: int,
):
    """Upsert a daily account summary snapshot (one row per calendar day).

    Uses the snapshot_date unique index so re-runs on the same day
    update the existing row instead of creating duplicates.
    """
    now = datetime.utcnow()
    snapshot_date = now.date()
    sql = """
        INSERT INTO algo_trading.account_snapshot
            (recorded_at, snapshot_date, net_liquidation, free_cash,
             total_positions_value, total_unrealized_pnl,
             total_realized_pnl, num_positions)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (snapshot_date) DO UPDATE SET
            recorded_at          = EXCLUDED.recorded_at,
            net_liquidation      = EXCLUDED.net_liquidation,
            free_cash            = EXCLUDED.free_cash,
            total_positions_value = EXCLUDED.total_positions_value,
            total_unrealized_pnl = EXCLUDED.total_unrealized_pnl,
            total_realized_pnl   = EXCLUDED.total_realized_pnl,
            num_positions        = EXCLUDED.num_positions;
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute(sql, (
            now, snapshot_date,
            round(net_liquidation, 2), round(free_cash, 2),
            round(total_positions_value, 2), round(total_unrealized_pnl, 2),
            round(total_realized_pnl, 2), num_positions,
        ))
        conn.commit()
        logger.info(
            "Saved account snapshot — NLV=%.2f cash=%.2f positions_value=%.2f (date=%s)",
            net_liquidation, free_cash, total_positions_value, snapshot_date,
        )
    except Exception:
        conn.rollback()
        logger.exception("Failed to save account snapshot")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reconciliation Log (new v2)
# ---------------------------------------------------------------------------

def log_reconciliation(entries: list):
    """Bulk-insert reconciliation comparison rows.
    Each entry: { symbol, ib_quantity, db_quantity, ib_avg_cost, db_avg_cost, status }
    """
    if not entries:
        return
    sql = """
        INSERT INTO algo_trading.reconciliation_log
            (checked_at, symbol, ib_quantity, db_quantity, ib_avg_cost, db_avg_cost, status)
        VALUES %s;
    """
    now = datetime.utcnow()
    rows = [
        (now, e["symbol"],
         e.get("ib_quantity"), e.get("db_quantity"),
         e.get("ib_avg_cost"), e.get("db_avg_cost"),
         e["status"])
        for e in entries
    ]
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        execute_values(cur, sql, rows, page_size=100)
        conn.commit()
        logger.info("Logged %d reconciliation entries", len(rows))
    except Exception:
        conn.rollback()
        logger.exception("Failed to log reconciliation")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pending Signals (new v2)
# ---------------------------------------------------------------------------

def save_pending_signal(
    symbol: str,
    signal_type: str,
    quantity: int,
    target_price: float,
    strategy_type: str = "",
    reason: str = "",
):
    """Save a generated signal to be executed at next market open."""
    sql = """
        INSERT INTO algo_trading.pending_signals
            (generated_at, symbol, signal_type, quantity, target_price,
             strategy_type, reason, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING');
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute(sql, (
            datetime.utcnow(), symbol, signal_type.upper(), quantity,
            round(target_price, 4), strategy_type, reason,
        ))
        conn.commit()
        logger.info("Saved pending signal: %s %d %s @ %.4f", signal_type, quantity, symbol, target_price)
    except Exception:
        conn.rollback()
        logger.exception("Failed to save pending signal for %s", symbol)
        raise
    finally:
        conn.close()


def get_pending_signals() -> list:
    """Return all PENDING signals (to be executed at market open)."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute("""
            SELECT id, generated_at, symbol, signal_type, quantity,
                   target_price, strategy_type, reason, status
            FROM algo_trading.pending_signals
            WHERE status = 'PENDING'
            ORDER BY generated_at DESC;
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def mark_signal_executed(signal_id: int, fill_price: float, ib_order_id: int = None):
    """Mark a pending signal as executed after fill."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute("""
            UPDATE algo_trading.pending_signals
            SET status = 'EXECUTED', executed_at = %s,
                fill_price = %s, ib_order_id = %s
            WHERE id = %s;
        """, (datetime.utcnow(), round(fill_price, 4), ib_order_id, signal_id))
        conn.commit()
        logger.info("Signal %d marked as EXECUTED @ %.4f", signal_id, fill_price)
    except Exception:
        conn.rollback()
        logger.exception("Failed to mark signal %d as executed", signal_id)
        raise
    finally:
        conn.close()


def expire_old_signals():
    """Mark any PENDING signals older than 1 day as EXPIRED."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute("""
            UPDATE algo_trading.pending_signals
            SET status = 'EXPIRED'
            WHERE status = 'PENDING'
              AND generated_at < NOW() - INTERVAL '24 hours';
        """)
        expired = cur.rowcount
        conn.commit()
        if expired:
            logger.info("Expired %d stale pending signals", expired)
    except Exception:
        conn.rollback()
        logger.exception("Failed to expire old signals")
        raise
    finally:
        conn.close()


def mark_signal_expired_with_reason(signal_id: int, reason: str):
    """Mark a specific signal as EXPIRED with a reason (e.g. price drift)."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute("""
            UPDATE algo_trading.pending_signals
            SET status = 'EXPIRED',
                reason = reason || ' | SKIPPED: ' || %s
            WHERE id = %s;
        """, (reason, signal_id))
        conn.commit()
        logger.info("Signal %d marked EXPIRED: %s", signal_id, reason[:80])
    except Exception:
        conn.rollback()
        logger.exception("Failed to expire signal %d", signal_id)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scan Results (v3 — Market Scanner)
# ---------------------------------------------------------------------------

def save_scan_results(entries: list):
    """Bulk upsert scan result rows for all 100 tickers.
    Each entry is a dict with keys matching the scan_results columns.
    """
    if not entries:
        return

    sql = """
        INSERT INTO algo_trading.scan_results
            (scan_date, symbol, close_price, sma_30, upper_bb, lower_bb,
             rsi_14, atr_14, atr_signal, macd_signal, bb_signal,
             market_regime, signal_result, rejection_reason, is_held)
        VALUES %s
        ON CONFLICT (scan_date, symbol) DO UPDATE SET
            close_price      = EXCLUDED.close_price,
            sma_30           = EXCLUDED.sma_30,
            upper_bb         = EXCLUDED.upper_bb,
            lower_bb         = EXCLUDED.lower_bb,
            rsi_14           = EXCLUDED.rsi_14,
            atr_14           = EXCLUDED.atr_14,
            atr_signal       = EXCLUDED.atr_signal,
            macd_signal      = EXCLUDED.macd_signal,
            bb_signal        = EXCLUDED.bb_signal,
            market_regime    = EXCLUDED.market_regime,
            signal_result    = EXCLUDED.signal_result,
            rejection_reason = EXCLUDED.rejection_reason,
            is_held          = EXCLUDED.is_held;
    """

    rows = [
        (
            e["scan_date"], e["symbol"],
            e.get("close_price"), e.get("sma_30"), e.get("upper_bb"), e.get("lower_bb"),
            e.get("rsi_14"), e.get("atr_14"), e.get("atr_signal"), e.get("macd_signal"),
            e.get("bb_signal"), e.get("market_regime"), e.get("signal_result"),
            e.get("rejection_reason"), e.get("is_held", False),
        )
        for e in entries
    ]

    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        execute_values(cur, sql, rows, page_size=200)
        conn.commit()
        logger.info("Saved %d scan result rows", len(rows))
    except Exception:
        conn.rollback()
        logger.exception("Failed to save scan results")
        raise
    finally:
        conn.close()


def get_latest_account_snapshot() -> dict | None:
    """Return the most recent non-zero account snapshot, or None."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute("""
            SELECT recorded_at, snapshot_date, net_liquidation, free_cash,
                   total_positions_value, total_unrealized_pnl,
                   total_realized_pnl, num_positions
            FROM algo_trading.account_snapshot
            WHERE net_liquidation > 0
            ORDER BY recorded_at DESC
            LIMIT 1;
        """)
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def get_latest_scan_results() -> list:
    """Return the most recent scan results (all tickers for latest scan_date)."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        _set_schema(cur)
        cur.execute("""
            SELECT scan_date, symbol, close_price, sma_30, upper_bb, lower_bb,
                   rsi_14, atr_14, atr_signal, macd_signal, bb_signal,
                   market_regime, signal_result, rejection_reason, is_held
            FROM algo_trading.scan_results
            WHERE scan_date = (
                SELECT MAX(scan_date) FROM algo_trading.scan_results
            )
            ORDER BY symbol;
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()
