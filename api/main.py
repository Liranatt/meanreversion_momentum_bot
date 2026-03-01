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
