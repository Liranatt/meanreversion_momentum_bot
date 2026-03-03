"""IB Gateway connectivity test using ib_insync.

Connects to IB Gateway/TWS, prints account summary and positions, then exits.
Does NOT place any orders.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ib_connection import IBConnection
import config


def main():
    print(f"Connecting to IB at {config.IB_HOST}:{config.IB_PORT} (client {config.IB_CLIENT_ID})")
    conn = IBConnection()
    conn.connect_to_ib()

    summary = conn.get_account_summary()
    print(f"\n--- Account Summary ---")
    print(f"  NetLiquidation: {summary['nlv']:.2f}")
    print(f"  TotalCashValue: {summary['cash']:.2f}")

    positions = conn.get_positions()
    print(f"\n--- Open Positions ({len(positions)}) ---")
    for sym, data in sorted(positions.items()):
        print(f"  {sym}: {data['quantity']} @ avg {data['average_cost']:.2f}")

    conn.disconnect()
    print("\nDone.")


if __name__ == "__main__":
    main()
