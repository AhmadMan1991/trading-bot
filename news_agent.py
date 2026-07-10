"""
Red-folder USD news agent.

Watches the ForexFactory high-impact calendar (same feed data_feeds.py already
uses for the news-block gate) and sends two Telegram alerts per watched event:

  1. PRE-ALERT  — ~NEWS_PRE_ALERT_MIN minutes before release: shows the
     previous reading and the forecast/consensus reading, so you know what's
     coming and what "beat" vs "miss" means for this specific number.
  2. POST-ALERT — once the feed populates an "actual" value: shows
     previous / forecast / actual side by side, and a bias read for
     USD / XAU / equity indices based on whether the number beat or missed
     consensus.

Meant to run frequently (every ~5 min) via its own workflow — each run is a
cheap calendar check, not a full market scan.

Note on the bias read: "beat forecast -> USD up -> gold/indices down" is a
historical tendency, not a rule — risk sentiment, positioning, and Fed
expectations can all override it. Treat it as context, not a signal.
"""

import json
from pathlib import Path
import pandas as pd

from config import (NEWS_PRE_ALERT_MIN, NEWS_PRE_ALERT_WINDOW, NEWS_WATCH_CURRENCIES,
                    NEWS_POST_GRACE_MIN)
from data_feeds import fetch_news_events_raw
import telegram

DATA_ROOT  = Path(__file__).parent / "data"
STATE_FILE = DATA_ROOT / "news_agent_state.json"


def _event_id(ev: dict) -> str:
    return f"{ev.get('title','')}_{ev.get('time')}"


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"pre_sent": [], "post_sent": []}


def _save_state(state: dict) -> None:
    DATA_ROOT.mkdir(exist_ok=True)
    # Prune old dedup markers so this file doesn't grow forever. Was 2 days,
    # keyed off the EVENT's own timestamp — but ForexFactory's "this week"
    # feed keeps listing an event for the rest of its calendar week, so an
    # event from early in the week would age out of post_sent/pre_sent
    # after 2 days while the feed still returned it, making the "already
    # sent" check go false again and firing the same alert on a ~2-day
    # repeat loop indefinitely. 9 days safely outlives a full calendar week
    # either side, so a dedup marker never expires while the feed can still
    # surface that event.
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=9)
    for key in ("pre_sent", "post_sent"):
        state[key] = [e for e in state[key]
                      if _safe_ts(e.split("_")[-1]) is None or _safe_ts(e.split("_")[-1]) > cutoff]
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _safe_ts(s: str):
    try:
        return pd.Timestamp(s)
    except Exception:
        return None


def _bias_read(forecast, actual) -> str:
    """Very simple beat/miss -> USD bias -> XAU/indices tendency read.
    Terminology (Beat/Miss, USD/XAU) stays English, the explanatory note is
    Arabic per user request."""
    try:
        f = float(str(forecast).replace("%", "").replace("K", "").replace("M", ""))
        a = float(str(actual).replace("%", "").replace("K", "").replace("M", ""))
    except (ValueError, TypeError):
        return "تعذرت المقارنة الرقمية — يُرجى مراجعة الحدث يدويًا."

    if a > f:
        return ("تجاوز التوقعات (Beat) ← عادةً <b>USD-bullish</b> ← يميل للضغط على "
                "<b>XAU (bearish)</b> والمؤشرات (bearish)، مع افتراض ثبات باقي العوامل.")
    if a < f:
        return ("قصور عن التوقعات (Miss) ← عادةً <b>USD-bearish</b> ← يميل لدعم "
                "<b>XAU (bullish)</b> والمؤشرات (bullish)، مع افتراض ثبات باقي العوامل.")
    return "جاءت القراءة مطابقة للتوقعات (in line) ← عادةً ردة فعل هادئة/محايدة."


def run_news_agent() -> None:
    events = fetch_news_events_raw()
    now = pd.Timestamp.now(tz="UTC")
    state = _load_state()
    sent = 0
    red_folder = []   # high-impact USD events this week — for the dashboard

    for ev in events:
        currency = (ev.get("currency") or "").upper()
        impact   = str(ev.get("impact", "")).lower()
        # "Red folder" = high-impact only. fetch_news_events_raw() returns
        # every impact level (unlike fetch_news_events(), which already
        # filters to high-impact for the news-block gate) — this was
        # previously un-filtered here, so low/medium-impact USD prints were
        # triggering alerts too. Restricting to high-impact matches what
        # this agent has always been documented to watch.
        if currency not in NEWS_WATCH_CURRENCIES or impact != "high":
            continue

        red_folder.append(ev)

        ev_time = ev.get("time")
        if ev_time is None:
            continue
        eid = _event_id(ev)
        mins_until = (ev_time - now).total_seconds() / 60

        # PRE-ALERT window
        lo, hi = NEWS_PRE_ALERT_MIN - NEWS_PRE_ALERT_WINDOW / 2, NEWS_PRE_ALERT_MIN + NEWS_PRE_ALERT_WINDOW / 2
        if lo <= mins_until <= hi and eid not in state["pre_sent"]:
            telegram.send_text(telegram.format_news_pre(ev))
            state["pre_sent"].append(eid)
            sent += 1
            print(f"  📰 pre-alert sent: {ev.get('title')} ({currency}) in {mins_until:.0f}min")

        # POST-ALERT: once actual is populated and release time has passed
        actual = ev.get("actual")
        has_actual = actual not in (None, "", "N/A")
        if has_actual and mins_until <= 0 and eid not in state["post_sent"]:
            telegram.send_text(telegram.format_news_post(ev, _bias_read(ev.get("forecast"), actual)))
            state["post_sent"].append(eid)
            sent += 1
            print(f"  📰 post-alert sent: {ev.get('title')} ({currency}) actual={actual}")

        # POST-ALERT FALLBACK: some releases never get a numeric "actual" —
        # qualitative/event-type items like FOMC Minutes, speeches, or
        # testimony, plus any numeric print the free calendar feed is slow
        # to fill in. Without this, those events sat as "awaiting data"
        # forever and nothing was ever sent. After a grace period past
        # release with still no actual, send one fallback alert instead of
        # silently waiting.
        elif (not has_actual and mins_until <= -NEWS_POST_GRACE_MIN
                and eid not in state["post_sent"]):
            telegram.send_text(telegram.format_news_post(
                ev, "لا توجد قراءة رقمية لهذا الحدث (حدث نوعي مثل محضر اجتماع/خطاب/شهادة، "
                    "أو أن المصدر لم يحدّث القيمة الفعلية بعد) — يُرجى المراجعة يدويًا إذا "
                    "كان هذا الحدث مهمًا لتحليلك."))
            state["post_sent"].append(eid)
            sent += 1
            print(f"  📰 post-alert (no-data fallback) sent: {ev.get('title')} ({currency})")

    _save_state(state)
    print(f"  {sent} news alert(s) sent this run" if sent else "  no news alerts due this run")

    try:
        import dashboard_export as dash
        dash.record_news(red_folder)
    except Exception as e:
        print(f"    ⚠ dashboard export failed: {e}")
