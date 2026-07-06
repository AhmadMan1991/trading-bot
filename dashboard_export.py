"""
Dashboard data export — keeps docs/dashboard.json and docs/charts/*.png up to
date so the local dashboard (see /dashboard in the project root) always has
fresh signals, forecasts, and performance stats to render.

Called from main.py every time a layer produces a signal/forecast/performance
update. Cheap (no extra network calls) — reuses chart bytes already generated
for Telegram.
"""

import json
from pathlib import Path
import pandas as pd

DOCS_ROOT   = Path(__file__).parent / "docs"
CHARTS_ROOT = DOCS_ROOT / "charts"
DASH_FILE   = DOCS_ROOT / "dashboard.json"

MAX_SIGNALS   = 40
MAX_FORECASTS = 12


def _load() -> dict:
    if DASH_FILE.exists():
        try:
            return json.loads(DASH_FILE.read_text())
        except Exception:
            pass
    return {"updated_at": None, "signals": [], "forecasts": [],
            "performance": {}, "open_positions": []}


def _save(d: dict) -> None:
    DOCS_ROOT.mkdir(exist_ok=True)
    CHARTS_ROOT.mkdir(exist_ok=True)
    d["updated_at"] = str(pd.Timestamp.now(tz="UTC"))
    DASH_FILE.write_text(json.dumps(d, indent=2, default=str))


def record_signal(layer: str, asset: str, direction: str, entry, stop, tp1, tp2,
                   score_or_conf, chart_png: bytes | None, extra: dict | None = None) -> None:
    """Record a fired scalp/swing/council signal + its setup chart."""
    d = _load()
    chart_rel = None
    if chart_png:
        CHARTS_ROOT.mkdir(exist_ok=True, parents=True)
        fname = f"{layer}_{asset}_{pd.Timestamp.now(tz='UTC').strftime('%Y%m%dT%H%M%SZ')}.png"
        (CHARTS_ROOT / fname).write_bytes(chart_png)
        chart_rel = f"charts/{fname}"

    entry_rec = {
        "layer": layer, "asset": asset, "direction": direction,
        "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
        "score_or_conf": score_or_conf,
        "timestamp": str(pd.Timestamp.now(tz="UTC")),
        "chart": chart_rel,
        **(extra or {}),
    }
    d["signals"].insert(0, entry_rec)
    d["signals"] = d["signals"][:MAX_SIGNALS]
    _save(d)


def record_forecast(asset: str, bias: str, price, forecast: dict, chart_png: bytes | None) -> None:
    """Record the latest forecast + chart for an asset (one slot per asset)."""
    d = _load()
    chart_rel = None
    if chart_png:
        CHARTS_ROOT.mkdir(exist_ok=True, parents=True)
        fname = f"forecast_{asset}.png"   # overwrite — always show latest per asset
        (CHARTS_ROOT / fname).write_bytes(chart_png)
        chart_rel = f"charts/{fname}"

    d["forecasts"] = [f for f in d["forecasts"] if f.get("asset") != asset]
    d["forecasts"].insert(0, {
        "asset": asset, "bias": bias, "price": price, "forecast": forecast,
        "timestamp": str(pd.Timestamp.now(tz="UTC")), "chart": chart_rel,
    })
    d["forecasts"] = d["forecasts"][:MAX_FORECASTS]
    _save(d)


def record_performance(perf_summary: dict, open_positions: list) -> None:
    """Refresh performance stats + currently-open positions (called by the performance layer)."""
    d = _load()
    d["performance"] = perf_summary
    d["open_positions"] = open_positions
    _save(d)
