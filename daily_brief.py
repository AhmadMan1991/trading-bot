"""
Daily brief — Arabic-language executive summary, gold-only.

Native equivalent of a format you already use in a separate bot: an executive
summary paragraph, a per-asset scored table (-4..+4, mapped to a signal
emoji + confidence level), a signal legend, and a ranked action list. Since
the gold-only rebuild, "per-asset" is just XAUUSD — the table still renders
as a table (of one row) so the Telegram format and dashboard.json shape
don't need to change.

Rebuilt from forecast_engine.compute_forecast() (deprecated along with the
rest of the old 9-asset architecture) onto gold_engine's own structure
reads, so this doesn't depend on a deprecated module:

  score = H4 structure bias(+-2) + H1 structure bias(+-1) + COT signal(+-1)
          + latest fired gold_scalp/gold_swing signal in the last 24h(+-1)

That combination can range roughly -5..+5; displayed clipped to -4..+4 to
match the template. The executive-summary paragraph is built deterministically
from the computed table so the narrative actually reflects the numbers,
rather than being freeform/unfounded commentary.
"""

from pathlib import Path

import pandas as pd

from config import MARKETS
from gold_engine import structure_bias
from data_feeds import fetch_intraday, fetch_all_cot
from indicators import add_base
import dashboard_export as dash
import telegram

DATA_ROOT = Path(__file__).parent / "data"

_BIAS_PTS = {"BULLISH": 1, "BEARISH": -1, "NEUTRAL": 0}
_SIGNAL_EMOJI = [
    (3,  "🟢🟢"), (1,  "🟢"), (0, "⬜"), (-2, "🔴"), (-99, "🔴🔴"),
]
_CONF_AR = {"high": "عالية", "medium": "متوسطة", "low": "منخفضة"}


def _signal_emoji(score: int) -> str:
    for floor, emoji in _SIGNAL_EMOJI:
        if score >= floor:
            return emoji
    return "🔴🔴"


def _weekly_daily_points(bias: str) -> int:
    b = (bias or "").split(" ")[0]   # handles "BULLISH (weekly) / BEARISH (daily)" style strings
    return _BIAS_PTS.get(b, 0)


def _recent_signal_direction(asset: str, dashboard: dict, hours: int = 24) -> int:
    """+1/-1 if a scalp/swing/council signal fired for this asset in the last
    N hours (per the dashboard's rolling signal log), else 0."""
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=hours)
    for sig in dashboard.get("signals", []):
        if sig.get("asset") != asset:
            continue
        try:
            ts = pd.Timestamp(sig["timestamp"])
        except Exception:
            continue
        if ts < cutoff:
            continue
        direction = (sig.get("direction") or "").upper()
        if direction in ("LONG", "BUY"):
            return 1
        if direction in ("SHORT", "SELL"):
            return -1
    return 0


def compute_asset_scores(assets: list | None = None) -> list[dict]:
    assets = assets or list(MARKETS.keys())   # gold-only config -> just XAUUSD
    dashboard = dash._load()
    cot_map = fetch_all_cot()
    rows = []

    for asset in assets:
        cfg = MARKETS[asset]
        try:
            df_h4 = fetch_intraday(asset, "4h", 150)
            df_h1 = fetch_intraday(asset, "1h", 150)
            if df_h4 is None or len(df_h4) < 60 or df_h1 is None or len(df_h1) < 60:
                print(f"  ⚠ {asset}: insufficient data for daily brief")
                continue
            h4_bias = structure_bias(add_base(df_h4))
            h1_bias = structure_bias(add_base(df_h1))
            price = float(df_h1["close"].iloc[-1])
        except Exception as e:
            print(f"  ⚠ {asset} bias failed: {e}")
            continue

        h4_pts     = _weekly_daily_points(h4_bias.get("bias")) * 2
        h1_pts     = _weekly_daily_points(h1_bias.get("bias"))
        cot        = cot_map.get(asset) or {}
        cot_pts    = _BIAS_PTS.get(cot.get("signal", "NEUTRAL"), 0)
        recent_pts = _recent_signal_direction(asset, dashboard)

        score = h4_pts + h1_pts + cot_pts + recent_pts
        score = max(-4, min(4, score))

        n_components = sum(1 for p in (h4_pts, h1_pts, cot_pts) if p != 0)
        confidence = "high" if n_components >= 2 and abs(score) >= 2 else \
                     "medium" if n_components >= 1 else "low"

        rows.append({
            "asset": asset, "emoji": cfg.get("emoji", ""),
            "score": score, "signal": _signal_emoji(score),
            "confidence": confidence, "confidence_ar": _CONF_AR[confidence],
            "bias": h4_bias.get("bias", "NEUTRAL"), "price": price,
            "cot_signal": cot.get("signal", "NEUTRAL"),
        })

    rows.sort(key=lambda r: abs(r["score"]), reverse=True)
    return rows


_BIAS_AR = {"BULLISH": "صاعد", "BEARISH": "هابط", "NEUTRAL": "محايد"}


def generate_executive_summary(scores: list[dict]) -> str:
    """Deterministic Arabic executive summary built directly from the scored
    table — no LLM. Highlights the two strongest assets by absolute score and
    adds a rule-based risk note."""
    if not scores:
        return "لا توجد بيانات كافية اليوم — راجع جدول الأصول أدناه مباشرة."
    top = scores[:2]
    parts = []
    for r in top:
        bias_ar = _BIAS_AR.get(r.get("bias", "NEUTRAL"), r.get("bias", ""))
        parts.append(f"{r.get('emoji', '')} {r['asset']}: اتجاه {bias_ar} "
                     f"(ثقة {r.get('confidence_ar', '')}، نقاط {r['score']:+d})")
    lead = "أبرز الأصول للمراقبة اليوم — " + "؛ ".join(parts) + "."
    strongest = top[0]
    if abs(strongest["score"]) >= 4:
        risk = "⚠️ الخطر الأكبر: تشبّع في التموضع عند الأصل الأقوى — احذر الانعكاس المضاد."
    elif strongest.get("confidence") == "low":
        risk = "⚠️ الخطر الأكبر: إشارات ضعيفة ومتضاربة — يُفضّل انتظار تأكيد أوضح."
    else:
        risk = "⚠️ الخطر الأكبر: بيانات اقتصادية أمريكية قادمة قد تقلب الصورة — تابع المفكرة."
    return lead + "\n" + risk


_ACTION_AR = {
    "long_watch":   "مراقبة مناطق الدعم للدخول LONG",
    "short_watch":  "فرصة SHORT عند المقاومة",
    "follow_session": "متابعة الجلسة القادمة (London/NY killzone)",
    "neutral":      "لا توجد فرصة واضحة حاليًا",
}


def build_action_list(scores: list[dict], top_n: int = 5) -> list[dict]:
    actions = []
    for r in scores[:top_n]:
        if r["score"] >= 2:
            action = _ACTION_AR["long_watch"]
        elif r["score"] <= -2:
            action = _ACTION_AR["short_watch"]
        elif r["score"] != 0:
            action = _ACTION_AR["follow_session"]
        else:
            action = _ACTION_AR["neutral"]
        actions.append({
            "asset": r["asset"], "direction_ar": "صعود" if r["score"] > 0 else "هبوط" if r["score"] < 0 else "محايد",
            "action_ar": action,
        })
    return actions


def format_daily_brief_telegram(date_str: str, summary: str, scores: list[dict], actions: list[dict]) -> str:
    # One line per asset rather than a fixed-width monospace table — mixing
    # RTL Arabic labels with LTR-padded columns renders garbled in Telegram's
    # bidi text handling, so simple lines (matching format_cot_map's style
    # elsewhere in this codebase) are more reliable than a <pre> table here.
    lines = [f"<b>📊 التحليل اليومي — {date_str}</b>", "", "<b>الملخص التنفيذي</b>", summary,
             "", "<b>جدول الأصول</b>"]
    for r in scores:
        lines.append(f"{r['signal']} <b>{r['asset']}</b>  {r['score']:+d}  — الثقة: {r['confidence_ar']}")
    lines.append("")
    lines.append("<b>الخلاصة التشغيلية</b>")
    for i, a in enumerate(actions, 1):
        lines.append(f"{i}. {a['asset']} ({a['direction_ar']}) — {a['action_ar']}")
    return "\n".join(lines)


def run_daily_brief() -> None:
    date_str = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    scores  = compute_asset_scores()
    if not scores:
        print("  no data available — skipping daily brief")
        return
    summary = generate_executive_summary(scores)
    actions = build_action_list(scores)

    telegram.send_text(format_daily_brief_telegram(date_str, summary, scores, actions))

    d = dash._load()
    d["daily_brief"] = {
        "date": date_str, "summary": summary,
        "scores": scores, "actions": actions,
    }
    dash._save(d)
    print(f"  daily brief sent for {date_str} ({len(scores)} assets)")
