"""
Performance tracker & outcome resolver.

Loads open signals from data/open_signals.jsonl, walks forward TwelveData
candles to determine if TP1, TP2, STOP, or EXPIRED was hit, writes
resolved outcomes to data/resolved_trades.jsonl, and sends a summary
to Telegram.
"""

import json
import time
from pathlib import Path
import pandas as pd

from config import MARKETS
from data_feeds import fetch_intraday
import telegram

DATA_ROOT    = Path(__file__).parent / "data"
OPEN_FILE    = DATA_ROOT / "open_signals.jsonl"
RESOLVED_FILE= DATA_ROOT / "resolved_trades.jsonl"
MAX_HOLD_H   = 72        # expire after 72h if no target hit


def log_open_signal(signal: dict) -> None:
    """Call this when a new signal fires to track it for resolution."""
    DATA_ROOT.mkdir(exist_ok=True)
    rec = {k: (str(v) if isinstance(v, (pd.Timestamp,)) else v)
           for k, v in signal.items()
           if k not in ("chart_png",)}
    rec.setdefault("opened_at", str(pd.Timestamp.now(tz="UTC")))
    with open(OPEN_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _resolve_signal(sig: dict) -> dict | None:
    """Walk forward candles and determine outcome. Returns updated sig or None if still open."""
    asset = sig.get("asset")
    if not asset or asset not in MARKETS:
        return {**sig, "outcome": "INVALID", "outcome_r": 0.0}

    direction = sig.get("direction") or sig.get("verdict")
    entry     = float(sig.get("entry", 0))
    stop      = float(sig.get("stop_loss", sig.get("stop", 0)))
    tp1       = float(sig.get("target_1",  sig.get("tp1", 0)))
    tp2       = float(sig.get("target_2",  sig.get("tp2", 0)))
    tp3       = float(sig.get("target_3",  sig.get("tp3", 0)) or 0)
    opened_at = pd.Timestamp(sig["opened_at"])

    if not entry or not stop or not tp1:
        return {**sig, "outcome": "INCOMPLETE", "outcome_r": 0.0}

    risk = abs(entry - stop)
    if risk == 0:
        return {**sig, "outcome": "INVALID", "outcome_r": 0.0}

    df = fetch_intraday(asset, "1h", 300)
    if df is None or len(df) < 5:
        return None   # still open — data unavailable

    df = df[df.index >= opened_at]
    if df.empty:
        return None

    now = pd.Timestamp.now(tz="UTC")
    expire = opened_at + pd.Timedelta(hours=MAX_HOLD_H)

    # Three targets now (TP1/TP2/TP3) — once a target is hit the stop is
    # assumed moved to breakeven, so a later STOPPED is booked at 0R rather
    # than -1R. TP2/TP3 only return once BOTH the prior target and this one
    # have been hit; if the loop runs out (data ends / max hold reached)
    # with a later target un-hit, the highest target actually reached wins.
    hit_tp1 = hit_tp2 = False
    for ts, row in df.iterrows():
        h, l = row["high"], row["low"]
        if direction in ("LONG", "BUY"):
            if l <= stop:
                r = -1.0 if not hit_tp1 else 0.0
                return {**sig, "outcome":"STOPPED", "outcome_r":r, "resolved_at":str(ts)}
            if h >= tp1 and not hit_tp1:
                hit_tp1 = True
            if hit_tp1 and tp2 and h >= tp2 and not hit_tp2:
                hit_tp2 = True
            if hit_tp2 and tp3 and h >= tp3:
                return {**sig, "outcome":"TP3", "outcome_r":3.0*(tp3-entry)/risk, "resolved_at":str(ts)}
        elif direction in ("SHORT", "SELL"):
            if h >= stop:
                r = -1.0 if not hit_tp1 else 0.0
                return {**sig, "outcome":"STOPPED", "outcome_r":r, "resolved_at":str(ts)}
            if l <= tp1 and not hit_tp1:
                hit_tp1 = True
            if hit_tp1 and tp2 and l <= tp2 and not hit_tp2:
                hit_tp2 = True
            if hit_tp2 and tp3 and l <= tp3:
                return {**sig, "outcome":"TP3", "outcome_r":3.0*(entry-tp3)/risk, "resolved_at":str(ts)}

    if hit_tp2:
        r2 = 2.0*(tp2-entry)/risk if direction in ("LONG","BUY") else 2.0*(entry-tp2)/risk
        return {**sig, "outcome":"TP2", "outcome_r":r2, "resolved_at":str(now)}
    if hit_tp1:
        return {**sig, "outcome":"TP1", "outcome_r":abs(tp1-entry)/risk, "resolved_at":str(now)}
    if now >= expire:
        return {**sig, "outcome":"EXPIRED", "outcome_r":0.0, "resolved_at":str(now)}
    return None   # still open


def resolve_open_signals() -> list[dict]:
    if not OPEN_FILE.exists():
        return []
    lines = [l for l in OPEN_FILE.read_text().splitlines() if l.strip()]
    if not lines:
        return []

    still_open = []
    resolved   = []
    for line in lines:
        try:
            sig = json.loads(line)
            result = _resolve_signal(sig)
            if result is None:
                still_open.append(line)
            else:
                resolved.append(result)
        except Exception as e:
            print(f"  ⚠ resolve error: {e}")
            still_open.append(line)
        time.sleep(5)

    OPEN_FILE.write_text("\n".join(still_open) + ("\n" if still_open else ""))
    if resolved:
        with open(RESOLVED_FILE, "a") as f:
            for r in resolved:
                f.write(json.dumps(r) + "\n")
    return resolved


def performance_summary() -> dict:
    """Compute summary stats from resolved trades."""
    if not RESOLVED_FILE.exists():
        return {}
    records = [json.loads(l) for l in RESOLVED_FILE.read_text().splitlines() if l.strip()]
    if not records:
        return {}
    wins      = [r for r in records if r.get("outcome") in ("TP1","TP2","TP3")]
    losses    = [r for r in records if r.get("outcome") == "STOPPED"]
    total_r   = sum(r.get("outcome_r",0) for r in records)
    win_rate  = len(wins) / len(records)
    return {
        "total":    len(records),
        "wins":     len(wins),
        "losses":   len(losses),
        "expired":  len(records) - len(wins) - len(losses),
        "win_rate": round(win_rate, 2),
        "total_r":  round(total_r, 2),
        "avg_r":    round(total_r / len(records), 2),
    }


def run_performance_check() -> list[dict]:
    print("  📊 Resolving open signals...")
    resolved = resolve_open_signals()
    if not resolved:
        print("  ✅ No signals resolved (all still open or none logged)")
        return []

    for r in resolved:
        icon = "✅" if r.get("outcome") in ("TP1","TP2","TP3") else "❌" if r.get("outcome")=="STOPPED" else "⏱"
        print(f"  {icon} {r['asset']} {r.get('direction','?')} → {r['outcome']} {r.get('outcome_r',0):+.1f}R")

    summary = performance_summary()
    text = (
        "<b>📊 Performance Tracker</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total trades: <b>{summary.get('total',0)}</b>\n"
        f"Win rate:     <b>{summary.get('win_rate',0)*100:.0f}%</b>\n"
        f"Total R:      <b>{summary.get('total_r',0):+.1f}R</b>\n"
        f"Avg R/trade:  <b>{summary.get('avg_r',0):+.2f}R</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
    )
    for r in resolved[-8:]:
        icon = "✅" if r.get("outcome") in ("TP1","TP2","TP3") else "❌" if r.get("outcome")=="STOPPED" else "⏱"
        text += f"{icon} {r['asset']} {r.get('direction','?')} {r['outcome']} {r.get('outcome_r',0):+.1f}R\n"
    telegram.send_text(text)
    print(f"  📨 Summary sent to Telegram")
    return resolved
