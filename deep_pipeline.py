"""
Deep Pipeline — the BTC-only engine in btc_deep_pipeline.py, generalized to
run across all 9 configured markets.

Reuses (does not duplicate) the TA / Synthesis agents and the 4-layer risk
engine from btc_deep_pipeline.py. What changes per asset:

  - OHLCV comes from data_feeds.fetch_intraday(asset, "1h", N) — TwelveData
    or OKX depending on asset — instead of the BTC-hardcoded OKX/Bybit fetch.

  - The COT vote is built directly from this run's own cot_agent.py output
    (official CFTC + insider-week fallback, already the first step of the
    sequential chain) via a deterministic mapping, instead of calling the
    pipeline's internal LLM-based COT agent again. Same information, one
    fewer Ollama call per asset.

  - Funding rate / derivatives sentiment is a crypto-perpetual concept with
    no equivalent for FX/indices/gold, and the original fetch_funding() /
    run_sentiment_agent prompt are both hardcoded specifically to BTC-USDT
    (OKX BTC-USDT-SWAP funding endpoint; sentiment prompt text literally says
    "BTC/USDT"). Rather than fake a funding curve for ETHUSD or any FX/index
    asset, only BTCUSD gets the real funding fetch + real LLM sentiment call.
    Every other asset (ETHUSD included) gets funding_rate injected as a
    neutral 0.0 placeholder (so compute_indicators' unguarded
    out["funding_rate"] references don't crash — the funding-based detectors
    like derivatives-trap correctly stay inert) and a deterministic NEUTRAL
    sentiment vote instead of a real LLM call, since the real prompt would
    just produce an always-neutral, asset-mislabeled result otherwise.

  - Risk state (equity / peak equity / daily loss) is tracked PER ASSET
    (data/risk_state_{asset}.json) rather than one shared pool — a loss on
    one asset shouldn't trip another asset's circuit breaker. This is done
    by swapping btc_deep_pipeline's module-level load_risk_state/
    save_risk_state for a per-asset closure immediately before each asset's
    run_risk_agent() call (run_risk_agent looks these up by name in the
    module's own globals at call time, so the swap takes effect immediately).

Sniper setups (signal_label == "SNIPER") get distinct, louder Telegram
formatting per the project's explicit request for extra focus on this setup.
"""

import json
from pathlib import Path

import pandas as pd

import btc_deep_pipeline as bp
import telegram
import dashboard_export as dash
from config import MARKETS, OLLAMA_KEY
from data_feeds import fetch_intraday
from indicators import add_base
from charts import generate_chart

DATA_ROOT = Path(__file__).parent / "data"

# Same taxonomy the user provided (APEX_PICK / SNIPER / Wyckoff etc.) — used
# for Telegram header + dashboard tagging. btc_deep_pipeline.SIGNAL_TAXONOMY
# already has emoji/name/description per label; reused directly below.


def _per_asset_risk_state_funcs(asset: str):
    """Build load/save closures pointed at this asset's own risk-state file,
    so 9 assets don't share one equity/drawdown/daily-loss pool."""
    path = DATA_ROOT / f"risk_state_{asset}.json"

    def load():
        today = pd.Timestamp.now(tz="UTC").date().isoformat()
        if path.exists():
            try:
                state = json.loads(path.read_text())
            except Exception:
                state = {}
        else:
            state = {}
        if state.get("date") != today:
            state["daily_loss_usd"] = 0.0
            state["date"] = today
        state.setdefault("peak_equity_usd", bp.ACCOUNT_SIZE_USD)
        state.setdefault("current_equity_usd", bp.ACCOUNT_SIZE_USD)
        state.setdefault("daily_loss_usd", 0.0)
        return state

    def save(state):
        DATA_ROOT.mkdir(exist_ok=True)
        path.write_text(json.dumps(state, indent=2, default=str))

    return load, save


def _cot_vote(asset: str, cot_data: dict | None) -> dict:
    """Deterministic COT vote built from this run's own cot_agent.py output —
    no extra LLM call needed for information we already have."""
    c = (cot_data or {}).get(asset)
    if not c:
        return {"agent": "COTAgent", "bias": "NEUTRAL", "confidence": 0.3,
                "key_points": ["no COT data available this run"],
                "reasoning": "No positioning data — treated as neutral."}
    bias = c.get("signal", "NEUTRAL")
    idx = c.get("cot_index", 50)
    conf = 0.65 if bias != "NEUTRAL" else 0.35
    return {
        "agent": "COTAgent", "bias": bias, "confidence": conf,
        "key_points": [
            f"COT index {idx}/100 ({c.get('source', '?')})",
            f"net change {c.get('change', 0):+d}, as of {c.get('date', '?')}",
        ],
        "reasoning": f"Positioning index at {idx}/100 — {bias.lower()} skew.",
    }


def _neutral_sentiment_vote(asset: str) -> dict:
    """Placeholder for non-crypto assets — see module docstring for why the
    real (funding-rate-based) sentiment agent is skipped here."""
    return {
        "agent": "SentimentAgent", "bias": "NEUTRAL", "confidence": 0.3,
        "key_points": [f"{asset} has no perpetual-funding equivalent"],
        "reasoning": "Derivatives-sentiment read is crypto-specific; "
                     "skipped for this asset class, treated as neutral.",
    }


def _prepare_df(asset: str, cfg: dict) -> pd.DataFrame | None:
    df = fetch_intraday(asset, "1h", 250)
    if df is None or len(df) < 60:
        return None
    df = df.copy()
    if asset == "BTCUSD":
        # Real funding data — bp.fetch_funding()/align_funding_to_1h() are the
        # original BTC-only functions (OKX BTC-USDT-SWAP, Bybit fallback);
        # reused as-is since this is the one asset they were built for.
        try:
            funding = bp.fetch_funding()
            aligned = bp.align_funding_to_1h(funding, df.index)
            df["funding_rate"] = aligned["funding_rate"]
        except Exception as e:
            print(f"    ⚠ funding fetch failed, using neutral 0.0: {e}")
            df["funding_rate"] = 0.0
    else:
        # ETHUSD and all non-crypto assets: no funding-rate source is wired
        # up for them (fetch_funding() is hardcoded to BTC-USDT-SWAP), so use
        # a neutral placeholder — this keeps the funding-based detectors
        # (derivatives trap etc.) correctly inert rather than crashing.
        df["funding_rate"] = 0.0
    return bp.compute_indicators(df)


def _format_deep_signal(asset: str, cfg: dict, r: dict) -> str:
    label = r.get("signal_label", "NO_SIGNAL")
    emoji, name, desc = bp.SIGNAL_TAXONOMY.get(label, ("📊", label, ""))
    is_sniper = label == "SNIPER"
    header = (f"🎯🎯🎯 <b>SNIPER SETUP</b> 🎯🎯🎯" if is_sniper
              else f"{emoji} <b>{name}</b>")
    ez = r.get("entry_zone") or []
    entry_txt = f"{ez[0]}–{ez[1]}" if len(ez) == 2 else str(ez)
    lines = [
        header,
        f"<i>{desc}</i>" if desc else "",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"{cfg.get('emoji','')} <b>{asset}</b>  {r.get('direction','?')}"
        f"  · conf {r.get('confidence', 0):.0%}  · {r.get('timeframe','1h')}",
        f"Entry: {entry_txt}   SL: {r.get('stop_loss')}",
        f"TP1: {r.get('target_1')}   TP2: {r.get('target_2')}"
        f"   R:R {r.get('risk_reward', 0):.1f}",
        f"Size: ${r.get('position_size', 0):,.0f}"
        f"   Agreement: {r.get('agent_agreement','?')}"
        f"   Regime: {r.get('regime','?')}",
        "━━━━━━━━━━━━━━━━━━━━━",
        (r.get("reasoning", "") or "")[:600],
    ]
    if is_sniper:
        lines.append("\n🎯 <b>Extra focus:</b> multi-agent confluence flagged "
                      "this as a Sniper-grade entry — highest-conviction "
                      "setup type in this pipeline.")
    return "\n".join(l for l in lines if l)


def run_deep_pipeline(assets: list | None = None, cot_data: dict | None = None) -> list:
    """Run the deep TA→Sentiment→Synthesis→Risk chain for each asset.
    Returns the list of APPROVED signal dicts that fired this run."""
    assets = assets or [a for a, cfg in MARKETS.items()]
    fired = []

    for asset in assets:
        cfg = MARKETS[asset]
        print(f"\n  {cfg.get('emoji','')} {asset} — deep pipeline")
        try:
            df = _prepare_df(asset, cfg)
            if df is None:
                print("    ⚠ insufficient OHLCV data, skipped")
                continue

            ta_vote = bp.run_ta_agent(df, OLLAMA_KEY, verbose=False)
            cot_vote = _cot_vote(asset, cot_data)
            sent_vote = (bp.run_sentiment_agent(df, OLLAMA_KEY)
                         if asset == "BTCUSD"
                         else _neutral_sentiment_vote(asset))

            signal = bp.run_synthesis_agent(df, [ta_vote, cot_vote, sent_vote], OLLAMA_KEY)

            load_fn, save_fn = _per_asset_risk_state_funcs(asset)
            bp.load_risk_state, bp.save_risk_state = load_fn, save_fn
            risk_result = bp.run_risk_agent(signal, df)

            label = risk_result.get("signal_label", "NO_SIGNAL")
            verdict = risk_result.get("verdict", "BLOCKED")
            print(f"    → {label}  verdict={verdict}")

            bp.save_signal(risk_result, filename=f"deep_signals_{asset}.jsonl")
            bp.log_trade(signal, risk_result, df)

            if verdict != "APPROVED":
                continue

            caption = _format_deep_signal(asset, cfg, risk_result)
            try:
                chart_png = generate_chart(asset, add_base(df), risk_result)
                telegram.send_photo(chart_png, caption=caption[:1024])
            except Exception as e:
                print(f"    ⚠ chart failed, sending text only: {e}")
                chart_png = None
                telegram.send_text(caption)

            ez = risk_result.get("entry_zone") or []
            entry = (ez[0] + ez[1]) / 2 if len(ez) == 2 else risk_result.get("entry_zone")
            try:
                dash.record_signal(
                    layer="deep_pipeline", asset=asset,
                    direction=risk_result.get("direction"),
                    entry=entry, stop=risk_result.get("stop_loss"),
                    tp1=risk_result.get("target_1"), tp2=risk_result.get("target_2"),
                    score_or_conf=risk_result.get("confidence"),
                    chart_png=chart_png,
                    extra={"signal_label": label,
                           "sniper": label == "SNIPER",
                           "caption": caption},
                )
            except Exception as e:
                print(f"    ⚠ dashboard export failed: {e}")

            fired.append(risk_result)
        except Exception as e:
            print(f"    ⚠ {asset} deep pipeline failed: {e}")

    print(f"\n  Deep pipeline: {len(fired)} approved signal(s) across {len(assets)} asset(s)")
    return fired


def run_deep_pipeline_layer(assets=None):
    """Standalone entry point for `main.py --layer deep_pipeline`."""
    cot_data = None
    latest = DATA_ROOT / "cot_latest.json"
    if latest.exists():
        try:
            cot_data = json.loads(latest.read_text()).get("data")
        except Exception:
            cot_data = None
    return run_deep_pipeline(assets, cot_data)


if __name__ == "__main__":
    run_deep_pipeline_layer()
