#!/usr/bin/env python3
"""
check_ib_connection.py — CI-friendly IB Gateway connectivity check.

Exit codes:
  0  — Connected successfully, nextValidId received
  1  — Connection failed or timed out

Usage:
  python scripts/check_ib_connection.py              # defaults: 127.0.0.1:4002 client 99
  python scripts/check_ib_connection.py --host 10.0.0.5 --port 4002 --timeout 20
"""

import argparse
import sys
import threading
import logging
from queue import Queue

from ibapi.client import EClient
from ibapi.wrapper import EWrapper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


class IBProbe(EWrapper, EClient):
    """Minimal IB wrapper that only waits for nextValidId."""

    def __init__(self):
        EClient.__init__(self, self)
        self._connected = threading.Event()
        self._order_id = None

    def nextValidId(self, orderId: int):
        self._order_id = orderId
        self._connected.set()
        logger.info("✓ nextValidId received: %d — IB Gateway is fully connected", orderId)

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # 2104/2106/2158 are informational (market data farm statuses)
        if errorCode in (2104, 2106, 2158):
            logger.info("IB info %d: %s", errorCode, errorString)
        elif errorCode < 2000:
            logger.error("IB ERROR %d: %s", errorCode, errorString)

    def connectAck(self):
        logger.info("IB connection acknowledged (TCP handshake OK)")

    def wait_for_connection(self, timeout: float) -> bool:
        return self._connected.wait(timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description="Check IB Gateway connectivity")
    parser.add_argument("--host", default="127.0.0.1", help="IB Gateway host")
    parser.add_argument("--port", type=int, default=4002, help="IB Gateway port")
    parser.add_argument("--client-id", type=int, default=99, help="Client ID (use 99 to avoid conflicts)")
    parser.add_argument("--timeout", type=int, default=15, help="Seconds to wait for handshake")
    args = parser.parse_args()

    logger.info("Probing IB Gateway at %s:%d (client %d, timeout %ds)...",
                args.host, args.port, args.client_id, args.timeout)

    probe = IBProbe()
    try:
        probe.connect(args.host, args.port, clientId=args.client_id)
    except Exception as e:
        logger.error("✗ TCP connection failed: %s", e)
        sys.exit(1)

    thread = threading.Thread(target=probe.run, daemon=True)
    thread.start()

    if probe.wait_for_connection(timeout=args.timeout):
        logger.info("✓ IB Gateway is READY (order ID = %d)", probe._order_id)
        try:
            probe.disconnect()
        except Exception:
            pass
        sys.exit(0)
    else:
        logger.error("✗ IB Gateway handshake TIMED OUT after %ds — nextValidId never received", args.timeout)
        try:
            probe.disconnect()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
