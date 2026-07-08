"""
DEPRECATED — replaced by gold_engine.py (run_gold_swing).

Part of the 9-asset, 5-engine architecture removed in the gold-only rebuild.
See gold_engine.py for the replacement: H1/H4 multi-day structure scan with
additive ICT/SMC confluence scoring (no LLM debate gate, no cross-engine
voting) instead of this file's LLM swing-plan + COT contrarian gate.

main.py no longer imports this file. Kept only because the Cowork sandbox
can't delete files from the mounted project folder — delete it yourself
locally whenever convenient:

    rm swing_engine.py

Nothing below this point is executed by the pipeline.
"""

raise ImportError(
    "swing_engine.py is deprecated and no longer wired into main.py. "
    "Use gold_engine.run_gold_swing() instead. Safe to delete this file."
)
