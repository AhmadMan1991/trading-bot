"""
DEPRECATED — replaced by gold_engine.py.

This was btc_deep_pipeline.py's TA/Sentiment/Synthesis/4-layer-risk engine
generalized to run across all 9 configured markets. It's one of the five
competing scoring engines identified as the cause of conflicting, rarely
firing signals — running this alongside scalp_engine/swing_engine/council/
forecast_engine on 9 assets in parallel was the core problem, not a
different fifth opinion worth keeping. gold_engine.py replaces it: one
deterministic ICT/SMC engine, XAUUSD-only, additive confluence scoring.

main.py no longer imports this file. Kept only because the Cowork sandbox
can't delete files from the mounted project folder — delete it yourself
locally whenever convenient:

    rm deep_pipeline.py

Nothing below this point is executed by the pipeline.
"""

raise ImportError(
    "deep_pipeline.py is deprecated and no longer wired into main.py. "
    "Use gold_engine.py instead. Safe to delete this file."
)
