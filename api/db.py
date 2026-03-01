"""
api/db.py — Async database connection pool for the FastAPI layer.
Connects to the shared Heroku Postgres using DATABASE_URL and routes
all queries through the algo_trading schema.
"""
import os
import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        url = os.getenv("DATABASE_URL", "")
        if not url:
            raise RuntimeError("DATABASE_URL not set")
        url = url.replace("postgres://", "postgresql://", 1)
        _pool = await asyncpg.create_pool(
            dsn=url,
            min_size=1,
            max_size=5,
            ssl="require",
            server_settings={"search_path": "algo_trading,public"},
        )
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
