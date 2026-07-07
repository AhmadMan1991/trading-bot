# Trading Bot — Unified System

One merged trading system, built by combining 4 previously-separate repos
(`ai-trading-bot`, `trading-bot`, `forecast_agent.py`, `scalp-council`) into a
single OODA-loop pipeline with charts and a private dashboard.

**This system only sends alerts (Telegram + dashboard). It does not place
real trades or connect to a broker.** You decide whether to act on a signal.

## Layers (`python main.py --layer <name>`)

| Layer | What it does | Schedule |
|---|---|---|
| `forecast` | Daily/weekly bias + BS_OB_RJB_FVG pattern detection + forward-path chart (target zone / invalidation) | 05:55 UTC weekdays + Sun 20:00 (weekly outlook) |
| `scalp` | 15m dual REV/CON scoring across 9 markets, with a trade-setup chart (entry/SL/TP overlay) per signal | every 15 min, 07–20 UTC weekdays (:01/:16/:31/:46) |
| `swing` | 1h/4h LLM swing plan + COT contrarian gate — the "4h analysis" | every 4h, offset :10 |
| `council` (scalp mode) | 7-agent LLM debate (Trend/PriceAction/Institutional/Quant/SMC/Tracer/Performance) → Chair verdict | every 2h, 07–19 UTC, offset :35 |
| `council` (swing mode) | same 7-agent debate on the 1h timeframe — the "H1 forecast" pulse | hourly, 07–20 UTC, offset :50 |
| `news` | Red-folder USD news pre/post alerts (see below) | every 5 min, 06–22 UTC weekdays |
| `tracer` | Live open-position progress updates (see below) | every 15 min, 07–20 UTC weekdays (:05/:20/:35/:50) |
| `cot_weekly` | Standalone COT positioning map, once a week | Friday 19:45 UTC |
| `daily_brief` | Arabic executive-summary brief across all configured markets (see below) | 06:15 UTC weekdays |
| `btc_deep` | Standalone deep BTC pipeline (Wyckoff spring, absorption, stealth accumulation, derivatives trap, sniper score, 4-layer risk engine) — ported from the old `ai-trading-bot` repo, kept as its own layer since it's a much deeper/heavier analysis than the other layers | every 3h |
| `performance` | Resolves open signals (TP1/TP2/STOP/EXPIRED), posts a scorecard, refreshes dashboard stats | daily 22:00 UTC |
| `backtest` | Walk-forward simulation using the live scoring functions | manual only |

Run everything once: `python main.py` (runs forecast → scalp → swing → council → performance — the lightweight `news`/`tracer`/`cot_weekly` layers are meant to run on their own schedules, not as part of the full cycle).

### Why not literally "every minute"?

Two real constraints shaped the schedule above, worth knowing before you tighten it further:

1. **GitHub Actions' `schedule` trigger isn't built for sub-5-minute cadences.** GitHub explicitly doesn't guarantee scheduled workflows fire at the exact minute — during busy periods (like the top of the hour, when everyone's cron fires) runs get queued and can slip by several minutes. Below ~5 minutes, this stops being reliable.
2. **`scalp` reads 15-minute candles.** Scanning it every minute wouldn't catch anything a 15-minute-aligned scan misses — the underlying data only changes every 15 minutes. So `scalp` runs on `:01/:16/:31/:46`, one run per candle close, which *is* "catch every scalp chance" for a 15m strategy. Running it every minute would just burn Actions minutes and TwelveData API calls for zero extra signal coverage.
3. **Free-tier Actions minutes are finite.** A private repo gets 2,000 min/month free. A workflow that ran continuously every minute for the ~13h session window, 5 days/week, would alone blow past that budget several times over. The schedule above (15-min scalp, hourly h1, 4h swing, 5-min news checks) is designed to stay comfortably inside the free tier while still being responsive to anything that actually needs faster polling (news timing, live position tracking).

If you ever do want true sub-minute, tick-level responsiveness (not candle-based), that needs a different architecture entirely — a small always-on process (a cheap VPS, or even your own Mac left running) subscribed to a live price/tick feed, not a scheduled CI job. Happy to help build that separately if you want it, but it's a different kind of system than "run a script every N minutes."

### New agents

- **`news_agent.py`** — watches the ForexFactory high-impact ("red folder") calendar for USD events. ~15 minutes before a watched release it sends the previous reading + forecast/consensus so you know what "beat" vs "miss" means for that number. Once the feed populates the actual value, it sends a second message comparing previous/forecast/actual and a beat/miss read on typical USD bias — noting that this also tends to push XAU and equity indices the opposite way. That correlation is a historical tendency, not a rule (risk sentiment and positioning can override it), and the code says so in the message. Extend `NEWS_WATCH_CURRENCIES` in `config.py` to watch other currencies too.
- **`daily_brief.py`** — an Arabic-language executive daily brief, built natively from data this system already computes (weekly/daily bias, COT signal, and any signal fired in the last 24h combine into a -4..+4 score per asset). This is a from-scratch equivalent of a format from a separate bot you run elsewhere (not one of the merged repos) — it does not pull any data from that other bot, it recomputes the same *kind* of report from this system's own markets (so the asset list differs slightly — this system covers the 9 configured in `config.py`, not that bot's exact list). The executive-summary paragraph is LLM-written (Ollama) from the computed score table so the narrative reflects real numbers rather than being freeform. Shows up as its own RTL section on the dashboard.
- **`tracer_agent.py`** — runs far more often than the daily `performance` layer. For every currently open position it checks live price, computes progress toward TP1 vs SL as a percentage, updates the dashboard continuously, and sends one Telegram nudge the first time a position crosses 50%/75%/100% of the way to target (not every run — that would spam the channel). The daily `performance` layer is still the authoritative TP1/TP2/STOP/EXPIRED resolver; tracer is just a faster live view on top of it.

### Keeping frequent workflows from stepping on each other

With this many workflows now committing to the repo, the "Commit dashboard + state" step in every workflow does `git pull --rebase --autostash` before pushing, and retries a few times with a short random delay if another run's push landed first. This handles the occasional collision between, say, `tracer` and `news_agent` firing in the same 5-minute slot — without it, one of the two pushes would just silently fail.

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
python3 main.py --layer news          # USD red-folder pre/post alerts
python3 main.py --layer tracer        # live open-position progress nudges
python3 main.py --layer cot_weekly    # standalone COT positioning map
python3 main.py --layer daily_brief   # Arabic executive daily brief
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
