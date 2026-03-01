# Mean-Reversion / Momentum Trading Bot

Automated NASDAQ-100 trading bot using a combined mean-reversion + momentum strategy.  
Runs daily via GitHub Actions: generates signals after market close, executes orders at market open via Interactive Brokers Gateway.

Live dashboard: [liranattar.dev/algotrading](https://liranattar.dev/algotrading)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     GitHub Actions (Cron)                       │
│                                                                 │
│  9:35 AM IST ──► daily_sync.py                                  │
│     • Expire stale signals                                      │
│     • Connect IB → reconcile positions (IB = source of truth)   │
│     • Download Yahoo Finance OHLCV → save to DB                 │
│     • Update current prices for all held positions              │
│     • Generate BUY / SELL signals → save as PENDING              │
│     • Compute metrics (Sharpe, drawdown, win rate)              │
│     • Save account snapshot                                      │
│                                                                 │
│  4:30 PM IST ──► execute_signals.py                              │
│     • Read PENDING signals from DB                               │
│     • Connect IB → place bracket (BUY) / market (SELL) orders    │
│     • Wait for fills → log trades → update positions             │
│     • Mark signals EXECUTED                                      │
└────────────────────────────┬────────────────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              │    Heroku Postgres (DB)       │
              │    Schema: algo_trading       │
              └──────────────┬──────────────┘
                             │
              ┌──────────────┴──────────────┐
              │ Heroku App: algotrading      │
              │ FastAPI REST API             │
              │ → liranattar.dev dashboard   │
              └─────────────────────────────┘
```

## Daily Flow

| Step | Time (Israel) | Script | What happens |
|------|--------------|--------|-------------|
| 1 | 9:35 AM | `daily_sync.py` | After US close: reconcile, download data, generate signals |
| 2 | 4:30 PM | `execute_signals.py` | US market open: execute pending signals via IB |

## Project Structure

```
meanreversion_momentum_bot/
├── api/                        # FastAPI REST API (Heroku web dyno)
│   ├── main.py                 #   Routes: /health, /positions, /account, /signals, etc.
│   └── db.py                   #   Async DB pool (asyncpg)
│
├── scripts/                    # One-time / utility scripts
│   ├── apply_schema.py         #   Apply v1 DB schema
│   ├── apply_schema_v2.py      #   Apply v2 DB schema
│   ├── backfill_market_data.py #   Backfill OHLCV from Yahoo Finance
│   ├── seed_initial_state.py   #   Seed positions & account snapshot
│   ├── reset_portfolio.py      #   Reset portfolio state in DB
│   └── test_ib.py              #   Test IB Gateway connectivity
│
├── sql/                        # Database schema definitions
│   ├── schema_v1.sql           #   Original tables (market_data, portfolio_state, trades_log, metrics)
│   └── schema_v2.sql           #   V2 tables (positions, account_snapshot, pending_signals, reconciliation_log)
│
├── .github/workflows/
│   └── daily_trading.yml       # GitHub Actions: cron schedule + manual dispatch
│
├── config.py                   # Centralized config (tickers, strategy params, IB settings)
├── daily_sync.py               # Entry point: end-of-day pipeline
├── execute_signals.py          # Entry point: market-open order execution
├── db_manager.py               # All DB write operations (psycopg2)
├── db_maintenance.py           # Prune old market data rows
├── ib_connection.py            # IB Gateway wrapper (ibapi)
├── strategy_mean_momentum.py   # Signal generation strategy
├── requirements.txt            # Python dependencies
├── Procfile                    # Heroku process definition
└── .gitignore
```

## Strategy

**Combined mean-reversion + momentum** on NASDAQ-100 stocks:

- **Indicators:** SMA(30), Bollinger Bands(30, 2σ), RSI(14), ATR(14), MACD(24/52/18)
- **Bull market BUY:** High ATR volatility + strong/medium MACD momentum
- **Bear market BUY:** Price below lower Bollinger Band + RSI < 40
- **SELL triggers:** Stop-loss (10%), momentum fading, mean-reversion target hit, or time stop (≥20 days)
- **Position sizing:** 10% of cash per signal, minimum $5,000 per trade
- **Order types:** Bracket orders for buys (stop-loss + take-profit), market orders for sells

## Database Schema

All tables live in the `algo_trading` schema (isolated from other apps sharing the same Postgres instance).

| Table | Purpose |
|-------|---------|
| `market_data` | Daily OHLCV for 100 NASDAQ tickers |
| `portfolio_state` | Historical equity snapshots |
| `trades_log` | Every executed trade |
| `metrics` | Daily Sharpe, drawdown, return, win rate |
| `positions` | Current held positions with live P&L |
| `account_snapshot` | Daily account summary (NLV, cash, positions value) |
| `pending_signals` | Signals waiting for execution |
| `reconciliation_log` | IB vs DB position comparison |

## API Endpoints

Base URL: `https://algotrading-d240dd1ebd61.herokuapp.com/api/v1`

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /positions/current` | All held positions with unrealized P&L |
| `GET /account/current` | Latest account snapshot |
| `GET /account/history` | Account equity curve |
| `GET /signals/pending` | Signals awaiting execution |
| `GET /signals/history` | All signal history |
| `GET /trades/recent` | Recent trade executions |
| `GET /metrics/current` | Latest Sharpe, drawdown, etc. |
| `GET /portfolio/history` | Portfolio equity curve |
| `GET /market/summary` | Market data statistics |
| `GET /market/latest?ticker=AAPL` | Latest OHLCV for a ticker |
| `GET /reconciliation/latest` | Latest IB vs DB reconciliation |

## Setup

### Environment Variables

| Variable | Where | Description |
|----------|-------|-------------|
| `DATABASE_URL` | Heroku + GitHub Secrets | Postgres connection string |
| `IB_USER` | GitHub Secrets | Interactive Brokers username |
| `IB_PASSWORD` | GitHub Secrets | Interactive Brokers password |
| `IB_ACCOUNT_ID` | GitHub Secrets | IB paper account ID (e.g. `DUN505877`) |

### GitHub Secrets Required

Set these in the repo's **Settings → Secrets and variables → Actions**:

- `DATABASE_URL` — Postgres connection string
- `IB_USER` — IB Gateway login username
- `IB_PASSWORD` — IB Gateway login password
- `IB_ACCOUNT_ID` — IB account identifier

### Heroku Config Vars

The `algotrading` Heroku app needs:

```bash
heroku config:set DATABASE_URL="postgresql://..." -a algotrading
```

### Local Development

```bash
# Set environment
$env:DATABASE_URL = "postgresql://..."

# Activate venv
& .\venv\Scripts\Activate.ps1

# Run API locally
uvicorn api.main:app --reload --port 8000

# Run daily sync (skip IB)
python daily_sync.py --skip-ib

# Run signal execution (dry run)
python execute_signals.py --dry-run
```

### Utility Scripts

```bash
# Backfill market data
python scripts/backfill_market_data.py --period 1y

# Seed initial portfolio state from IB
python scripts/seed_initial_state.py --skip-ib

# Test IB Gateway connectivity
python scripts/test_ib.py

# Apply database schema
python scripts/apply_schema.py
python scripts/apply_schema_v2.py
```

## Tech Stack

- **Python 3.10** — Core language
- **FastAPI + Uvicorn + Gunicorn** — REST API
- **Interactive Brokers (ibapi)** — Order execution
- **TA-Lib** — Technical indicators (SMA, Bollinger, RSI, ATR, MACD)
- **yfinance** — Market data download
- **PostgreSQL (asyncpg / psycopg2)** — Database
- **Heroku** — API hosting
- **GitHub Actions** — Scheduled automation
