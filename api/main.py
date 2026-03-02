"""
api/main.py — FastAPI microservice serving algo-trading data to the
static frontend dashboard at liranattar.dev/algotrading.

Deployed as the Heroku app "algotrading".
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.db import get_pool, close_pool

# ── CORS origins ─────────────────────────────────────────
ALLOWED_ORIGINS = [
    "https://liranattar.dev",
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:8000",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: warm the pool
    await get_pool()
    yield
    # shutdown: close the pool
    await close_pool()


app = FastAPI(
    title="AlgoTrading API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Health check ──────────────────────────────────────────

@app.get("/health")
@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ── Portfolio equity curve ────────────────────────────────

@app.get("/api/v1/portfolio/history")
async def portfolio_history(limit: int = Query(default=365, ge=1, le=3650)):
    """Return the equity curve — total_equity over time."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT recorded_at, free_cash, total_equity, positions_json
        FROM algo_trading.portfolio_state
        ORDER BY recorded_at DESC
        LIMIT $1;
        """,
        limit,
    )
    data = [
        {
            "date": r["recorded_at"].isoformat(),
            "free_cash": float(r["free_cash"]) if r["free_cash"] else 0,
            "total_equity": float(r["total_equity"]) if r["total_equity"] else 0,
            "positions": r["positions_json"],
        }
        for r in reversed(rows)
    ]
    return JSONResponse(content={"count": len(data), "data": data})


# ── Recent trades ─────────────────────────────────────────

@app.get("/api/v1/trades/recent")
async def recent_trades(limit: int = Query(default=50, ge=1, le=500)):
    """Return the most recent trade executions."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT executed_at, symbol, action, quantity, price,
               commission, pnl, strategy_type, order_type
        FROM algo_trading.trades_log
        ORDER BY executed_at DESC
        LIMIT $1;
        """,
        limit,
    )
    data = [
        {
            "date": r["executed_at"].isoformat(),
            "symbol": r["symbol"],
            "action": r["action"],
            "quantity": r["quantity"],
            "price": float(r["price"]) if r["price"] else 0,
            "commission": float(r["commission"]) if r["commission"] else 0,
            "pnl": float(r["pnl"]) if r["pnl"] else 0,
            "strategy": r["strategy_type"],
            "order_type": r["order_type"],
        }
        for r in rows
    ]
    return JSONResponse(content={"count": len(data), "data": data})


# ── Current metrics ───────────────────────────────────────

@app.get("/api/v1/metrics/current")
async def current_metrics():
    """Return the latest Sharpe Ratio, Max Drawdown, etc."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT calculated_at, sharpe_ratio, max_drawdown, total_return, win_rate
        FROM algo_trading.metrics
        ORDER BY calculated_at DESC
        LIMIT 1;
        """
    )
    if row is None:
        return JSONResponse(
            content={"data": None, "message": "No metrics available yet."},
            status_code=200,
        )
    return JSONResponse(content={
        "data": {
            "date": row["calculated_at"].isoformat(),
            "sharpe_ratio": float(row["sharpe_ratio"]) if row["sharpe_ratio"] else 0,
            "max_drawdown": float(row["max_drawdown"]) if row["max_drawdown"] else 0,
            "total_return": float(row["total_return"]) if row["total_return"] else 0,
            "win_rate": float(row["win_rate"]) if row["win_rate"] else 0,
        }
    })


# ── Market data summary ──────────────────────────────────

@app.get("/api/v1/market/summary")
async def market_summary():
    """Return a summary of backfilled market data: row count, tickers, date range."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT COUNT(*) AS row_count,
               COUNT(DISTINCT ticker) AS ticker_count,
               MIN(date) AS first_date,
               MAX(date) AS last_date
        FROM algo_trading.market_data;
        """
    )
    return JSONResponse(content={
        "data": {
            "total_rows": row["row_count"],
            "tickers_tracked": row["ticker_count"],
            "first_date": row["first_date"].isoformat() if row["first_date"] else None,
            "last_date": row["last_date"].isoformat() if row["last_date"] else None,
        }
    })


@app.get("/api/v1/market/latest")
async def market_latest(
    ticker: str = Query(default="AAPL"),
    limit: int = Query(default=30, ge=1, le=365),
):
    """Return the latest daily OHLCV rows for a single ticker."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT date, open, high, low, close, adj_close, volume
        FROM algo_trading.market_data
        WHERE ticker = $1
        ORDER BY date DESC
        LIMIT $2;
        """,
        ticker.upper(),
        limit,
    )
    data = [
        {
            "date": r["date"].isoformat(),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "adj_close": float(r["adj_close"]) if r["adj_close"] else float(r["close"]),
            "volume": r["volume"],
        }
        for r in reversed(rows)
    ]
    return JSONResponse(content={"ticker": ticker.upper(), "count": len(data), "data": data})


# ── Current positions ─────────────────────────────────────

@app.get("/api/v1/positions/current")
async def current_positions():
    """Return all held positions with current price, unrealized P&L, etc."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT symbol, quantity, avg_cost, current_price, market_value,
               unrealized_pnl, realized_pnl, strategy_type, entry_date, last_updated
        FROM algo_trading.positions
        ORDER BY market_value DESC NULLS LAST;
        """
    )
    data = [
        {
            "symbol": r["symbol"],
            "quantity": r["quantity"],
            "avg_cost": float(r["avg_cost"]) if r["avg_cost"] else 0,
            "current_price": float(r["current_price"]) if r["current_price"] else 0,
            "market_value": float(r["market_value"]) if r["market_value"] else 0,
            "unrealized_pnl": float(r["unrealized_pnl"]) if r["unrealized_pnl"] else 0,
            "realized_pnl": float(r["realized_pnl"]) if r["realized_pnl"] else 0,
            "strategy": r["strategy_type"] or "",
            "entry_date": r["entry_date"].isoformat() if r["entry_date"] else None,
            "last_updated": r["last_updated"].isoformat() if r["last_updated"] else None,
        }
        for r in rows
    ]
    totals = {
        "total_market_value": sum(d["market_value"] for d in data),
        "total_unrealized_pnl": sum(d["unrealized_pnl"] for d in data),
        "total_realized_pnl": sum(d["realized_pnl"] for d in data),
        "num_positions": len(data),
    }
    return JSONResponse(content={"count": len(data), "totals": totals, "data": data})


# ── Account overview ──────────────────────────────────────

@app.get("/api/v1/account/current")
async def account_current():
    """Return the latest account snapshot."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT recorded_at, net_liquidation, free_cash, total_positions_value,
               total_unrealized_pnl, total_realized_pnl, num_positions
        FROM algo_trading.account_snapshot
        ORDER BY recorded_at DESC
        LIMIT 1;
        """
    )
    if row is None:
        return JSONResponse(content={"data": None, "message": "No account data yet."})
    return JSONResponse(content={"data": {
        "date": row["recorded_at"].isoformat(),
        "net_liquidation": float(row["net_liquidation"]) if row["net_liquidation"] else 0,
        "free_cash": float(row["free_cash"]) if row["free_cash"] else 0,
        "total_positions_value": float(row["total_positions_value"]) if row["total_positions_value"] else 0,
        "total_unrealized_pnl": float(row["total_unrealized_pnl"]) if row["total_unrealized_pnl"] else 0,
        "total_realized_pnl": float(row["total_realized_pnl"]) if row["total_realized_pnl"] else 0,
        "num_positions": row["num_positions"] or 0,
    }})


@app.get("/api/v1/account/history")
async def account_history(limit: int = Query(default=365, ge=1, le=3650)):
    """Return equity curve from account snapshots."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT recorded_at, net_liquidation, free_cash,
               total_positions_value, total_unrealized_pnl, total_realized_pnl
        FROM algo_trading.account_snapshot
        ORDER BY recorded_at DESC
        LIMIT $1;
        """,
        limit,
    )
    data = [
        {
            "date": r["recorded_at"].isoformat(),
            "net_liquidation": float(r["net_liquidation"]) if r["net_liquidation"] else 0,
            "free_cash": float(r["free_cash"]) if r["free_cash"] else 0,
            "positions_value": float(r["total_positions_value"]) if r["total_positions_value"] else 0,
            "unrealized_pnl": float(r["total_unrealized_pnl"]) if r["total_unrealized_pnl"] else 0,
            "realized_pnl": float(r["total_realized_pnl"]) if r["total_realized_pnl"] else 0,
        }
        for r in reversed(rows)
    ]
    return JSONResponse(content={"count": len(data), "data": data})


# ── Pending signals ───────────────────────────────────────

@app.get("/api/v1/signals/pending")
async def pending_signals():
    """Return all signals awaiting execution at next market open."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, generated_at, symbol, signal_type, quantity,
               target_price, strategy_type, reason, status
        FROM algo_trading.pending_signals
        WHERE status = 'PENDING'
        ORDER BY generated_at DESC;
        """
    )
    data = [
        {
            "id": r["id"],
            "generated_at": r["generated_at"].isoformat(),
            "symbol": r["symbol"],
            "signal_type": r["signal_type"],
            "quantity": r["quantity"],
            "target_price": float(r["target_price"]) if r["target_price"] else 0,
            "strategy": r["strategy_type"] or "",
            "reason": r["reason"] or "",
        }
        for r in rows
    ]
    return JSONResponse(content={"count": len(data), "data": data})


@app.get("/api/v1/signals/history")
async def signals_history(limit: int = Query(default=100, ge=1, le=1000)):
    """Return recent signal history (all statuses)."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, generated_at, symbol, signal_type, quantity,
               target_price, strategy_type, reason, status, executed_at, fill_price
        FROM algo_trading.pending_signals
        ORDER BY generated_at DESC
        LIMIT $1;
        """,
        limit,
    )
    data = [
        {
            "id": r["id"],
            "generated_at": r["generated_at"].isoformat(),
            "symbol": r["symbol"],
            "signal_type": r["signal_type"],
            "quantity": r["quantity"],
            "target_price": float(r["target_price"]) if r["target_price"] else 0,
            "strategy": r["strategy_type"] or "",
            "reason": r["reason"] or "",
            "status": r["status"],
            "executed_at": r["executed_at"].isoformat() if r["executed_at"] else None,
            "fill_price": float(r["fill_price"]) if r["fill_price"] else None,
        }
        for r in rows
    ]
    return JSONResponse(content={"count": len(data), "data": data})

# ── Market Scanner ────────────────────────────────────

@app.get("/api/v1/scanner/latest")
async def scanner_latest():
    """Return the most recent scan results for all ~100 tickers."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT scan_date, symbol, close_price, sma_30, upper_bb, lower_bb,
               rsi_14, atr_14, atr_signal, macd_signal, bb_signal,
               market_regime, signal_result, rejection_reason, is_held
        FROM algo_trading.scan_results
        WHERE scan_date = (
            SELECT MAX(scan_date) FROM algo_trading.scan_results
        )
        ORDER BY symbol;
        """
    )
    if not rows:
        return JSONResponse(content={"count": 0, "scan_date": None, "data": []})

    data = [
        {
            "scan_date": r["scan_date"].isoformat(),
            "symbol": r["symbol"],
            "close_price": float(r["close_price"]) if r["close_price"] else None,
            "sma_30": float(r["sma_30"]) if r["sma_30"] else None,
            "upper_bb": float(r["upper_bb"]) if r["upper_bb"] else None,
            "lower_bb": float(r["lower_bb"]) if r["lower_bb"] else None,
            "rsi_14": float(r["rsi_14"]) if r["rsi_14"] else None,
            "atr_14": float(r["atr_14"]) if r["atr_14"] else None,
            "atr_signal": r["atr_signal"],
            "macd_signal": r["macd_signal"],
            "bb_signal": r["bb_signal"],
            "market_regime": r["market_regime"],
            "signal_result": r["signal_result"],
            "rejection_reason": r["rejection_reason"],
            "is_held": r["is_held"],
        }
        for r in rows
    ]
    return JSONResponse(content={
        "count": len(data),
        "scan_date": data[0]["scan_date"] if data else None,
        "data": data,
    })


@app.get("/api/v1/scanner/history")
async def scanner_history(
    symbol: str = Query(default="AAPL"),
    limit: int = Query(default=30, ge=1, le=365),
):
    """Return scan history for a single ticker."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT scan_date, symbol, close_price, sma_30, upper_bb, lower_bb,
               rsi_14, atr_14, atr_signal, macd_signal, bb_signal,
               market_regime, signal_result, rejection_reason, is_held
        FROM algo_trading.scan_results
        WHERE symbol = $1
        ORDER BY scan_date DESC
        LIMIT $2;
        """,
        symbol.upper(),
        limit,
    )
    data = [
        {
            "scan_date": r["scan_date"].isoformat(),
            "symbol": r["symbol"],
            "close_price": float(r["close_price"]) if r["close_price"] else None,
            "sma_30": float(r["sma_30"]) if r["sma_30"] else None,
            "rsi_14": float(r["rsi_14"]) if r["rsi_14"] else None,
            "atr_signal": r["atr_signal"],
            "macd_signal": r["macd_signal"],
            "bb_signal": r["bb_signal"],
            "signal_result": r["signal_result"],
            "rejection_reason": r["rejection_reason"],
            "is_held": r["is_held"],
        }
        for r in reversed(rows)
    ]
    return JSONResponse(content={"symbol": symbol.upper(), "count": len(data), "data": data})

# ── Reconciliation ────────────────────────────────────────

@app.get("/api/v1/reconciliation/latest")
async def reconciliation_latest():
    """Return the most recent reconciliation results."""
    pool = await get_pool()
    # Get the timestamp of the latest reconciliation run
    ts_row = await pool.fetchrow(
        "SELECT MAX(checked_at) AS latest FROM algo_trading.reconciliation_log;"
    )
    if ts_row is None or ts_row["latest"] is None:
        return JSONResponse(content={"data": [], "message": "No reconciliation data yet."})

    rows = await pool.fetch(
        """
        SELECT checked_at, symbol, ib_quantity, db_quantity,
               ib_avg_cost, db_avg_cost, status
        FROM algo_trading.reconciliation_log
        WHERE checked_at = $1
        ORDER BY symbol;
        """,
        ts_row["latest"],
    )
    data = [
        {
            "checked_at": r["checked_at"].isoformat(),
            "symbol": r["symbol"],
            "ib_quantity": r["ib_quantity"],
            "db_quantity": r["db_quantity"],
            "ib_avg_cost": float(r["ib_avg_cost"]) if r["ib_avg_cost"] else 0,
            "db_avg_cost": float(r["db_avg_cost"]) if r["db_avg_cost"] else 0,
            "status": r["status"],
        }
        for r in rows
    ]
    return JSONResponse(content={"count": len(data), "checked_at": ts_row["latest"].isoformat(), "data": data})
