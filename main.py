from __future__ import annotations

import os
from datetime import date
from typing import Dict

import numpy as np

from db_manager import DBManager
from ib_connection import IBConnection
from strategy import MeanReversionMomentumStrategy

DEFAULT_TICKERS = [
    "MSFT", "AAPL", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "AVGO",
    "TSLA", "COST", "AMD", "PEP", "ADBE", "NFLX", "QCOM", "LIN",
    "INTC", "AMAT", "CMCSA", "INTU", "TXN", "AMGN", "CSCO", "LRCX",
    "HON", "BKNG", "ADP", "SBUX", "ISRG", "VRTX",
]


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _calculate_position_size(available_cash: float, risk_per_trade: float, price: float) -> int:
    if price <= 0:
        return 0
    capital = max(available_cash, 0) * risk_per_trade
    return max(int(capital // price), 0)


def _calculate_metrics(equity_history: list[float]) -> tuple[float, float]:
    if len(equity_history) < 2:
        return 0.0, 0.0

    equity = np.array(equity_history, dtype=float)
    daily_returns = np.diff(equity) / equity[:-1]

    if daily_returns.std(ddof=1) == 0:
        sharpe = 0.0
    else:
        sharpe = float((daily_returns.mean() / daily_returns.std(ddof=1)) * np.sqrt(252))

    running_max = np.maximum.accumulate(equity)
    drawdowns = (equity / running_max) - 1.0
    max_drawdown = float(drawdowns.min())
    return sharpe, max_drawdown


def run() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    tickers_env = os.getenv("TICKERS")
    tickers = [item.strip().upper() for item in tickers_env.split(",")] if tickers_env else DEFAULT_TICKERS

    benchmark = os.getenv("BENCHMARK_TICKER", "^NDX")
    risk_per_trade = float(os.getenv("RISK_PER_TRADE", "0.10"))
    stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "0.03"))
    take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT", "0.06"))
    use_margin = _parse_bool(os.getenv("USE_MARGIN", "true"), default=True)

    strategy = MeanReversionMomentumStrategy(tickers=tickers, benchmark=benchmark)
    market_data = strategy.download_daily_data(lookback_days=450)
    if not market_data:
        raise RuntimeError("No market data downloaded from Yahoo Finance")

    ib = IBConnection(
        host=os.getenv("IB_HOST", "127.0.0.1"),
        port=int(os.getenv("IB_PORT", "4002")),
        client_id=int(os.getenv("IB_CLIENT_ID", "1")),
        account_code=os.getenv("IB_ACCOUNT"),
        order_wait_seconds=int(os.getenv("IB_ORDER_WAIT_SECONDS", "2")),
    )

    db = DBManager(database_url=database_url, sslmode=os.getenv("DB_SSLMODE", "require"))

    trade_records = []
    final_account_snapshot: Dict[str, float] = {}
    try:
        ib.connect()
        account_snapshot, positions = ib.reconcile_account_and_positions()

        available_cash = account_snapshot.get("AvailableFunds", account_snapshot.get("TotalCashValue", 0.0))
        signals = strategy.generate_signals(market_data=market_data, current_positions=positions)

        for symbol, signal in signals.items():
            current_quantity = int(abs(positions.get(symbol, 0)))
            if signal.action == "BUY" and current_quantity == 0:
                quantity = _calculate_position_size(available_cash, risk_per_trade, signal.price)
                if quantity <= 0:
                    continue

                if use_margin:
                    entry_price = round(signal.price, 2)
                    stop_price = round(entry_price * (1 - stop_loss_pct), 2)
                    take_profit_price = round(entry_price * (1 + take_profit_pct), 2)
                    trade_records.extend(
                        ib.place_bracket_order(
                            symbol=symbol,
                            quantity=quantity,
                            entry_price=entry_price,
                            take_profit_price=take_profit_price,
                            stop_loss_price=stop_price,
                        )
                    )
                else:
                    trade_records.append(ib.place_market_order(symbol=symbol, action="BUY", quantity=quantity))

                available_cash -= quantity * signal.price

            elif signal.action == "SELL" and current_quantity > 0:
                trade_records.append(ib.place_market_order(symbol=symbol, action="SELL", quantity=current_quantity))

        final_account_snapshot, _ = ib.reconcile_account_and_positions()
    finally:
        ib.disconnect()

    free_cash = final_account_snapshot.get("AvailableFunds", final_account_snapshot.get("TotalCashValue", 0.0))
    total_equity = final_account_snapshot.get("NetLiquidation", 0.0)

    as_of_date = date.today()
    db.insert_market_data(market_data)
    db.insert_portfolio_state(as_of_date=as_of_date, free_cash=free_cash, total_equity=total_equity)
    db.insert_trades(trade_records)

    equity_history = db.get_equity_history()
    sharpe_ratio, max_drawdown = _calculate_metrics(equity_history)
    db.insert_metrics(as_of_date=as_of_date, sharpe_ratio=sharpe_ratio, max_drawdown=max_drawdown)

    print(f"Saved daily run for {as_of_date} with {len(trade_records)} trade log rows")


if __name__ == "__main__":
    run()
