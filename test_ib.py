"""Safe IB Gateway connectivity test.

Connects to local IB Gateway/TWS, requests account summary and positions,
prints results and exits. Does NOT place any orders.
"""
import time
import logging
from queue import Queue, Empty
import argparse

import config
from ib_connection import IBConnection

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('test_ib')


def run_test(timeout=10):
    q = Queue()
    conn = IBConnection(q)
    conn.connect_to_ib()

    # Request account summary and positions
    conn.request_account_summary()
    conn.request_positions()

    start = time.time()
    results = []
    try:
        while time.time() - start < timeout:
            try:
                ev = q.get(timeout=1)
            except Empty:
                continue
            results.append(ev)
            # Print event
            print(ev)
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass

    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='IB Gateway connectivity test')
    parser.add_argument('--mock-net', type=float, help='Mock NetLiquidation value to display')
    parser.add_argument('--mock-cash', type=float, help='Mock TotalCashValue to display')
    parser.add_argument('--timeout', type=int, default=15, help='How long to wait for events (seconds)')
    args = parser.parse_args()

    print('Using IB host', config.IB_HOST, 'port', config.IB_PORT, 'client', config.IB_CLIENT_ID)
    events = run_test(timeout=args.timeout)

    # If user requested mocked values, show them instead of real account summary
    if args.mock_net is not None or args.mock_cash is not None:
        print('\n*** MOCKED ACCOUNT SUMMARY ***')
        if args.mock_net is not None:
            print('Mock Net Liquidation: %.2f USD' % args.mock_net)
        if args.mock_cash is not None:
            print('Mock Total Cash Value: %.2f USD' % args.mock_cash)
    else:
        print('\nSummary: received %d events' % len(events))
