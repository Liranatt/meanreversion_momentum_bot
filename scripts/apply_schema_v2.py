"""Apply schema_v2.sql to the database."""
import psycopg2
import os

url = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
conn = psycopg2.connect(url, sslmode="require")
conn.autocommit = True
cur = conn.cursor()

schema_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sql', 'schema_v2.sql')
with open(schema_path, "r") as f:
    cur.execute(f.read())

print("Schema v2 applied successfully!")

cur.execute(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema='algo_trading' ORDER BY table_name;"
)
for row in cur.fetchall():
    print(f"  - {row[0]}")

conn.close()
