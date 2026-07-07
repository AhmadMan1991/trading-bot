"""
COT Agent — pipeline step 1.

Gathers institutional positioning (official CFTC first, insider-week.com as
fallback — see data_feeds.fetch_all_cot) for every configured market and
hands it forward to the rest of the chain (Forecast, Council, Deep Pipeline
all read this instead of re-fetching COT themselves mid-run).
"""

import json
from pathlib import Path
import pandas as pd

from data_feeds import fetch_all_cot
import dashboard_export as dash

DATA_ROOT = Path(__file__).parent / "data"
LATEST_FILE = DATA_ROOT / "cot_latest.json"


def build_cot_summary(cot_map: dict) -> str:
    """Deterministic (no LLM call — this is just counting/sorting) one-line
    read of the whole COT map: how many markets loaded, the split of
    bullish/bearish/neutral positioning, and whichever market is sitting at
    the most extreme (most crowded) reading."""
    present = {m: c for m, c in cot_map.items() if c}
    if not present:
        return "No COT data available this run."

    bullish = [m for m, c in present.items() if c.get("signal") == "BULLISH"]
    bearish = [m for m, c in present.items() if c.get("signal") == "BEARISH"]
    neutral = [m for m in present if m not in bullish and m not in bearish]

    extreme_market, extreme_c = max(present.items(), key=lambda kv: abs(kv[1].get("cot_index", 50) - 50))
    idx = extreme_c.get("cot_index", 50)
    crowd = "crowded long" if idx >= 75 else "crowded short" if idx <= 25 else "no extreme"

    parts = [f"{len(present)}/{len(cot_map)} markets reporting.",
             f"{len(bullish)} bullish, {len(bearish)} bearish, {len(neutral)} neutral."]
    if crowd != "no extreme":
        parts.append(f"Most extreme: {extreme_market} at {idx}/100 ({crowd}).")
    return " ".join(parts)


def run_cot_agent() -> dict:
    cot_map = fetch_all_cot()

    DATA_ROOT.mkdir(exist_ok=True)
    LATEST_FILE.write_text(json.dumps({
        "updated_at": str(pd.Timestamp.now(tz="UTC")),
        "data": cot_map,
    }, indent=2, default=str))

    summary = build_cot_summary(cot_map)
    try:
        dash.record_cot(cot_map, summary)
    except Exception as e:
        print(f"  ⚠ dashboard export failed: {e}")

    n_ok = sum(1 for v in cot_map.values() if v)
    sources = {v["source"] for v in cot_map.values() if v and v.get("source")}
    print(f"  COT data loaded for {n_ok}/{len(cot_map)} markets ({', '.join(sources) or 'none'})")
    return cot_map
