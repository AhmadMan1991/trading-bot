"""
DEPRECATED — removed, no direct replacement (by design).

The 7-agent debate/voting model was diagnosed as a root cause of the
"no real signals" complaint: requiring N agents to agree before firing
produced exactly the conservative NO_TRADE deadlock the gold-only rebuild
was meant to fix (e.g. a legitimate 1-bull/3-bear/3-neutral BTCUSD split
that never cleared the agreement threshold). gold_engine.py replaces
debate/voting with additive confluence scoring: each factor (session sweep,
HTF structure agreement, order block/FVG confluence, COT alignment) adds to
one confidence score, so disagreement lowers confidence instead of blocking
the trade outright.

main.py no longer imports this file. Kept only because the Cowork sandbox
can't delete files from the mounted project folder — delete it yourself
locally whenever convenient:

    rm council.py

Nothing below this point is executed by the pipeline.
"""

raise ImportError(
    "council.py is deprecated and no longer wired into main.py. "
    "Multi-agent voting was replaced by gold_engine.py's additive confluence "
    "scoring. Safe to delete this file."
)
