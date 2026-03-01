"""
reset_portfolio.py — Safely insert a portfolio snapshot with cash=100000 and
empty positions into algo_trading.portfolio_state. Does NOT touch IB account.

Usage:
  $env:DATABASE_URL='postgres://...'
  python reset_portfolio.py --cash 100000
"""
import argparse
import logging
import db_manager

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('reset_portfolio')

parser = argparse.ArgumentParser(description='Insert portfolio snapshot into DB')
parser.add_argument('--cash', type=float, default=100000.0, help='Free cash amount')
args = parser.parse_args()

cash = float(args.cash)
positions = {}

log.info('Inserting portfolio snapshot: cash=%.2f, positions=%s', cash, positions)
try:
    db_manager.save_portfolio_state(cash, cash, positions)
    log.info('Snapshot inserted.')
except Exception as e:
    log.exception('Failed to insert snapshot: %s', e)
    raise
