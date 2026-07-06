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
