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
            round(sharpe_ratio, 4),
            round(max_drawdown, 4),
            round(total_return, 4),
            round(win_rate, 2),
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
