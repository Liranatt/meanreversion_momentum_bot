"""
db_maintenance.py — Retention policy for algo_trading.market_data.
Deletes rows older than 365 days and runs VACUUM ANALYZE.
Intended to be called as a post-step in the GitHub Actions daily workflow.
"""
import os
import sys
import logging
from datetime import datetime, timedelta

import psycopg2
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def run_maintenance():
    url = config.DATABASE_URL
    if not url:
        logger.error("DATABASE_URL is not set — aborting maintenance.")
        sys.exit(1)

    url = url.replace("postgres://", "postgresql://", 1)
    conn = psycopg2.connect(url, sslmode="require")

    try:
        # --- 1. Delete old rows (inside a transaction) ---
        cutoff = (datetime.utcnow() - timedelta(days=365)).date()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM algo_trading.market_data WHERE date < %s;",
            (cutoff,),
        )
        deleted = cur.rowcount
        conn.commit()
        logger.info("Deleted %d market_data rows older than %s.", deleted, cutoff)

        # --- 2. VACUUM ANALYZE (requires autocommit) ---
        conn.autocommit = True
        cur.execute("VACUUM ANALYZE algo_trading.market_data;")
        logger.info("VACUUM ANALYZE complete on algo_trading.market_data.")

    except Exception:
        conn.rollback()
        logger.exception("Maintenance failed")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run_maintenance()
