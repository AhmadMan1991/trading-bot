"""
Professional dark-theme trading charts with price action, EMAs, RSI, and COT Index.
"""

import io
import warnings
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
import pandas as pd
import numpy as np
from config import EMA_FAST, EMA_MID, EMA_SLOW, COT_EXTREME_LONG, COT_EXTREME_SHORT

warnings.filterwarnings("ignore")

# ── Colour palette ────────────────────────────────────────────────────────────
BG      = "#0f1115"
PANEL   = "#171a21"
GREEN   = "#26a69a"
RED     = "#ef5350"
YELLOW  = "#ffd700"
BLUE    = "#5c9bd6"
PURPLE  = "#ba68c8"
GREY    = "#5a5f6e"
WHITE   = "#e6e6e6"
DIM     = "#9aa0ad"


def _style():
    plt.rcParams.update({
        "figure.facecolor":  BG,
        "axes.facecolor":    PANEL,
        "axes.edgecolor":    GREY,
        "axes.labelcolor":   DIM,
        "xtick.color":       DIM,
        "ytick.color":       DIM,
        "grid.color":        "#2a2f3a",
        "grid.linewidth":    0.5,
        "text.color":        WHITE,
        "font.size":         9,
        "axes.titlesize":    10,
        "axes.titlecolor":   WHITE,
    })


def _candlesticks(ax, df: pd.DataFrame):
    """Draw candlestick bars manually."""
    x    = np.arange(len(df))
    up   = df["close"] >= df["open"]
    down = ~up

    ax.bar(x[up],   df["close"][up]  - df["open"][up],  0.6,
           bottom=df["open"][up],  color=GREEN, zorder=2)
    ax.bar(x[up],   df["high"][up]   - df["close"][up], 0.1,
           bottom=df["close"][up], color=GREEN, zorder=2)
    ax.bar(x[up],   df["open"][up]   - df["low"][up],   0.1,
           bottom=df["low"][up],   color=GREEN, zorder=2)

    ax.bar(x[down], df["open"][down] - df["close"][down], 0.6,
           bottom=df["close"][down], color=RED, zorder=2)
    ax.bar(x[down], df["high"][down] - df["open"][down],  0.1,
           bottom=df["open"][down],  color=RED, zorder=2)
    ax.bar(x[down], df["close"][down]- df["low"][down],   0.1,
           bottom=df["low"][down],   color=RED, zorder=2)

    # x-axis labels every ~20 bars
    step = max(1, len(df) // 6)
    ticks = list(range(0, len(df), step))
    labels = [df.index[i].strftime("%b %d") for i in ticks]
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, rotation=0)
    ax.set_xlim(-1, len(df))
    ax.grid(True, axis="y")
    ax.grid(True, axis="x", alpha=0.3)


def generate_chart(market: str, df: pd.DataFrame, signal: dict,
                   cot_history: list | None = None) -> bytes:
    """
    Generate a professional analysis chart.
    Returns PNG bytes.
    """
    _style()

    has_cot = bool(cot_history and len(cot_history) >= 4)
    n_panels = 4 if has_cot else 3
    heights  = [3, 1, 1, 1] if has_cot else [3, 1, 1]

    fig = plt.figure(figsize=(14, 9), facecolor=BG)
    gs  = gridspec.GridSpec(n_panels, 1, hspace=0.05,
                            height_ratios=heights,
                            top=0.93, bottom=0.05, left=0.07, right=0.97)

    # ── Panel 0: Price + EMAs ─────────────────────────────────────────────────
    ax0 = fig.add_subplot(gs[0])
    _candlesticks(ax0, df)

    x = np.arange(len(df))
    ax0.plot(x, df[f"ema{EMA_FAST}"],  color=YELLOW, linewidth=1.0, label=f"EMA{EMA_FAST}")
    ax0.plot(x, df[f"ema{EMA_MID}"],   color=BLUE,   linewidth=1.2, label=f"EMA{EMA_MID}")
    ax0.plot(x, df[f"ema{EMA_SLOW}"],  color=PURPLE, linewidth=1.4, label=f"EMA{EMA_SLOW}")

    # Trade level lines (SL / TP) — normalize across scalp/swing/council signal shapes:
    #   scalp:   {entry, stop, tp1, tp2}
    #   swing:   {entry, stop_loss, target_1, target_2}
    #   council: {entry_zone:[lo,hi], stop_loss, target_1, target_2}
    plan = signal.get("plan") or {}
    if not plan:
        ez = signal.get("entry_zone")
        entry_v = (ez[0] if isinstance(ez, (list, tuple)) and ez else signal.get("entry"))
        plan = {
            "entry": entry_v,
            "sl":    signal.get("stop", signal.get("stop_loss")),
            "tp1":   signal.get("tp1",  signal.get("target_1")),
            "tp2":   signal.get("tp2",  signal.get("target_2")),
        }
    if plan.get("sl"):
        ax0.axhline(plan["sl"],  color=RED,   linestyle="--", linewidth=1, alpha=0.7, label="SL")
    if plan.get("tp1"):
        ax0.axhline(plan["tp1"], color=GREEN, linestyle="--", linewidth=1, alpha=0.7, label="TP1")
    if plan.get("tp2"):
        ax0.axhline(plan["tp2"], color=GREEN, linestyle=":",  linewidth=1, alpha=0.5, label="TP2")
    if plan.get("entry"):
        ax0.axhline(plan["entry"], color=WHITE, linestyle="-.", linewidth=0.8, alpha=0.6, label="Entry")

    leg = ax0.legend(loc="upper left", fontsize=7, framealpha=0.3,
                     facecolor=PANEL, edgecolor=GREY)
    for t in leg.get_texts():
        t.set_color(WHITE)

    direction = signal.get("direction", signal.get("verdict", ""))
    dir_color = GREEN if "LONG" in direction else RED if "SHORT" in direction else DIM
    _score = signal.get("score", signal.get("confidence"))
    if isinstance(_score, float) and _score <= 1:
        score_str = f"Confidence: {_score:.0%}"
    else:
        score_str = f"Score: {signal.get('score', 0):+d}/10" if isinstance(_score, int) else f"Score: {_score}"
    ax0.set_title(
        f"  {market}  ·  {direction or 'WATCH'}  ·  {score_str}  ·  {df.index[-1].date()}",
        loc="left", color=dir_color, fontsize=11, fontweight="bold"
    )
    ax0.set_xticklabels([])

    # ── Panel 1: Volume ───────────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[1], sharex=ax0)
    vol_colors = [GREEN if df["close"].iloc[i] >= df["open"].iloc[i] else RED
                  for i in range(len(df))]
    ax1.bar(x, df["volume"], color=vol_colors, alpha=0.7, width=0.6)
    ax1.set_ylabel("Vol", fontsize=8, color=DIM)
    ax1.set_xticklabels([])
    ax1.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, _: f"{v/1e6:.1f}M" if v >= 1e6 else f"{v/1e3:.0f}K")
    )
    ax1.grid(True, axis="y")

    # ── Panel 2: RSI ──────────────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[2], sharex=ax0)
    rsi = df["rsi"]
    ax2.plot(x, rsi, color=BLUE, linewidth=1.2)
    ax2.axhline(70, color=RED,   linestyle="--", linewidth=0.8, alpha=0.6)
    ax2.axhline(30, color=GREEN, linestyle="--", linewidth=0.8, alpha=0.6)
    ax2.axhline(50, color=GREY,  linestyle=":",  linewidth=0.6, alpha=0.4)
    ax2.fill_between(x, rsi, 70, where=(rsi > 70), color=RED,   alpha=0.15)
    ax2.fill_between(x, rsi, 30, where=(rsi < 30), color=GREEN, alpha=0.15)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("RSI", fontsize=8, color=DIM)
    ax2.set_yticks([30, 50, 70])
    ax2.grid(True, axis="y")
    if not has_cot:
        step = max(1, len(df) // 6)
        ticks = list(range(0, len(df), step))
        ax2.set_xticks(ticks)
        ax2.set_xticklabels([df.index[i].strftime("%b %d") for i in ticks])
    else:
        ax2.set_xticklabels([])

    # ── Panel 3: COT Index ────────────────────────────────────────────────────
    if has_cot:
        ax3 = fig.add_subplot(gs[3], sharex=ax0)
        nets   = [e["non_commercial"] for e in cot_history if e.get("non_commercial") is not None]
        mn, mx = min(nets), max(nets)
        indices = [
            round((n - mn) / (mx - mn) * 100) if mx != mn else 50
            for n in nets
        ]
        # Align COT (weekly) with daily bars — place last COT value at last bar
        cot_x = np.linspace(max(0, len(df) - len(indices)), len(df) - 1, len(indices))
        ax3.plot(cot_x, indices, color=YELLOW, linewidth=1.5)
        ax3.axhline(COT_EXTREME_LONG,  color=RED,   linestyle="--", linewidth=0.8, alpha=0.7)
        ax3.axhline(COT_EXTREME_SHORT, color=GREEN, linestyle="--", linewidth=0.8, alpha=0.7)
        ax3.fill_between(cot_x, indices, COT_EXTREME_LONG,
                         where=[v >= COT_EXTREME_LONG for v in indices],
                         color=RED,   alpha=0.15)
        ax3.fill_between(cot_x, indices, COT_EXTREME_SHORT,
                         where=[v <= COT_EXTREME_SHORT for v in indices],
                         color=GREEN, alpha=0.15)
        ax3.set_ylim(0, 100)
        ax3.set_ylabel("COT Idx", fontsize=8, color=DIM)
        ax3.set_yticks([25, 50, 75])
        ax3.grid(True, axis="y")

        step = max(1, len(df) // 6)
        ticks = list(range(0, len(df), step))
        ax3.set_xticks(ticks)
        ax3.set_xticklabels([df.index[i].strftime("%b %d") for i in ticks])

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _swing_pivots(df: pd.DataFrame, window: int = 8) -> tuple[list, list]:
    """Simple fractal-style pivot detector: a bar is a swing high/low if it's
    the max/min within +/- window bars either side. Only pivots with `window`
    bars of confirmation on both sides are returned, so the most recent
    `window` bars never produce one — there isn't enough data yet to know if
    they'll hold."""
    h, l = df["high"].values, df["low"].values
    n = len(df)
    highs, lows = [], []
    for i in range(window, n - window):
        seg_h = h[i - window:i + window + 1]
        seg_l = l[i - window:i + window + 1]
        if h[i] == seg_h.max():
            highs.append(i)
        if l[i] == seg_l.min():
            lows.append(i)
    return highs, lows


def _external_bos(df: pd.DataFrame, window: int = 8) -> dict:
    """Approximate 'external' break-of-structure: find the most recent
    CONFIRMED major swing pivot (a real fractal, not just a rolling extreme)
    and check whether the latest close has broken beyond it — that's the
    break that actually matters structurally, versus a minor internal
    pullback high/low. Returns {direction, level, idx} — direction is None
    if no break is currently in effect."""
    highs, lows = _swing_pivots(df, window)
    last_close = float(df["close"].iloc[-1])
    result = {"direction": None, "level": None, "idx": None}
    if highs:
        piv_i = highs[-1]
        piv_level = float(df["high"].iloc[piv_i])
        if last_close > piv_level:
            result = {"direction": "BULLISH", "level": piv_level, "idx": piv_i}
    if lows:
        piv_i = lows[-1]
        piv_level = float(df["low"].iloc[piv_i])
        if last_close < piv_level and (result["direction"] is None or piv_i > result["idx"]):
            result = {"direction": "BEARISH", "level": piv_level, "idx": piv_i}
    return result


def generate_scenario_chart(market: str, timeframe_label: str, df: pd.DataFrame, bias: dict,
                            display_bars: int = 100) -> bytes:
    """ICT/SMC structure snapshot for the dashboard scenarios (1H/4H/Daily/
    Weekly): thick internal support/resistance, external buyside/sellside
    liquidity, the most recent order block + fair value gap, an external
    break-of-structure marker, and the current price clearly tagged.
    Deliberately no EMAs / no indicator soup — just the price-action
    structure an ICT read actually uses.

    `df` should have add_base() run on the FULL fetched series already (this
    function only *displays* the last `display_bars` of it, so support/
    resistance/ATR-driven detections stay accurate near the left edge
    instead of being biased by a truncated window)."""
    from gold_engine import detect_order_blocks, detect_fvg

    _style()

    obs  = detect_order_blocks(df)
    fvgs = detect_fvg(df)
    bos  = _external_bos(df)

    internal_high = float(df["resistance"].iloc[-1]) if pd.notna(df["resistance"].iloc[-1]) else None
    internal_low  = float(df["support"].iloc[-1])     if pd.notna(df["support"].iloc[-1])     else None

    # External (buyside/sellside) liquidity — a longer lookback than the
    # internal range, representing the older highs/lows resting liquidity
    # sits beyond. Only drawn if meaningfully past the internal level, so it
    # doesn't just redraw the same line.
    ext_lookback  = min(len(df), display_bars * 3)
    external_high = float(df["high"].tail(ext_lookback).max())
    external_low  = float(df["low"].tail(ext_lookback).min())

    view = df.tail(display_bars).copy()
    x_of_ts = {ts: i for i, ts in enumerate(view.index)}
    last_close = float(view["close"].iloc[-1])
    n = len(view)

    fig = plt.figure(figsize=(12, 6.6), facecolor=BG)
    gs = gridspec.GridSpec(1, 1, top=0.91, bottom=0.09, left=0.06, right=0.85)
    ax0 = fig.add_subplot(gs[0])
    _candlesticks(ax0, view)

    # Label placement rules of thumb used throughout this function:
    #  - Support/Resistance labels sit at the LEFT edge — the right edge is
    #    reserved for the current-price tag, so anything else there gets
    #    crowded whenever price is trading near either level.
    #  - External liquidity (BSL/SSL) labels are pushed toward the *visible
    #    price action* rather than toward the axis edge (BSL text sits just
    #    below its line, SSL just above), since those lines usually sit
    #    outside the candle range by definition and the space between the
    #    external line and the nearest candle is otherwise empty.
    left_x = max(int(n * 0.015), 1)

    # ── Order block / FVG zones — most recent ONE each, only if in view ────
    # (showing 2 of each risked their labels overlapping into an unreadable
    # mess whenever they formed within a few bars of each other, which is
    # common during a strong trend leg — one clean zone beats two cluttered
    # ones.)
    for ob in obs[-1:]:
        ts = pd.Timestamp(ob["timestamp"])
        if ts not in x_of_ts:
            continue
        xi = x_of_ts[ts]
        color = GREEN if ob["direction"] == "BULLISH" else RED
        mid = (ob["low"] + ob["high"]) / 2
        ax0.axhspan(ob["low"], ob["high"], xmin=max(xi - 0.5, 0) / n, xmax=1.0,
                    color=color, alpha=0.10, zorder=1)
        ax0.text(xi, mid, " OB", fontsize=7, color=color, va="center", fontweight="bold", alpha=.9)

    for fvg in fvgs[-1:]:
        ts = pd.Timestamp(fvg["timestamp"])
        if ts not in x_of_ts:
            continue
        xi = x_of_ts[ts]
        color = BLUE if fvg["direction"] == "BULLISH" else PURPLE
        mid = (fvg["low"] + fvg["high"]) / 2
        ax0.axhspan(fvg["low"], fvg["high"], xmin=max(xi - 0.5, 0) / n, xmax=1.0,
                    color=color, alpha=0.14, zorder=1)
        ax0.text(xi, mid, " FVG", fontsize=7, color=color, va="center", fontweight="bold", alpha=.9)

    # ── Internal support / resistance — thick solid lines ──────────────────
    if internal_high is not None:
        ax0.axhline(internal_high, color=RED, linewidth=2.4, alpha=.85, zorder=3)
        ax0.text(left_x, internal_high, f"Resistance {internal_high:.2f}  ",
                 fontsize=8, color=RED, va="bottom", ha="left", fontweight="bold")
    if internal_low is not None:
        ax0.axhline(internal_low, color=GREEN, linewidth=2.4, alpha=.85, zorder=3)
        ax0.text(left_x, internal_low, f"Support {internal_low:.2f}  ",
                 fontsize=8, color=GREEN, va="top", ha="left", fontweight="bold")

    # ── External liquidity (buyside / sellside) — thick dashed lines ───────
    if internal_high is None or external_high > internal_high * 1.0005:
        ax0.axhline(external_high, color=YELLOW, linewidth=2.0, linestyle="--", alpha=.85, zorder=3)
        ax0.text(left_x, external_high, "BSL — external liquidity  ",
                 fontsize=7.5, color=YELLOW, va="top", ha="left", fontweight="bold")
    if internal_low is None or external_low < internal_low * 0.9995:
        ax0.axhline(external_low, color=YELLOW, linewidth=2.0, linestyle="--", alpha=.85, zorder=3)
        ax0.text(left_x, external_low, "SSL — external liquidity  ",
                 fontsize=7.5, color=YELLOW, va="bottom", ha="left", fontweight="bold")

    # ── External BOS marker ─────────────────────────────────────────────────
    if bos["direction"] and bos["idx"] is not None:
        piv_ts = df.index[bos["idx"]]
        color = GREEN if bos["direction"] == "BULLISH" else RED
        if piv_ts in x_of_ts:
            xi = x_of_ts[piv_ts]
            marker = "^" if bos["direction"] == "BULLISH" else "v"
            ax0.scatter([xi], [bos["level"]], marker=marker, color=color, s=90,
                        zorder=5, edgecolors=WHITE, linewidths=0.6)
            ax0.text(xi, bos["level"], "  BOS", fontsize=8.5, color=color, fontweight="bold")

    # ── Current price — clear tag on the right edge ─────────────────────────
    ax0.axhline(last_close, color=WHITE, linewidth=1.3, alpha=.9, zorder=4)
    ax0.annotate(f" {last_close:.2f} ", xy=(1, last_close), xycoords=("axes fraction", "data"),
                xytext=(4, 0), textcoords="offset points", va="center", ha="left",
                fontsize=9.5, fontweight="bold", color="#0d0f13",
                bbox=dict(boxstyle="round,pad=0.3", facecolor=WHITE, edgecolor="none"),
                annotation_clip=False)

    bias_label = bias.get("bias", "RANGING")
    dir_color = GREEN if bias_label == "BULLISH" else RED if bias_label == "BEARISH" else DIM
    bos_note = f"  ·  BOS: {bos['direction']}" if bos["direction"] else ""
    ax0.set_title(
        f"  {market}  ·  {timeframe_label}  ·  {bias_label}{bos_note}",
        loc="left", color=dir_color, fontsize=12, fontweight="bold"
    )

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
