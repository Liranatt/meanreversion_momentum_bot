import os
import sys
import psycopg2
from psycopg2 import sql

DB_URL = os.environ.get('DATABASE_URL')
if not DB_URL:
    print('DATABASE_URL not set', file=sys.stderr)
    sys.exit(2)

path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'sql', 'schema_v1.sql')
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

conn = psycopg2.connect(DB_URL)
conn.autocommit = True
cur = conn.cursor()

# Try executing whole script first
try:
    cur.execute(content)
    print('Applied schema script (whole).')
except Exception as e:
    print('Whole-script execution failed, trying statement-by-statement:', e)
    # naive split by semicolon
    stmts = [s.strip() for s in content.split(';') if s.strip()]
    for i, stmt in enumerate(stmts, 1):
        try:
            cur.execute(stmt)
            print(f'Statement {i}: OK')
        except Exception as ex:
            print(f'Statement {i}: SKIPPED ({ex})')

cur.close()
conn.close()
