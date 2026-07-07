"""
Daily brief — Arabic-language executive summary across all configured markets.

Native equivalent of a format you already use in a separate bot: an executive
summary paragraph, a per-asset scored table (-4..+4, mapped to a signal
emoji + confidence level), a signal legend, and a ranked action list.

Unlike that separate bot, this one is built entirely from data this merged
system already computes — it doesn't call any new external data source:

  score = weekly_bias(+-2) + daily_bias(+-1) + COT signal(+-1)
          + latest fired scalp/swing/council signal in the last 24h(+-1)

That combination can range roughly -5..+5; displayed clipped to -4..+4 to
match the template. The executive-summary paragraph is LLM-written (Ollama)
from the computed table so the narrative actually reflects the numbers,
rather than being freeform/unfounded commentary.
"""

import json
import time
from pathlib import Path

import requests
import pandas as pd

from config import MARKETS, OLLAMA_URL, OLLAMA_MODEL, OLLAMA_KEY
from forecast_engine import compute_forecast
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
    assets = assets or list(MARKETS.keys())
    dashboard = dash._load()
    rows = []

    for asset in assets:
        cfg = MARKETS[asset]
        try:
            fc = compute_forecast(asset)
        except Exception as e:
            print(f"  ⚠ {asset} forecast failed: {e}")
            continue

        weekly_pts = _weekly_daily_points(fc.get("weekly_bias")) * 2
        daily_pts  = _weekly_daily_points(fc.get("daily_bias"))
        cot        = fc.get("cot_iw") or {}
        cot_pts    = _BIAS_PTS.get(cot.get("signal", "NEUTRAL"), 0)
        recent_pts = _recent_signal_direction(asset, dashboard)

        score = weekly_pts + daily_pts + cot_pts + recent_pts
        score = max(-4, min(4, score))

        n_components = sum(1 for p in (weekly_pts, daily_pts, cot_pts) if p != 0)
        confidence = "high" if n_components >= 2 and abs(score) >= 2 else \
                     "medium" if n_components >= 1 else "low"

        rows.append({
            "asset": asset, "emoji": cfg.get("emoji", ""),
            "score": score, "signal": _signal_emoji(score),
            "confidence": confidence, "confidence_ar": _CONF_AR[confidence],
            "bias": fc.get("bias", "NEUTRAL"), "price": fc.get("price"),
            "cot_signal": cot.get("signal", "NEUTRAL"),
            "scalp_skip": bool(cfg.get("scalp_skip")),
        })

    rows.sort(key=lambda r: abs(r["score"]), reverse=True)
    return rows


def _ollama_text(system: str, user: str, max_tokens: int = 500) -> str:
    """Same Ollama Cloud endpoint the rest of the system uses, but returns raw
    text instead of parsed JSON — free-form Arabic prose isn't safe to force
    through a JSON round-trip."""
    for attempt in range(2):
        try:
            r = requests.post(OLLAMA_URL,
                              headers={"Authorization": f"Bearer {OLLAMA_KEY}",
                                       "Content-Type": "application/json"},
                              json={"model": OLLAMA_MODEL, "stream": False,
                                    "options": {"temperature": 0.4, "num_predict": max(max_tokens, 800)},
                                    "messages": [{"role": "system", "content": system},
                                                 {"role": "user",   "content": user}]},
                              timeout=120)
            if not r.ok:
                time.sleep(3)
                continue
            return r.json()["message"]["content"].strip()
        except Exception:
            if attempt < 1:
                time.sleep(2)
    return "تعذر توليد الملخص التنفيذي هذا اليوم — راجع جدول الأصول أدناه مباشرة."


SUMMARY_SYSTEM_AR = """أنت محلل أسواق مالية. تكتب ملخصًا تنفيذيًا يوميًا موجزًا بالعربية
الفصحى، بأسلوب مهني مباشر (3-5 جمل)، بناءً فقط على جدول النقاط المرفق — لا تخترع
بيانات أو أخبارًا غير مذكورة في الجدول. اذكر أبرز أصلين للمراقبة، وأنهِ الملخص بسطر
تحذير يبدأ بـ '⚠️ الخطر الأكبر:' يشير إلى أكبر عامل يمكن أن يقلب الصورة (مثل بيانات
اقتصادية قادمة أو تشبع في التموضع). أخرج نص عادي فقط بدون markdown."""


def generate_executive_summary(scores: list[dict]) -> str:
    table_txt = "\n".join(
        f"{r['asset']}: score={r['score']:+d} bias={r['bias']} cot={r['cot_signal']}"
        for r in scores
    )
    return _ollama_text(SUMMARY_SYSTEM_AR, f"جدول نقاط اليوم:\n{table_txt}")


_ACTION_AR = {
    "confirm_only": "مؤشر تأكيد — لا يُتداول مباشرة",
    "long_watch":   "مراقبة مناطق الدعم للدخول LONG",
    "short_watch":  "فرصة SHORT عند المقاومة",
    "follow_session": "متابعة الجلسة القادمة",
    "neutral":      "لا توجد فرصة واضحة حاليًا",
}


def build_action_list(scores: list[dict], top_n: int = 5) -> list[dict]:
    actions = []
    for r in scores[:top_n]:
        if r["scalp_skip"]:
            action = _ACTION_AR["confirm_only"]
        elif r["score"] >= 2:
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
