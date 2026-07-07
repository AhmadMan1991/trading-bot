"""Central configuration — unified across all layers (scalp/swing/council/forecast)."""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()   # loads a local .env file if present — no-op in GitHub Actions
except ImportError:
    pass

# ── Credentials ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_URL     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

TWELVEDATA_KEY   = os.environ.get("TWELVEDATA_KEY", "")
OLLAMA_KEY       = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_URL       = "https://ollama.com/api/chat"
OLLAMA_MODEL     = "gpt-oss:20b-cloud"

GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL     = "gemini-2.5-flash"
GEMINI_URL       = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# GitHub Pages URL for the dashboard (repo made public, Pages serving /docs).
# Override via env var if you host it elsewhere (Cloudflare Pages, Vercel, a
# custom domain, etc.) — Telegram messages link here for "full report" reads.
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://ahmadman1991.github.io/trading-bot")

_missing = [n for n, v in [("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("TWELVEDATA_KEY", TWELVEDATA_KEY)] if not v]
if _missing:
    print(f"  [config] WARNING: missing env secrets: {', '.join(_missing)} "
          f"(set them as repo secrets — see README)")

# ── Account & Risk ────────────────────────────────────────────────────────────
ACCOUNT_SIZE  = float(os.environ.get("ACCOUNT_SIZE") or 1000)
RISK_PCT      = 0.01
ATR_SL_MULT   = 1.5
ATR_TP1_MULT  = 2.5
ATR_TP2_MULT  = 4.0

# ── Indicators ────────────────────────────────────────────────────────────────
EMA_FAST = 20; EMA_MID = 50; EMA_SLOW = 200
ATR_PERIOD = RSI_PERIOD = 14
CHART_BARS = 150

# ── COT ───────────────────────────────────────────────────────────────────────
COT_EXTREME_LONG  = 75
COT_EXTREME_SHORT = 25
COT_LOOKBACK      = 25

# ── Signal thresholds ─────────────────────────────────────────────────────────
SIGNAL_MIN_SCORE       = 6
SCALP_MIN_SCORE        = 7
SCALP_COOLDOWN_HOURS   = 4
NEWS_BUFFER_MIN        = 45
ML_CONF_BAND           = 0.60
COUNCIL_MIN_AGREE      = 4
COUNCIL_COOLDOWN_H     = 3
COUNCIL_SCALP_MIN_CONF = 0.65
COUNCIL_SWING_MIN_CONF = 0.68
COUNCIL_SCALP_MIN_RR   = 1.2
COUNCIL_SWING_MIN_RR   = 1.8

# ── News agent (red-folder USD pre/post alerts) ───────────────────────────────
NEWS_PRE_ALERT_MIN    = 15   # send the "coming up" alert this many minutes before release
NEWS_PRE_ALERT_WINDOW = 6    # tolerance window (minutes) around that mark, matched to the 5-min poll cadence
NEWS_WATCH_CURRENCIES = ["USD"]   # extend later, e.g. ["USD", "EUR"]

# ── Tracer / live position updater ────────────────────────────────────────────
TRACER_MILESTONES = [0.5, 0.75, 1.0]   # fraction of the way to TP1/SL that triggers a Telegram nudge

CORRELATION_GROUPS = [
    {"EURUSD", "GBPUSD"},
    {"SPX500", "US100"},
    {"BTCUSD", "ETHUSD"},
]

# ── Markets ───────────────────────────────────────────────────────────────────
MARKETS = {
    "EURUSD": {
        "td": "EUR/USD", "yf": "EURUSD=X", "iw_path": "euro-fx",
        "cot_name": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
        "asset_class": "forex", "pip_digits": 5, "pip_usd": 0.10,
        "sessions_utc": [(7, 17)], "rsi_os": 35, "rsi_ob": 65,
        "min_score": 6, "atr_sl": 1.0, "atr_tp1": 1.2, "atr_tp2": 2.0,
        "long_bias": 0, "decimals": 5, "emoji": "🇪🇺",
        "correlates": ["GBPUSD", "DXY"],
    },
    "GBPUSD": {
        "td": "GBP/USD", "yf": "GBPUSD=X", "iw_path": "british-pound",
        "cot_name": "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
        "asset_class": "forex", "pip_digits": 5, "pip_usd": 0.10,
        "sessions_utc": [(7, 16)], "rsi_os": 30, "rsi_ob": 70,
        "min_score": 7, "atr_sl": 1.3, "atr_tp1": 1.5, "atr_tp2": 2.4,
        "long_bias": 0, "decimals": 5, "emoji": "🇬🇧",
        "correlates": ["EURUSD"],
    },
    "USDJPY": {
        "td": "USD/JPY", "yf": "JPY=X", "iw_path": "japanese-yen",
        "cot_name": "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",
        "asset_class": "forex", "pip_digits": 3, "pip_usd": 0.10,
        "sessions_utc": [(0, 9), (12, 21)], "rsi_os": 30, "rsi_ob": 70,
        "min_score": 6, "atr_sl": 1.2, "atr_tp1": 1.5, "atr_tp2": 2.5,
        "long_bias": 0, "decimals": 3, "emoji": "🇯🇵",
        "correlates": [],
    },
    "DXY": {
        "td": "UUP", "yf": "DX-Y.NYB", "iw_path": "dollar-index",
        "cot_name": "U.S. DOLLAR INDEX - ICE FUTURES U.S.",
        "asset_class": "index", "pip_digits": 3, "pip_usd": None,
        "scalp_skip": True,   # not directly tradeable; used as macro bias only
        "sessions_utc": [(7, 21)], "rsi_os": 35, "rsi_ob": 65,
        "min_score": 6, "atr_sl": 1.2, "atr_tp1": 1.5, "atr_tp2": 2.5,
        "long_bias": 0, "decimals": 3, "emoji": "💵",
        "correlates": ["EURUSD", "GBPUSD"],
    },
    "XAUUSD": {
        "td": "XAU/USD", "yf": "GC=F", "iw_path": "gold",
        "cot_name": "GOLD - COMMODITY EXCHANGE INC.",
        "asset_class": "commodity", "pip_digits": 2, "pip_usd": None,
        "sessions_utc": [(7, 21)], "rsi_os": 30, "rsi_ob": 70,
        "min_score": 7, "atr_sl": 1.2, "atr_tp1": 1.5, "atr_tp2": 2.5,
        "long_bias": 0, "decimals": 2, "emoji": "🥇",
        "correlates": ["DXY"],
    },
    "SPX500": {
        "td": "SPY", "yf": "^GSPC", "iw_path": "s&p-500",        # SPY proxy for 15m/1h
        "cot_name": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
        "asset_class": "index", "pip_digits": 1, "pip_usd": None,
        "sessions_utc": [(13, 20)], "rsi_os": 40, "rsi_ob": 75,
        "min_score": 6, "atr_sl": 1.0, "atr_tp1": 1.3, "atr_tp2": 2.0,
        "long_bias": 1, "decimals": 1, "emoji": "📈",
        "correlates": ["US100"],
    },
    "US100": {
        "td": "QQQ", "yf": "^NDX", "iw_path": "nasdaq-e-mini",   # QQQ proxy
        "cot_name": "NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE",
        "asset_class": "index", "pip_digits": 1, "pip_usd": None,
        "sessions_utc": [(13, 20)], "rsi_os": 40, "rsi_ob": 75,
        "min_score": 6, "atr_sl": 1.1, "atr_tp1": 1.4, "atr_tp2": 2.2,
        "long_bias": 1, "decimals": 1, "emoji": "💻",
        "correlates": ["SPX500"],
    },
    "BTCUSD": {
        "td": "BTC/USD", "yf": "BTC-USD", "iw_path": "bitcoin",
        "cot_name": None,
        "asset_class": "crypto", "pip_digits": 2, "pip_usd": None,
        "sessions_utc": [(0, 24)], "rsi_os": 30, "rsi_ob": 70,
        "min_score": 7, "atr_sl": 1.3, "atr_tp1": 1.6, "atr_tp2": 2.6,
        "long_bias": 0, "decimals": 2, "emoji": "₿",
        "correlates": ["ETHUSD"],
    },
    "ETHUSD": {
        "td": "ETH/USD", "yf": "ETH-USD", "iw_path": "ethereum",
        "cot_name": "ETHER - CHICAGO MERCANTILE EXCHANGE",
        "asset_class": "crypto", "pip_digits": 2, "pip_usd": None,
        "sessions_utc": [(0, 24)], "rsi_os": 30, "rsi_ob": 70,
        "min_score": 7, "atr_sl": 1.3, "atr_tp1": 1.6, "atr_tp2": 2.6,
        "long_bias": 0, "decimals": 2, "emoji": "💠",
        "correlates": ["BTCUSD"],
    },
}

# Council operates on a subset (BTC + XAU + EUR — deep COT + vol)
COUNCIL_ASSETS = ["BTCUSD", "XAUUSD", "EURUSD"]
