# testing.py (Corrected to request all data)

import time
from queue import Queue, Empty
from connection import Connection


def run_full_test():

    print("--- Starting Full Suite Connection Test ---")

    event_queue = Queue()
    conn = Connection(event_queue)

    conn.Connect_to_IB()
    if not conn.isConnected():
        print(" FAILED TO CONNECT. Please ensure TWS/Gateway is running.")
        return

    print("\n--- Requesting All Data Streams ---")
    conn.request_account_summary()
    conn.request_positions()

    conn.request_market_data("AAPL")

    print("\n--- Waiting for initial data... ---")
    time.sleep(3)

    print("\n--- Placing a LIVE PAPER TRADE for 1 Share of AAPL ---")
    aapl_contract = conn.create_contract('AAPL')
    buy_order = conn.create_order("BUY", 1, orderType="MKT")
    conn.place_new_order(aapl_contract, buy_order)

    print("\n--- Listening for all events for 25 seconds... ---")
    start_time = time.time()
    while time.time() - start_time < 10:
        try:
            event = event_queue.get_nowait()
            print(f"✅ Event Received: {event}")
        except Empty:
            time.sleep(0.1)

    print("\n--- Test Finished. Disconnecting. ---")
    conn.disconnect()


if __name__ == "__main__":
    run_full_test()