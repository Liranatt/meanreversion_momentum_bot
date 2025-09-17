from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.client import MarketDataTypeEnum
from ibapi.ticktype import TickTypeEnum
import threading
import time

class IBapi(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)

    def error(self, reqId, errorCode, errorString, additionalInfo=''):
        # Standard error callback, prints all errors
        print(f"Error: reqId={reqId}, errorCode={errorCode}, errorString='{errorString}', info='{additionalInfo}'")

    def tickPrice(self, reqId, tickType, price, attrib):
        # Callback for live streaming data from reqMktData
        if reqId == 1: # Ensure it's for our live data request
            if tickType == TickTypeEnum.BID:
                print(f"LIVE TICK -> Bid Price: {price}")
            elif tickType == TickTypeEnum.ASK:
                print(f"LIVE TICK -> Ask Price: {price}")
            elif tickType == TickTypeEnum.LAST:
                print(f"LIVE TICK -> Last Price: {price}")

    def historicalData(self, reqId, bar):
        # Callback for historical data from reqHistoricalData
        if reqId == 2: # Ensure it's for our historical data request
            print(f"\nHISTORICAL DATA -> Date: {bar.date}, Close Price: {bar.close}\n")

def run_loop():
    app.run()

# --- Main Execution ---
app = IBapi()
# Connect to TWS or Gateway. clientId must be unique.
# TWS default port is 7496 for live account, 7497 for paper.
# Gateway default port is 4001 for live, 4002 for paper.
app.connect('127.0.0.1', 7496, clientId=123)

# Start the socket in a separate thread to avoid blocking
api_thread = threading.Thread(target=run_loop, daemon=True)
api_thread.start()

# Allow 1-2 seconds for connection to establish
time.sleep(2)

# Create a contract object for Apple (AAPL)
apple_contract = Contract()
apple_contract.symbol = 'AAPL'
apple_contract.secType = 'STK'
apple_contract.exchange = 'SMART'
apple_contract.currency = 'USD'

# --- 1. Request Live Market Data (Corrected) ---

# First, set the market data type to DELAYED.
# 1 = LIVE, 2 = FROZEN, 3 = DELAYED, 4 = DELAYED_FROZEN
print("--- Setting market data type to Delayed (3)... ---")
# Second, request the market data stream.
# This is the call that was missing. snapshot=False gets a live stream.
print("--- Requesting live market data stream (reqId=1)... ---")
app.reqMktData(reqId=1, contract=apple_contract, genericTickList='',
               snapshot=False, regulatorySnapshot=False, mktDataOptions=[])


# --- 2. Request Historical Data ---
print("--- Requesting last day's historical closing price (reqId=2)... ---")
# Request the most recent daily bar.
app.reqHistoricalData(reqId=2, contract=apple_contract, endDateTime="",
                      durationStr="1 D", barSizeSetting="1 day",
                      whatToShow="TRADES", useRTH=1, formatDate=1, keepUpToDate=False,
                      chartOptions=[])

# Wait for 15 seconds to allow data to be received
print("\n--- Waiting 15 seconds for data... ---")
time.sleep(15)

# Disconnect
print("--- Disconnecting. ---")
app.disconnect()