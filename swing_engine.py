"""
LLM-powered swing engine.

Flow:
  1. Fetch 1h + 4h bars via TwelveData/OKX
  2. Compute base indicators + quant math
  3. Fetch COT index (official CFTC, 0-100) + CFTC spec_net percentile
  4. Build structured prompt → Ollama → parse trade plan
  5. Apply gates: COT conflict, news, session, cooldown
  6. Return fired signals list
"""

import json
import time
import requests
import pandas as pd
from pathlib import Path

from config import (OLLAMA_URL, OLLAMA_MODEL, OLLAMA_KEY,
                    MARKETS, COT_EXTREME_LONG, COT_EXTREME_SHORT)
from indicators import add_base, add_quant
from data_feeds  import (fetch_intraday, fetch_td, fetch_cftc_cot,
                          fetch_all_cot, news_blocked, dollar_bias)

DATA_ROOT = Path(__file__).parent / "data"
DATA_ROOT.mkdir(exist_ok=True)

_SWING_SYSTEM = """You are a professional swing trader. You receive multi-timeframe market data,
COT institutional positioning, and quant analytics. Produce a precise trade plan or abstain.

Rules:
- Only trade WITH multi-timeframe alignment (1h + 4h agree)
- COT contrarian: spec_net >75th %ile = crowded LONG = BEARISH signal; <25th = BULLISH
- Minimum R:R = 1.8 for a trade; otherwise NO_TRADE
- If COT conflicts with price action, state in reasoning and lean toward COT
- Stops at nearest significant structure (not ATR only)

Output ONLY JSON:
{
  "verdict": "LONG|SHORT|NO_TRADE",
  "confidence": 0.0,
  "entry": 0.0,
  "stop_loss": 0.0,
  "target_1": 0.0,
  "target_2": 0.0,
  "risk_reward": 0.0,
  "hold_hours": 0,
  "cot_bias": "BULLISH|BEARISH|NEUTRAL",
  "key_reasons": ["r1","r2","r3"],
  "reasoning": "max 100 words"
}"""


def _llm_swing(context: str) -> dict:
    for attempt in range(2):
        try:
            r = requests.post(OLLAMA_URL,
                              headers={"Authorization": f"Bearer {OLLAMA_KEY}",
                                       "Content-Type": "application/json"},
                              json={"model": OLLAMA_MODEL, "stream": False,
                                    "options": {"temperature": 0.15, "num_predict": 1600},
                                    "messages": [{"role": "system", "content": _SWING_SYSTEM},
                                                 {"role": "user",   "content": context}]},
                              timeout=120)
            if not r.ok:
                time.sleep(3); continue
            raw = r.json()["message"]["content"]
            raw = raw.replace("```json", "").replace("```", "").strip()
            s, e = raw.find("{"), raw.rfind("}")
            if s == -1 or e <= s:
                if attempt == 0: time.sleep(2); continue
                raise ValueError("no JSON")
            return json.loads(raw[s:e+1])
        except Exception:
            if attempt == 0: time.sleep(2)
    return {"verdict": "NO_TRADE", "confidence": 0.0,
            "key_reasons": ["parse error"], "reasoning": ""}


def _build_context(asset: str, df_1h: pd.DataFrame, df_4h: pd.DataFrame,
                   cot_iw: dict | None, cot_cftc: dict) -> str:
    cfg  = MARKETS[asset]; dec = cfg["decimals"]
    df1  = add_base(df_1h); q1 = add_quant(df1)
    df4  = add_base(df_4h); q4 = add_quant(df4)

    def ema_stack(df):
        last = df.iloc[-1]; c = last["close"]
        if last["ema20"] > last["ema50"] > last["ema200"]: return "BULLISH 20>50>200"
        if last["ema20"] < last["ema50"] < last["ema200"]: return "BEARISH 20<50<200"
        return "MIXED"

    l1 = df1.iloc[-1]; l4 = df4.iloc[-1]
    px = l1["close"]

    # ATR-based structure levels
    atr1 = float(l1["atr"]); atr4 = float(l4["atr"])
    hi40_1h = df_1h["high"].tail(40).max(); lo40_1h = df_1h["low"].tail(40).min()
    hi40_4h = df_4h["high"].tail(40).max(); lo40_4h = df_4h["low"].tail(40).min()

    cot_str = "N/A"
    if cot_iw:
        arrow = "↑" if cot_iw["change"] > 0 else "↓"
        cot_str = (f"IW COT Index: {cot_iw['cot_index']}/100 {arrow} "
                   f"({cot_iw['signal']}) net: {cot_iw['net']:,}")
    cftc_str = "N/A"
    if cot_cftc.get("spec_net") is not None:
        pct = int(cot_cftc["pct_rank_20w"] * 100)
        cftc_str = (f"CFTC spec net: {cot_cftc['spec_net']:,} "
                    f"({pct}th %ile 20w) | comm net: {cot_cftc.get('comm_net','?')}")

    return f"""Asset: {asset} ({cfg['emoji']}) @ {px:.{dec}f}

=== 1H TIMEFRAME ===
EMA stack: {ema_stack(df1)} | RSI: {l1['rsi']:.1f} | ADX: {l1['adx']:.0f}
ATR: {atr1:.{dec}f} | Range 40h: {lo40_1h:.{dec}f} – {hi40_1h:.{dec}f}
MACD hist: {l1['macd_hist']:+.{dec}f} | Stoch K: {l1['stoch_k']:.0f}
Hurst: {q1['hurst']:.2f} | VWAP-z: {q1['vwap_z']:+.2f} | Kaufman ER: {q1['kaufman']:.2f}
Squeeze: {'ON — energy loading' if q1['squeezed'] else 'off'}
BB: {l1['bb_lower']:.{dec}f} – {l1['bb_upper']:.{dec}f}

=== 4H TIMEFRAME ===
EMA stack: {ema_stack(df4)} | RSI: {l4['rsi']:.1f} | ADX: {l4['adx']:.0f}
ATR: {atr4:.{dec}f} | Range 40×4h: {lo40_4h:.{dec}f} – {hi40_4h:.{dec}f}
MACD hist: {l4['macd_hist']:+.{dec}f}
Hurst: {q4['hurst']:.2f} | VWAP-z: {q4['vwap_z']:+.2f} | OU half-life: {q4['ou_hl']:.1f}b

=== COT POSITIONING ===
{cot_str}
{cftc_str}

=== CONTEXT ===
Dollar: {dollar_bias()} | Asset class: {cfg['asset_class']}
Produce a swing trade plan for {asset}. Entry must be current price ±0.5 ATR.
Targets at 4h structure. Stops at 4h swing low/high."""


def run_swing_scan(assets: list | None = None) -> list:
    assets = assets or list(MARKETS.keys())
    state_path = DATA_ROOT / "swing_state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            pass
    state.setdefault("last_signal", {})

    now   = pd.Timestamp.now(tz="UTC")
    fired = []

    cot_map = fetch_all_cot()

    for asset in assets:
        cfg  = MARKETS[asset]
        wday = now.dayofweek
        if wday >= 5 and cfg["asset_class"] != "crypto":
            continue

        last_t = state["last_signal"].get(asset)
        if last_t and (now - pd.Timestamp(last_t)) < pd.Timedelta(hours=8):
            print(f"  ⏭ {asset}: swing cooldown")
            continue

        print(f"\n  {cfg['emoji']} {asset} swing scan")

        df_1h = fetch_intraday(asset, "1h",  200)
        df_4h = fetch_intraday(asset, "4h",  120)
        time.sleep(10)
        if df_1h is None or df_4h is None or len(df_1h) < 80 or len(df_4h) < 40:
            print(f"    ⚠ insufficient data")
            continue

        # News gate
        event = news_blocked(asset)
        if event:
            print(f"    📰 BLOCKED: {event}")
            continue

        cot_iw   = cot_map.get(asset)
        cot_cftc = fetch_cftc_cot(asset)
        time.sleep(5)

        ctx = _build_context(asset, df_1h, df_4h, cot_iw, cot_cftc)
        plan = _llm_swing(ctx)

        verdict = plan.get("verdict", "NO_TRADE")
        conf    = plan.get("confidence", 0.0)
        rr      = plan.get("risk_reward", 0.0)

        # COT conflict gate
        cot_signal = (cot_iw or {}).get("signal", "NEUTRAL")
        cot_ok = True
        if verdict == "LONG"  and cot_signal == "BEARISH": cot_ok = False
        if verdict == "SHORT" and cot_signal == "BULLISH": cot_ok = False
        if not cot_ok:
            print(f"    ⛔ COT conflict: LLM {verdict} vs COT {cot_signal}")
            continue

        if verdict in ("LONG", "SHORT") and conf >= 0.55 and rr >= 1.8:
            plan.update({"asset": asset, "timestamp": str(now),
                         "cot_iw": cot_iw, "cot_cftc": cot_cftc})
            fired.append(plan)
            state["last_signal"][asset] = str(now)
            print(f"    ✅ SWING {verdict} conf={conf:.0%} RR={rr:.1f}")
        else:
            print(f"    ⏸ No signal (verdict={verdict} conf={conf:.0%} RR={rr:.1f})")

        time.sleep(8)

    state_path.write_text(json.dumps(state, indent=2))
    return fired
