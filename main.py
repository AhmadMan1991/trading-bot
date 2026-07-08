"""
Sequential Pipeline Orchestrator — gold-only entry point.

Rebuilt (again) from a 9-asset, 7-step, 5-competing-scoring-engine chain
(COT -> Macro -> Forecast -> Swing -> Scalp -> Council -> Deep Pipeline ->
Performance) down to ONE deterministic ICT/SMC gold engine, because running
five different scoring philosophies in parallel across nine assets was
exactly what produced rare, conflicting, debate-gated NO_TRADE signals.

New chain, XAUUSD only:

    COT → Macro (Gemini) → Gold Bias → Gold Scalp → Gold Swing → Performance

Tracer and News stay OUTSIDE this chain — always-on, faster-cadence agents
(live position tracking, red-folder news timing). Daily Brief and Weekly COT
are their own once-a-day / once-a-week jobs. Backtest is manual-only.

Every step is individually runnable via --layer <name> for standalone testing.

Examples:
  python main.py                       # full sequential pipeline
  python main.py --layer cot
  python main.py --layer macro
  python main.py --layer gold_bias
  python main.py --layer gold_scalp
  python main.py --layer gold_swing
  python main.py --layer performance
  python main.py --layer news          # standalone, own schedule
  python main.py --layer tracer        # standalone, own schedule
  python main.py --layer cot_weekly    # standalone, own schedule
  python main.py --layer daily_brief   # standalone, own schedule
  python main.py --layer backtest --asset XAUUSD --bars 2000
"""

import argparse
import sys
import traceback
from pathlib import Path

import telegram

ASSET = "XAUUSD"


def _send_gold_chart(interval: str, sig: dict, caption: str, layer: str = ""):
    """Render + send a trade-setup chart (price/EMA/entry/SL/TP + volume + RSI),
    and record it (+ the signal) into the dashboard data feed."""
    try:
        from data_feeds import fetch_intraday
        from indicators import add_base
        from charts import generate_chart
        import dashboard_export as dash
        df = fetch_intraday(ASSET, interval, 200)
        if df is None or len(df) < 30:
            return
        chart_png = generate_chart(ASSET, add_base(df), sig)
        telegram.send_photo(chart_png, caption=caption[:1024])
        dash.record_signal(
            layer=layer or interval, asset=ASSET, direction=sig.get("direction"),
            entry=sig.get("entry"),
            stop=sig.get("stop_loss"),
            tp1=sig.get("target_1"),
            tp2=sig.get("target_2"),
            tp3=sig.get("target_3"),
            score_or_conf=sig.get("confidence"),
            chart_png=chart_png,
            extra={"caption": caption, "signal_label": sig.get("signal_label"),
                   "sniper": sig.get("signal_label") == "SNIPER"},
        )
    except Exception as e:
        print(f"    ⚠ chart failed for {ASSET}: {e}")


# ─────────────────────────────────────────────────────────────────────────
# Chain steps — each returns whatever context the next step wants
# ─────────────────────────────────────────────────────────────────────────

def run_cot_layer():
    """Step 1 — official CFTC positioning (+ insider-week fallback). Gold
    engine reads this as an additive confidence input, not a veto."""
    print("\n═══ STEP 1 — COT AGENT ═══")
    from cot_agent import run_cot_agent, build_cot_summary
    cot_map = run_cot_agent()
    cot_list = [{"market": m, **c} for m, c in cot_map.items() if c]
    if cot_list:
        telegram.send_text(telegram.format_cot_map(cot_list, summary=build_cot_summary(cot_map)))
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


def run_gold_bias_layer():
    """Step 3 — H4/H1 structural bias for XAUUSD (EMA stack + swing
    structure). Informs scalp/swing but doesn't gate them — it's context,
    surfaced on its own so you can see it even when no trade fires.

    Also computes the 1H/4H/Daily/Weekly scenario snapshots for the
    dashboard (gold_engine.run_gold_scenarios()) — a wider top-down picture
    than the single H4 read, purely informational, doesn't affect the
    scalp/swing decision."""
    print("\n═══ STEP 3 — GOLD BIAS & SCENARIOS ═══")
    from gold_engine import run_gold_bias, run_gold_scenarios
    import dashboard_export as dash
    bias = run_gold_bias()
    if bias:
        b = bias.get("bias", "NEUTRAL")
        reasons = ", ".join(bias.get("reasons", [])[:3])
        telegram.send_text(f"🥇 <b>Gold Bias (H4)</b>: {b}\n<i>{reasons}</i>")
        try:
            dash.record_forecast(ASSET, b, None, bias, None)
        except Exception as e:
            print(f"    ⚠ dashboard export failed: {e}")

    scenarios = run_gold_scenarios()
    try:
        dash.record_scenarios(scenarios)
    except Exception as e:
        print(f"    ⚠ scenario dashboard export failed: {e}")

    return bias


def run_gold_scalp_layer(cot_map=None):
    """Step 4 — session-gated (London/NY killzone) liquidity-sweep scalp
    scan. Additive confluence scoring — no multi-agent veto, so it can't
    deadlock into NO_TRADE the way the old council did."""
    print("\n═══ STEP 4 — GOLD SCALP ═══")
    from gold_engine import run_gold_scalp
    from performance_tracker import log_open_signal
    signals = run_gold_scalp(cot_map)
    print(f"\n  Fired: {len(signals)} gold scalp signal(s)")
    for sig in signals:
        telegram.send_text(telegram.format_gold_signal_short(sig))
        telegram.send_text(telegram.format_gold_signal(sig))
        _send_gold_chart("15min", sig,
                          f"XAUUSD — {sig.get('direction')} scalp ({sig.get('signal_label')}, "
                          f"conf {sig.get('confidence', 0):.0%})",
                          layer="gold_scalp")
        log_open_signal({**sig, "asset": ASSET,
                          "entry": sig.get("entry", 0),
                          "stop": sig.get("stop_loss", 0),
                          "tp1": sig.get("target_1", 0),
                          "tp2": sig.get("target_2", 0),
                          "tp3": sig.get("target_3", 0)})
    if not signals:
        print("  (no session sweep setup — or outside killzone/cooldown/risk-limit)")
    return signals


def run_gold_swing_layer(cot_map=None):
    """Step 5 — H1/H4 multi-day structure scan. Not session-gated (swing
    doesn't need killzone timing), still risk/cooldown/news gated."""
    print("\n═══ STEP 5 — GOLD SWING ═══")
    from gold_engine import run_gold_swing
    from performance_tracker import log_open_signal
    signals = run_gold_swing(cot_map)
    print(f"\n  Fired: {len(signals)} gold swing signal(s)")
    for sig in signals:
        telegram.send_text(telegram.format_gold_signal_short(sig))
        telegram.send_text(telegram.format_gold_signal(sig))
        _send_gold_chart("1h", sig,
                          f"XAUUSD — {sig.get('direction')} swing ({sig.get('signal_label')}, "
                          f"conf {sig.get('confidence', 0):.0%})",
                          layer="gold_swing")
        log_open_signal({**sig, "asset": ASSET,
                          "entry": sig.get("entry", 0),
                          "stop": sig.get("stop_loss", 0),
                          "tp1": sig.get("target_1", 0),
                          "tp2": sig.get("target_2", 0),
                          "tp3": sig.get("target_3", 0)})
    if not signals:
        print("  (no multi-day structure setup — or cooldown/risk-limit/news block)")
    return signals


def run_performance_layer():
    """Step 6 (last) — audits the whole run: resolves TP1/TP2/STOP/EXPIRED,
    feeds resolved trades back into gold_engine's risk state (daily loss
    circuit breaker), refreshes dashboard stats."""
    print("\n═══ STEP 6 — PERFORMANCE (audit) ═══")
    from performance_tracker import run_performance_check, performance_summary, OPEN_FILE
    import dashboard_export as dash
    import json
    resolved = run_performance_check()
    try:
        from gold_engine import update_risk_state_from_resolved
        update_risk_state_from_resolved(resolved or [])
    except Exception as e:
        print(f"    ⚠ risk-state update failed: {e}")
    try:
        open_positions = []
        if OPEN_FILE.exists():
            open_positions = [json.loads(l) for l in OPEN_FILE.read_text().splitlines() if l.strip()]
        dash.record_performance(performance_summary(), open_positions)
    except Exception as e:
        print(f"    ⚠ dashboard export failed: {e}")


def run_pipeline():
    """The full sequential chain, gold-only. Recommended cadence: every
    30-60min during London/NY sessions (07-16 UTC weekdays) via pipeline.yml —
    tighter than the old 2h cycle since scalp setups are session-timed."""
    cot_map = run_cot_layer()
    run_macro_layer()
    run_gold_bias_layer()
    run_gold_scalp_layer(cot_map)
    run_gold_swing_layer(cot_map)
    run_performance_layer()


# ─────────────────────────────────────────────────────────────────────────
# Standalone-only layers (outside the chain, own schedules)
# ─────────────────────────────────────────────────────────────────────────

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
    from cot_agent import build_cot_summary
    import dashboard_export as dash
    cot_map = fetch_all_cot()
    cot_list = [{"market": m, **c} for m, c in cot_map.items() if c]
    if cot_list:
        summary = build_cot_summary(cot_map)
        telegram.send_text(telegram.format_cot_map(cot_list, summary=summary))
        try:
            dash.record_cot(cot_map, summary)
        except Exception as e:
            print(f"    ⚠ dashboard export failed: {e}")
        print(f"  sent COT map for {len(cot_list)} market(s)")
    else:
        print("  no COT data available this run")


def run_backtest_layer(assets=None, bars=2000, window=500):
    print("\n═══ BACKTEST LAYER ═══")
    from backtest import run_backtest
    run_backtest(assets, bars, window)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gold-only Sequential Pipeline TradingBot")
    parser.add_argument("--layer", default=None,
                        choices=["cot", "macro", "gold_bias", "gold_scalp", "gold_swing",
                                 "performance", "news", "tracer", "cot_weekly", "daily_brief",
                                 "backtest"])
    parser.add_argument("--asset",  default=None)   # kept for backtest compat; engine itself is gold-only
    parser.add_argument("--bars",   type=int, default=2000)
    parser.add_argument("--window", type=int, default=500)
    args = parser.parse_args()

    assets  = [args.asset.upper()] if args.asset else None
    run_all = args.layer is None
    Path("data").mkdir(exist_ok=True)

    try:
        if run_all:
            run_pipeline()
        elif args.layer == "cot":
            run_cot_layer()
        elif args.layer == "macro":
            run_macro_layer()
        elif args.layer == "gold_bias":
            run_gold_bias_layer()
        elif args.layer == "gold_scalp":
            run_gold_scalp_layer()
        elif args.layer == "gold_swing":
            run_gold_swing_layer()
        elif args.layer == "performance":
            run_performance_layer()
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
