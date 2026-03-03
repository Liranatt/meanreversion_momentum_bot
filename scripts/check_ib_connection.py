"""
check_ib_connection.py — CI/Dev IB Gateway connectivity check using ib_insync.

Connects to IB Gateway, prints account summary and positions, exits with code 0
on success or 1 on failure. Used in GitHub Actions CI pipeline.

Usage:
  python scripts/check_ib_connection.py
  python scripts/check_ib_connection.py --host 127.0.0.1 --port 4002 --timeout 20
"""

import sys
import argparse
from ib_insync import IB


def main():
    parser = argparse.ArgumentParser(description="IB Gateway connectivity check")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--client-id", type=int, default=99)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--require-account", type=str, default=None)
    args = parser.parse_args()

    ib = IB()
    try:
        ib.connect(args.host, args.port, clientId=args.client_id, timeout=args.timeout)
    except Exception as e:
        print(f"FAIL — Could not connect to IB Gateway at {args.host}:{args.port}: {e}")
        sys.exit(1)

    try:
        # Account summary
        summary = ib.accountSummary()
        nlv = cash = 0.0
        for item in summary:
            if item.tag == "NetLiquidation":
                nlv = float(item.value)
            elif item.tag == "TotalCashValue":
                cash = float(item.value)

        print(f"OK — NetLiquidation: {nlv:.2f}  Cash: {cash:.2f}")

        if args.require_account:
            accounts = [item.account for item in summary]
            if args.require_account not in accounts:
                print(f"FAIL — Required account {args.require_account} not found (got {set(accounts)})")
                sys.exit(1)

        # Positions
        positions = ib.positions()
        if positions:
            print(f"Positions ({len(positions)}):")
            for pos in positions:
                print(f"  {pos.contract.symbol}: {pos.position} @ avg {pos.avgCost:.2f}")
        else:
            print("No open positions.")

    finally:
        ib.disconnect()

    print("IB Gateway check PASSED.")


if __name__ == "__main__":
    main()
