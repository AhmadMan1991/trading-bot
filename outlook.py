"""
Outlook — per-symbol 4H / Daily / Weekly structural analysis.

A deterministic Python port of the TradingView "Multi-Asset · 4H/Daily/Weekly
Outlook" indicator, run for EVERY symbol in OUTLOOK_SYMBOLS (not just gold) and
fed into Telegram + the dashboard. No LLM — every number comes from price.

Per symbol it produces:
  • structure trend on 4H / Daily / Weekly (EMA stack + swing read, via
    gold_engine.structure_bias)
  • reference levels: prior day H/L/C, prior week H/L, prior month H/L,
    current-week H/L, prior-4H H/L
  • a weighted BIAS vote — Weekly 3.0 · Daily 2.5 · EMA 2.0 · 4H 1.5 · BOS 1.5 ·
    Position-in-week 1.0, normalised to [-1, 1] (same weights as the indicator)
  • scenarios: nearest real level above/below price = bull/bear trigger, with
    the next levels as T1/T2/T3
  • volatility: average daily range and how much of it today has used
"""

import pandas as pd

from config import MARKETS, OUTLOOK_SYMBOLS
from data_feeds import fetch_intraday
from indicators import add_base
from gold_engine import structure_bias


# ── small deterministic helpers ──────────────────────────────────────────────
def _vote(bias: str) -> int:
    return 1 if bias == "BULLISH" else -1 if bias == "BEARISH" else 0


def _range_pos(price: float, lo, hi) -> float:
    if lo is None or hi is None or hi <= lo:
        return 50.0
    return max(0.0, min(100.0, (price - lo) / (hi - lo) * 100.0))


def _f(v):
    try:
        return None if v is None or pd.isna(v) else float(v)
    except Exception:
        return None


def _prev_month_hl(df_daily: pd.DataFrame):
    """Prior-month high/low by resampling the daily frame; None if the index
    isn't datetime or there isn't a completed prior month yet."""
    try:
        idx = pd.to_datetime(df_daily.index)
        m = df_daily.copy()
        m.index = idx
        g = m.resample("MS").agg({"high": "max", "low": "min"}).dropna()
        if len(g) >= 2:
            return _f(g["high"].iloc[-2]), _f(g["low"].iloc[-2])
    except Exception:
        pass
    return None, None


def _bos(df_daily: pd.DataFrame, lookback: int = 10) -> int:
    """Simple break-of-structure proxy: +1 if the last daily close took out the
    prior `lookback` swing high, -1 if it broke the prior swing low, else 0."""
    try:
        c = float(df_daily["close"].iloc[-1])
        prior = df_daily.iloc[-(lookback + 1):-1]
        if c > float(prior["high"].max()):
            return 1
        if c < float(prior["low"].min()):
            return -1
    except Exception:
        pass
    return 0


def _ema_vote(df_daily: pd.DataFrame) -> int:
    try:
        last = df_daily.iloc[-1]
        c, e_f, e_s = float(last["close"]), float(last["ema20"]), float(last["ema50"])
        if c > e_f > e_s:
            return 1
        if c < e_f < e_s:
            return -1
    except Exception:
        pass
    return 0


def _label(norm: float):
    if norm >= 0.45:
        return "BULLISH", "🐂"
    if norm >= 0.15:
        return "MILDLY BULLISH", "🐂"
    if norm <= -0.45:
        return "BEARISH", "🐻"
    if norm <= -0.15:
        return "MILDLY BEARISH", "🐻"
    return "NEUTRAL / RANGE", "⚖️"


def _trigger_pools(price, ups_src, downs_src):
    def _round(v):
        return round(float(v), 6)
    ups = sorted({_round(v) for v in ups_src if v is not None and v > price})
    downs = sorted({_round(v) for v in downs_src if v is not None and v < price}, reverse=True)
    return ups, downs


def compute_outlook(symbol: str) -> dict | None:
    """Full deterministic outlook for one symbol, or None if data is missing."""
    cfg = MARKETS.get(symbol, {})
    df_d = fetch_intraday(symbol, "1day", 300)
    if df_d is None or len(df_d) < 30:
        print(f"  ⚠ {symbol}: insufficient daily data for outlook")
        return None
    df_w = fetch_intraday(symbol, "1week", 200)
    df_4 = fetch_intraday(symbol, "4h", 200)

    df_d = add_base(df_d)
    df_w = add_base(df_w) if df_w is not None and len(df_w) >= 10 else None
    df_4 = add_base(df_4) if df_4 is not None and len(df_4) >= 30 else None

    price = _f(df_4["close"].iloc[-1]) if df_4 is not None else _f(df_d["close"].iloc[-1])

    d_trend = structure_bias(df_d)["bias"]
    w_trend = structure_bias(df_w)["bias"] if df_w is not None else "RANGING"
    h4_trend = structure_bias(df_4)["bias"] if df_4 is not None else "RANGING"

    # ── reference levels ──────────────────────────────────────────────
    pdh, pdl, pdc = _f(df_d["high"].iloc[-2]), _f(df_d["low"].iloc[-2]), _f(df_d["close"].iloc[-2])
    if df_w is not None:
        pwh, pwl = _f(df_w["high"].iloc[-2]), _f(df_w["low"].iloc[-2])
        w_high, w_low = _f(df_w["high"].iloc[-1]), _f(df_w["low"].iloc[-1])
    else:
        pwh = pwl = w_high = w_low = None
    pmh, pml = _prev_month_hl(df_d)
    h4_hi1 = _f(df_4["high"].iloc[-2]) if df_4 is not None else None
    h4_lo1 = _f(df_4["low"].iloc[-2]) if df_4 is not None else None

    # ── weighted bias vote (same weights as the indicator) ────────────
    v_w, v_d, v_h4 = _vote(w_trend), _vote(d_trend), _vote(h4_trend)
    v_ema = _ema_vote(df_d)
    v_bos = _bos(df_d)
    pos_pw = _range_pos(price, pwl, pwh)
    v_pos = 1 if pos_pw > 65 else -1 if pos_pw < 35 else 0

    score = v_w * 3.0 + v_d * 2.5 + v_ema * 2.0 + v_h4 * 1.5 + v_bos * 1.5 + v_pos * 1.0
    norm = round(score / 11.5, 3)
    label, icon = _label(norm)

    # ── scenarios: nearest real levels above / below ──────────────────
    ups, downs = _trigger_pools(price, [pdh, pwh, pmh, w_high, h4_hi1], [pdl, pwl, pml, w_low, h4_lo1])
    bull = {"trigger": ups[0] if ups else None, "targets": ups[1:4]}
    bear = {"trigger": downs[0] if downs else None, "targets": downs[1:4]}

    # ── volatility (ADR + how much today has used) ────────────────────
    rng = (df_d["high"] - df_d["low"]).astype(float)
    adr = _f(rng.tail(14).mean())
    today_rng = _f(rng.iloc[-1])
    used_pct = round(today_rng / adr * 100.0, 1) if adr and today_rng is not None and adr > 0 else None
    if used_pct is None:
        vol_note = "—"
    elif used_pct >= 90:
        vol_note = "Range largely spent — fading extensions is higher-probability."
    elif used_pct <= 60:
        vol_note = "Room left in the daily range for continuation."
    else:
        vol_note = "Roughly two-thirds of the average range used."

    return {
        "symbol": symbol,
        "name": cfg.get("name", symbol),
        "emoji": cfg.get("emoji", ""),
        "decimals": cfg.get("decimals", 2),
        "price": price,
        "bias": {"label": label, "icon": icon, "norm": norm, "score": round(score, 2)},
        "structure": {"h4": h4_trend, "daily": d_trend, "weekly": w_trend},
        "votes": {"weekly": v_w, "daily": v_d, "ema": v_ema, "h4": v_h4, "bos": v_bos, "position": v_pos},
        "levels": {
            "pdh": pdh, "pdl": pdl, "pdc": pdc, "pwh": pwh, "pwl": pwl,
            "pmh": pmh, "pml": pml, "week_high": w_high, "week_low": w_low,
            "h4_high": h4_hi1, "h4_low": h4_lo1, "pos_in_week_pct": round(pos_pw, 1),
        },
        "scenarios": {"bull": bull, "bear": bear},
        "volatility": {"adr": adr, "today_range": today_rng, "used_pct": used_pct, "note": vol_note},
        "timestamp": str(pd.Timestamp.now(tz="UTC")),
    }


def run_outlook(symbols: list | None = None) -> list[dict]:
    """Compute the outlook for every configured symbol (skips any with no data)."""
    symbols = symbols or OUTLOOK_SYMBOLS
    out = []
    for s in symbols:
        try:
            o = compute_outlook(s)
            if o:
                out.append(o)
        except Exception as e:
            print(f"  ⚠ outlook {s} failed: {e}")
    # strongest conviction first
    out.sort(key=lambda o: abs(o["bias"]["norm"]), reverse=True)
    return out
