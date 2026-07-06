"""
Dual-setup scalp scoring engine.

Two independent scoring tracks — anti-correlated components are separated:
  REVERSAL    max 9 pts  — price at extremes, RSI/OBOS, pin bars, at S/R
  CONTINUATION max 9 pts — trend aligned, MACD cross, momentum, structure

A signal fires only if ONE track scores >= threshold (not both mixed).
This fixes the anti-correlation problem in the old 12-point single score
(trend + reversal fighting each other made threshold 7 nearly impossible).

Per-asset tuning: every market has its own RSI bands and score minimums.
"""

import pandas as pd
from config import MARKETS
from indicators import add_base
from data_feeds import news_blocked, dollar_bias, fetch_td, fetch_intraday

import time

_news_cache = None
_dxy_cache  = None


# ── ML filter (optional — degrades gracefully) ────────────────────────────────
try:
    from ml_filter import ml_score_adjustment
    _ML_OK = True
except ImportError:
    _ML_OK = False
    def ml_score_adjustment(df, direction):
        return 0, 0.5


# ── 1h bias gate ─────────────────────────────────────────────────────────────

def _1h_bias(asset: str) -> str:
    df = fetch_intraday(asset, "1h", 80)
    if df is None or len(df) < 30:
        return "NEUTRAL"
    c = df["close"]
    ema20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
    price = c.iloc[-1]
    if price > ema20 > ema50:
        return "BULLISH"
    if price < ema20 < ema50:
        return "BEARISH"
    return "NEUTRAL"


# ── Dollar adjustment ─────────────────────────────────────────────────────────

def _dxy_adj(asset: str, direction: str) -> int:
    cfg = MARKETS[asset]
    if cfg["asset_class"] in ("index",) or asset in ("BTCUSD", "ETHUSD"):
        return 0
    bias = dollar_bias()
    if bias == "USD_STRONG":
        return -1 if direction == "LONG" else 1
    if bias == "USD_WEAK":
        return 1 if direction == "LONG" else -1
    return 0


# ── Core scoring ──────────────────────────────────────────────────────────────

def score_scalp(df: pd.DataFrame, cfg: dict) -> dict:
    """
    Score one asset on 15m data.
    Returns full result dict; direction='NONE' if no setup found.
    """
    df = add_base(df)
    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    price = last["close"]
    atr   = last["atr"]
    dec   = cfg["decimals"]

    rev_long, rev_short   = 0, 0
    con_long, con_short   = 0, 0
    rev_l_f, rev_s_f      = [], []
    con_l_f, con_s_f      = [], []

    # ── REVERSAL ─────────────────────────────────────────────────────────────
    # RSI extreme
    if last["rsi"] < cfg["rsi_os"]:
        rev_long += 2; rev_l_f.append(f"RSI {last['rsi']:.0f} oversold")
    if last["rsi"] > cfg["rsi_ob"]:
        rev_short += 2; rev_s_f.append(f"RSI {last['rsi']:.0f} overbought")

    # RSI turning from extreme
    rsi_3ago = df["rsi"].iloc[-4]
    if last["rsi"] > rsi_3ago and last["rsi"] < 45:
        rev_long += 1; rev_l_f.append("RSI turning up from low")
    if last["rsi"] < rsi_3ago and last["rsi"] > 55:
        rev_short += 1; rev_s_f.append("RSI turning down from high")

    # At Support / Resistance
    if (price - last["support"]) < atr * 0.6:
        rev_long  += 2; rev_l_f.append("Price at support")
    if (last["resistance"] - price) < atr * 0.6:
        rev_short += 2; rev_s_f.append("Price at resistance")

    # Pin bars
    if last["bull_pin"]:
        rev_long  += 1; rev_l_f.append("Bullish rejection wick")
    if last["bear_pin"]:
        rev_short += 1; rev_s_f.append("Bearish rejection wick")

    # Stochastic extreme
    if last["stoch_k"] < 20 and last["stoch_k"] > df["stoch_k"].iloc[-4]:
        rev_long  += 1; rev_l_f.append(f"Stoch {last['stoch_k']:.0f} turning up from OS")
    if last["stoch_k"] > 80 and last["stoch_k"] < df["stoch_k"].iloc[-4]:
        rev_short += 1; rev_s_f.append(f"Stoch {last['stoch_k']:.0f} turning down from OB")

    # Volume spike at extreme
    if last["vol_ratio"] > 1.8:
        rev_long  += 1; rev_l_f.append(f"Volume spike {last['vol_ratio']:.1f}x at extreme")
        rev_short += 1; rev_s_f.append(f"Volume spike {last['vol_ratio']:.1f}x at extreme")

    # BB lower/upper touch
    if price <= last["bb_lower"] * 1.001:
        rev_long  += 1; rev_l_f.append("Price at lower Bollinger Band")
    if price >= last["bb_upper"] * 0.999:
        rev_short += 1; rev_s_f.append("Price at upper Bollinger Band")

    # ── CONTINUATION ─────────────────────────────────────────────────────────
    # MACD cross
    bull_cross = (last["macd_hist"] > 0 and prev["macd_hist"] <= 0) or \
                 (prev["macd_hist"] > 0 and df["macd_hist"].iloc[-3] <= 0)
    bear_cross = (last["macd_hist"] < 0 and prev["macd_hist"] >= 0) or \
                 (prev["macd_hist"] < 0 and df["macd_hist"].iloc[-3] >= 0)
    if bull_cross:
        con_long  += 2; con_l_f.append("MACD bullish cross")
    if bear_cross:
        con_short += 2; con_s_f.append("MACD bearish cross")

    # EMA 9 > 21 (short-term momentum)
    if last["ema9"] > last["ema20"]:
        con_long  += 1; con_l_f.append("EMA9 > EMA20")
    else:
        con_short += 1; con_s_f.append("EMA9 < EMA20")

    # Above/below EMA50 (trend filter)
    if price > last["ema50"]:
        con_long  += 1; con_l_f.append("Above EMA50")
    else:
        con_short += 1; con_s_f.append("Below EMA50")

    # Structure: higher low / lower high
    if df["low"].iloc[-1] > df["low"].iloc[-11]:
        con_long  += 1; con_l_f.append("Higher low structure")
    if df["high"].iloc[-1] < df["high"].iloc[-11]:
        con_short += 1; con_s_f.append("Lower high structure")

    # Continuation volume
    if last["vol_ratio"] > 1.5:
        con_long  += 1; con_l_f.append(f"Volume {last['vol_ratio']:.1f}x — participation")
        con_short += 1; con_s_f.append(f"Volume {last['vol_ratio']:.1f}x — participation")

    # MACD histogram diverging (momentum building)
    if last["macd_hist"] > prev["macd_hist"] > 0:
        con_long  += 1; con_l_f.append("MACD histogram rising")
    if last["macd_hist"] < prev["macd_hist"] < 0:
        con_short += 1; con_s_f.append("MACD histogram falling")

    # ── Determine direction ───────────────────────────────────────────────────
    long_needed  = cfg["min_score"]
    short_needed = cfg["min_score"] + cfg.get("long_bias", 0)

    # Pick the stronger of reversal / continuation (they're on separate tracks)
    best_long  = max(rev_long,  con_long)
    best_short = max(rev_short, con_short)
    long_type  = "REV" if rev_long >= con_long else "CON"
    short_type = "REV" if rev_short >= con_short else "CON"
    long_f  = rev_l_f if long_type  == "REV" else con_l_f
    short_f = rev_s_f if short_type == "REV" else con_s_f

    direction = "NONE"
    score, factors, setup_type = 0, [], "—"
    if best_long >= long_needed and best_long > best_short:
        direction, score, factors, setup_type = "LONG",  best_long,  long_f,  long_type
    elif best_short >= short_needed and best_short > best_long:
        direction, score, factors, setup_type = "SHORT", best_short, short_f, short_type

    # ── Levels ────────────────────────────────────────────────────────────────
    if direction == "LONG":
        entry = price
        stop  = price - atr * cfg["atr_sl"]
        tp1   = price + atr * cfg["atr_tp1"]
        tp2   = price + atr * cfg["atr_tp2"]
    elif direction == "SHORT":
        entry = price
        stop  = price + atr * cfg["atr_sl"]
        tp1   = price - atr * cfg["atr_tp1"]
        tp2   = price - atr * cfg["atr_tp2"]
    else:
        entry = stop = tp1 = tp2 = 0

    rr = abs(tp1 - entry) / abs(entry - stop) if direction != "NONE" and entry != stop else 0

    return {
        "direction":   direction,
        "score":       score,
        "setup_type":  setup_type,   # REV or CON
        "rev_long":    rev_long, "rev_short": rev_short,
        "con_long":    con_long, "con_short": con_short,
        "factors":     factors[:6],
        "entry":       round(entry, dec),
        "stop":        round(stop,  dec),
        "tp1":         round(tp1,   dec),
        "tp2":         round(tp2,   dec),
        "rr":          round(rr, 2),
        "rsi":         round(float(last["rsi"]), 1),
        "atr":         float(atr),
        "price":       price,
    }


def run_scalp_scan(assets: list | None = None) -> list:
    """
    Full scalp scan: fetch 15m → score → tier-2 gates → return fired signals.
    """
    from pathlib import Path
    import json

    assets = assets or list(MARKETS.keys())
    data_root = Path(__file__).parent / "data"
    data_root.mkdir(exist_ok=True)

    state_path = data_root / "scalp_state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            pass
    state.setdefault("last_signal", {})

    now    = pd.Timestamp.now(tz="UTC")
    fired  = []

    for asset in assets:
        cfg = MARKETS[asset]

        if cfg.get("scalp_skip"):
            continue

        # Session check
        hour = now.hour
        wday = now.dayofweek
        if wday >= 5 and cfg["asset_class"] != "crypto":
            continue
        in_session = any(s <= hour < e for s, e in cfg["sessions_utc"])
        if not in_session:
            continue

        # Cooldown
        last_t = state["last_signal"].get(asset)
        if last_t and (now - pd.Timestamp(last_t)) < pd.Timedelta(hours=4):
            continue

        print(f"  {cfg['emoji']} {asset}")
        df = fetch_intraday(asset, "15min", 200)
        if df is None or len(df) < 80:
            print(f"    ⚠ insufficient data")
            continue

        result = score_scalp(df, cfg)
        print(f"    rev L{result['rev_long']}/S{result['rev_short']}  "
              f"con L{result['con_long']}/S{result['con_short']}  "
              f"→ {result['direction']} {result['setup_type']}")

        if result["direction"] == "NONE" or result["rr"] < 1.0:
            continue

        # Gate 1: news
        event = news_blocked(asset)
        if event:
            print(f"    📰 BLOCKED: {event}")
            continue

        # Gate 2: 1h MTF
        bias = _1h_bias(asset)
        time.sleep(8)
        if (result["direction"] == "LONG" and bias == "BEARISH") or \
           (result["direction"] == "SHORT" and bias == "BULLISH"):
            print(f"    ⛔ MTF conflict: 15m {result['direction']} vs 1h {bias}")
            continue

        # Gate 3: DXY
        dxy = _dxy_adj(asset, result["direction"])

        # Gate 4: ML
        ml_adj, ml_p = ml_score_adjustment(df, result["direction"])
        adjusted = result["score"] + dxy + ml_adj
        print(f"    adjusted score: {result['score']} +{dxy} DXY +{ml_adj} ML = {adjusted}")
        if adjusted < cfg["min_score"]:
            print(f"    ⛔ adjusted below threshold")
            continue
        result["score"] = adjusted
        result["factors"] = (result["factors"] +
                             [f"1h: {bias}", f"ML p(up)={ml_p:.0%}" if ml_adj else None,
                              f"DXY {dollar_bias()}" if dxy else None])
        result["factors"] = [f for f in result["factors"] if f][:6]

        # Correlation guard
        corr_blocked = False
        for group in [g for g in [{"EURUSD","GBPUSD"},{"SPX500","US100"},{"BTCUSD","ETHUSD"}]
                      if asset in g]:
            for prev_a, prev_d in [(f["asset"], f["direction"]) for f in fired]:
                if prev_a in group and prev_d == result["direction"]:
                    corr_blocked = True
                    break
        if corr_blocked:
            print(f"    ⛔ correlation duplicate — skipped")
            continue

        result["asset"] = asset
        result["timestamp"] = str(now)
        fired.append(result)
        state["last_signal"][asset] = str(now)
        print(f"    🎯 FIRE: {result['direction']} score={result['score']}/9 RR={result['rr']}")
        time.sleep(8)

    state_path.write_text(json.dumps(state, indent=2))
    return fired
