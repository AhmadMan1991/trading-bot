"""
DEPRECATED — removed along with the rest of the 9-asset architecture.

This was the original BTC-only ICT/SMC-flavored engine (the source of the
SNIPER/WYCKOFF_SPRING/etc. signal taxonomy) later generalized into
deep_pipeline.py for all 9 assets. In the gold-only rebuild the project no
longer trades BTC at all — the taxonomy and detection concepts it pioneered
(order blocks, liquidity sweeps, Wyckoff spring/absorption labeling) were
ported directly into gold_engine.py's SIGNAL_TAXONOMY, scoped to XAUUSD.

main.py no longer imports this file. Kept only because the Cowork sandbox
can't delete files from the mounted project folder — delete it yourself
locally whenever convenient:

    rm btc_deep_pipeline.py

Nothing below this point is executed by the pipeline.
"""

raise ImportError(
    "btc_deep_pipeline.py is deprecated and no longer wired into main.py. "
    "BTC is no longer traded; see gold_engine.py for the gold-only ICT/SMC "
    "engine (its SIGNAL_TAXONOMY carries this file's labels forward). "
    "Safe to delete this file."
)
