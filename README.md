# Trading Bot — Unified System

One merged trading system, run as a **single sequential pipeline** — the way
a desk runs a morning process: read positioning, read macro, form a bias,
look for setups top-down, debate it, run the deep per-asset engine, then
audit the whole run.

**This system only sends alerts (Telegram + dashboard). It does not place
real trades or connect to a broker.** You decide whether to act on a signal.

## The pipeline (`python main.py`, no flags)

```
COT  →  Macro (Gemini)  →  Forecast  →  Swing  →  Scalp  →  Council  →  Deep Pipeline (9 assets)  →  Performance
```

Each step hands context to the next and they run as **one job**, not
independent parallel schedules — the old model had `scalp`/`swing`/
`council`/`forecast`/`btc_deep`/`performance` each on their own cron, which
worked but meant no step actually knew what the previous one just found.
This is that same logic, reorganized into a chain:

| # | Step | What it does |
|---|---|---|
| 1 | `cot_agent.py` | Official CFTC positioning per market (insider-week.com as fallback if CFTC has no data for an asset that week). Cached 180 min so later steps reading COT don't re-fetch. |
| 2 | `macro_agent.py` | Gemini (with Google Search grounding) gathers a live factual brief — central bank stances, DXY/yields, risk sentiment, the next 14 days of high-impact US calendar events. Ollama then turns that into a short synthesis. Optional — skips cleanly if `GEMINI_API_KEY` isn't set. |
| 3 | `forecast_engine.py` | Daily/weekly bias + BS_OB_RJB_FVG pattern detection + forward-path chart. Already COT-informed (reads step 1's cached data). |
| 4 | `swing_engine.py` | 1h/4h LLM swing plan + COT contrarian gate — runs *before* scalp so the shorter-timeframe step has a bias to work inside of, not against. |
| 5 | `scalp_engine.py` | 15m dual REV/CON scoring, entries inside/against the swing bias. |
| 6 | `council.py` | 7-agent LLM debate (Trend/PriceAction/Institutional/Quant/SMC/Tracer/Performance) → Chair verdict, on whichever assets/mode you configure. |
| 7 | `deep_pipeline.py` | TA + COT + Sentiment + Synthesis + 4-layer risk engine (vol-targeted sizing, CVaR Monte Carlo, drawdown throttle, regime filter) — **now across all 9 configured assets**, not just BTC (see below). Extra focus on **Sniper** setups. |
| 8 | `performance_tracker.py` | Resolves open signals (TP1/TP2/STOP/EXPIRED), posts a scorecard, refreshes dashboard stats. Audits everything the chain just fired. |

Runs **every 2 hours during sessions, 07-19 UTC weekdays** (`pipeline.yml`).
Every step is still individually runnable for standalone testing:

```bash
python main.py --layer cot
python main.py --layer macro
python main.py --layer forecast
python main.py --layer swing
python main.py --layer scalp
python main.py --layer council --council-mode swing
python main.py --layer deep_pipeline
python main.py --layer performance
```

### Deep Pipeline, generalized to 9 assets

This started as `btc_deep_pipeline.py` — a BTC-only engine (Wyckoff spring,
MM absorption, stealth accumulation, derivatives trap, sniper score, 4-layer
risk engine) ported from the old `ai-trading-bot` repo. `deep_pipeline.py`
generalizes it to run the same TA/Sentiment/Synthesis/Risk chain across all
9 configured markets:

- OHLCV comes from `data_feeds.fetch_intraday()` (TwelveData or OKX) instead
  of the BTC-hardcoded fetch.
- The COT vote is built directly from step 1's own data (no extra LLM call).
- Funding-rate / derivatives sentiment is a crypto-perpetual concept with no
  FX/index/gold equivalent, and the original funding fetch + sentiment
  prompt are both hardcoded to BTC-USDT specifically. So **only BTCUSD**
  gets the real funding fetch + real LLM sentiment call; every other asset
  (ETHUSD included) gets a neutral placeholder, keeping the funding-based
  detectors correctly inert rather than fabricating a reading that doesn't
  exist.
- Risk state (equity / drawdown / daily loss) is tracked **per asset**
  (`data/risk_state_{asset}.json`), not shared — a loss on one asset
  shouldn't trip another asset's circuit breaker.

The old BTC-only version is kept as `--layer btc_deep` / `btc_deep.yml`
(manual only) purely for side-by-side comparison.

### Extra focus on Sniper setups

`SNIPER` (multi-agent confluence, high-probability entry) gets distinct
Telegram formatting — a louder header and an explicit "why this is
higher-conviction than the rest" line — plus a gold-highlighted card and its
own filter tab on the dashboard. All 19 signal labels from the taxonomy
(APEX_PICK, SNIPER, WYCKOFF_SPRING, MM_ABSORPTION, SILENT_INSTITUTIONAL,
LIQUIDITY_ABSORPTION, STEALTH_ACCUM, STRUCTURAL_COMPRESSION, ACTIVE_ACCUM,
DERIVATIVES_TRAP, BREAKOUT, BREAKDOWN, BULL_DIVERGENCE, BEAR_DIVERGENCE,
HIGH_PROBABILITY, SWING_LONG, SWING_SHORT, SCALP_LONG, SCALP_SHORT) still
get their own emoji/name/description, Sniper just gets louder treatment on
top of that.

### COT sourcing

Official CFTC Socrata API is primary (`publicreporting.cftc.gov`, per
"always official website first"). insider-week.com is kept as an explicit
fallback for the 6 assets it covers (EURUSD, GBPUSD, USDJPY, DXY, XAUUSD,
BTCUSD) using the corrected URL pattern (`/en/cot/{path}/?all_data=ok` —
their site restructured URLs since this was first built; the old
`/en/commitment-of-traders/{path}/` pattern 404s everywhere now).

## Standalone agents (outside the chain, own schedules)

These don't belong on a 2-hourly cycle — they're either faster-cadence
always-on checks or once-a-day/week jobs:

- **`news_agent.py`** — watches the ForexFactory high-impact ("red folder")
  calendar for USD events. ~15 minutes before a watched release it sends the
  previous reading + forecast/consensus. Once the actual value posts, a
  second message compares previous/forecast/actual and gives a beat/miss
  read on typical USD bias (and the historical XAU/indices tendency this
  implies) — noting that's a tendency, not a rule. Every 5 min, 06-22 UTC
  weekdays.
- **`tracer_agent.py`** — for every open position, checks live progress
  toward TP1 vs SL and sends a nudge the first time it crosses 50%/75%/100%.
  Every 15 min, 07-20 UTC weekdays.
- **`daily_brief.py`** — Arabic-language executive daily brief, computed
  natively from this system's own data (weekly/daily bias, COT signal,
  recent signal direction combine into a -4..+4 score per asset). Daily
  06:15 UTC weekdays.
- **`cot_weekly.py`** (via `--layer cot_weekly`) — standalone COT positioning
  map, once a week (Friday 19:45 UTC), independent of step 1's per-run cache.
- **`backtest.py`** — walk-forward simulation using the live scoring
  functions. Manual dispatch only.

## Dashboard

`docs/index.html` is a self-contained dashboard: Arabic daily brief, a Macro
Context card (Gemini synthesis), a **COT — Institutional Positioning**
section (current snapshot table + per-asset weekly history, ~90 days back),
forecasts (with the forward-path chart), latest trade setups — filterable by
layer, with a dedicated **Deep Pipeline** tab and a **🎯 Sniper only** tab
(Sniper cards get a gold highlight) — performance stats, and open positions.
It reads `docs/dashboard.json`, which every layer updates automatically
(`dashboard_export.py`) whenever it fires a signal, forecast, macro read, COT
snapshot, or resolves trades.

**Hosted on GitHub Pages** (repo is public, per your choice — no hardcoded
secrets exist anywhere in this codebase, so nothing sensitive is exposed by
that, but your strategy logic and signal history are visible to anyone with
the link). Telegram messages link straight to it — the COT message links to
`{DASHBOARD_URL}/#cot` specifically. One-time setup on GitHub:
1. Settings → General → Danger Zone → Change repository visibility → Public.
2. Settings → Pages → Source: "Deploy from a branch" → Branch: `main`,
   folder: `/docs` → Save.
3. Give it a couple minutes; it'll be live at
   `https://<your-username>.github.io/<repo-name>/`.

`config.py`'s `DASHBOARD_URL` defaults to
`https://ahmadman1991.github.io/trading-bot` — override it with a
`DASHBOARD_URL` env var (or repo variable in Actions) if you host it
somewhere else (Cloudflare Pages, Vercel, a custom domain, etc.) instead.

**To view it locally instead** (e.g. before Pages is set up, or for a
private-only setup): run `python3 serve_dashboard.py` from inside
`Trading_Bot_COT` — opens `http://127.0.0.1:8765`, auto-refreshes every 60s.

## GitHub Actions

One scheduler runs the chain — **`pipeline.yml`**, every 2h, 07-19 UTC
weekdays. The workflows that used to run `scalp`/`swing`/`council`/
`forecast`/`btc_deep`/`performance` on their own crons (`scalp.yml`,
`swing.yml`, `council.yml`, `forecast.yml`, `btc_deep.yml`,
`performance.yml`) are still there but **manual-dispatch only** now — useful
for testing a single step from the Actions tab, but they won't fire on a
schedule of their own anymore. Running two schedulers against the same
signals is exactly the "duplicate signals" trap this project has hit before,
so there's deliberately only one cron touching the repo for the chain.

`news_agent.yml`, `tracer_agent.yml`, `daily_brief.yml`, `cot_weekly.yml`
keep their own independent schedules (see table above) — they're meant to,
per the design.

## Setup

```bash
pip install -r requirements.txt
```

Set these as GitHub repo secrets (Settings → Secrets and variables →
Actions):

| Secret | Used for |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Channel/chat to post signals to |
| `TWELVEDATA_KEY` | TwelveData market data |
| `OLLAMA_API_KEY` | Ollama Cloud (reasoning/synthesis calls, used throughout) |
| `GEMINI_API_KEY` | Gemini live macro data gathering (step 2). Optional — the pipeline skips step 2 cleanly if unset, everything else still runs. |

For local runs, export the same variables in your shell instead.

## Markets covered

EURUSD, GBPUSD, USDJPY, DXY (macro-only), XAUUSD, SPX500, US100, BTCUSD,
ETHUSD — see `config.py` → `MARKETS` for per-asset tuning (RSI bands, ATR
multiples, sessions, correlations).

## Safety notes

- This is a **signal-only** system — it does not execute trades. You still
  place every order yourself.
- Do not run this GitHub Actions setup alongside any other local/cron copy
  of the same bot — running two schedulers against the same Telegram
  channel produces duplicate signals (this bit the project before, and is
  exactly why the workflow consolidation above exists).
- Backtest results are informative, not a guarantee — always sanity check
  a strategy change against the score-bucket report before trusting it live.
