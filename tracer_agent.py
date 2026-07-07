"""
Tracer agent — frequent, lightweight live-position updates.

performance_tracker.py is the authoritative resolver (TP1/TP2/STOP/EXPIRED,
once daily). This agent runs much more often (every ~15 min) and does a
cheaper job: for every currently-open signal, check how far price has moved
toward TP1 vs SL, refresh that onto the dashboard so it always shows live
progress (not just "open" with no context), and send a Telegram nudge only
when a position crosses a new milestone (50% / 75% / 100% of the way to
target) — not on every single run, to avoid spamming the channel.
"""

import json
from pathlib import Path
import pandas as pd

from config import MARKETS, TRACER_MILESTONES
from data_feeds import fetch_intraday
from performance_tracker import OPEN_FILE
import dashboard_export as dash
import telegram

DATA_ROOT  = Path(__file__).parent / "data"
STATE_FILE = DATA_ROOT / "tracer_state.json"


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    DATA_ROOT.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _progress(direction: str, entry: float, stop: float, tp1: float, price: float) -> float:
    """0.0 = at entry, 1.0 = at TP1, negative = moved toward stop instead."""
    target_dist = abs(tp1 - entry)
    risk_dist   = abs(entry - stop)
    if target_dist == 0:
        return 0.0
    moved = (price - entry) if direction in ("LONG", "BUY") else (entry - price)
    if moved >= 0:
        return moved / target_dist
    return -abs(moved) / risk_dist if risk_dist else 0.0


def run_tracer_agent() -> None:
    if not OPEN_FILE.exists():
        print("  no open positions to trace")
        return

    lines = [l for l in OPEN_FILE.read_text().splitlines() if l.strip()]
    positions = [json.loads(l) for l in lines]
    if not positions:
        print("  no open positions to trace")
        return

    state = _load_state()
    updated = []

    for pos in positions:
        asset = pos.get("asset")
        if not asset or asset not in MARKETS:
            updated.append(pos)
            continue
        try:
            df = fetch_intraday(asset, "15min", 5)
            if df is None or df.empty:
                updated.append(pos)
                continue
            price = float(df["close"].iloc[-1])

            direction = pos.get("direction") or pos.get("verdict")
            entry = float(pos.get("entry", 0) or 0)
            stop  = float(pos.get("stop", pos.get("stop_loss", 0)) or 0)
            tp1   = float(pos.get("tp1",  pos.get("target_1", 0)) or 0)
            if not entry or not stop or not tp1:
                updated.append(pos)
                continue

            pct = _progress(direction, entry, stop, tp1, price)
            pos["_live_price"] = price
            pos["_progress_pct"] = round(pct, 3)

            key = f"{asset}_{pos.get('opened_at','')}"
            last_milestone = state.get(key, 0)
            for m in TRACER_MILESTONES:
                if pct >= m > last_milestone:
                    telegram.send_text(telegram.format_tracer_update(pos, pct, price))
                    state[key] = m
                    print(f"  🧭 {asset} crossed {m:.0%} toward target")
                    break

            updated.append(pos)
        except Exception as e:
            print(f"  ⚠ tracer failed for {asset}: {e}")
            updated.append(pos)

    _save_state(state)
    dash_positions = [{k: v for k, v in p.items() if not k.startswith("_")} | 
                      {"live_price": p.get("_live_price"), "progress_pct": p.get("_progress_pct")}
                      for p in updated]
    try:
        d = dash._load()
        d["open_positions"] = dash_positions
        dash._save(d)
    except Exception as e:
        print(f"  ⚠ dashboard update failed: {e}")

    print(f"  traced {len(updated)} open position(s)")
