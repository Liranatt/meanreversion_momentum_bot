from ib_insync import IB
import sys

def main():
    # קביעת מזהה הלקוח
    client_id = int(sys.argv[1]) if len(sys.argv) > 1 else 5467
    
    ib = IB()
    
    try:
        # התחברות ל-IB Gateway (פורט 4002 הוא ברירת המחדל ל-Paper Trading ב-Gateway)
        ib.connect('127.0.0.1', 4002, clientId=client_id)
        
        # 1. שליפת נתוני החשבון (Net Liquidation & Available Funds)
        print("--- Account Summary ---")
        account_summary = ib.accountSummary()
        for item in account_summary:
            if item.tag in ['NetLiquidation', 'AvailableFunds']:
                print(f"{item.tag}: {item.value} {item.currency}")
        
        # 2. שליפת פוזיציות פתוחות
        print("\n--- Open Positions ---")
        positions = ib.positions()
        if not positions:
            print("No open positions.")
        else:
            for pos in positions:
                # pos.contract.symbol - שם המניה/נכס
                # pos.position - כמות
                # pos.avgCost - מחיר ממוצע
                print(f" {pos.contract.symbol}: {pos.position} @ avg {pos.avgCost:.2f}")

    except Exception as e:
        print(f"Connection error: {e}")
        
    finally:
        # ניתוק מסודר בסיום
        if ib.isConnected():
            ib.disconnect()

if __name__ == "__main__":
    main()