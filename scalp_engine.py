"""
DEPRECATED — replaced by gold_engine.py (run_gold_scalp).

This file was one of five competing scoring engines (scalp_engine,
swing_engine, council, forecast_engine, btc_deep_pipeline/deep_pipeline)
that ran in parallel across 9 assets and produced conflicting, rarely-firing
signals. The system was rebuilt gold-only around one deterministic ICT/SMC
engine (see gold_engine.py) — session-killzone-gated liquidity-sweep scalp
detection with additive confluence scoring instead of indicator-threshold
voting.

main.py no longer imports this file. It is kept in the repo only because the
Cowork sandbox can't delete files from the mounted project folder — delete
it yourself locally whenever convenient:

    rm scalp_engine.py

Nothing below this point is executed by the pipeline.
"""

raise ImportError(
    "scalp_engine.py is deprecated and no longer wired into main.py. "
    "Use gold_engine.run_gold_scalp() instead. Safe to delete this file."
)
