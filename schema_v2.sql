-- ============================================================
-- Schema v2: Positions, Account Snapshots, Reconciliation,
--            and Pending Signals for end-of-day → market-open flow
-- ============================================================

-- 1. Positions table — one row per held symbol
CREATE TABLE IF NOT EXISTS algo_trading.positions (
    id              BIGSERIAL PRIMARY KEY,
    symbol          VARCHAR(10)  NOT NULL,
    quantity        INTEGER      NOT NULL,
    avg_cost        NUMERIC(12,4) NOT NULL,
    current_price   NUMERIC(12,4),
    market_value    NUMERIC(14,2),
    unrealized_pnl  NUMERIC(14,2),
    realized_pnl    NUMERIC(14,2) DEFAULT 0,
    strategy_type   VARCHAR(20),
    entry_date      TIMESTAMPTZ,
    last_updated    TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (symbol)
);

CREATE INDEX IF NOT EXISTS idx_positions_symbol
    ON algo_trading.positions (symbol);

-- 2. Account snapshot — daily totals
CREATE TABLE IF NOT EXISTS algo_trading.account_snapshot (
    id                   BIGSERIAL PRIMARY KEY,
    recorded_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    net_liquidation      NUMERIC(14,2),
    free_cash            NUMERIC(14,2),
    total_positions_value NUMERIC(14,2),
    total_unrealized_pnl NUMERIC(14,2),
    total_realized_pnl   NUMERIC(14,2),
    num_positions        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_account_snapshot_date
    ON algo_trading.account_snapshot (recorded_at DESC);

-- 3. Reconciliation log — IB vs DB comparison
CREATE TABLE IF NOT EXISTS algo_trading.reconciliation_log (
    id           BIGSERIAL PRIMARY KEY,
    checked_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    symbol       VARCHAR(10)  NOT NULL,
    ib_quantity  INTEGER,
    db_quantity  INTEGER,
    ib_avg_cost  NUMERIC(12,4),
    db_avg_cost  NUMERIC(12,4),
    status       VARCHAR(20)  NOT NULL  -- MATCH, MISMATCH, IB_ONLY, DB_ONLY
);

CREATE INDEX IF NOT EXISTS idx_recon_date
    ON algo_trading.reconciliation_log (checked_at DESC);

-- 4. Pending signals — generated end-of-day, executed at market open
CREATE TABLE IF NOT EXISTS algo_trading.pending_signals (
    id              BIGSERIAL PRIMARY KEY,
    generated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    symbol          VARCHAR(10)  NOT NULL,
    signal_type     VARCHAR(4)   NOT NULL,            -- BUY / SELL
    quantity        INTEGER      NOT NULL,
    target_price    NUMERIC(12,4),                    -- last close (reference)
    strategy_type   VARCHAR(20),
    reason          TEXT,
    status          VARCHAR(20)  NOT NULL DEFAULT 'PENDING',  -- PENDING / EXECUTED / CANCELLED / EXPIRED
    executed_at     TIMESTAMPTZ,
    fill_price      NUMERIC(12,4),
    ib_order_id     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pending_signals_status
    ON algo_trading.pending_signals (status, generated_at DESC);
