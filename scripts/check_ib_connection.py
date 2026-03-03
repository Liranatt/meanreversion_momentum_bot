#!/usr/bin/env python3
"""
check_ib_connection.py — CI/Dev IB Gateway connectivity and account verification.

This script performs a TCP connect to the IB Gateway/TWS and waits for the
`nextValidId` callback (handshake). Optionally it will request managed
accounts and verify that a specific account ID (from `--require-account` or
the environment variable `IB_ACCOUNT_ID`) is present.

Exit codes:
  0 — Success (handshake succeeded, and account verification passed if requested)
  1 — Failure (TCP/connect or handshake/account verification failed)

Usage examples:
  python scripts/check_ib_connection.py
  python scripts/check_ib_connection.py --host 127.0.0.1 --port 4002 --client-id 1 --require-account DUN505877
"""

import os
import argparse
import sys
import threading
import logging

from ibapi.client import EClient
from ibapi.wrapper import EWrapper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


class IBProbe(EWrapper, EClient):
    """IB wrapper that waits for nextValidId and can return managed accounts."""

    def __init__(self):
        EClient.__init__(self, self)
        self._connected = threading.Event()
        self._accounts_event = threading.Event()
        self._order_id = None
        self.managed_accounts = []
        # Data containers for diagnostics
        self.positions = []
        self._positions_event = threading.Event()
        self.account_summary = {}
        self._acct_summary_event = threading.Event()
        self.mkt_data = {}
        self._mkt_event = threading.Event()

    def nextValidId(self, orderId: int):
        self._order_id = orderId
        self._connected.set()
        logger.info("✓ nextValidId received: %d — IB Gateway handshake complete", orderId)

    def managedAccounts(self, accountsList: str):
        # accountsList is a comma-separated string of account IDs
        self.managed_accounts = [a.strip() for a in accountsList.split(',') if a.strip()]
        logger.info("Managed accounts: %s", self.managed_accounts)
        self._accounts_event.set()

    # --- Positions callbacks -------------------------------------------------
    def position(self, account: str, contract, position: float, avgCost: float):
        try:
            symbol = getattr(contract, 'symbol', None)
        except Exception:
            symbol = None
        self.positions.append({
            'account': account,
            'symbol': symbol,
            'position': position,
            'avg_cost': avgCost,
        })

    def positionEnd(self):
        logger.info('Position download complete (%d positions)', len(self.positions))
        self._positions_event.set()

    # --- Account summary callbacks ------------------------------------------
    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str):
        self.account_summary.setdefault(account, {})[tag] = {'value': value, 'currency': currency}

    def accountSummaryEnd(self, reqId: int):
        logger.info('Account summary download complete for req %d', reqId)
        self._acct_summary_event.set()

    # --- Market data callbacks ----------------------------------------------
    def tickPrice(self, reqId: int, tickType: int, price: float, attrib):
        # tickType 4 == LAST, 1 == BID, 2 == ASK
        # store last non-zero price
        if price and price > 0:
            self.mkt_data.setdefault(reqId, {})['last_price'] = price
            self._mkt_event.set()

    def tickSize(self, reqId: int, tickType: int, size: int):
        # ignore sizes for now
        pass

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        # Keep informational codes quiet; surface real errors
        if errorCode in (2104, 2106, 2158):
            logger.debug("IB info %d: %s", errorCode, errorString)
        elif errorCode < 2000:
            logger.error("IB ERROR %d: %s", errorCode, errorString)

    def connectAck(self):
        logger.info("IB TCP connection acknowledged")

    def wait_for_connection(self, timeout: float) -> bool:
        return self._connected.wait(timeout=timeout)

    def wait_for_accounts(self, timeout: float) -> bool:
        return self._accounts_event.wait(timeout=timeout)

    def wait_for_positions(self, timeout: float) -> bool:
        return self._positions_event.wait(timeout=timeout)

    def wait_for_account_summary(self, timeout: float) -> bool:
        return self._acct_summary_event.wait(timeout=timeout)

    def wait_for_mkt(self, timeout: float) -> bool:
        return self._mkt_event.wait(timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description="Check IB Gateway connectivity and optionally verify account")
    parser.add_argument("--host", default=os.environ.get('IB_HOST', "127.0.0.1"), help="IB Gateway host")
    parser.add_argument("--port", type=int, default=int(os.environ.get('IB_PORT', 4002)), help="IB Gateway port")
    parser.add_argument("--client-id", type=int, default=int(os.environ.get('IB_CLIENT_ID', 99)), help="Client ID")
    parser.add_argument("--timeout", type=int, default=15, help="Seconds to wait for handshake/account info")
    parser.add_argument("--require-account", default=None, help="Account ID expected (env IB_ACCOUNT_ID if omitted)")
    args = parser.parse_args()

    required_account = args.require_account or os.environ.get('IB_ACCOUNT_ID')

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

    # Wait for nextValidId (handshake)
    if not probe.wait_for_connection(timeout=args.timeout):
        logger.error("✗ IB handshake timed out after %ds — nextValidId never received", args.timeout)
        try:
            probe.disconnect()
        except Exception:
            pass
        sys.exit(1)

    logger.info("✓ IB Gateway handshake complete (order ID = %d)", probe._order_id)

    # If requested, verify managed accounts include the required account ID
    if required_account:
        logger.info("Requesting managed accounts to verify '%s'...", required_account)
        try:
            probe.reqManagedAccts()
        except Exception as e:
            logger.error("Failed to request managed accounts: %s", e)
            try:
                probe.disconnect()
            except Exception:
                pass
            sys.exit(1)

        if not probe.wait_for_accounts(timeout=args.timeout):
            logger.error("✗ Did not receive managed accounts within %ds", args.timeout)
            try:
                probe.disconnect()
            except Exception:
                pass
            sys.exit(1)

        if required_account in probe.managed_accounts:
            logger.info("✓ Expected account '%s' found: %s", required_account, probe.managed_accounts)
                # Fetch full diagnostic data: positions, account summary, and AAPL price
                # 1) Positions
                probe.positions = []
                probe._positions_event.clear()
                try:
                    probe.reqPositions()
                except Exception as e:
                    logger.error("Failed to request positions: %s", e)
                probe.wait_for_positions(timeout=args.timeout)

                # 2) Account summary (NetLiquidation,TotalCashValue,AvailableFunds)
                probe.account_summary = {}
                probe._acct_summary_event.clear()
                acct_req_id = 9001
                tags = "NetLiquidation,TotalCashValue,AvailableFunds,BuyingPower"
                try:
                    probe.reqAccountSummary(acct_req_id, "All", tags)
                except Exception as e:
                    logger.error("Failed to request account summary: %s", e)
                probe.wait_for_account_summary(timeout=args.timeout)
                try:
                    probe.cancelAccountSummary(acct_req_id)
                except Exception:
                    pass

                # 3) Market data snapshot for AAPL
                from ibapi.contract import Contract
                aapl = Contract()
                aapl.symbol = "AAPL"
                aapl.secType = "STK"
                aapl.exchange = "SMART"
                aapl.currency = "USD"
                mkt_req_id = 7001
                probe.mkt_data = {}
                probe._mkt_event.clear()
                try:
                    probe.reqMktData(mkt_req_id, aapl, "", True, False, [])
                except Exception as e:
                    logger.error("Failed to request market data: %s", e)
                probe.wait_for_mkt(timeout=args.timeout)
                try:
                    probe.cancelMktData(mkt_req_id)
                except Exception:
                    pass

                # Print full data to terminal
                import json
                out = {
                    'managed_accounts': probe.managed_accounts,
                    'positions': probe.positions,
                    'account_summary': probe.account_summary,
                    'market_data': probe.mkt_data.get(mkt_req_id, {}),
                }
                print(json.dumps(out, indent=2, default=str))

                try:
                    probe.disconnect()
                except Exception:
                    pass
                sys.exit(0)
        else:
            logger.error("✗ Expected account '%s' NOT found among managed accounts: %s", required_account, probe.managed_accounts)
            try:
                probe.disconnect()
            except Exception:
                pass
            sys.exit(1)

    # No account verification requested — success
    try:
        probe.disconnect()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
