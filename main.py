"""
OODA Orchestrator — unified entry point for all trading layers.

Observe → Orient → Decide → Act

Layers (--layer flag):
  scalp      → 15m dual REV/CON scoring
  swing      → 1h/4h LLM + COT
  council    → 7-agent debate (--council-mode scalp|swing)
  forecast   → daily/weekly bias + BS_OB_RJB_FVG chart
  performance→ resolve open signals, Telegram summary
  backtest   → walk-forward simulation (manual dispatch only)

Examples:
  python main.py                               # all layers
  python main.py --layer scalp
  python main.py --layer council --council-mode swing
  python main.py --layer forecast
  python main.py --layer performance
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


def run_scalp_layer(assets=None):
    print("\n═══ SCALP LAYER ═══")
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


def run_swing_layer(assets=None):
    print("\n═══ SWING LAYER ═══")
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


def run_council_layer(assets=None, mode="scalp"):
    print(f"\n═══ COUNCIL LAYER ({mode.upper()}) ═══")
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


def run_forecast_layer(assets=None):
    print("\n═══ FORECAST LAYER ═══")
    from forecast_engine import run_forecast
    from data_feeds import fetch_all_cot
    import dashboard_export as dash
    results = run_forecast(assets)
    cot_list = [{"market": r["asset"], **r["cot_iw"]}
                for r in results if r.get("cot_iw")]
    if cot_list:
        telegram.send_text(telegram.format_cot_map(cot_list))
    for fc in results:
        msg   = telegram.format_forecast_new(fc)
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


def run_performance_layer():
    print("\n═══ PERFORMANCE LAYER ═══")
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


def run_btc_deep_layer():
    print("\n═══ BTC DEEP-PIPELINE LAYER ═══")
    import btc_deep_pipeline
    btc_deep_pipeline.run_once()


def run_backtest_layer(assets=None, bars=2000, window=500):
    print("\n═══ BACKTEST LAYER ═══")
    from backtest import run_backtest
    run_backtest(assets, bars, window)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OODA TradingBot")
    parser.add_argument("--layer", default=None,
                        choices=["scalp","swing","council","forecast","performance","backtest","btc_deep"])
    parser.add_argument("--asset",        default=None)
    parser.add_argument("--council-mode", default="scalp", choices=["scalp","swing"])
    parser.add_argument("--bars",         type=int, default=2000)
    parser.add_argument("--window",       type=int, default=500)
    args = parser.parse_args()

    assets  = [args.asset.upper()] if args.asset else None
    run_all = args.layer is None
    Path("data").mkdir(exist_ok=True)

    try:
        if run_all or args.layer == "forecast":
            run_forecast_layer(assets)
        if run_all or args.layer == "scalp":
            run_scalp_layer(assets)
        if run_all or args.layer == "swing":
            run_swing_layer(assets)
        if run_all or args.layer == "council":
            run_council_layer(assets, mode=args.council_mode)
        if run_all or args.layer == "performance":
            run_performance_layer()
        if args.layer == "backtest":
            run_backtest_layer(assets, args.bars, args.window)
        if args.layer == "btc_deep":
            run_btc_deep_layer()
        print("\n  ✅ OODA cycle complete")
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
