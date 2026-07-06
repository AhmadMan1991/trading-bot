"""
Walk-forward backtester.

  - Imports live score_scalp() from scalp_engine
  - Walks historical 15m data in windows
  - Simulates entry/TP/STOP resolution bar-by-bar
  - Reports by score bucket: ≥7, ≥8, ≥9

Usage:
  python backtest.py [--asset EURUSD] [--bars 2000] [--window 500]
"""

import argparse
import json
from pathlib import Path
import pandas as pd
import numpy as np

from config  import MARKETS
from data_feeds import fetch_intraday
from scalp_engine import score_scalp
from indicators   import add_base

DATA_ROOT = Path(__file__).parent / "data"
DATA_ROOT.mkdir(exist_ok=True)

MAX_HOLD   = 20     # bars before expiry


def _simulate_trade(df: pd.DataFrame, entry: float, stop: float, tp1: float,
                    direction: str, start_i: int) -> dict:
    """Walk forward from start_i, return outcome dict."""
    risk = abs(entry - stop); rr1 = abs(tp1 - entry)
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


def backtest_asset(asset: str, total_bars: int = 2000, window: int = 500) -> dict:
    cfg = MARKETS[asset]
    print(f"\n  Backtest: {asset} ({total_bars} bars, window={window})")

    df = fetch_intraday(asset, "15min", total_bars)
    if df is None or len(df) < window + 100:
        print(f"  ⚠ insufficient data")
        return {}

    trades = []
    for i in range(window, len(df) - 30, 15):   # step 15 bars
        sub = df.iloc[:i].copy()
        if len(sub) < 100:
            continue
        sub_ind = add_base(sub)
        result  = score_scalp(sub_ind, cfg)
        if result["direction"] == "NONE":
            continue

        outcome = _simulate_trade(
            df, result["entry"], result["stop"], result["tp1"],
            result["direction"], i)
        outcome["score"]     = result["score"]
        outcome["direction"] = result["direction"]
        outcome["setup_type"]= result["setup_type"]
        trades.append(outcome)

    if not trades:
        return {}

    # Bucket analysis
    buckets = {}
    for thresh in [6, 7, 8, 9]:
        subset = [t for t in trades if t["score"] >= thresh]
        if not subset:
            continue
        wins = [t for t in subset if t["outcome"] == "TP1"]
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
        print(f"  Score≥{thresh}: {b['trades']} trades | "
              f"WR {b['win_rate']*100:.0f}% | "
              f"Total R {b['total_r']:+.1f} | "
              f"Avg {b['avg_r']:+.2f}R")

    report = {"asset": asset, "total_bars": total_bars,
              "window": window, "trades": trades, "buckets": buckets}
    out = DATA_ROOT / f"backtest_{asset}.json"
    out.write_text(json.dumps({k: v for k, v in report.items() if k != "trades"},
                              indent=2))
    return report


def run_backtest(assets: list | None = None, bars: int = 2000, window: int = 500) -> None:
    assets = assets or list(MARKETS.keys())
    results = {}
    for asset in assets:
        try:
            results[asset] = backtest_asset(asset, bars, window)
        except Exception as e:
            print(f"  ⚠ {asset}: {e}")
    print("\n  ── Summary ──")
    for asset, r in results.items():
        if not r or "buckets" not in r:
            continue
        b7 = r["buckets"].get(7, {})
        if b7:
            print(f"  {asset:<10} score≥7: {b7['win_rate']*100:.0f}% WR, "
                  f"{b7['total_r']:+.1f}R / {b7['trades']} trades")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset",  default=None, help="single asset to backtest")
    parser.add_argument("--bars",   type=int, default=2000)
    parser.add_argument("--window", type=int, default=500)
    args = parser.parse_args()
    assets = [args.asset] if args.asset else None
    run_backtest(assets, args.bars, args.window)
