"""
Sequential Pipeline Orchestrator — unified entry point for all trading layers.

Rebuilt architecture (replaces the old independent-parallel-layers model):
one gradual chain where each agent finishes and hands context to the next,
run as a single ordered pass every 2h during sessions — the way a desk runs
a morning process: read positioning, read macro, form a bias, then look for
setups top-down (swing before scalp), debate it, run the deep per-asset
engine, and audit the whole run at the end.

    COT → Macro (Gemini) → Forecast → Swing → Scalp → Council → Deep Pipeline
    (9 assets) → Performance

Tracer and News stay OUTSIDE this chain — they are always-on, faster-cadence
agents (live position tracking, red-folder news timing) that don't belong on
a 2-hourly cycle. Daily Brief and Weekly COT are their own once-a-day /
once-a-week jobs, also outside the chain. Backtest is manual-only.

Every step is still individually runnable via --layer <name> for standalone
testing, exactly as before — the chain in run_pipeline() below is additive,
not a replacement for that.

Examples:
  python main.py                                # full sequential pipeline
  python main.py --layer cot
  python main.py --layer macro
  python main.py --layer forecast
  python main.py --layer swing
  python main.py --layer scalp
  python main.py --layer council --council-mode swing
  python main.py --layer deep_pipeline          # generalized, all 9 assets
  python main.py --layer btc_deep               # legacy, BTC-only (kept for comparison)
  python main.py --layer performance
  python main.py --layer news                   # standalone, own schedule
  python main.py --layer tracer                  # standalone, own schedule
  python main.py --layer cot_weekly              # standalone, own schedule
  python main.py --layer daily_brief             # standalone, own schedule
  python main.py --layer backtest --asset EURUSD --bars 2000
"""

import argparse
import sys
import traceback
from pathlib import Path

import telegram
from config import MARKETS, COUNCIL_ASSETS


def _send_setup_chart(asset: str, interval: str, signal: dict, caption: str, layer: str = ""):
    """Render + send a trade-setup chart (price/EMA/entry/SL/TP + volume + RSI),
    and record it (+ the signal) into the dashboard data feed."""
    try:
        from data_feeds import fetch_intraday
        from indicators import add_base
        from charts import generate_chart
        import dashboard_export as dash
        df = fetch_intraday(asset, interval, 200)
        if df is None or len(df) < 30:
            return
        chart_png = generate_chart(asset, add_base(df), signal)
        telegram.send_photo(chart_png, caption=caption[:1024])

        direction = signal.get("direction", signal.get("verdict", ""))
        ez = signal.get("entry_zone")
        entry = (ez[0] if isinstance(ez, (list, tuple)) and ez else
                 signal.get("entry"))
        dash.record_signal(
            layer=layer or interval, asset=asset, direction=direction,
            entry=entry,
            stop=signal.get("stop", signal.get("stop_loss")),
            tp1=signal.get("tp1", signal.get("target_1")),
            tp2=signal.get("tp2", signal.get("target_2")),
            score_or_conf=signal.get("score", signal.get("confidence")),
            chart_png=chart_png,
            extra={"caption": caption},
        )
    except Exception as e:
        print(f"    ⚠ chart failed for {asset}: {e}")


# ─────────────────────────────────────────────────────────────────────────
# Chain steps (1..7) — each returns whatever context the next step wants
# ─────────────────────────────────────────────────────────────────────────

def run_cot_layer():
    """Step 1 — official CFTC positioning (+ insider-week fallback). Every
    downstream step that reads COT (forecast/swing internally, deep_pipeline
    explicitly) gets this run's data for free via the 180-min cache / the
    returned dict."""
    print("\n═══ STEP 1 — COT AGENT ═══")
    from cot_agent import run_cot_agent
    cot_map = run_cot_agent()
    cot_list = [{"market": m, **c} for m, c in cot_map.items() if c]
    if cot_list:
        telegram.send_text(telegram.format_cot_map(cot_list))
    return cot_map


def run_macro_layer():
    """Step 2 — Gemini-grounded live macro gather + Ollama synthesis.
    Optional: skips cleanly if GEMINI_API_KEY isn't set."""
    print("\n═══ STEP 2 — MACRO AGENT (Gemini + Ollama) ═══")
    from macro_agent import run_macro_agent
    result = run_macro_agent()
    if result.get("synthesis"):
        telegram.send_text(f"🌐 <b>Macro Read</b>\n\n{result['synthesis']}")
    return result


def run_forecast_layer(assets=None):
    """Step 3 — daily/weekly bias per asset. Already COT-informed internally
    (compute_forecast calls fetch_all_cot(), which just hit step 1's cache)."""
    print("\n═══ STEP 3 — FORECAST ═══")
    from forecast_engine import run_forecast
    import dashboard_export as dash
    results = run_forecast(assets)
    for fc in results:
        msg = telegram.format_forecast_new(fc)
        chart = fc.get("chart_png")
        if chart:
            telegram.send_photo(chart, caption=f"{fc['asset']} — {fc.get('bias','?')}")
        else:
            telegram.send_text(msg)
        try:
            dash.record_forecast(fc["asset"], fc.get("bias", "N/A"), fc.get("price"),
                                  fc.get("forecast", {}), chart)
        except Exception as e:
            print(f"    ⚠ dashboard export failed for {fc.get('asset')}: {e}")
    return results


def run_swing_layer(assets=None):
    """Step 4 — 1h/4h LLM swing plan + COT contrarian gate. Runs BEFORE
    scalp per the confirmed chain order (top-down: swing bias first, then
    scalp looks for entries inside/against it)."""
    print("\n═══ STEP 4 — SWING ═══")
    from swing_engine import run_swing_scan
    from performance_tracker import log_open_signal
    signals = run_swing_scan(assets)
    print(f"\n  Fired: {len(signals)} swing signal(s)")
    for sig in signals:
        telegram.send_text(telegram.format_swing_new(sig))
        _send_setup_chart(sig["asset"], "1h", sig,
                           f"{sig['asset']} — {sig.get('verdict')} swing setup (conf {sig.get('confidence', 0):.0%})",
                           layer="swing")
        log_open_signal({**sig, "direction": sig.get("verdict", sig.get("direction")),
                          "entry": sig.get("entry", 0),
                          "stop":  sig.get("stop_loss", 0),
                          "tp1":   sig.get("target_1", 0),
                          "tp2":   sig.get("target_2", 0)})
    return signals


def run_scalp_layer(assets=None):
    """Step 5 — 15m dual REV/CON scoring."""
    print("\n═══ STEP 5 — SCALP ═══")
    from scalp_engine import run_scalp_scan
    from performance_tracker import log_open_signal
    signals = run_scalp_scan(assets)
    print(f"\n  Fired: {len(signals)} scalp signal(s)")
    for sig in signals:
        telegram.send_text(telegram.format_scalp_new(sig))
        _send_setup_chart(sig["asset"], "15min", sig,
                           f"{sig['asset']} — {sig['direction']} scalp setup (score {sig['score']}/9)",
                           layer="scalp")
        log_open_signal(sig)
    if not signals:
        print("  (nothing passed all gates)")
    return signals


def run_council_layer(assets=None, mode="scalp"):
    """Step 6 — 7-agent debate, sees whatever fired in steps 3-5."""
    print(f"\n═══ STEP 6 — COUNCIL ({mode.upper()}) ═══")
    from council import run_council
    from performance_tracker import log_open_signal
    verdicts = run_council(assets or COUNCIL_ASSETS, mode=mode)
    print(f"\n  Council fired: {len(verdicts)} verdict(s)")
    for v in verdicts:
        telegram.send_text(telegram.format_council(v))
        interval = "15min" if mode == "scalp" else "1h"
        _send_setup_chart(v["asset"], interval, v,
                           f"{v['asset']} — {v['verdict']} council verdict ({mode}, "
                           f"conf {v.get('confidence', 0):.0%}, {max(v.get('bulls',0), v.get('bears',0))}/7 agree)",
                           layer=f"council_{mode}")
        ez = v.get("entry_zone", [0, 0])
        entry = ez[0] if isinstance(ez, (list, tuple)) and len(ez) >= 1 else 0
        log_open_signal({"asset": v["asset"], "direction": v["verdict"],
                          "entry": entry, "stop": v.get("stop_loss", 0),
                          "tp1": v.get("target_1", 0), "tp2": v.get("target_2", 0),
                          "source": f"council_{mode}"})
    return verdicts


def run_deep_pipeline_layer(assets=None, cot_data=None):
    """Step 7 — generalized deep TA/Sentiment/Synthesis/4-layer-risk engine,
    now across all 9 assets (was BTC-only). Extra focus on Sniper setups —
    see deep_pipeline.py's distinct Telegram formatting for that label."""
    print("\n═══ STEP 7 — DEEP PIPELINE (9 assets) ═══")
    from deep_pipeline import run_deep_pipeline
    if cot_data is None:
        import json
        latest = Path("data") / "cot_latest.json"
        if latest.exists():
            try:
                cot_data = json.loads(latest.read_text()).get("data")
            except Exception:
                cot_data = None
    return run_deep_pipeline(assets, cot_data)


def run_performance_layer():
    """Step 8 (last) — audits the whole run: resolves TP1/TP2/STOP/EXPIRED
    across everything the chain fired, refreshes dashboard stats."""
    print("\n═══ STEP 8 — PERFORMANCE (audit) ═══")
    from performance_tracker import run_performance_check, performance_summary, OPEN_FILE
    import dashboard_export as dash
    import json
    run_performance_check()
    try:
        open_positions = []
        if OPEN_FILE.exists():
            open_positions = [json.loads(l) for l in OPEN_FILE.read_text().splitlines() if l.strip()]
        dash.record_performance(performance_summary(), open_positions)
    except Exception as e:
        print(f"    ⚠ dashboard export failed: {e}")


def run_pipeline(assets=None, council_mode="scalp"):
    """The full sequential chain, in the confirmed order. Every 2h during
    sessions (07-19 UTC weekdays) via pipeline.yml. Runs as ONE job so each
    step genuinely sees the previous step's fresh output — no overlap, no
    two schedulers racing each other."""
    cot_map = run_cot_layer()
    run_macro_layer()
    run_forecast_layer(assets)
    run_swing_layer(assets)
    run_scalp_layer(assets)
    run_council_layer(assets, mode=council_mode)
    run_deep_pipeline_layer(assets, cot_data=cot_map)
    run_performance_layer()


# ─────────────────────────────────────────────────────────────────────────
# Standalone-only layers (outside the chain, own schedules)
# ─────────────────────────────────────────────────────────────────────────

def run_btc_deep_layer():
    """Legacy BTC-only deep pipeline — kept for manual comparison against
    the new generalized deep_pipeline.py. Not part of run_pipeline()."""
    print("\n═══ BTC DEEP-PIPELINE LAYER (legacy, BTC-only) ═══")
    import btc_deep_pipeline
    btc_deep_pipeline.run_once()


def run_news_layer():
    print("\n═══ NEWS AGENT LAYER ═══")
    from news_agent import run_news_agent
    run_news_agent()


def run_tracer_layer():
    print("\n═══ TRACER LAYER ═══")
    from tracer_agent import run_tracer_agent
    run_tracer_agent()


def run_daily_brief_layer():
    print("\n═══ DAILY BRIEF (AR) ═══")
    from daily_brief import run_daily_brief
    run_daily_brief()


def run_cot_weekly_layer():
    print("\n═══ WEEKLY COT REPORT ═══")
    from data_feeds import fetch_all_cot
    cot_map = fetch_all_cot()
    cot_list = [{"market": m, **c} for m, c in cot_map.items() if c]
    if cot_list:
        telegram.send_text(telegram.format_cot_map(cot_list))
        print(f"  sent COT map for {len(cot_list)} market(s)")
    else:
        print("  no COT data available this run")


def run_backtest_layer(assets=None, bars=2000, window=500):
    print("\n═══ BACKTEST LAYER ═══")
    from backtest import run_backtest
    run_backtest(assets, bars, window)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sequential Pipeline TradingBot")
    parser.add_argument("--layer", default=None,
                        choices=["cot", "macro", "forecast", "swing", "scalp", "council",
                                 "deep_pipeline", "performance",
                                 "btc_deep", "news", "tracer", "cot_weekly", "daily_brief",
                                 "backtest"])
    parser.add_argument("--asset",        default=None)
    parser.add_argument("--council-mode", default="scalp", choices=["scalp", "swing"])
    parser.add_argument("--bars",         type=int, default=2000)
    parser.add_argument("--window",       type=int, default=500)
    args = parser.parse_args()

    assets  = [args.asset.upper()] if args.asset else None
    run_all = args.layer is None
    Path("data").mkdir(exist_ok=True)

    try:
        if run_all:
            run_pipeline(assets, council_mode=args.council_mode)
        elif args.layer == "cot":
            run_cot_layer()
        elif args.layer == "macro":
            run_macro_layer()
        elif args.layer == "forecast":
            run_forecast_layer(assets)
        elif args.layer == "swing":
            run_swing_layer(assets)
        elif args.layer == "scalp":
            run_scalp_layer(assets)
        elif args.layer == "council":
            run_council_layer(assets, mode=args.council_mode)
        elif args.layer == "deep_pipeline":
            run_deep_pipeline_layer(assets)
        elif args.layer == "performance":
            run_performance_layer()
        elif args.layer == "btc_deep":
            run_btc_deep_layer()
        elif args.layer == "news":
            run_news_layer()
        elif args.layer == "tracer":
            run_tracer_layer()
        elif args.layer == "cot_weekly":
            run_cot_weekly_layer()
        elif args.layer == "daily_brief":
            run_daily_brief_layer()
        elif args.layer == "backtest":
            run_backtest_layer(assets, args.bars, args.window)
        print("\n  ✅ run complete")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception:
        err = traceback.format_exc()
        print(err)
        try:
            telegram.send_error(err[-400:])
        except Exception:
            pass
        sys.exit(1)
