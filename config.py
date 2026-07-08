"""Central configuration — gold-only (XAUUSD) ICT/SMC engine.

Rebuilt from a 9-asset, 5-competing-engine system (scalp_engine + swing_engine
+ council + forecast_engine + btc_deep_pipeline, each with their own scoring
method) down to ONE deterministic engine (gold_engine.py) focused entirely on
XAUUSD, because running five different methods in parallel on nine assets was
producing exactly the kind of cross-engine disagreement (and debate-gated
NO_TRADE deadlock) that made signals rare and inconsistent."""

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

# ── Indicators ────────────────────────────────────────────────────────────────
EMA_FAST = 20; EMA_MID = 50; EMA_SLOW = 200
ATR_PERIOD = RSI_PERIOD = 14
CHART_BARS = 150

# ── COT ───────────────────────────────────────────────────────────────────────
COT_EXTREME_LONG  = 75
COT_EXTREME_SHORT = 25
COT_LOOKBACK      = 25

# ── News agent (red-folder USD pre/post alerts) ───────────────────────────────
NEWS_PRE_ALERT_MIN    = 15   # send the "coming up" alert this many minutes before release
NEWS_PRE_ALERT_WINDOW = 6    # tolerance window (minutes) around that mark, matched to the 5-min poll cadence
NEWS_WATCH_CURRENCIES = ["USD"]   # extend later, e.g. ["USD", "EUR"]

# ── Tracer / live position updater ────────────────────────────────────────────
TRACER_MILESTONES = [0.5, 0.75, 1.0]   # fraction of the way to TP1/SL that triggers a Telegram nudge

# ── Markets ───────────────────────────────────────────────────────────────────
# Gold only. dollar_bias() in data_feeds.py still reads a raw EUR/USD quote
# for USD-direction context — that's a background input, not a second traded
# market, so it doesn't need its own MARKETS entry.
MARKETS = {
    "XAUUSD": {
        "td": "XAU/USD", "yf": "GC=F", "iw_path": "gold",
        "cot_name": "GOLD - COMMODITY EXCHANGE INC.",
        "asset_class": "commodity", "pip_digits": 2, "pip_usd": None,
        "sessions_utc": [(7, 21)], "rsi_os": 30, "rsi_ob": 70,
        "decimals": 2, "emoji": "🥇",
    },
}

# ── Gold engine — ICT/SMC concepts ────────────────────────────────────────────
# One deterministic engine, not a multi-agent debate: confluence of these
# factors produces a confidence score directly, so there's no "agents
# disagree -> NO_TRADE" deadlock possible.
GOLD_SESSIONS_UTC = [
    (7, 10, "London Killzone"),
    (12, 16, "NY Killzone + Overlap"),
]   # highest-liquidity windows — outside these, the scalp scan doesn't run.
    # Adjust if you trade a different session focus.

GOLD_JUDAS_WINDOW_MIN   = 60     # first N minutes of a session — actively watched
                                  # for a sweep-then-reverse (the "Judas Swing"),
                                  # not filtered out, since that reversal IS the setup
GOLD_IMPULSE_ATR_MULT   = 1.5    # a move counts as "impulsive" (order-block-forming)
                                  # if its range exceeds this many x current ATR
GOLD_SWEEP_LOOKBACK     = 20     # bars searched for the swing high/low being swept
GOLD_STRUCTURE_LOOKBACK = 40     # bars used for H4/H1 higher-high/lower-low structure

GOLD_ATR_STOP_BUFFER    = 0.5    # stop = swept structural level +/- this x ATR
GOLD_TP1_RR             = 2.0    # target 1, expressed as risk:reward multiple
GOLD_TP2_RR             = 3.0    # target 2
GOLD_TP3_RR             = 4.0    # target 3 — runner, for trends that keep extending

GOLD_MIN_CONFIDENCE     = 0.55   # minimum confluence score to fire a signal
GOLD_SCALP_COOLDOWN_MIN = 30     # don't re-fire a scalp signal within this many minutes
GOLD_SWING_COOLDOWN_H   = 6      # don't re-fire a swing signal within this many hours

GOLD_DAILY_LOSS_LIMIT_PCT = 0.03   # stop trading for the rest of the day after
                                    # losing this % of account equity
GOLD_MAX_TRADES_PER_DAY   = 3      # hard cap on fired signals/day regardless of setups
