"""
Multi-asset scalp/swing layer — BTC + EUR through the SAME validated engine.

Consolidates the retired scalp-council's coverage into the one deterministic
gold engine. It reuses gold_engine.evaluate_setup (trend-pullback + ADX gate)
and its %-of-price risk model verbatim; only the plumbing (which asset, per-
asset cooldown/cap, session rules) lives here. The proven gold pipeline
(gold_engine.run_gold_*) is deliberately NOT touched — this runs alongside it
with its own state file, so a BTC cooldown can never affect gold.

Each asset is evaluated independently inside try/except, so one asset failing
(data hiccup, API 429) never blocks the others or the gold run.
"""
import json
from pathlib import Path

import pandas as pd

from config import (
    MARKETS, EXTRA_ASSETS, EXTRA_MAX_TRADES_PER_DAY,
    GOLD_MIN_CONFIDENCE, GOLD_SCALP_COOLDOWN_MIN, GOLD_SWING_COOLDOWN_H,
)
from indicators import add_base
from data_feeds import fetch_intraday, news_blocked
from gold_engine import evaluate_setup, current_session

DATA_ROOT = Path(__file__).parent / "data"
STATE_FILE = DATA_ROOT / "multi_asset_state.json"


# ── per-asset state (cooldown + daily cap), separate from gold's ────────────
def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save(state: dict) -> None:
    DATA_ROOT.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def _asset_state(state: dict, asset: str) -> dict:
    today = str(pd.Timestamp.now(tz="UTC").date())
    a = state.get(asset) or {}
    if a.get("date") != today:
        a = {"date": today, "trades_today": 0, "last_scalp": None, "last_swing": None}
    return a


def _cap_ok(a: dict) -> bool:
    return a.get("trades_today", 0) < EXTRA_MAX_TRADES_PER_DAY


def _in_session(asset: str) -> dict:
    """Crypto trades 24/7 (never session-gated); FX uses the gold London/NY
    killzone window. Returns the same shape current_session() does."""
    if not MARKETS[asset].get("session_gated", True):
        return {"in_session": True, "name": "24h", "judas_watch": False}
    return current_session()


def _weekend_skip(asset: str) -> bool:
    if MARKETS[asset].get("weekend", False):
        return False
    return pd.Timestamp.now(tz="UTC").dayofweek >= 5


def _evaluate(asset: str, tf: str, ltf_iv: str, htf_iv: str, ltf_bars: int, htf_bars: int):
    df_ltf = fetch_intraday(asset, ltf_iv, ltf_bars)
    df_htf = fetch_intraday(asset, htf_iv, htf_bars)
    if df_ltf is None or df_htf is None or len(df_ltf) < 60 or len(df_htf) < 60:
        print(f"  {MARKETS[asset]['emoji']} {asset} {tf}: ⚠ insufficient data")
        return None
    df_ltf, df_htf = add_base(df_ltf), add_base(df_htf)
    session = _in_session(asset)
    result = evaluate_setup(df_ltf, df_htf, None, session, timeframe=tf)
    print(f"  {MARKETS[asset]['emoji']} {asset} {tf}: {result['signal_label']} "
          f"conf={result['confidence']:.0%}")
    if result["direction"] == "NEUTRAL" or result["confidence"] < GOLD_MIN_CONFIDENCE:
        return None
    return result, session


def run_asset_scalp(asset: str, state: dict) -> list[dict]:
    a = _asset_state(state, asset)
    if not _cap_ok(a):
        print(f"  {MARKETS[asset]['emoji']} {asset} scalp: daily cap reached")
        return []
    session = _in_session(asset)
    if not session["in_session"]:
        return []
    now = pd.Timestamp.now(tz="UTC")
    last = a.get("last_scalp")
    if last and (now - pd.Timestamp(last)) < pd.Timedelta(minutes=GOLD_SCALP_COOLDOWN_MIN):
        return []
    # FX respects USD-news blackout; crypto ignores it
    if MARKETS[asset]["asset_class"] == "fx":
        ev = news_blocked(asset)
        if ev:
            print(f"  {MARKETS[asset]['emoji']} {asset} scalp: 📰 blocked — {ev}")
            return []
    ev = _evaluate(asset, "SCALP", "15min", "1h", 200, 150)
    if not ev:
        return []
    result, session = ev
    result.update({"asset": asset, "timestamp": str(now), "layer": "scalp",
                   "timeframe": "15m", "session": session["name"]})
    a["last_scalp"] = str(now); a["trades_today"] = a.get("trades_today", 0) + 1
    state[asset] = a
    return [result]


def run_asset_swing(asset: str, state: dict) -> list[dict]:
    a = _asset_state(state, asset)
    if not _cap_ok(a):
        return []
    now = pd.Timestamp.now(tz="UTC")
    last = a.get("last_swing")
    if last and (now - pd.Timestamp(last)) < pd.Timedelta(hours=GOLD_SWING_COOLDOWN_H):
        return []
    if MARKETS[asset]["asset_class"] == "fx":
        ev = news_blocked(asset)
        if ev:
            return []
    ev = _evaluate(asset, "SWING", "1h", "4h", 200, 150)
    if not ev:
        return []
    result, session = ev
    result.update({"asset": asset, "timestamp": str(now), "layer": "swing",
                   "timeframe": "1h/4h", "session": session["name"] or "any"})
    a["last_swing"] = str(now); a["trades_today"] = a.get("trades_today", 0) + 1
    state[asset] = a
    return [result]


def run_extra_assets() -> list[dict]:
    """Run BTC + EUR scalp/swing through the gold engine. Returns all fired
    signals (main.py handles Telegram + logging + charts)."""
    state = _load()
    fired = []
    for asset in EXTRA_ASSETS:
        if asset not in MARKETS:
            continue
        if _weekend_skip(asset):
            print(f"  {MARKETS[asset]['emoji']} {asset}: market closed (weekend)")
            continue
        try:
            fired += run_asset_scalp(asset, state)
            fired += run_asset_swing(asset, state)
        except Exception as e:
            print(f"  ⚠ {asset} failed: {e}")
    _save(state)
    return fired
