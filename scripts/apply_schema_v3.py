"""
apply_schema_v3.py — One-time migration to create the scan_results table.
Run:  python scripts/apply_schema_v3.py
"""
import os, sys, pathlib

# Allow running from project root
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config
import psycopg2


def main():
    url = config.DATABASE_URL
    if not url:
        print("ERROR: DATABASE_URL is not set.")
        sys.exit(1)
    url = url.replace("postgres://", "postgresql://", 1)

    sql_path = pathlib.Path(__file__).resolve().parent.parent / "sql" / "schema_v3.sql"
    sql = sql_path.read_text(encoding="utf-8")

    conn = psycopg2.connect(url, sslmode="require")
    try:
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
        print("✅ schema_v3.sql applied successfully — scan_results table created.")
    except Exception as exc:
        conn.rollback()
        print(f"❌ Migration failed: {exc}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
