"""
Centralised configuration for the mean-reversion / momentum trading bot.
All environment-specific values are read from env vars with sensible defaults.
"""
import os

# ── IB Gateway / TWS ─────────────────────────────────────
IB_HOST       = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT       = int(os.getenv("IB_PORT", "4002"))        # 4002 = IB Gateway, 7497 = TWS paper
IB_CLIENT_ID  = int(os.getenv("IB_CLIENT_ID", "1"))
IB_ACCOUNT_ID = os.getenv("IB_ACCOUNT_ID", "DUN505877")  # paper-trading account

# ── Database ──────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Strategy parameters ───────────────────────────────────
TICKERS = [
    # ── NASDAQ-100 constituents (as of early 2026) ──────────
    "AAPL",  "ABNB",  "ADBE",  "ADI",   "ADP",   "ADSK",  "AEP",   "AMAT",
    "AMD",   "AMGN",  "AMZN",  "ANSS",  "ARM",   "ASML",  "AVGO",  "AZN",
    "BIIB",  "BKNG",  "BKR",   "CCEP",  "CDNS",  "CDW",   "CEG",   "CHTR",
    "CMCSA", "COST",  "CPRT",  "CRWD",  "CSCO",  "CSGP",  "CSX",   "CTAS",
    "CTSH",  "DASH",  "DDOG",  "DLTR",  "DXCM",  "EA",    "EXC",   "FANG",
    "FAST",  "FTNT",  "GEHC",  "GFS",   "GILD",  "GOOG",  "GOOGL", "HON",
    "IDXX",  "ILMN",  "INTC",  "INTU",  "ISRG",  "KDP",   "KHC",   "KLAC",
    "LIN",   "LRCX",  "LULU",  "MAR",   "MCHP",  "MDB",   "MDLZ",  "MELI",
    "META",  "MNST",  "MRNA",  "MRVL",  "MSFT",  "MU",    "NFLX",  "NVDA",
    "NXPI",  "ODFL",  "ON",    "ORLY",  "PANW",  "PAYX",  "PCAR",  "PDD",
    "PEP",   "PYPL",  "QCOM",  "REGN",  "ROP",   "ROST",  "SBUX",  "SMCI",
    "SNPS",  "TEAM",  "TMUS",  "TSLA",  "TTD",   "TTWO",  "TXN",   "VRSK",
    "VRTX",  "WBA",   "WBD",   "WDAY",  "XEL",   "ZS",
]

POSITION_SIZE_PCT  = 0.10      # invest 10 % of cash per signal
STOP_LOSS_PCT      = 0.10      # trailing stop-loss distance
TAKE_PROFIT_PCT    = 0.20      # bracket-order take-profit distance
MIN_INVESTMENT     = 5000      # minimum dollar investment per trade
COMMISSION         = 2.50      # estimated commission per leg

# ── Indicator windows (kept identical to the existing strategy) ──
SMA_WINDOW         = 30
BOLLINGER_STD_MULT = 2
RSI_PERIOD         = 14
ATR_PERIOD         = 14
MACD_FAST          = 24
MACD_SLOW          = 52
MACD_SIGNAL        = 18

# ── CORS (for FastAPI) ────────────────────────────────────
ALLOWED_ORIGINS = [
    "https://liranattar.dev",
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:8000",
]
