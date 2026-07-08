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

MAX_SIGNALS    = 40
MAX_FORECASTS  = 12
MAX_COT_WEEKS  = 15   # ~90 days of weekly COT reports


def _load() -> dict:
    if DASH_FILE.exists():
        try:
            return json.loads(DASH_FILE.read_text())
        except Exception:
            pass
    return {"updated_at": None, "signals": [], "forecasts": [],
            "performance": {}, "open_positions": [], "cot_history": [],
            "scenarios": {}, "news": {"events": [], "updated_at": None}}


def _save(d: dict) -> None:
    DOCS_ROOT.mkdir(exist_ok=True)
    CHARTS_ROOT.mkdir(exist_ok=True)
    d["updated_at"] = str(pd.Timestamp.now(tz="UTC"))
    DASH_FILE.write_text(json.dumps(d, indent=2, default=str))


def record_signal(layer: str, asset: str, direction: str, entry, stop, tp1, tp2,
                   score_or_conf, chart_png: bytes | None, extra: dict | None = None,
                   tp3=None) -> None:
    """Record a fired scalp/swing signal + its setup chart. tp3 is optional
    (kept as a kwarg with a default so older call sites without a third
    target don't need updating)."""
    d = _load()
    chart_rel = None
    if chart_png:
        CHARTS_ROOT.mkdir(exist_ok=True, parents=True)
        fname = f"{layer}_{asset}_{pd.Timestamp.now(tz='UTC').strftime('%Y%m%dT%H%M%SZ')}.png"
        (CHARTS_ROOT / fname).write_bytes(chart_png)
        chart_rel = f"charts/{fname}"

    entry_rec = {
        "layer": layer, "asset": asset, "direction": direction,
        "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3,
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


def record_macro(synthesis: str) -> None:
    """Record the latest Gemini-sourced macro synthesis (called by macro_agent, step 2 of the pipeline)."""
    if not synthesis:
        return
    d = _load()
    d["macro"] = {"synthesis": synthesis, "timestamp": str(pd.Timestamp.now(tz="UTC"))}
    _save(d)


def record_scenarios(scenarios: dict) -> None:
    """Record the 1H/4H/Daily/Weekly structural snapshot
    (gold_engine.run_gold_scenarios()) + each timeframe's chart. One slot
    per timeframe, overwritten every run — a current snapshot, not a
    history log (unlike COT, structure bias doesn't need a trend-over-time
    view; each run's read stands on its own)."""
    d = _load()
    out = {}
    for label, info in scenarios.items():
        info = dict(info)
        chart_png = info.pop("chart_png", None)
        chart_rel = None
        if chart_png:
            CHARTS_ROOT.mkdir(exist_ok=True, parents=True)
            fname = f"scenario_{label}.png"   # overwrite — always latest per timeframe
            (CHARTS_ROOT / fname).write_bytes(chart_png)
            chart_rel = f"charts/{fname}"
        info["chart"] = chart_rel
        out[label] = info
    d["scenarios"] = out
    _save(d)


def record_news(events: list) -> None:
    """Record the current red-folder (high-impact) USD calendar — called by
    news_agent.py every ~5min run. Replaces the whole list each time (it's a
    live calendar snapshot, not a history log); includes both upcoming
    releases and already-released ones with their actual value, so the
    dashboard can show recent beat/miss context alongside what's next."""
    d = _load()
    out = []
    for ev in events:
        t = ev.get("time")
        out.append({
            "title":    ev.get("title", ""),
            "currency": ev.get("currency", ""),
            "time":     str(t) if t is not None else None,
            "forecast": ev.get("forecast"),
            "previous": ev.get("previous"),
            "actual":   ev.get("actual"),
        })
    out.sort(key=lambda e: e["time"] or "")
    d["news"] = {"events": out, "updated_at": str(pd.Timestamp.now(tz="UTC"))}
    _save(d)


def record_cot(cot_map: dict, summary: str) -> None:
    """Record the latest COT snapshot + a rolling weekly history so the
    dashboard's COT section can show a trend, not just today's read.
    Called by cot_agent.py (step 1 of the pipeline). One history entry per
    calendar date — re-runs on the same day overwrite that day's entry
    instead of piling up duplicates (COT itself only updates weekly)."""
    d = _load()
    today = str(pd.Timestamp.now(tz="UTC").date())

    d["cot"] = {"data": cot_map, "summary": summary, "timestamp": str(pd.Timestamp.now(tz="UTC"))}

    history = d.get("cot_history", [])
    history = [h for h in history if h.get("date") != today]
    history.append({"date": today, "data": cot_map})
    history.sort(key=lambda h: h["date"])
    d["cot_history"] = history[-MAX_COT_WEEKS:]

    _save(d)
