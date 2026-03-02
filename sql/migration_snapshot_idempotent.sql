-- Migration: Make account_snapshot idempotent (one row per calendar day)
-- Adds snapshot_date column + unique index for ON CONFLICT upsert.

-- 1. Add the new column (nullable first)
ALTER TABLE algo_trading.account_snapshot
    ADD COLUMN IF NOT EXISTS snapshot_date DATE;

-- 2. Backfill from existing recorded_at timestamps
UPDATE algo_trading.account_snapshot
   SET snapshot_date = (recorded_at AT TIME ZONE 'UTC')::date
 WHERE snapshot_date IS NULL;

-- 3. De-duplicate: keep only the latest row per day
DELETE FROM algo_trading.account_snapshot a
 USING algo_trading.account_snapshot b
 WHERE a.snapshot_date = b.snapshot_date
   AND a.id < b.id;

-- 4. Now make it NOT NULL
ALTER TABLE algo_trading.account_snapshot
    ALTER COLUMN snapshot_date SET NOT NULL;

-- 5. Create the unique index for ON CONFLICT
CREATE UNIQUE INDEX IF NOT EXISTS idx_account_snapshot_unique_date
    ON algo_trading.account_snapshot (snapshot_date);
