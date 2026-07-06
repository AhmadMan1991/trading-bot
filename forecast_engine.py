"""
Forecast engine — Daily/Weekly bias with BS_OB_RJB_FVG pattern detection.

Layers:
  Weekly  → bias (COT + weekly structure) + weekly key levels
  Daily   → refine bias + daily OB/FVG/SB-FVG/RJB identification
  Chart   → dark matplotlib overlay: price + patterns + levels → PNG

Patterns detected (BS_OB_RJB_FVG):
  OB   — Order Block:  down-then-up structure (close above prior high)
  SB-FVG — Straddle FVG: FVG that straddles a structural pivot
  RJB1 — Rejection Bar (Type 1): large wick rejection off key level
  RJB2 — Rejection Bar (Type 2): inside-bar breakout rejection
  IFVG — Inverse FVG: previously filled gap acting as new support/resistance
"""

import io
import pandas as pd
import numpy as np
from pathlib import Path

from config  import MARKETS, COT_EXTREME_LONG, COT_EXTREME_SHORT
from indicators import add_base
from data_feeds  import fetch_yf, fetch_intraday, fetch_all_cot, fetch_cftc_cot

DATA_ROOT = Path(__file__).parent / "data"
DATA_ROOT.mkdir(exist_ok=True)


# ── Pattern detection ─────────────────────────────────────────────────────────

def detect_ob(df: pd.DataFrame) -> list[dict]:
    """Order Blocks: down bar followed by up bar that closes above prior high."""
    results = []
    for i in range(2, len(df) - 1):
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        if c.iloc[i-1] < o.iloc[i-1]:                       # prior bar bearish
            if c.iloc[i] > h.iloc[i-2]:                     # close above 2-bar-back high
                results.append({
                    "type": "OB_BULL",
                    "bar": df.index[i-1],
                    "top": float(h.iloc[i-1]),
                    "bot": float(l.iloc[i-1]),
                    "strength": float(abs(c.iloc[i] - h.iloc[i-2]) / df["atr"].iloc[i])
                })
        if c.iloc[i-1] > o.iloc[i-1]:                       # prior bar bullish
            if c.iloc[i] < l.iloc[i-2]:                     # close below 2-bar-back low
                results.append({
                    "type": "OB_BEAR",
                    "bar": df.index[i-1],
                    "top": float(h.iloc[i-1]),
                    "bot": float(l.iloc[i-1]),
                    "strength": float(abs(l.iloc[i-2] - c.iloc[i]) / df["atr"].iloc[i])
                })
    return results[-5:]  # most recent 5


def detect_fvg(df: pd.DataFrame) -> list[dict]:
    """Fair Value Gaps: 3-bar structure where body[i] doesn't overlap with body[i-2]."""
    results = []
    for i in range(2, len(df)):
        h, l = df["high"], df["low"]
        bull_fvg = l.iloc[i] > h.iloc[i-2]
        bear_fvg = h.iloc[i] < l.iloc[i-2]
        if bull_fvg:
            results.append({
                "type": "FVG_BULL",
                "bar": df.index[i],
                "top": float(l.iloc[i]),
                "bot": float(h.iloc[i-2]),
            })
        if bear_fvg:
            results.append({
                "type": "FVG_BEAR",
                "bar": df.index[i],
                "top": float(l.iloc[i-2]),
                "bot": float(h.iloc[i]),
            })
    return results[-5:]


def detect_sb_fvg(df: pd.DataFrame) -> list[dict]:
    """Straddle FVG: FVG that spans a swing high/low (pivot-straddle)."""
    fvgs = detect_fvg(df)
    pivots_hi = {df.index[i]: df["high"].iloc[i] for i in range(2, len(df)-2)
                 if df["high"].iloc[i] == df["high"].iloc[i-2:i+3].max()}
    pivots_lo = {df.index[i]: df["low"].iloc[i]  for i in range(2, len(df)-2)
                 if df["low"].iloc[i]  == df["low"].iloc[i-2:i+3].min()}
    results = []
    for fvg in fvgs:
        for _, ph in pivots_hi.items():
            if fvg["bot"] < ph < fvg["top"]:
                results.append({**fvg, "type": "SB_FVG_BULL_STRADDLE", "pivot": ph})
        for _, pl in pivots_lo.items():
            if fvg["bot"] < pl < fvg["top"]:
                results.append({**fvg, "type": "SB_FVG_BEAR_STRADDLE", "pivot": pl})
    return results[-3:]


def detect_rjb(df: pd.DataFrame) -> list[dict]:
    """
    RJB1: Large wick (>60% of range) rejecting off a S/R level.
    RJB2: Inside-bar breakout failure (false break trapped).
    """
    h, l, o, c = df["high"], df["low"], df["open"], df["close"]
    atr = df["atr"]
    results = []
    for i in range(1, len(df)):
        rng = h.iloc[i] - l.iloc[i]
        if rng == 0: continue
        up_wick  = h.iloc[i] - max(o.iloc[i], c.iloc[i])
        dn_wick  = min(o.iloc[i], c.iloc[i]) - l.iloc[i]
        sup  = df["support"].iloc[i];    res = df["resistance"].iloc[i]
        # RJB1 bearish
        if up_wick / rng > 0.6 and abs(h.iloc[i] - res) < atr.iloc[i] * 0.3:
            results.append({"type":"RJB1_BEAR","bar":df.index[i],
                             "level":float(res),"strength":up_wick/rng})
        # RJB1 bullish
        if dn_wick / rng > 0.6 and abs(l.iloc[i] - sup) < atr.iloc[i] * 0.3:
            results.append({"type":"RJB1_BULL","bar":df.index[i],
                             "level":float(sup),"strength":dn_wick/rng})
        # RJB2: inside-bar breakout failure
        if i >= 2:
            inside  = h.iloc[i-1] < h.iloc[i-2] and l.iloc[i-1] > l.iloc[i-2]
            br_bear = c.iloc[i-1] > h.iloc[i-2] and c.iloc[i] < h.iloc[i-2]
            br_bull = c.iloc[i-1] < l.iloc[i-2] and c.iloc[i] > l.iloc[i-2]
            if inside and br_bear:
                results.append({"type":"RJB2_BEAR","bar":df.index[i],"level":float(h.iloc[i-2])})
            if inside and br_bull:
                results.append({"type":"RJB2_BULL","bar":df.index[i],"level":float(l.iloc[i-2])})
    return results[-5:]


def detect_ifvg(df: pd.DataFrame) -> list[dict]:
    """Inverse FVG: a prior FVG that has been mitigated (price passed through it)."""
    fvgs  = detect_fvg(df)
    price = df["close"].iloc[-1]
    results = []
    for fvg in fvgs:
        mid = (fvg["top"] + fvg["bot"]) / 2
        if fvg["type"] == "FVG_BULL" and price < fvg["bot"]:
            results.append({**fvg, "type": "IFVG_BEAR", "mid": mid})
        if fvg["type"] == "FVG_BEAR" and price > fvg["top"]:
            results.append({**fvg, "type": "IFVG_BULL", "mid": mid})
    return results[-3:]


# ── Bias engine ───────────────────────────────────────────────────────────────

def _weekly_bias(df_w: pd.DataFrame, cot: dict | None) -> str:
    df  = add_base(df_w)
    c   = df["close"].iloc[-1]
    st  = ("BULLISH" if df["ema20"].iloc[-1] > df["ema50"].iloc[-1]
           else "BEARISH")
    if cot:
        idx = cot.get("cot_index", 50)
        if idx >= COT_EXTREME_LONG:
            st = "BEARISH"   # crowded longs = contrarian bearish
        elif idx <= COT_EXTREME_SHORT:
            st = "BULLISH"   # bearish extreme = contrarian bullish
    return st


def _daily_bias(df_d: pd.DataFrame) -> str:
    df = add_base(df_d)
    if df["ema20"].iloc[-1] > df["ema50"].iloc[-1] and df["rsi"].iloc[-1] > 50:
        return "BULLISH"
    if df["ema20"].iloc[-1] < df["ema50"].iloc[-1] and df["rsi"].iloc[-1] < 50:
        return "BEARISH"
    return "NEUTRAL"


def project_forecast(bias: str, price: float, key_levels: list, patterns: list) -> dict:
    """
    Deterministic forward-path projection (no extra LLM call): given the bias and the
    key levels / pattern zones already detected, pick the nearest level in the bias
    direction as the TARGET zone, and the nearest opposing structure as INVALIDATION.
    Mirrors the visual the standalone forecast_agent used (dashed path -> target band,
    invalid line) so every forecast chart shows a concrete "what happens next" picture.
    """
    direction = "UP" if "BULLISH" in (bias or "") else "DOWN" if "BEARISH" in (bias or "") else "NEUTRAL"
    levels = sorted(set(round(x, 6) for x in (key_levels or []) if x))
    if not levels or price is None or direction == "NEUTRAL":
        return {"direction": direction, "target_zone": [0, 0], "invalidation": 0}

    above = [lvl for lvl in levels if lvl > price]
    below = [lvl for lvl in levels if lvl < price]

    if direction == "UP":
        target = min(above) if above else price * 1.01
        invalidation = max(below) if below else price * 0.99
        target_zone = [price + (target - price) * 0.85, target]
    else:
        target = max(below) if below else price * 0.99
        invalidation = min(above) if above else price * 1.01
        target_zone = [target, price - (price - target) * 0.85]

    return {"direction": direction, "target_zone": target_zone, "invalidation": invalidation}


def compute_forecast(asset: str) -> dict:
    cfg = MARKETS[asset]; dec = cfg["decimals"]

    df_w = fetch_yf(asset, period="2y",  interval="1wk")
    df_d = fetch_yf(asset, period="1y",  interval="1d")
    df_1h = fetch_intraday(asset, "1h", 200)

    cot_map  = fetch_all_cot()
    cot_iw   = cot_map.get(asset)
    cot_cftc = fetch_cftc_cot(asset)

    result = {
        "asset": asset, "emoji": cfg["emoji"],
        "price": None,
        "weekly_bias": "N/A", "daily_bias": "N/A", "bias": "N/A",
        "cot_iw": cot_iw, "cot_cftc": cot_cftc,
        "patterns": [], "key_levels": [],
        "forecast_text": "",
    }

    if df_d is not None and len(df_d) > 50:
        df_da = add_base(df_d)
        result["price"]       = float(df_da["close"].iloc[-1])
        result["daily_bias"]  = _daily_bias(df_d)
        result["patterns"]   += detect_ob(df_da)
        result["patterns"]   += detect_fvg(df_da)
        result["patterns"]   += detect_rjb(df_da)
        result["patterns"]   += detect_sb_fvg(df_da)
        result["patterns"]   += detect_ifvg(df_da)
        result["key_levels"]  = [
            float(df_da["high"].tail(20).max()),
            float(df_da["low"].tail(20).min()),
            float(df_da["resistance"].iloc[-1]),
            float(df_da["support"].iloc[-1]),
        ]

    if df_w is not None and len(df_w) > 20:
        result["weekly_bias"] = _weekly_bias(df_w, cot_iw)

    wb, db = result["weekly_bias"], result["daily_bias"]
    if wb == db and wb != "N/A":
        result["bias"] = wb
    elif wb != "N/A":
        result["bias"] = f"{wb} (weekly) / {db} (daily)"
    else:
        result["bias"] = db

    # Summary text
    cot_line = ""
    if cot_iw:
        cot_line = f"COT {cot_iw['cot_index']}/100 ({cot_iw['signal']})"
    result["forecast_text"] = (
        f"{asset} {result['emoji']} — {result['bias']}\n"
        + (f"  {cot_line}\n" if cot_line else "")
        + f"  {len(result['patterns'])} pattern(s) detected on daily"
    )
    result["forecast"] = project_forecast(result.get("bias"), result.get("price"),
                                           result.get("key_levels"), result.get("patterns"))
    return result


# ── Chart rendering ───────────────────────────────────────────────────────────

def render_forecast_chart(fc: dict) -> bytes | None:
    """Render dark-theme chart: daily OHLC + EMA + detected patterns. Returns PNG bytes."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.lines import Line2D

        asset = fc["asset"]; cfg = MARKETS[asset]; dec = cfg["decimals"]
        df_d = fetch_yf(asset, period="6mo", interval="1d")
        if df_d is None or len(df_d) < 30:
            return None
        df = add_base(df_d).tail(120)
        fut = 14  # bars of blank projection space on the right for the forecast overlay

        fig, ax = plt.subplots(figsize=(16, 8))
        fig.patch.set_facecolor("#0d1117"); ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e"); ax.yaxis.label.set_color("#8b949e")
        for spine in ax.spines.values():
            spine.set_edgecolor("#21262d")

        c_bull, c_bear = "#26a641", "#f85149"
        xs = range(len(df))
        for i, (idx, row) in enumerate(df.iterrows()):
            col = c_bull if row["close"] >= row["open"] else c_bear
            ax.plot([i, i], [row["low"], row["high"]], color=col, linewidth=0.7)
            ax.add_patch(mpatches.FancyBboxPatch(
                (i - 0.3, min(row["open"], row["close"])),
                0.6, abs(row["close"] - row["open"]) + 1e-9,
                boxstyle="square,pad=0", linewidth=0, facecolor=col))

        ax.plot(xs, df["ema20"].values,  color="#58a6ff", linewidth=1,   label="EMA20")
        ax.plot(xs, df["ema50"].values,  color="#f0883e", linewidth=1,   label="EMA50")
        ax.plot(xs, df["ema200"].values, color="#bc8cff", linewidth=1.2, label="EMA200")

        # Pattern overlays
        date_to_xi = {d: i for i, d in enumerate(df.index)}
        PCOLORS = {
            "OB_BULL":"#26a641", "OB_BEAR":"#f85149",
            "FVG_BULL":"#1f6feb", "FVG_BEAR":"#db6d28",
            "RJB1_BULL":"#26a641","RJB1_BEAR":"#f85149",
            "RJB2_BULL":"#39d353","RJB2_BEAR":"#ff7b72",
            "SB_FVG_BULL_STRADDLE":"#79c0ff","SB_FVG_BEAR_STRADDLE":"#ffa657",
            "IFVG_BULL":"#56d364","IFVG_BEAR":"#ffa198",
        }
        for pat in fc.get("patterns", []):
            xi = date_to_xi.get(pat.get("bar"))
            if xi is None: continue
            col = PCOLORS.get(pat["type"], "#ffffff")
            top = pat.get("top", pat.get("level", 0))
            bot = pat.get("bot", top)
            if top and bot:
                ax.axhspan(bot, top, alpha=0.15, color=col, xmin=xi/len(df), xmax=1.0)
                ax.text(xi, (top+bot)/2, pat["type"][:8], color=col,
                        fontsize=6, ha="left", va="center")

        # Key levels
        for lvl in (fc.get("key_levels") or []):
            ax.axhline(lvl, color="#8b949e", linewidth=0.5, linestyle="--", alpha=0.5)

        # Forward path overlay: dashed line from last close into TARGET zone, INVALID line
        n = len(df)
        f = fc.get("forecast") or {}
        tz = f.get("target_zone", [0, 0])
        last_close = float(df["close"].iloc[-1])
        if tz and tz[0] and tz[1]:
            lo, hi = min(tz), max(tz)
            ax.add_patch(mpatches.FancyBboxPatch(
                (n + 2, lo), fut - 4, hi - lo,
                boxstyle="square,pad=0", linewidth=0, facecolor="#ffee58", alpha=0.28))
            ax.text(n + fut - 1.5, (lo + hi) / 2, "TARGET", color="#ffee58",
                    fontsize=8, va="center", ha="left", weight="bold")
            ax.plot([n - 1, n + 2 + (fut - 4) / 2], [last_close, (lo + hi) / 2],
                    "--", color="#ffee58", linewidth=1.6, zorder=4)
        inv = f.get("invalidation", 0)
        if inv:
            ax.plot([n - 8, n + fut - 2], [inv, inv], "--", color="#f85149", linewidth=1.2)
            ax.text(n + fut - 1.5, inv, "INVALID", color="#f85149", fontsize=7, va="center")

        dirn = f.get("direction", "?")
        ax.set_title(f"{asset} — {fc['bias']} · forecast {dirn}", color="#e6edf3", fontsize=13)
        ax.legend(facecolor="#161b22", edgecolor="#21262d",
                  labelcolor="#8b949e", fontsize=8, loc="upper left")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.{dec}f}"))
        step = max(n // 6, 1)
        ax.set_xticks(range(0, n, step))
        ax.set_xticklabels([df.index[i].strftime("%d %b") for i in range(0, n, step)],
                           color="#8b949e", fontsize=8)
        ax.set_xlim(-1, n + fut)
        plt.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        print(f"  [CHART] render failed: {e}")
        return None


def run_forecast(assets: list | None = None) -> list:
    assets = assets or list(MARKETS.keys())
    results = []
    for asset in assets:
        print(f"\n  {MARKETS[asset]['emoji']} {asset} forecast")
        try:
            fc  = compute_forecast(asset)
            img = render_forecast_chart(fc)
            fc["chart_png"] = img
            results.append(fc)
            print(f"    → {fc['bias']}")
        except Exception as e:
            print(f"    ⚠ {e}")
    return results
