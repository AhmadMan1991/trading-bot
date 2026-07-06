"""
Trading Council — 7 specialist agents debate → Chair verdict.

Agents (each computes deterministic toolkit data, then LLM votes):
  🧭 Trend        — EMA stack, ADX, pullback depth, RSI divergence
  🕯 PriceAction  — candlestick patterns, move efficiency, engulfing
  🏛 Institutional— liquidity pools, climax vol, OBOS, cum delta, key levels
  ⚗️ Quant        — Hurst, OU half-life, Shannon entropy, Kaufman ER, VWAP-z,
                     squeeze momentum, probability oscillator
  💰 SMC          — market structure, OB/FVG/IFVG, premium/discount, BOS/CHoCH
  🔍 Tracer       — PDH/PDL raids, ICT kill zones, Judas swing detection
  📈 Performance  — self-audit (grades last 8 verdicts), Kelly fraction, agent ranking

Signal fires when:
  - Chair confidence >= threshold (SCALP 0.65, SWING 0.68)
  - >= 4 of 7 agents lean same way
  - R:R >= 1.2 (scalp) or 1.8 (swing)
  - Cooldown respected (3h scalp, 8h swing)
"""

import json
import time
import requests
import pandas as pd
import numpy as np
from pathlib import Path

from config import (OLLAMA_URL, OLLAMA_MODEL, OLLAMA_KEY,
                    COUNCIL_ASSETS, COUNCIL_MIN_AGREE,
                    COUNCIL_COOLDOWN_H, COUNCIL_SCALP_MIN_CONF,
                    COUNCIL_SWING_MIN_CONF, COUNCIL_SCALP_MIN_RR,
                    COUNCIL_SWING_MIN_RR, MARKETS)
from indicators import add_base, add_quant
from data_feeds  import fetch_intraday, fetch_td

DATA_ROOT = Path(__file__).parent / "data"
DATA_ROOT.mkdir(exist_ok=True)


# ── LLM call ─────────────────────────────────────────────────────────────────

def _llm(system: str, user: str, max_tokens: int = 700, is_chair: bool = False) -> dict:
    for attempt in range(2):
        try:
            r = requests.post(OLLAMA_URL,
                              headers={"Authorization": f"Bearer {OLLAMA_KEY}",
                                       "Content-Type": "application/json"},
                              json={"model": OLLAMA_MODEL, "stream": False,
                                    "options": {"temperature": 0.2, "num_predict": max(max_tokens, 1600)},
                                    "messages": [{"role": "system", "content": system},
                                                 {"role": "user",   "content": user}]},
                              timeout=120)
            if not r.ok:
                print(f"      ↻ Ollama {r.status_code}")
                time.sleep(3)
                continue
            raw = r.json()["message"]["content"]
            raw = raw.replace("```json", "").replace("```", "").strip()
            s, e = raw.find("{"), raw.rfind("}")
            if s == -1 or e <= s:
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise ValueError("no JSON")
            return json.loads(raw[s:e+1])
        except Exception as ex:
            if attempt < 1:
                time.sleep(2)
    if is_chair:
        return {"verdict": "NO_TRADE", "confidence": 0.0,
                "key_factors": ["parse failed"], "reasoning": ""}
    return {"bias": "NEUTRAL", "confidence": 0.0,
            "key_points": ["parse failed"], "reasoning": ""}


# ── Toolkits ─────────────────────────────────────────────────────────────────

def _tk_trend(df: pd.DataFrame, dec: int) -> str:
    df = add_base(df)
    c = df["close"]
    stack = ("BULLISH 20>50>200"   if df["ema20"].iloc[-1] > df["ema50"].iloc[-1] > df["ema200"].iloc[-1]
             else "BEARISH 20<50<200" if df["ema20"].iloc[-1] < df["ema50"].iloc[-1] < df["ema200"].iloc[-1]
             else "MIXED")
    hi50, lo50 = df["high"].rolling(50).max().iloc[-1], df["low"].rolling(50).min().iloc[-1]
    pull = (hi50 - c.iloc[-1]) / (hi50 - lo50) * 100 if hi50 > lo50 else 50
    div = "none"
    if c.iloc[-1] <= c.tail(30).quantile(0.15) and df["rsi"].iloc[-1] > df["rsi"].tail(30).min() * 1.05:
        div = "BULLISH divergence (price lower, RSI higher)"
    elif c.iloc[-1] >= c.tail(30).quantile(0.85) and df["rsi"].iloc[-1] < df["rsi"].tail(30).max() * 0.95:
        div = "BEARISH divergence (price higher, RSI lower)"
    return (f"EMA stack: {stack} | price vs 200: {(c.iloc[-1]/df['ema200'].iloc[-1]-1)*100:+.2f}%\n"
            f"ADX: {df['adx'].iloc[-1]:.0f} | RSI: {df['rsi'].iloc[-1]:.1f}\n"
            f"Pullback: {pull:.0f}% of 50-bar range from high | RSI div: {div}")


def _tk_price_action(df: pd.DataFrame, dec: int) -> str:
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    rng  = (h - l).replace(0, float("nan"))
    bars = []
    for i in [-3, -2, -1]:
        col = "green" if c.iloc[i] >= o.iloc[i] else "red"
        b, r = body.iloc[i], rng.iloc[i]
        upw = h.iloc[i] - max(o.iloc[i], c.iloc[i])
        dnw = min(o.iloc[i], c.iloc[i]) - l.iloc[i]
        pats = []
        if r > 0 and b/r < 0.3 and dnw/r > 0.55: pats.append("hammer/bull-pin")
        if r > 0 and b/r < 0.3 and upw/r > 0.55: pats.append("shooting-star")
        if r > 0 and b/r < 0.12: pats.append("doji")
        if i >= -2:
            if c.iloc[i] > o.iloc[i-1] and o.iloc[i] < c.iloc[i-1] and col == "green" and c.iloc[i-1] < o.iloc[i-1]:
                pats.append("BULL ENGULF")
            if c.iloc[i] < o.iloc[i-1] and o.iloc[i] > c.iloc[i-1] and col == "red" and c.iloc[i-1] > o.iloc[i-1]:
                pats.append("BEAR ENGULF")
        bars.append(f"[{i}] {col} body {min(b/r*100,100) if r>0 else 0:.0f}%{' | '+', '.join(pats) if pats else ''}")
    net  = abs(c.iloc[-1] - c.iloc[-12])
    path = body.tail(12).sum()
    eff  = net / path * 100 if path > 0 else 0
    return "\n".join(bars) + f"\n12-bar efficiency: {eff:.0f}% ({'IMPULSIVE' if eff>55 else 'CORRECTIVE' if eff<30 else 'mixed'})"


def _tk_institutional(df: pd.DataFrame, dec: int) -> str:
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
    px = c.iloc[-1]
    tol = px * 0.0007
    highs = h.tail(120); lows = l.tail(120)
    eq_h = sorted({round(x, dec) for i, x in highs.items()
                   for j, y in highs.items() if i < j and abs(x-y) < tol and x > px})[:3]
    eq_l = sorted({round(x, dec) for i, x in lows.items()
                   for j, y in lows.items() if i < j and abs(x-y) < tol and x < px}, reverse=True)[:3]
    vol_ok = v.sum() > 0
    climax = "no volume"
    if vol_ok:
        vavg = v.rolling(20).mean()
        cx = [(str(df.index[i])[:16], v.iloc[i]/vavg.iloc[i])
              for i in range(-5, 0) if vavg.iloc[i] > 0 and v.iloc[i]/vavg.iloc[i] > 2.5]
        climax = "; ".join(f"{t} {r:.1f}x" for t, r in cx) or "none"
    delta = c.diff()
    rsi = 100 - 100/(1 + delta.clip(lower=0).ewm(com=13, adjust=False).mean() /
                     (-delta).clip(lower=0).ewm(com=13, adjust=False).mean().replace(0, float("nan")))
    stoch = (c - l.rolling(14).min()) / (h.rolling(14).max() - l.rolling(14).min()).replace(0, float("nan")) * 100
    obos = (rsi.iloc[-1] + stoch.iloc[-1]) / 2
    obos_t = ("EXTREME OVERSOLD" if obos < 20 else "oversold" if obos < 32
              else "EXTREME OVERBOUGHT" if obos > 80 else "overbought" if obos > 68 else "neutral")
    sign = (c >= o).astype(int) * 2 - 1
    flow = (v * sign) if vol_ok else ((c - o) / c * 10000)
    cum_now = flow.tail(24).sum(); cum_pr = flow.iloc[-48:-24].sum()
    return (f"Buyside liq (eq highs above): {eq_h or 'none'}\n"
            f"Sellside liq (eq lows below): {eq_l or 'none'}\n"
            f"Climax vol: {climax} | OBOS: {obos:.0f}/100 {obos_t}\n"
            f"Cum delta: {cum_now:+,.0f} vs prior 24h {cum_pr:+,.0f}")


def _tk_quant(df: pd.DataFrame, dec: int) -> str:
    df = add_base(df)
    q = add_quant(df)
    sq_str = (f"SQUEEZE ON — energy loading, mom {'+' if q['sq_mom']>0 else ''}{q['sq_mom']:.{dec}f}"
              if q["squeezed"] else "squeeze off")
    fib = q["fib"]
    fib_str = (f"retraced {fib['retr_pct']:.0f}% of {'up' if fib['leg_up'] else 'down'} leg — "
               f"{'AT ' + fib['near_fib'] + ' fib' if fib['at_fib'] else 'nearest fib ' + fib['near_fib']}")
    return (f"Squeeze: {sq_str}\n"
            f"Hurst: {q['hurst']:.2f} ({'TRENDING regime — ride moves' if q['hurst']>0.55 else 'MEAN-REVERTING regime — fade extremes' if q['hurst']<0.45 else 'random walk — no statistical edge'})\n"
            f"OU half-life: {q['ou_hl']:.1f} bars | Kaufman ER: {q['kaufman']:.2f} ({'clean directional move' if q['kaufman']>0.45 else 'choppy'}) | Entropy: {q['entropy']:.2f}\n"
            f"VWAP z-score: {q['vwap_z']:+.2f} ({'stretched ABOVE vwap' if q['vwap_z']>2 else 'stretched BELOW vwap' if q['vwap_z']<-2 else 'near fair value'}) "
            f"| Prob osc: {q['prob_osc']:.0f}th %ile | Vol impulse: {q['vol_imp']:.2f}x\n"
            f"200-bar z: {q['z_score']:+.2f} | Buying pressure: {q['buy_pct']:.0f}%\n"
            f"Fib confluence: {fib_str}")


def _tk_smc(df: pd.DataFrame, dec: int) -> str:
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    px = c.iloc[-1]
    sw_hi = [(df.index[i], h.iloc[i]) for i in range(2, len(df)-2)
             if h.iloc[i] == h.iloc[i-2:i+3].max()][-6:]
    sw_lo = [(df.index[i], l.iloc[i]) for i in range(2, len(df)-2)
             if l.iloc[i] == l.iloc[i-2:i+3].min()][-6:]
    structure = "unclear"
    if len(sw_hi) >= 2 and len(sw_lo) >= 2:
        hh = sw_hi[-1][1] > sw_hi[-2][1]; hl = sw_lo[-1][1] > sw_lo[-2][1]
        structure = ("BULLISH (HH+HL)" if hh and hl else
                     "BEARISH (LH+LL)" if not hh and not hl else "TRANSITION")
    bos = ""
    if sw_hi and px > sw_hi[-1][1]: bos = " | ⚡ BOS UP"
    if sw_lo and px < sw_lo[-1][1]: bos = " | ⚡ BOS DOWN"
    atr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1)\
            .max(axis=1).ewm(com=13, adjust=False).mean()
    rhi, rlo = h.tail(120).max(), l.tail(120).min()
    mid = (rhi + rlo) / 2
    zone = ("PREMIUM (sell)" if px > rlo + (rhi-rlo)*0.62
            else "DISCOUNT (buy)" if px < rlo + (rhi-rlo)*0.38 else "EQUILIBRIUM")
    return (f"Structure: {structure}{bos}\n"
            f"Range: {rlo:.{dec}f}–{rhi:.{dec}f} | mid: {mid:.{dec}f}\n"
            f"Zone: {zone} | price: {px:.{dec}f}")


def _tk_tracer(df: pd.DataFrame, dec: int) -> str:
    c, h, l = df["close"], df["high"], df["low"]
    pdh = h.iloc[-2]; pdl = l.iloc[-2]
    raid_hi = c.iloc[-1] > pdh
    raid_lo = c.iloc[-1] < pdl
    tz = pd.Timestamp.now(tz="UTC")
    london_open = 7 <= tz.hour < 9
    ny_open     = 13 <= tz.hour < 15
    kz = "London open" if london_open else "NY open" if ny_open else "regular session"
    hi_range = h.tail(20).max(); lo_range = l.tail(20).min()
    mid_range = (hi_range + lo_range) / 2
    price = c.iloc[-1]
    judas = ""
    if price > mid_range + (hi_range - mid_range) * 0.7:
        judas = "Potential Judas swing UP — price pushed high, watch for reversal"
    elif price < mid_range - (mid_range - lo_range) * 0.7:
        judas = "Potential Judas swing DOWN — price pushed low, watch for reversal"
    return (f"PDH: {pdh:.{dec}f} {'← RAIDED' if raid_hi else ''} | PDL: {pdl:.{dec}f} {'← RAIDED' if raid_lo else ''}\n"
            f"Kill zone: {kz}\n"
            f"Judas: {judas or 'none detected'}")


def _tk_performance(asset: str) -> str:
    log = DATA_ROOT / "council_trades_log.jsonl"
    if not log.exists():
        return "No prior verdicts for self-audit."
    try:
        records = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
        asset_r = [r for r in records if r.get("asset") == asset and r.get("outcome")]
        if not asset_r:
            return f"No resolved trades for {asset} yet."
        wins = sum(1 for r in asset_r if r.get("outcome") in ("TP1", "TP2"))
        total_r = sum(r.get("outcome_r", 0) for r in asset_r)
        wrate = wins / len(asset_r)
        kelly = max(0.4, min(1.3, wrate / (1 - wrate) - (1 - wrate) / wrate + 0.5))
        lines = [f"Past verdicts: {len(asset_r)} | Win rate: {wrate:.0%} | Total: {total_r:+.1f}R",
                 f"Kelly conviction: {kelly:.2f}x | Expectancy: {total_r/len(asset_r):+.2f}R/trade"]
        by_agent = {}
        for r in asset_r:
            for vs in r.get("votes_summary", []):
                a = vs["agent"]
                correct = (vs["bias"] == "BULLISH" and r.get("outcome") in ("TP1","TP2")) or \
                          (vs["bias"] == "BEARISH" and r.get("outcome") == "STOPPED")
                by_agent.setdefault(a, []).append(correct)
        for a, results in sorted(by_agent.items(), key=lambda x: -sum(x[1])/max(len(x[1]),1)):
            acc = sum(results) / len(results)
            lines.append(f"  {a}: {acc:.0%} ({len(results)} votes)")
        return "\n".join(lines)
    except Exception as e:
        return f"Performance audit error: {e}"


# ── Agent definitions ─────────────────────────────────────────────────────────

AGENTS = [
    ("🧭", "TrendAgent",         _tk_trend,
     "You are a trend and pullback specialist. Judge trend health, pullback quality, reversal risk."),
    ("🕯", "PriceActionAgent",   _tk_price_action,
     "You are a pure price-action reader. Judge who is winning and if the move is impulsive or corrective."),
    ("🏛", "InstitutionalAgent", _tk_institutional,
     "You are an institutional-flow analyst. Judge liquidity, climax volume, and cumulative delta implications."),
    ("⚗️", "QuantAgent",         _tk_quant,
     "You are a quantitative analyst. Judge squeeze state, statistical regime, volatility, and flow balance."),
    ("💰", "SMCAgent",           _tk_smc,
     "You are an SMC trader. Judge market structure (BOS/CHoCH), OBs/FVGs, and premium/discount zone."),
]

_VOTE_FORMAT = 'Output ONLY JSON: {"bias":"BULLISH|BEARISH|NEUTRAL","confidence":0.0,"key_points":["p1","p2"],"reasoning":"max 50 words"}'

_CHAIR_SCALP = """You are the Chair of a scalp council (15m timeframe).
Weigh 7 specialists: agreement=conviction, conflict=caution. Institutional+SMC evidence at key
levels outweighs momentum. This is SCALPING — stops at nearest 15m structure, targets at next
liquidity pool. R:R must be >=1.2 or NO_TRADE. If <4 agents agree, NO_TRADE.
Output ONLY JSON: {"verdict":"LONG|SHORT|NO_TRADE","confidence":0.0,"entry_zone":[0,0],"stop_loss":0,"target_1":0,"target_2":0,"risk_reward":0.0,"key_factors":["f1","f2","f3"],"reasoning":"max 80 words"}"""

_CHAIR_SWING = """You are the Chair of a swing council (1h timeframe).
Weigh 7 specialists. COT positioning + Quant regime carry more weight on swing.
Minimum R:R 1.8 or NO_TRADE. Holds 1-7 days — targets at weekly structure.
Output ONLY JSON: {"verdict":"LONG|SHORT|NO_TRADE","confidence":0.0,"entry_zone":[0,0],"stop_loss":0,"target_1":0,"target_2":0,"risk_reward":0.0,"key_factors":["f1","f2","f3"],"reasoning":"max 80 words"}"""


def _run_session(asset: str, interval: str, mode: str) -> dict:
    cfg = MARKETS[asset]; dec = cfg["decimals"]
    df  = fetch_intraday(asset, interval, 500)
    if df is None or len(df) < 100:
        raise RuntimeError(f"insufficient data for {asset}")
    df  = add_base(df)
    px  = df["close"].iloc[-1]
    print(f"\n  {cfg['emoji']} {asset} @ {px:.{dec}f} — council convening ({mode})")

    votes = []
    agents = AGENTS + [
        ("🔍", "TracerAgent", lambda d, dc: _tk_tracer(d, dc),
         "You are an ICT/SMC tracer. Judge PDH/PDL raids, kill zone timing, and Judas swing setups."),
        ("📈", "PerformAgent", lambda d, dc: _tk_performance(asset),
         "You are the performance auditor. Grade past verdicts, state Kelly conviction, rank agents by accuracy."),
    ]

    for emoji, name, toolkit_fn, persona in agents:
        evidence = toolkit_fn(df, dec)
        vote = _llm(persona + "\n" + _VOTE_FORMAT,
                    f"Asset: {asset} @ {px:.{dec}f} ({mode})\n\n{evidence}")
        vote["agent"] = name; vote["emoji"] = emoji
        icon = "🟢" if vote["bias"]=="BULLISH" else "🔴" if vote["bias"]=="BEARISH" else "⚪"
        print(f"    {icon} {emoji} {name:<18} {vote['bias']:<8} {vote.get('confidence',0):.0%}")
        votes.append(vote)
        time.sleep(1)

    bulls = sum(1 for v in votes if v["bias"] == "BULLISH")
    bears = sum(1 for v in votes if v["bias"] == "BEARISH")

    debate  = f"Asset: {asset} @ {px:.{dec}f}\nTally: {bulls} bull / {bears} bear\n\n"
    for v in votes:
        debate += (f"[{v['agent']}] {v['bias']} ({v.get('confidence',0):.0%}) "
                   f"— {v.get('reasoning','')[:100]}\n")

    chair_sys = _CHAIR_SCALP if mode == "scalp" else _CHAIR_SWING
    verdict = _llm(chair_sys, debate, max_tokens=1600, is_chair=True)
    verdict.update({"asset": asset, "price": px, "votes": votes,
                    "bulls": bulls, "bears": bears, "mode": mode,
                    "timestamp": str(pd.Timestamp.now(tz="UTC"))})
    print(f"    ⚖️  CHAIR: {verdict.get('verdict','?')} ({verdict.get('confidence',0):.0%})")
    return verdict


def run_council(assets: list | None = None, mode: str = "scalp") -> list:
    assets = assets or COUNCIL_ASSETS
    interval = "15min" if mode == "scalp" else "1h"
    min_conf = COUNCIL_SCALP_MIN_CONF if mode == "scalp" else COUNCIL_SWING_MIN_CONF
    min_rr   = COUNCIL_SCALP_MIN_RR   if mode == "scalp" else COUNCIL_SWING_MIN_RR

    state_path = DATA_ROOT / "council_state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            pass
    state.setdefault("last_signal", {})

    now     = pd.Timestamp.now(tz="UTC")
    fired   = []

    for asset in assets:
        last = state["last_signal"].get(f"{asset}_{mode}")
        cooldown_h = COUNCIL_COOLDOWN_H if mode == "scalp" else 8
        if last and (now - pd.Timestamp(last)) < pd.Timedelta(hours=cooldown_h):
            print(f"  ⏭ {asset} {mode}: cooldown")
            continue
        if now.dayofweek >= 5 and MARKETS[asset]["asset_class"] != "crypto":
            continue
        try:
            v = _run_session(asset, interval, mode)
            # log always
            rec = {k: x for k, x in v.items() if k != "votes"}
            rec["votes_summary"] = [{"agent": x["agent"], "bias": x["bias"],
                                     "confidence": x.get("confidence", 0)} for x in v["votes"]]
            with open(DATA_ROOT / "council_trades_log.jsonl", "a") as f:
                f.write(json.dumps(rec) + "\n")

            agree = max(v["bulls"], v["bears"])
            if (v.get("verdict") in ("LONG","SHORT")
                    and v.get("confidence", 0) >= min_conf
                    and agree >= COUNCIL_MIN_AGREE
                    and v.get("risk_reward", 0) >= min_rr):
                fired.append(v)
                state["last_signal"][f"{asset}_{mode}"] = str(now)
                print(f"    ✅ Signal: {v['verdict']} conf={v['confidence']:.0%} agree={agree}/7")
            else:
                print(f"    ⏸ No signal (conf={v.get('confidence',0):.0%}, agree={agree}/7)")
        except Exception as e:
            print(f"  ⚠ {asset}: {e}")
        time.sleep(10)

    state_path.write_text(json.dumps(state, indent=2))
    return fired
