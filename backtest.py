"""
Walk-forward backtester — gold-only.

  - Imports live evaluate_setup() from gold_engine (same additive-confluence
    decision function the real scalp/swing layers use — no separate scoring
    logic to keep in sync)
  - Walks historical 15m data in windows, evaluating the M15 sweep setup
    against a time-aligned H1 structure slice at each step
  - Simulates entry/TP/STOP resolution bar-by-bar
  - Reports by confidence bucket: >=0.55, >=0.65, >=0.75, >=0.85 (SNIPER)

Usage:
  python backtest.py [--bars 2000] [--window 500]

--asset is accepted for backward compatibility but ignored — this system
only trades XAUUSD now.
"""

import argparse
import json
from pathlib import Path
import pandas as pd
import numpy as np

from data_feeds import fetch_intraday, fetch_all_cot
from gold_engine import ASSET, evaluate_setup, current_session
from indicators import add_base

DATA_ROOT = Path(__file__).parent / "data"
DATA_ROOT.mkdir(exist_ok=True)

MAX_HOLD = 40     # M15 bars before expiry (~10h)
STEP     = 5      # advance 5 M15 bars (~75min) between evaluations — keeps runtime sane
CONF_BUCKETS = [0.55, 0.65, 0.75, 0.85]


def _simulate_trade(df: pd.DataFrame, entry: float, stop: float, tp1: float,
                    direction: str, start_i: int) -> dict:
    """Walk forward from start_i, return outcome dict."""
    risk = abs(entry - stop); rr1 = abs(tp1 - entry)
    if risk <= 0:
        return {"outcome": "INVALID", "r": 0.0, "bars": 0}
    for j in range(start_i, min(start_i + MAX_HOLD, len(df))):
        h, l = df["high"].iloc[j], df["low"].iloc[j]
        if direction == "LONG":
            if l <= stop:
                return {"outcome": "STOPPED", "r": -1.0, "bars": j - start_i}
            if h >= tp1:
                return {"outcome": "TP1",     "r":  rr1 / risk, "bars": j - start_i}
        else:
            if h >= stop:
                return {"outcome": "STOPPED", "r": -1.0, "bars": j - start_i}
            if l <= tp1:
                return {"outcome": "TP1",     "r":  rr1 / risk, "bars": j - start_i}
    return {"outcome": "EXPIRED", "r": 0.0, "bars": MAX_HOLD}


def backtest_gold(total_bars: int = 2000, window: int = 500) -> dict:
    print(f"\n  Backtest: {ASSET} ({total_bars} M15 bars, window={window})")

    df_m15 = fetch_intraday(ASSET, "15min", total_bars)
    # ~4x fewer H1 bars covers the same wall-clock span as the M15 series,
    # plus a cushion so the earliest M15 bars still have H1 history behind them.
    df_h1 = fetch_intraday(ASSET, "1h", total_bars // 4 + 200)
    if df_m15 is None or len(df_m15) < window + 100 or df_h1 is None or len(df_h1) < 100:
        print(f"  ⚠ insufficient data")
        return {}

    df_m15 = add_base(df_m15)
    df_h1  = add_base(df_h1)
    cot = (fetch_all_cot() or {}).get(ASSET)

    trades = []
    for i in range(window, len(df_m15) - 30, STEP):
        sub_ltf = df_m15.iloc[:i]
        if len(sub_ltf) < 60:
            continue
        cutoff = sub_ltf.index[-1]
        sub_htf = df_h1[df_h1.index <= cutoff]
        if len(sub_htf) < 60:
            continue

        session = current_session(now=pd.Timestamp(cutoff))
        result = evaluate_setup(sub_ltf, sub_htf, cot, session,
                                 timeframe="SCALP", skip_reasoning=True)
        if result["direction"] == "NEUTRAL":
            continue

        outcome = _simulate_trade(
            df_m15, result["entry"], result["stop_loss"], result["target_1"],
            result["direction"], i)
        outcome["confidence"]   = result["confidence"]
        outcome["direction"]    = result["direction"]
        outcome["signal_label"] = result["signal_label"]
        trades.append(outcome)

    if not trades:
        print("  no setups fired across this window")
        return {}

    buckets = {}
    for thresh in CONF_BUCKETS:
        subset = [t for t in trades if t["confidence"] >= thresh]
        if not subset:
            continue
        wins   = [t for t in subset if t["outcome"] == "TP1"]
        losses = [t for t in subset if t["outcome"] == "STOPPED"]
        r_total = sum(t["r"] for t in subset)
        buckets[thresh] = {
            "trades": len(subset),
            "wins":   len(wins),
            "losses": len(losses),
            "expired":len(subset) - len(wins) - len(losses),
            "win_rate": round(len(wins) / len(subset), 2),
            "total_r":  round(r_total, 2),
            "avg_r":    round(r_total / len(subset), 2),
        }

    print(f"  Total signals: {len(trades)}")
    for thresh, b in buckets.items():
        print(f"  Conf>={thresh:.2f}: {b['trades']} trades | "
              f"WR {b['win_rate']*100:.0f}% | "
              f"Total R {b['total_r']:+.1f} | "
              f"Avg {b['avg_r']:+.2f}R")

    report = {"asset": ASSET, "total_bars": total_bars,
              "window": window, "trades": trades, "buckets": buckets}
    out = DATA_ROOT / f"backtest_{ASSET}.json"
    out.write_text(json.dumps({k: v for k, v in report.items() if k != "trades"},
                              indent=2))
    return report


def run_backtest(assets: list | None = None, bars: int = 2000, window: int = 500) -> None:
    """`assets` is accepted for CLI/back-compat but ignored — gold-only now."""
    if assets and assets not in (None, [ASSET]):
        print(f"  ⚠ backtest is gold-only now — ignoring requested asset(s) {assets}, running {ASSET}")
    report = backtest_gold(bars, window)
    if not report or "buckets" not in report:
        return
    print("\n  ── Summary ──")
    b = report["buckets"].get(0.55, {})
    if b:
        print(f"  {ASSET:<10} conf>=0.55: {b['win_rate']*100:.0f}% WR, "
              f"{b['total_r']:+.1f}R / {b['trades']} trades")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset",  default=None, help="ignored — backtest is gold-only")
    parser.add_argument("--bars",   type=int, default=2000)
    parser.add_argument("--window", type=int, default=500)
    args = parser.parse_args()
    run_backtest([args.asset] if args.asset else None, args.bars, args.window)
