# Trading Bot — Unified System

One merged trading system, built by combining 4 previously-separate repos
(`ai-trading-bot`, `trading-bot`, `forecast_agent.py`, `scalp-council`) into a
single OODA-loop pipeline with charts and a private dashboard.

**This system only sends alerts (Telegram + dashboard). It does not place
real trades or connect to a broker.** You decide whether to act on a signal.

## Layers (`python main.py --layer <name>`)

| Layer | What it does | Schedule |
|---|---|---|
| `forecast` | Daily/weekly bias + BS_OB_RJB_FVG pattern detection + forward-path chart (target zone / invalidation) | daily 06:00 UTC + Sun 20:00 |
| `scalp` | 15m dual REV/CON scoring across 9 markets, with a trade-setup chart (entry/SL/TP overlay) per signal | hourly, 07–20 UTC weekdays |
| `swing` | 1h/4h LLM swing plan + COT contrarian gate | every 3h weekdays |
| `council` | 7-agent LLM debate (Trend/PriceAction/Institutional/Quant/SMC/Tracer/Performance) → Chair verdict | every 2h weekdays |
| `btc_deep` | Standalone deep BTC pipeline (Wyckoff spring, absorption, stealth accumulation, derivatives trap, sniper score, 4-layer risk engine) — ported from the old `ai-trading-bot` repo, kept as its own layer since it's a much deeper/heavier analysis than the other layers | every 3h |
| `performance` | Resolves open signals (TP1/TP2/STOP/EXPIRED), posts a scorecard, refreshes dashboard stats | daily 22:00 UTC |
| `backtest` | Walk-forward simulation using the live scoring functions | manual only |

Run everything once: `python main.py` (runs forecast → scalp → swing → council → performance).

## What changed in this merge

- **One config, no duplicated credentials.** `config.py` is the single source of
  truth; every layer imports from it instead of redefining its own
  `TELEGRAM_TOKEN`/`TWELVEDATA_KEY`/etc.
- **Removed hardcoded credentials.** The old code had real tokens hardcoded as
  fallback values (`os.environ.get(X) or "<real token>"`). Those fallbacks are
  gone — the bot now reads only from environment variables / GitHub secrets,
  and prints a warning if any are missing. **If you haven't already, rotate the
  Telegram/TwelveData/Ollama credentials that were previously exposed and set
  the new ones as repo secrets** (Settings → Secrets and variables → Actions):
  `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `TWELVEDATA_KEY`, `OLLAMA_API_KEY`.
- **Removed dead/orphaned files** from the old `trading-bot` repo
  (`scalping.py`, `signals.py`, `timeframes.py`, `intraday.py`, `swing.py`,
  `forecast.py`, `prices.py`, `cot.py`, `risk.py`) — these were earlier drafts
  that got superseded by `scalp_engine.py` / `swing_engine.py` /
  `forecast_engine.py` but never deleted, and nothing imported them.
- **Removed the duplicate `daily-analysis.yml` workflow** — it ran the same
  `main.py` at the same time as `forecast.yml` under different secret names,
  which is exactly the "don't run two schedulers at once → duplicate signals"
  trap called out in the project notes.
- **Every fired signal now gets a chart, not just text.** `charts.py`
  (previously written but never wired into anything) now renders a
  price+EMA+volume+RSI chart with entry/SL/TP lines for every scalp/swing/
  council signal, sent to Telegram alongside the text message.
- **Forecast charts now show a forward path.** Ported the "dashed line →
  target zone, invalidation line" visual from the old standalone
  `forecast_agent.py` into `forecast_engine.py`'s chart, driven by a new
  deterministic `project_forecast()` (nearest key level in the bias direction
  = target, nearest opposing level = invalidation — no extra LLM call needed).
- **Richer council quant/SMC commentary.** Ported the Fibonacci-confluence
  read-out and clearer regime labels from the standalone `agent_council.py`
  into the shared `council.py`/`indicators.py`.
- **`btc_deep_pipeline.py`** is the old `ai-trading-bot/pipeline.py` kept
  close to its original form (it's a large, specialized, already-working
  BTC analysis engine) — wired in as its own optional layer rather than
  rewritten, to avoid introducing bugs into working logic. A good next step
  if you want to go further: fold its unique detectors (Wyckoff spring,
  absorption, stealth accumulation) into the council's InstitutionalAgent.
- **`agent_council.py`** (standalone repo) and **`forecast_agent.py`**
  (standalone repo) are superseded by `council.py` and `forecast_engine.py`
  respectively — their useful bits (Fib confluence, forward-path chart) were
  ported in above; the standalone scripts themselves are not part of this
  merged repo.

## Dashboard

`docs/index.html` is a self-contained, private dashboard: latest trade
setups (with charts), forecasts (with the forward-path chart), performance
stats, and open positions. It reads `docs/dashboard.json`, which every layer
updates automatically (see `dashboard_export.py`) whenever it fires a signal,
forecast, or resolves trades. Each GitHub Actions workflow commits the
updated `docs/` + `data/` back to the repo after it runs.

**To view it:**
1. Make sure your local clone/synced project folder is up to date
   (`git pull`, or however you're syncing this folder from GitHub).
2. Run `python3 serve_dashboard.py` — it opens `http://127.0.0.1:8765` in
   your browser automatically.
3. Refresh any time after pulling new data (it also auto-refreshes every
   60s while the tab is open).

This keeps the dashboard private by default (repo stays private, nothing is
publicly hosted). If you'd rather have it always up to date without manually
pulling, you can instead enable GitHub Pages on this repo pointing at `/docs`
— note private-repo Pages requires GitHub Pro/Team/Enterprise; on the free
tier you'd need to make the repo public for Pages to work.

## Setup

```bash
pip install -r requirements.txt
```

Set these as GitHub repo secrets (Settings → Secrets and variables → Actions):

| Secret | Used for |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Channel/chat to post signals to |
| `TWELVEDATA_KEY` | TwelveData market data |
| `OLLAMA_API_KEY` | Ollama Cloud (swing/council/btc_deep LLM calls) |

For local runs, export the same variables in your shell instead.

## Running locally

```bash
python3 main.py --layer forecast
python3 main.py --layer scalp
python3 main.py --layer swing
python3 main.py --layer council --council-mode swing
python3 main.py --layer btc_deep
python3 main.py --layer performance
python3 main.py --layer backtest --asset EURUSD --bars 2000
python3 main.py                       # full cycle (forecast→scalp→swing→council→performance)
```

## Markets covered

EURUSD, GBPUSD, USDJPY, DXY (macro-only), XAUUSD, SPX500, US100, BTCUSD,
ETHUSD — see `config.py` → `MARKETS` for per-asset tuning (RSI bands, ATR
multiples, sessions, correlations).

## Safety notes

- This is a **signal-only** system — it does not execute trades. You still
  place every order yourself.
- Do not run this GitHub Actions setup alongside any other local/cron copy
  of the same bot — running two schedulers against the same Telegram
  channel produces duplicate signals (this bit the project before).
- Backtest results are informative, not a guarantee — always sanity check
  a strategy change against the score-bucket report before trusting it live.
