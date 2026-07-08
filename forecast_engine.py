"""
DEPRECATED — replaced by gold_engine.py (run_gold_bias).

Part of the 9-asset, 5-engine architecture removed in the gold-only rebuild.
gold_engine.run_gold_bias() gives the same "what's the structural read right
now" context, scoped to XAUUSD, using EMA-stack + swing-structure analysis
directly (no separate BS_OB_RJB_FVG pattern layer, no per-asset LLM call) —
it feeds the same additive-confidence engine that scalp/swing use, so bias
and setup detection can't disagree with each other.

main.py no longer imports this file. Kept only because the Cowork sandbox
can't delete files from the mounted project folder — delete it yourself
locally whenever convenient:

    rm forecast_engine.py

Nothing below this point is executed by the pipeline.
"""

raise ImportError(
    "forecast_engine.py is deprecated and no longer wired into main.py. "
    "Use gold_engine.run_gold_bias() instead. Safe to delete this file."
)
