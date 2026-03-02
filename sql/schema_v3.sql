-- schema_v3.sql — Add scan_results table for the Market Scanner feature
-- Stores indicator snapshots for ALL 100 NASDAQ tickers on each daily scan.

SET search_path TO algo_trading, public;

CREATE TABLE IF NOT EXISTS algo_trading.scan_results (
    id              BIGSERIAL       PRIMARY KEY,
    scan_date       DATE            NOT NULL,
    symbol          VARCHAR(10)     NOT NULL,
    close_price     NUMERIC(12,4),
    sma_30          NUMERIC(12,4),
    upper_bb        NUMERIC(12,4),
    lower_bb        NUMERIC(12,4),
    rsi_14          NUMERIC(8,4),
    atr_14          NUMERIC(12,4),
    atr_signal      VARCHAR(10),        -- 'high' | 'low'
    macd_signal     VARCHAR(10),        -- 'strong' | 'Medium' | 'weak'
    bb_signal       VARCHAR(20),        -- 'up above' | 'low below' | 'SMA'
    market_regime   VARCHAR(10),        -- 'bull' | 'bear'
    signal_result   VARCHAR(12),        -- 'BUY' | 'SELL' | 'HOLD' | 'NO_DATA'
    rejection_reason TEXT,              -- why signal was NOT generated
    is_held         BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT uq_scan_date_symbol UNIQUE (scan_date, symbol)
);

-- Fast lookups for the dashboard
CREATE INDEX IF NOT EXISTS idx_scan_results_date
    ON algo_trading.scan_results (scan_date DESC);

CREATE INDEX IF NOT EXISTS idx_scan_results_symbol_date
    ON algo_trading.scan_results (symbol, scan_date DESC);
