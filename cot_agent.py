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

DATA_ROOT = Path(__file__).parent / "data"
LATEST_FILE = DATA_ROOT / "cot_latest.json"


def run_cot_agent() -> dict:
    cot_map = fetch_all_cot()

    DATA_ROOT.mkdir(exist_ok=True)
    LATEST_FILE.write_text(json.dumps({
        "updated_at": str(pd.Timestamp.now(tz="UTC")),
        "data": cot_map,
    }, indent=2, default=str))

    n_ok = sum(1 for v in cot_map.values() if v)
    sources = {v["source"] for v in cot_map.values() if v and v.get("source")}
    print(f"  COT data loaded for {n_ok}/{len(cot_map)} markets ({', '.join(sources) or 'none'})")
    return cot_map
