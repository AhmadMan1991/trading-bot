"""
Gold Engine — one deterministic ICT/SMC-based engine for XAUUSD.

Replaces scalp_engine.py + swing_engine.py + council.py + forecast_engine.py
+ btc_deep_pipeline.py / deep_pipeline.py — five different scoring methods
running in parallel across nine assets, which produced exactly the kind of
cross-engine disagreement (and multi-agent-debate NO_TRADE deadlock) that was
making signals rare and inconsistent. This is ONE method, one asset, additive
confidence scoring instead of agent voting — no component can veto another,
so there's no deadlock possible.

Concepts used (session-based ICT/SMC — see README for sourcing):
  - Multi-timeframe structure: H4 bias -> H1 range -> M15 setup -> M5/M1 entry
    (scalp) or H1 setup -> H4 confirmation (swing)
  - Session gate: only hunts scalp setups during London/NY killzones (the
    real-liquidity windows); the first ~60min of a session is watched for a
    Judas Swing (fake move that reverses) rather than avoided, since that
    reversal often IS the entry
  - Liquidity sweep: price takes out a recent swing high/low then closes
    back inside — the core entry trigger
  - Order blocks / fair value gaps: confluence zones that make a sweep
    higher-confidence, not required on their own
  - COT positioning: additive confidence input, not a veto
  - ATR-based structural stop, fixed R:R targets, daily loss circuit breaker

The signal taxonomy from the original BTC pipeline (SNIPER, WYCKOFF_SPRING,
etc.) is kept and reused here — those labels are legitimately the same
liquidity-sweep/institutional concepts, just applied through one clean gold
engine instead of a separate heavy multi-LLM-vote pipeline.
"""

import json
import time
from pathlib import Path

import requests
import pandas as pd

from config import (
    MARKETS, OLLAMA_URL, OLLAMA_MODEL, OLLAMA_KEY, ACCOUNT_SIZE, RISK_PCT,
    GOLD_SESSIONS_UTC, GOLD_JUDAS_WINDOW_MIN, GOLD_IMPULSE_ATR_MULT,
    GOLD_SWEEP_LOOKBACK, GOLD_STRUCTURE_LOOKBACK, GOLD_ATR_STOP_BUFFER,
    GOLD_TP1_RR, GOLD_TP2_RR, GOLD_TP3_RR, GOLD_MIN_CONFIDENCE, GOLD_SCALP_COOLDOWN_MIN,
    GOLD_SWING_COOLDOWN_H, GOLD_DAILY_LOSS_LIMIT_PCT, GOLD_MAX_TRADES_PER_DAY,
)
from indicators import add_base
from data_feeds import fetch_intraday, fetch_all_cot, news_blocked, dollar_bias

# Multi-timeframe scenario snapshots for the dashboard (1H/4H/Daily/Weekly) —
# purely informational top-down context, doesn't feed the scalp/swing decision.
SCENARIO_TIMEFRAMES = [
    ("1h",   "1H",     200),
    ("4h",   "4H",     200),
    ("1day", "Daily",  250),
    ("1week","Weekly", 150),
]

DATA_ROOT = Path(__file__).parent / "data"
STATE_FILE = DATA_ROOT / "gold_engine_state.json"
RISK_STATE_FILE = DATA_ROOT / "gold_risk_state.json"

ASSET = "XAUUSD"

# Same taxonomy the project has used throughout — kept here since the file
# it originally lived in (btc_deep_pipeline.py) is being retired.
SIGNAL_TAXONOMY = {
    "SNIPER":              ("🎯", "SNIPER SETUP",         "High Probability Entry — Multi-Confluence"),
    "HIGH_PROBABILITY":    ("💠", "HIGH PROBABILITY",     "Multiple Factors Aligned Simultaneously"),
    "WYCKOFF_SPRING":      ("🌊", "WYCKOFF SPRING",       "Liquidity Swept Below Support — Reversal Up"),
    "LIQUIDITY_ABSORPTION":("💎", "LIQUIDITY ABSORPTION", "Liquidity Swept Above Resistance — Reversal Down"),
    "SWING_LONG":          ("📈", "SWING LONG",           "Multi-Day Uptrend — Hold 1-7 Days"),
    "SWING_SHORT":         ("📉", "SWING SHORT",          "Multi-Day Downtrend — Hold 1-7 Days"),
    "SCALP_LONG":          ("⚡", "SCALP LONG",           "Session Liquidity Sweep — Quick Move Up"),
    "SCALP_SHORT":         ("⚡", "SCALP SHORT",          "Session Liquidity Sweep — Quick Move Down"),
    "NO_SIGNAL":           ("⏸", "NO SIGNAL",            "No clear setup — stand aside"),
}


# =============================================================================
# STRUCTURE / SESSION / DETECTION
# =============================================================================

def structure_bias(df: pd.DataFrame, lookback: int = None) -> dict:
    """H4/H1 structure read: EMA stack + higher-highs/higher-lows vs
    lower-highs/lower-lows over the lookback window. Additive, not a veto —
    RANGING is a legitimate result, not a failure."""
    lookback = lookback or GOLD_STRUCTURE_LOOKBACK
    recent = df.tail(lookback)
    last = df.iloc[-1]
    reasons = []

    ema_bull = last["ema20"] > last["ema50"] > last["ema200"]
    ema_bear = last["ema20"] < last["ema50"] < last["ema200"]

    half = max(lookback // 2, 5)
    first_half, second_half = recent.iloc[:half], recent.iloc[half:]
    hh = second_half["high"].max() > first_half["high"].max()
    hl = second_half["low"].min()  > first_half["low"].min()
    lh = second_half["high"].max() < first_half["high"].max()
    ll = second_half["low"].min()  < first_half["low"].min()

    if ema_bull:
        reasons.append("EMA stack bullish (20>50>200)")
    if ema_bear:
        reasons.append("EMA stack bearish (20<50<200)")
    if hh and hl:
        reasons.append("Structure: higher-highs & higher-lows")
    if lh and ll:
        reasons.append("Structure: lower-highs & lower-lows")

    bull_votes = int(ema_bull) + int(hh and hl)
    bear_votes = int(ema_bear) + int(lh and ll)
    if bull_votes > bear_votes:
        bias = "BULLISH"
    elif bear_votes > bull_votes:
        bias = "BEARISH"
    else:
        bias = "RANGING"

    return {"bias": bias, "reasons": reasons}


def detect_order_blocks(df: pd.DataFrame, lookback: int = None) -> list[dict]:
    """Last opposing candle before an impulsive (>= GOLD_IMPULSE_ATR_MULT x
    ATR) move — a zone of institutional interest price often returns to."""
    lookback = lookback or GOLD_STRUCTURE_LOOKBACK
    recent = df.tail(lookback + 3)
    if len(recent) < 5 or "atr" not in recent.columns:
        return []
    atr = df["atr"].iloc[-1]
    if not atr or atr <= 0:
        return []

    blocks = []
    idx = recent.index
    for i in range(len(recent) - 2):
        bar, nxt = recent.iloc[i], recent.iloc[i + 1]
        impulse_range = abs(nxt["close"] - bar["close"])
        if impulse_range <= atr * GOLD_IMPULSE_ATR_MULT:
            continue
        move_up = nxt["close"] > bar["close"]
        bar_is_down = bar["close"] < bar["open"]
        bar_is_up = bar["close"] > bar["open"]
        if move_up and bar_is_down:
            blocks.append({"direction": "BULLISH", "low": float(bar["low"]),
                            "high": float(bar["high"]), "timestamp": str(idx[i])})
        elif not move_up and bar_is_up:
            blocks.append({"direction": "BEARISH", "low": float(bar["low"]),
                            "high": float(bar["high"]), "timestamp": str(idx[i])})
    return blocks[-5:]


def detect_fvg(df: pd.DataFrame, lookback: int = None) -> list[dict]:
    """3-candle fair value gap: candle 1's wick doesn't overlap candle 3's —
    an imbalance price tends to revisit before continuing."""
    lookback = lookback or GOLD_STRUCTURE_LOOKBACK
    recent = df.tail(lookback + 2)
    if len(recent) < 4:
        return []
    gaps = []
    idx = recent.index
    for i in range(1, len(recent) - 1):
        c1, c3 = recent.iloc[i - 1], recent.iloc[i + 1]
        if c1["high"] < c3["low"]:
            gaps.append({"direction": "BULLISH", "low": float(c1["high"]),
                         "high": float(c3["low"]), "timestamp": str(idx[i])})
        elif c1["low"] > c3["high"]:
            gaps.append({"direction": "BEARISH", "low": float(c3["high"]),
                         "high": float(c1["low"]), "timestamp": str(idx[i])})
    return gaps[-5:]


def detect_liquidity_sweep(df: pd.DataFrame, lookback: int = None) -> dict | None:
    """The core entry trigger: price takes out a recent swing high/low then
    closes back inside it within the next bar or two — a stop-hunt reversal,
    not a genuine breakout."""
    lookback = lookback or GOLD_SWEEP_LOOKBACK
    if len(df) < lookback + 3:
        return None
    # Reference window excludes the last TWO bars (prev + last) — otherwise
    # the sweep bar itself would leak into its own reference level, making
    # the "swept beyond" check always fail (level == itself, not exceeded).
    window = df.iloc[-(lookback + 3):-2]
    ref_high, ref_low = window["high"].max(), window["low"].min()
    last, prev = df.iloc[-1], df.iloc[-2]

    if prev["high"] > ref_high and last["close"] < ref_high:
        return {"direction": "BEARISH", "swept_level": float(ref_high)}
    if prev["low"] < ref_low and last["close"] > ref_low:
        return {"direction": "BULLISH", "swept_level": float(ref_low)}
    return None


def _zone_confluence(price: float, zones: list[dict], direction: str) -> bool:
    """Is price sitting inside any zone (order block / FVG) matching direction?"""
    for z in zones:
        if z["direction"] != direction:
            continue
        if z["low"] <= price <= z["high"]:
            return True
    return False


def current_session(now: pd.Timestamp | None = None) -> dict:
    now = now or pd.Timestamp.now(tz="UTC")
    hour = now.hour
    for start, end, name in GOLD_SESSIONS_UTC:
        if start <= hour < end:
            minutes_since_open = (hour - start) * 60 + now.minute
            return {"in_session": True, "name": name,
                    "judas_watch": minutes_since_open <= GOLD_JUDAS_WINDOW_MIN}
    return {"in_session": False, "name": None, "judas_watch": False}


# =============================================================================
# RISK STATE — daily loss circuit breaker + trade cap
# =============================================================================

def _load_risk_state() -> dict:
    today = str(pd.Timestamp.now(tz="UTC").date())
    state = {}
    if RISK_STATE_FILE.exists():
        try:
            state = json.loads(RISK_STATE_FILE.read_text())
        except Exception:
            state = {}
    if state.get("date") != today:
        state = {"date": today, "trades_today": 0, "daily_loss_usd": 0.0,
                 "equity": state.get("equity", ACCOUNT_SIZE)}
    state.setdefault("equity", ACCOUNT_SIZE)
    return state


def _save_risk_state(state: dict) -> None:
    DATA_ROOT.mkdir(exist_ok=True)
    RISK_STATE_FILE.write_text(json.dumps(state, indent=2))


def _risk_gate_ok(state: dict) -> tuple[bool, str]:
    if state["trades_today"] >= GOLD_MAX_TRADES_PER_DAY:
        return False, f"Daily trade cap reached ({GOLD_MAX_TRADES_PER_DAY}/day)"
    loss_limit = state["equity"] * GOLD_DAILY_LOSS_LIMIT_PCT
    if state["daily_loss_usd"] >= loss_limit:
        return False, f"Daily loss limit reached (${state['daily_loss_usd']:.2f} of ${loss_limit:.2f})"
    return True, ""


def update_risk_state_from_resolved(resolved: list[dict]) -> None:
    """Called by performance_tracker after resolving trades — feeds real
    win/loss outcomes into the daily-loss circuit breaker."""
    gold_resolved = [r for r in resolved if r.get("asset") == ASSET]
    if not gold_resolved:
        return
    state = _load_risk_state()
    for r in gold_resolved:
        r_mult = r.get("outcome_r", 0.0)
        pnl_usd = r_mult * state["equity"] * RISK_PCT
        state["equity"] += pnl_usd
        if pnl_usd < 0:
            state["daily_loss_usd"] += abs(pnl_usd)
    _save_risk_state(state)


# =============================================================================
# COOLDOWN STATE
# =============================================================================

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_scalp": None, "last_swing": None}


def _save_state(state: dict) -> None:
    DATA_ROOT.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# =============================================================================
# OPTIONAL REASONING TEXT (not part of the decision — decision is deterministic)
# =============================================================================

_REASONING_SYSTEM = """You are a gold (XAUUSD) trading desk analyst. Given a
list of factual factors behind a signal that has ALREADY been decided
(you are not making the call, only explaining it), write a tight 2-3 sentence
professional explanation. Plain text, no markdown, no hedging about whether
to take the trade — that decision is already made."""


def _llm_reasoning(direction: str, factors: list[str]) -> str:
    fallback = f"{direction} — " + "; ".join(factors)
    if not OLLAMA_KEY:
        return fallback
    try:
        r = requests.post(
            OLLAMA_URL,
            headers={"Authorization": f"Bearer {OLLAMA_KEY}", "Content-Type": "application/json"},
            json={"model": OLLAMA_MODEL, "stream": False,
                  "options": {"temperature": 0.3, "num_predict": 200},
                  "messages": [{"role": "system", "content": _REASONING_SYSTEM},
                               {"role": "user", "content": f"Direction: {direction}\nFactors: " + "; ".join(factors)}]},
            timeout=45,
        )
        if r.ok:
            text = r.json()["message"]["content"].strip()
            if text:
                return text
    except Exception as e:
        print(f"    ⚠ reasoning LLM call failed, using fallback text: {e}")
    return fallback


# =============================================================================
# SETUP EVALUATION — additive confluence, no voting/veto
# =============================================================================

def evaluate_setup(df_ltf: pd.DataFrame, df_htf: pd.DataFrame, cot: dict | None,
                   session: dict, timeframe: str, skip_reasoning: bool = False) -> dict:
    """Core decision function. Additive confidence score from independent
    factors — no factor can veto another, so disagreement lowers confidence
    rather than forcing NO_TRADE outright.

    skip_reasoning=True skips the optional LLM explanation call — used by
    backtest.py, which evaluates thousands of historical bars and doesn't
    use the reasoning text at all, so there's no reason to pay for (or wait
    on) an LLM call per candidate bar."""
    sweep = detect_liquidity_sweep(df_ltf)
    if sweep is None:
        return {"direction": "NEUTRAL", "signal_label": "NO_SIGNAL", "confidence": 0.0,
                "factors": ["No liquidity sweep detected this bar"]}

    direction = sweep["direction"]
    price = float(df_ltf.iloc[-1]["close"])
    atr = float(df_ltf.iloc[-1]["atr"])
    factors = [f"Liquidity sweep of {sweep['swept_level']:.2f} ({direction.lower()} reversal)"]
    confidence = 0.35

    bias = structure_bias(df_htf)
    if bias["bias"] == direction:
        confidence += 0.15
        factors.append(f"Higher-timeframe structure agrees ({bias['bias']})")
    elif bias["bias"] == "RANGING":
        factors.append("Higher-timeframe structure is ranging — no conflict")
    else:
        confidence -= 0.05
        factors.append(f"Higher-timeframe structure disagrees ({bias['bias']}) — reduced confidence")

    obs = detect_order_blocks(df_ltf)
    fvgs = detect_fvg(df_ltf)
    if _zone_confluence(price, obs, direction):
        confidence += 0.20
        factors.append("Price sitting in a matching order block")
    if _zone_confluence(price, fvgs, direction):
        confidence += 0.15
        factors.append("Price sitting in a matching fair value gap")

    cot_signal = (cot or {}).get("signal", "NEUTRAL")
    if (direction == "BULLISH" and cot_signal == "BULLISH") or \
       (direction == "BEARISH" and cot_signal == "BEARISH"):
        confidence += 0.15
        factors.append(f"COT positioning agrees ({cot_signal})")
    elif cot_signal not in ("NEUTRAL", ""):
        factors.append(f"COT positioning neutral/mixed vs setup ({cot_signal}) — no penalty, not a veto")

    last = df_ltf.iloc[-1]
    bull_pin = bool(last.get("bull_pin", False))
    bear_pin = bool(last.get("bear_pin", False))
    if (direction == "BULLISH" and bull_pin) or (direction == "BEARISH" and bear_pin):
        confidence += 0.10
        factors.append("Rejection candle confirms the sweep")

    if session.get("judas_watch"):
        factors.append(f"Inside Judas Swing window ({session.get('name')}) — early-session sweep")

    confidence = max(0.0, min(1.0, confidence))
    trade_direction = "LONG" if direction == "BULLISH" else "SHORT"

    if trade_direction == "LONG":
        stop = sweep["swept_level"] - atr * GOLD_ATR_STOP_BUFFER
    else:
        stop = sweep["swept_level"] + atr * GOLD_ATR_STOP_BUFFER
    risk = abs(price - stop)
    if risk <= 0:
        return {"direction": "NEUTRAL", "signal_label": "NO_SIGNAL", "confidence": 0.0,
                "factors": ["Invalid stop distance"]}

    if trade_direction == "LONG":
        tp1, tp2, tp3 = price + risk * GOLD_TP1_RR, price + risk * GOLD_TP2_RR, price + risk * GOLD_TP3_RR
    else:
        tp1, tp2, tp3 = price - risk * GOLD_TP1_RR, price - risk * GOLD_TP2_RR, price - risk * GOLD_TP3_RR

    if confidence >= 0.85:
        label = "SNIPER"
    elif confidence >= 0.70:
        label = "HIGH_PROBABILITY"
    elif _zone_confluence(price, obs, direction) or _zone_confluence(price, fvgs, direction):
        label = "WYCKOFF_SPRING" if trade_direction == "LONG" else "LIQUIDITY_ABSORPTION"
    else:
        label = (f"{timeframe}_LONG" if trade_direction == "LONG" else f"{timeframe}_SHORT")

    return {
        "direction": trade_direction, "signal_label": label, "confidence": round(confidence, 2),
        "entry": round(price, 2), "stop_loss": round(stop, 2),
        "target_1": round(tp1, 2), "target_2": round(tp2, 2), "target_3": round(tp3, 2),
        "risk_reward": GOLD_TP1_RR, "factors": factors,
        "reasoning": "" if skip_reasoning else _llm_reasoning(trade_direction, factors),
    }


# =============================================================================
# ENTRY POINTS
# =============================================================================

def run_gold_scenarios() -> dict:
    """Multi-timeframe structural snapshot for the dashboard: 1H, 4H, Daily,
    and Weekly bias, EMA stack, and nearest support/resistance for each —
    plus a chart per timeframe. Purely informational top-down context; it
    doesn't feed the scalp/swing decision (that stays H4-for-bias per
    run_gold_bias(), same as before). Chart generation happens here (not
    left to main.py) since each timeframe needs its own fetch+indicators
    pass anyway."""
    from charts import generate_scenario_chart

    scenarios = {}
    for interval, label, bars in SCENARIO_TIMEFRAMES:
        df = fetch_intraday(ASSET, interval, bars)
        if df is None or len(df) < 60:
            scenarios[label] = {"bias": "UNKNOWN", "reasons": ["insufficient data"],
                                 "timeframe": label, "chart_png": None}
            print(f"  🥇 Scenario {label}: ⚠ insufficient data")
            continue
        df = add_base(df)
        bias = structure_bias(df)
        last = df.iloc[-1]
        bias.update({
            "timeframe": label,
            "price":      round(float(last["close"]), 2),
            "ema20":      round(float(last["ema20"]), 2),
            "ema50":      round(float(last["ema50"]), 2),
            "ema200":     round(float(last["ema200"]), 2),
            "support":    round(float(last["support"]), 2) if pd.notna(last.get("support")) else None,
            "resistance": round(float(last["resistance"]), 2) if pd.notna(last.get("resistance")) else None,
            "updated_at": str(pd.Timestamp.now(tz="UTC")),
        })
        try:
            bias["chart_png"] = generate_scenario_chart(ASSET, label, df, bias)
        except Exception as e:
            print(f"  🥇 Scenario {label}: ⚠ chart failed: {e}")
            bias["chart_png"] = None
        print(f"  🥇 Scenario {label}: {bias['bias']}  ({bias['price']})")
        scenarios[label] = bias
    return scenarios


def run_gold_bias() -> dict:
    """H4 structure + COT — standalone context, also feeds scalp/swing."""
    df_h4 = fetch_intraday(ASSET, "4h", 150)
    if df_h4 is None or len(df_h4) < 60:
        return {"bias": "UNKNOWN", "reasons": ["insufficient H4 data"]}
    df_h4 = add_base(df_h4)
    bias = structure_bias(df_h4)
    cot_map = fetch_all_cot()
    cot = cot_map.get(ASSET)
    bias["cot"] = cot
    bias["dollar_bias"] = dollar_bias()
    print(f"  🥇 Gold bias: {bias['bias']}  |  COT: {(cot or {}).get('signal', 'N/A')}  "
          f"|  USD: {bias['dollar_bias']}")
    return bias


def run_gold_scalp(cot: dict | None = None) -> list[dict]:
    """M15 structure + M5 entry trigger. Session-gated (London/NY killzones
    only), cooldown-gated, daily-loss/trade-cap gated."""
    session = current_session()
    if not session["in_session"]:
        print(f"  🥇 Gold scalp: outside session windows, skipping")
        return []

    risk_state = _load_risk_state()
    ok, reason = _risk_gate_ok(risk_state)
    if not ok:
        print(f"  🥇 Gold scalp: ⛔ {reason}")
        return []

    state = _load_state()
    now = pd.Timestamp.now(tz="UTC")
    last_t = state.get("last_scalp")
    if last_t and (now - pd.Timestamp(last_t)) < pd.Timedelta(minutes=GOLD_SCALP_COOLDOWN_MIN):
        print(f"  🥇 Gold scalp: cooldown active")
        return []

    event = news_blocked(ASSET)
    if event:
        print(f"  🥇 Gold scalp: 📰 blocked by news — {event}")
        return []

    df_m15 = fetch_intraday(ASSET, "15min", 200)
    df_h1 = fetch_intraday(ASSET, "1h", 150)
    if df_m15 is None or df_h1 is None or len(df_m15) < 60 or len(df_h1) < 60:
        print(f"  🥇 Gold scalp: ⚠ insufficient data")
        return []

    df_m15, df_h1 = add_base(df_m15), add_base(df_h1)
    if cot is None:
        cot = fetch_all_cot().get(ASSET)

    result = evaluate_setup(df_m15, df_h1, cot, session, timeframe="SCALP")
    print(f"  🥇 Gold scalp: {result['signal_label']} conf={result['confidence']:.0%} "
          f"({session['name']})")

    if result["direction"] == "NEUTRAL" or result["confidence"] < GOLD_MIN_CONFIDENCE:
        return []

    result.update({"asset": ASSET, "timestamp": str(now), "layer": "gold_scalp",
                    "timeframe": "15m/5m", "session": session["name"]})
    state["last_scalp"] = str(now)
    _save_state(state)
    risk_state["trades_today"] += 1
    _save_risk_state(risk_state)
    return [result]


def run_gold_swing(cot: dict | None = None) -> list[dict]:
    """H1 structure + H4 confirmation. Not session-gated (swing holds
    through sessions), longer cooldown, still risk-gated."""
    risk_state = _load_risk_state()
    ok, reason = _risk_gate_ok(risk_state)
    if not ok:
        print(f"  🥇 Gold swing: ⛔ {reason}")
        return []

    state = _load_state()
    now = pd.Timestamp.now(tz="UTC")
    last_t = state.get("last_swing")
    if last_t and (now - pd.Timestamp(last_t)) < pd.Timedelta(hours=GOLD_SWING_COOLDOWN_H):
        print(f"  🥇 Gold swing: cooldown active")
        return []

    event = news_blocked(ASSET)
    if event:
        print(f"  🥇 Gold swing: 📰 blocked by news — {event}")
        return []

    df_h1 = fetch_intraday(ASSET, "1h", 200)
    df_h4 = fetch_intraday(ASSET, "4h", 150)
    if df_h1 is None or df_h4 is None or len(df_h1) < 60 or len(df_h4) < 60:
        print(f"  🥇 Gold swing: ⚠ insufficient data")
        return []

    df_h1, df_h4 = add_base(df_h1), add_base(df_h4)
    if cot is None:
        cot = fetch_all_cot().get(ASSET)
    session = current_session()

    result = evaluate_setup(df_h1, df_h4, cot, session, timeframe="SWING")
    print(f"  🥇 Gold swing: {result['signal_label']} conf={result['confidence']:.0%}")

    if result["direction"] == "NEUTRAL" or result["confidence"] < GOLD_MIN_CONFIDENCE:
        return []

    result.update({"asset": ASSET, "timestamp": str(now), "layer": "gold_swing",
                   "timeframe": "1h/4h", "session": session["name"] or "any"})
    state["last_swing"] = str(now)
    _save_state(state)
    risk_state["trades_today"] += 1
    _save_risk_state(risk_state)
    return [result]
