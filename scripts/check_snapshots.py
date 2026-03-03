import os
import psycopg2

def main():
    url=os.environ.get('DATABASE_URL')
    if not url:
        print('DATABASE_URL not set')
        return
    url=url.replace('postgres://','postgresql://',1)
    conn=psycopg2.connect(url,sslmode='require')
    cur=conn.cursor()
    cur.execute("SELECT recorded_at, snapshot_date, net_liquidation, free_cash FROM algo_trading.account_snapshot ORDER BY recorded_at DESC LIMIT 5;")
    rows=cur.fetchall()
    print('LAST_SNAPSHOTS:')
    for r in rows:
        print(r)
    cur.execute("SELECT snapshot_date, COUNT(*) FROM algo_trading.account_snapshot GROUP BY snapshot_date HAVING COUNT(*)>1;")
    dups=cur.fetchall()
    print('DUPLICATE_DATES:', dups)
    cur.execute("SELECT snapshot_date FROM algo_trading.account_snapshot ORDER BY recorded_at DESC LIMIT 1;")
    print('LATEST_DATE:', cur.fetchone())
    conn.close()

if __name__=='__main__':
    main()
