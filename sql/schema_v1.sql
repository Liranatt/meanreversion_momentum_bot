-- ============================================================
-- Schema: algo_trading
-- Isolated schema for the mean-reversion / momentum trading bot.
-- Shares the same Heroku Postgres instance as other projects
-- (e.g. Garmin in 'public') but never touches their tables.
-- ============================================================

-- 1. Create the dedicated schema
CREATE SCHEMA IF NOT EXISTS algo_trading;

-- 2. Tables
-- ---------------------------------------------------------
-- 2a. Market Data (daily OHLCV + Adjusted Close)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo_trading.market_data (
    id          BIGSERIAL PRIMARY KEY,
    ticker      VARCHAR(10)  NOT NULL,
    date        DATE         NOT NULL,
    open        NUMERIC(12,4),
    high        NUMERIC(12,4),
    low         NUMERIC(12,4),
    close       NUMERIC(12,4),
    adj_close   NUMERIC(12,4),
    volume      BIGINT,
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_market_data_ticker_date
    ON algo_trading.market_data (ticker, date DESC);

-- ---------------------------------------------------------
-- 2b. Portfolio State (daily snapshot)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo_trading.portfolio_state (
    id             BIGSERIAL PRIMARY KEY,
    recorded_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    free_cash      NUMERIC(14,2),
    total_equity   NUMERIC(14,2),
    positions_json JSONB
);

CREATE INDEX IF NOT EXISTS idx_portfolio_state_date
    ON algo_trading.portfolio_state (recorded_at DESC);

-- ---------------------------------------------------------
-- 2c. Trades Log
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo_trading.trades_log (
    id             BIGSERIAL PRIMARY KEY,
    executed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    symbol         VARCHAR(10)  NOT NULL,
    action         VARCHAR(4)   NOT NULL,          -- BUY / SELL
    quantity       INTEGER      NOT NULL,
    price          NUMERIC(12,4),
    commission     NUMERIC(8,2) DEFAULT 0,
    pnl            NUMERIC(12,2),
    strategy_type  VARCHAR(20),                    -- momentum / mean_reversion
    order_type     VARCHAR(20)  DEFAULT 'MKT',     -- MKT / LMT / STP
    ib_order_id    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_trades_log_date
    ON algo_trading.trades_log (executed_at DESC);

-- ---------------------------------------------------------
-- 2d. Metrics (daily Sharpe, Max Drawdown, etc.)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo_trading.metrics (
    id             BIGSERIAL PRIMARY KEY,
    calculated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    sharpe_ratio   NUMERIC(8,4),
    max_drawdown   NUMERIC(8,4),
    total_return   NUMERIC(8,4),
    win_rate       NUMERIC(5,2)
);

CREATE INDEX IF NOT EXISTS idx_metrics_date
    ON algo_trading.metrics (calculated_at DESC);

-- 3. Read-only role for the FastAPI backend
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'api_reader') THEN
        CREATE ROLE api_reader NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA algo_trading TO api_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA algo_trading TO api_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA algo_trading
    GRANT SELECT ON TABLES TO api_reader;
