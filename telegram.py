"""
Telegram message formatter and sender — multi-layer OODA system.
"""

import requests
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ACCOUNT_SIZE, RISK_PCT, DASHBOARD_URL

TELEGRAM_BOT_TOKEN = TELEGRAM_TOKEN  # backward-compat alias
API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def _post(endpoint: str, **kwargs):
    resp = requests.post(f"{API}/{endpoint}", timeout=20, **kwargs)
    if not resp.ok:
        print(f"  [TG] {endpoint} failed: {resp.text[:200]}")
    return resp


def send_text(text: str, chat_id: str = TELEGRAM_CHAT_ID):
    _post("sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


def send_photo(image_bytes: bytes, caption: str = "", chat_id: str = TELEGRAM_CHAT_ID):
    _post("sendPhoto",
          files={"photo": ("chart.png", image_bytes, "image/png")},
          data={"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"})


def _bar(value: int, width: int = 8) -> str:
    filled = round(max(0, min(100, value)) / 100 * width)
    color  = "🔴" if value >= 75 else "🟢" if value <= 25 else "🟡"
    return f"[{'█'*filled}{'░'*(width-filled)}] {color}"


def _stars(score: int, max_s: int = 5) -> str:
    n = min(abs(score) // 2, max_s)
    return "⭐" * n + f" ({abs(score):+d})"


# ── Forecast (daily/weekly bias) ──────────────────────────────────────────────

def format_forecast(fc: dict) -> str:
    bias   = fc.get("bias", "NEUTRAL")
    conf   = fc.get("confidence", "LOW")
    score  = fc.get("score", 0)
    market = fc["market"]
    emoji  = "📈" if bias == "BULLISH" else "📉" if bias == "BEARISH" else "⬛"
    conf_e = {"HIGH": "🔥", "MEDIUM": "✅", "LOW": "⚠️"}.get(conf, "")
    cot_idx = fc.get("cot_index")
    cot_str = f"COT: {cot_idx}/100  {_bar(cot_idx)}" if cot_idx is not None else "COT: no data"

    lines  = [
        f"{emoji} <b>FORECAST: {market} — {bias}</b>  {conf_e} {conf}",
        f"Score: {score:+d}   {cot_str}",
        "",
    ]
    for r in fc.get("reasons", [])[:6]:
        lines.append(f"  • {r}")

    lvl = fc.get("levels", {})
    if lvl.get("weekly_ema200"):
        lines += ["", f"  📌 Weekly EMA200: {lvl['weekly_ema200']:.5g}  |  EMA50: {lvl.get('weekly_ema50', 0):.5g}"]
    return "\n".join(lines)


# ── Swing signal ──────────────────────────────────────────────────────────────

def format_swing(sig: dict, plan: dict) -> str:
    market    = sig["market"]
    direction = sig.get("direction", "—")
    score     = sig.get("score", 0)
    structure = sig.get("structure", "—")
    is_watch  = "WATCHLIST" in (direction or "")
    d_clean   = (direction or "").replace("WATCHLIST_", "")
    status    = "👀 WATCHLIST" if is_watch else "🎯 SWING SIGNAL"
    dir_emoji = "📈" if "LONG" in (direction or "") else "📉"

    lines = [
        f"<b>{status}: {dir_emoji} {market} — {d_clean}</b>",
        f"Score: {_stars(score)}   Structure: {structure}",
        f"Confidence: {sig.get('confidence','?')}",
        "",
    ]

    p = sig.get("price", {})
    if p:
        lines += [
            "<b>📊 Daily</b>",
            f"  Price: {p.get('close',0):.5g}  |  ATR: {p.get('atr',0):.5g}  |  RSI: {p.get('rsi',0):.1f}  |  ADX: {p.get('adx',0):.1f}",
            f"  EMA20: {p.get('ema20',0):.5g}  EMA50: {p.get('ema50',0):.5g}  EMA200: {p.get('ema200',0):.5g}",
            "",
        ]

    if not is_watch and plan:
        lines += [
            "<b>🎯 Swing Setup</b>",
            f"  Entry:  {plan.get('entry',0):.5g}",
            f"  SL:     {plan.get('sl',0):.5g}   (R: {plan.get('risk_pts',0):.5g})",
            f"  TP1:    {plan.get('tp1',0):.5g}   (1:{plan.get('rr1',0)})",
            f"  TP2:    {plan.get('tp2',0):.5g}   (1:{plan.get('rr2',0)})",
            f"  Size:   {plan.get('sizing_str','—')}",
            "",
        ]

    lines += ["<b>📋 Reasons</b>"]
    for r in sig.get("reasons", [])[:6]:
        lines.append(f"  • {r}")

    lines.append(f"\n<i>{p.get('date','—')} | Swing Layer</i>")
    return "\n".join(lines)


# ── Intraday signal ───────────────────────────────────────────────────────────

def format_intraday(sig: dict, plan: dict) -> str:
    market    = sig["market"]
    direction = sig.get("direction", "—")
    score     = sig.get("score", 0)
    session   = sig.get("session", "")
    h1_trend  = sig.get("h1_trend", "")
    is_watch  = "WATCHLIST" in (direction or "")
    d_clean   = (direction or "").replace("WATCHLIST_", "")
    status    = "⏱️ INTRADAY WATCHLIST" if is_watch else "⚡ INTRADAY SIGNAL"
    dir_emoji = "📈" if "LONG" in (direction or "") else "📉"

    lines = [
        f"<b>{status}: {dir_emoji} {market} — {d_clean}</b>",
        f"Score: {_stars(score)}   1H: {h1_trend}",
        f"🕐 {session}",
        "",
    ]

    p = sig.get("price", {})
    m15 = sig.get("m15_vals", {})
    if p:
        lines += [
            f"  1H Close: {p.get('close',0):.5g}  RSI: {p.get('rsi',0):.1f}  MACD: {p.get('macd',0):.5g}",
        ]
    if m15:
        lines += [
            f"  15M Close: {m15.get('close',0):.5g}  Stoch: {m15.get('stoch_k',0):.0f}/{m15.get('stoch_d',0):.0f}  BB: [{m15.get('bb_lower',0):.5g}–{m15.get('bb_upper',0):.5g}]",
            "",
        ]

    if not is_watch and plan:
        lines += [
            "<b>🎯 Intraday Setup</b>",
            f"  Entry: {plan.get('entry',0):.5g}  SL: {plan.get('sl',0):.5g}  TP1: {plan.get('tp1',0):.5g}  TP2: {plan.get('tp2',0):.5g}",
            f"  R:R 1:{plan.get('rr1',0)} / 1:{plan.get('rr2',0)}   Size: {plan.get('sizing_str','—')}",
            "",
        ]

    lines += ["<b>📋 Key Reasons</b>"]
    for r in sig.get("reasons", [])[:5]:
        lines.append(f"  • {r}")

    return "\n".join(lines)


# ── Scalp signal ──────────────────────────────────────────────────────────────

def format_scalp(sig: dict) -> str:
    market    = sig["market"]
    direction = sig.get("direction", "—")
    score     = sig.get("score", 0)
    quality   = sig.get("quality", "?")
    session   = sig.get("session", "")
    plan      = sig.get("plan", {})
    d_clean   = (direction or "").replace("WATCHLIST_", "")
    dir_emoji = "📈" if "LONG" in (direction or "") else "📉"
    q_emoji   = {"A+": "🔥", "A": "✅", "B": "👀", "C": "⬛"}.get(quality, "")

    lines = [
        f"<b>⚡ SCALP {q_emoji}{quality}: {dir_emoji} {market} — {d_clean}</b>",
        f"Score: {score:+d}   🕐 {session}",
        "",
    ]

    p = sig.get("price", {})
    if p:
        lines += [
            f"  5M: {p.get('close',0):.5g}  RSI: {p.get('rsi',0):.1f}  Stoch: {p.get('stoch_k',0):.0f}",
            f"  BB: [{p.get('bb_lower',0):.5g} — {p.get('bb_upper',0):.5g}]",
            "",
        ]

    if plan:
        lines += [
            "<b>⚡ Scalp Levels</b>",
            f"  Entry: {plan.get('entry',0):.5g}  |  SL: {plan.get('sl',0):.5g}  (risk {plan.get('risk_pts',0):.5g})",
            f"  TP1: {plan.get('tp1',0):.5g} (1:{plan.get('rr1',0)})  |  TP2: {plan.get('tp2',0):.5g} (1:{plan.get('rr2',0)})",
            f"  Risk: ${plan.get('risk_usd', ACCOUNT_SIZE * RISK_PCT):.2f}",
            "",
        ]

    for r in sig.get("reasons", [])[:4]:
        lines.append(f"  • {r}")

    return "\n".join(lines)


# ── COT Map ───────────────────────────────────────────────────────────────────

def format_cot_map(cot_map: list[dict], summary: str = "") -> str:
    lines = ["<b>🗺️ COT MAP — Institutional Positioning</b>", ""]
    if summary:
        lines += [summary, ""]
    for item in cot_map:
        idx    = item.get("cot_index", 50)
        sig    = item.get("signal", "NEUTRAL")
        change = item.get("change", 0)
        emoji  = "🟢" if sig == "BULLISH" else "🔴" if sig == "BEARISH" else "⬛"
        arrow  = "▲" if change > 0 else "▼" if change < 0 else "─"
        lines.append(f"{emoji} <b>{item['market']:<10}</b>  {idx:>3}/100  {_bar(idx)}  {arrow}{abs(change):,}")
    lines += ["", "<i>&lt;25=extreme short🟢  &gt;75=extreme long🔴</i>"]
    if DASHBOARD_URL:
        lines += ["", f'🔗 <a href="{DASHBOARD_URL}/#cot">Full COT report + history on dashboard</a>']
    return "\n".join(lines)


# ── Full daily brief ──────────────────────────────────────────────────────────

def format_daily_header(date_str: str, forecasts: dict) -> str:
    lines = [
        f"<b>📊 DAILY BRIEF — {date_str}</b>",
        "",
        "<b>Bias Summary:</b>",
    ]
    for m, fc in forecasts.items():
        bias = fc.get("bias", "NEUTRAL")
        conf = fc.get("confidence", "LOW")
        e    = "📈" if bias == "BULLISH" else "📉" if bias == "BEARISH" else "⬛"
        lines.append(f"  {e} {m:<10} {bias:<8}  [{conf}]")
    return "\n".join(lines)


# ── Public send functions ─────────────────────────────────────────────────────

def send_daily_brief(date_str: str, forecasts: dict, cot_map: list[dict]):
    send_text(format_daily_header(date_str, forecasts))
    send_text(format_cot_map(cot_map))


def send_swing_signal(sig: dict, plan: dict, chart_bytes: bytes | None = None):
    msg = format_swing(sig, plan)
    if chart_bytes:
        send_photo(chart_bytes, caption=f"SWING {sig.get('direction','')} {sig['market']} | Score {sig.get('score',0):+d}")
    send_text(msg)


def send_intraday_signal(sig: dict, plan: dict, chart_bytes: bytes | None = None):
    msg = format_intraday(sig, plan)
    if chart_bytes:
        send_photo(chart_bytes, caption=f"INTRADAY {sig.get('direction','')} {sig['market']}")
    send_text(msg)


def send_scalp_signal(sig: dict, chart_bytes: bytes | None = None):
    msg = format_scalp(sig)
    if chart_bytes:
        send_photo(chart_bytes, caption=f"SCALP {sig.get('direction','')} {sig['market']} [{sig.get('quality','')}]")
    send_text(msg)


def send_error(msg: str):
    send_text(f"⚠️ TradingBot Error:\n<code>{msg[:500]}</code>")


# ── New-layer formatters ──────────────────────────────────────────────────────

def format_scalp_new(sig: dict) -> str:
    asset = sig["asset"]
    from config import MARKETS
    dec   = MARKETS.get(asset, {}).get("decimals", 5)
    d     = sig.get("direction", "?")
    arrow = "🟢" if d == "LONG" else "🔴"
    score = sig.get("score", 0)
    rr    = sig.get("rr", 0)
    tp    = "💫 REVERSAL" if sig.get("setup_type") == "REV" else "⚡ CONTINUATION"
    factors = "\n".join(f"  • {f}" for f in sig.get("factors", [])[:5])
    return (
        f"{arrow} <b>SCALP {d} — {asset}</b> {tp}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Score: {score}/9  R:R <b>{rr:.1f}</b>\n"
        f"Entry:  <code>{sig.get('entry',0):.{dec}f}</code>\n"
        f"Stop:   <code>{sig.get('stop',0):.{dec}f}</code>\n"
        f"TP1:    <code>{sig.get('tp1',0):.{dec}f}</code>\n"
        f"TP2:    <code>{sig.get('tp2',0):.{dec}f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{factors}"
    )


def format_council(verdict: dict) -> str:
    asset = verdict["asset"]
    from config import MARKETS
    dec   = MARKETS.get(asset, {}).get("decimals", 5)
    v     = verdict.get("verdict", "NO_TRADE")
    arrow = "🟢" if v == "LONG" else "🔴" if v == "SHORT" else "⚪"
    conf  = int(verdict.get("confidence", 0) * 100)
    rr    = verdict.get("risk_reward", 0)
    bulls = verdict.get("bulls", 0); bears = verdict.get("bears", 0)
    kf    = "\n".join(f"  • {f}" for f in verdict.get("key_factors", [])[:4])
    vote_lines = ""
    for vote in verdict.get("votes", [])[:7]:
        ic = "🟢" if vote["bias"] == "BULLISH" else "🔴" if vote["bias"] == "BEARISH" else "⚪"
        vote_lines += f"  {ic} {vote.get('emoji','')} {vote['agent']}: {vote['bias']}\n"
    ez = verdict.get("entry_zone", [0, 0])
    e0 = ez[0] if isinstance(ez, (list, tuple)) and len(ez) >= 1 else 0
    return (
        f"{arrow} <b>COUNCIL {v} — {asset}</b> {verdict.get('mode','').upper()}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Confidence: <b>{conf}%</b>  R:R <b>{rr:.1f}</b>\n"
        f"Votes: {bulls}🟢 / {bears}🔴 / 7\n"
        f"Entry:  <code>{e0:.{dec}f}</code>\n"
        f"Stop:   <code>{verdict.get('stop_loss',0):.{dec}f}</code>\n"
        f"TP1:    <code>{verdict.get('target_1',0):.{dec}f}</code>\n"
        f"TP2:    <code>{verdict.get('target_2',0):.{dec}f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{vote_lines}"
        f"{kf}\n\n"
        f"<i>{verdict.get('reasoning','')[:200]}</i>"
    )


def format_swing_new(sig: dict) -> str:
    asset = sig["asset"]
    from config import MARKETS
    dec   = MARKETS.get(asset, {}).get("decimals", 5)
    d     = sig.get("verdict", sig.get("direction", "?"))
    arrow = "🟢" if d == "LONG" else "🔴"
    conf  = int(sig.get("confidence", 0) * 100)
    rr    = sig.get("risk_reward", sig.get("rr", 0))
    reasons = "\n".join(f"  • {r}" for r in sig.get("key_reasons", sig.get("factors", []))[:4])
    cot_line = ""
    cot = sig.get("cot_iw")
    if cot:
        cot_line = f"\n📊 COT: {cot.get('cot_index',0)}/100 ({cot.get('signal','?')})"
    return (
        f"{arrow} <b>SWING {d} — {asset}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Confidence: <b>{conf}%</b>  R:R <b>{rr:.1f}</b>\n"
        f"Entry:  <code>{sig.get('entry',0):.{dec}f}</code>\n"
        f"Stop:   <code>{sig.get('stop_loss',sig.get('stop',0)):.{dec}f}</code>\n"
        f"TP1:    <code>{sig.get('target_1',sig.get('tp1',0)):.{dec}f}</code>\n"
        f"TP2:    <code>{sig.get('target_2',sig.get('tp2',0)):.{dec}f}</code>\n"
        f"Hold:   ~{sig.get('hold_hours',24)}h{cot_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{reasons}\n\n"
        f"<i>{sig.get('reasoning','')[:200]}</i>"
    )


def format_news_pre(ev: dict) -> str:
    """Pre-alert — field labels/values stay English (Time/Previous/Forecast,
    event title, currency code); the explanatory note is Arabic per user request."""
    t = ev["time"]
    return (
        "<b>🔔 حدث اقتصادي مرتقب — USD</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{ev.get('title','?')}</b>  ({ev.get('currency','?')})\n"
        f"Time:      {t.strftime('%H:%M UTC')}  (~15 min)\n"
        f"Previous:  <b>{ev.get('previous') or 'n/a'}</b>\n"
        f"Forecast:  <b>{ev.get('forecast') or 'n/a'}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>عادةً ما يكون تجاوز التوقعات (Beat) داعمًا للدولار وضاغطًا على الذهب "
        f"والمؤشرات، بينما يكون القصور عن التوقعات (Miss) مضعفًا للدولار وداعمًا "
        f"للذهب والمؤشرات — هذا ميل تاريخي وليس قاعدة ثابتة.</i>"
    )


def format_news_post(ev: dict, bias_read: str) -> str:
    """Post-alert — field labels/values stay English; bias_read (built by
    news_agent._bias_read) already carries the Arabic explanatory note."""
    t = ev["time"]
    return (
        "<b>📊 صدر الحدث الاقتصادي — USD</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{ev.get('title','?')}</b>  ({ev.get('currency','?')})  · {t.strftime('%H:%M UTC')}\n"
        f"Previous:  <b>{ev.get('previous') or 'n/a'}</b>\n"
        f"Forecast:  <b>{ev.get('forecast') or 'n/a'}</b>\n"
        f"Actual:    <b>{ev.get('actual') or 'n/a'}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{bias_read}"
    )


def format_tracer_update(pos: dict, progress_pct: float, current_price: float) -> str:
    direction = pos.get("direction", "?")
    return (
        "<b>🧭 Tracer — position update</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{pos.get('asset','?')}</b>  {direction}  · {pos.get('layer','')}\n"
        f"Entry: {pos.get('entry')}  Current: {current_price}\n"
        f"Progress to target: <b>{progress_pct:.0%}</b>\n"
        f"SL: {pos.get('stop')}  TP1: {pos.get('tp1')}  TP2: {pos.get('tp2')}"
    )


_GOLD_TF_SHORT = {"gold_scalp": "M15", "gold_swing": "H1"}


def format_gold_signal_short(sig: dict) -> str:
    """Compact summary card — just the trade numbers, no factors/reasoning/
    dashboard link. Sent as the primary alert; format_gold_signal() below
    follows immediately after as a detailed second message for anyone who
    wants the full confluence breakdown."""
    direction = sig.get("direction", "?")
    arrow = "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else "⚪"
    dec = 2
    rr = sig.get("risk_reward", 0)
    tf = _GOLD_TF_SHORT.get(sig.get("layer", ""), sig.get("timeframe", "-"))
    lines = [
        f"{arrow} <b>{direction} SIGNAL</b> — XAUUSD",
        "━━━━━━━━━━━━━━━━━━",
        f"📍 Entry  <code>{sig.get('entry', 0):.{dec}f}</code>",
        f"🛡 SL     <code>{sig.get('stop_loss', 0):.{dec}f}</code>",
        f"🎯 TP1    <code>{sig.get('target_1', 0):.{dec}f}</code>",
        f"🎯 TP2    <code>{sig.get('target_2', 0):.{dec}f}</code>",
        f"🎯 TP3    <code>{sig.get('target_3', 0):.{dec}f}</code>",
        f"📊 RR     1 : {rr:.1f}",
        f"⏱ TF     {tf}",
    ]
    return "\n".join(lines)


def format_gold_signal(sig: dict) -> str:
    """Formatter for gold_engine.py's scalp/swing signals. Uses the same
    signal taxonomy (SNIPER etc.) as before, with Sniper getting the same
    louder treatment it's had throughout this project."""
    from gold_engine import SIGNAL_TAXONOMY
    dec = 2
    label = sig.get("signal_label", "NO_SIGNAL")
    emoji, name, desc = SIGNAL_TAXONOMY.get(label, ("📊", label, ""))
    is_sniper = label == "SNIPER"
    direction = sig.get("direction", "?")
    arrow = "🟢" if direction == "LONG" else "🔴"
    conf = sig.get("confidence", 0)
    rr = sig.get("risk_reward", 0)
    layer = sig.get("layer", "gold").replace("gold_", "").upper()
    session = sig.get("session", "")
    factors = "\n".join(f"  • {f}" for f in sig.get("factors", [])[:6])

    header = f"🎯🎯🎯 <b>SNIPER SETUP</b> 🎯🎯🎯" if is_sniper else f"{emoji} <b>{name}</b>"
    lines = [
        header,
        f"<i>{desc}</i>" if desc else "",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"{arrow} <b>GOLD {direction}</b> — {layer}  · conf {conf:.0%}  · {session}",
        f"Entry:  <code>{sig.get('entry', 0):.{dec}f}</code>",
        f"Stop:   <code>{sig.get('stop_loss', 0):.{dec}f}</code>",
        f"TP1:    <code>{sig.get('target_1', 0):.{dec}f}</code>  (1:{rr:.1f})",
        f"TP2:    <code>{sig.get('target_2', 0):.{dec}f}</code>",
        f"TP3:    <code>{sig.get('target_3', 0):.{dec}f}</code>",
        "━━━━━━━━━━━━━━━━━━━━━",
        factors,
        "",
        f"<i>{sig.get('reasoning', '')[:300]}</i>",
    ]
    if is_sniper:
        lines.append("\n🎯 <b>Extra focus:</b> highest-conviction confluence this "
                      "engine detects — session sweep + structure + COT all aligned.")
    if DASHBOARD_URL:
        lines.append(f'\n🔗 <a href="{DASHBOARD_URL}/">Full dashboard</a>')
    return "\n".join(l for l in lines if l is not None)


def format_forecast_new(fc: dict) -> str:
    asset = fc["asset"]
    bias  = fc.get("bias", "N/A")
    price = fc.get("price")
    from config import MARKETS
    dec   = MARKETS.get(asset, {}).get("decimals", 5)
    b_icon = "🟢" if "BULLISH" in str(bias) else "🔴" if "BEARISH" in str(bias) else "⚪"
    cot_line = ""
    cot = fc.get("cot_iw")
    if cot:
        cot_line = (f"\n📊 COT: {cot.get('cot_index',0)}/100 "
                    f"({cot.get('signal','?')}) net {cot.get('net',0):,}")
    pats = fc.get("patterns", [])
    pat_summary = ""
    if pats:
        counts: dict = {}
        for p in pats:
            counts[p["type"]] = counts.get(p["type"], 0) + 1
        pat_summary = "\n" + " | ".join(f"{k}×{v}" for k, v in counts.items())
    price_str = f"\nPrice: <code>{price:.{dec}f}</code>" if price else ""
    return (
        f"{b_icon} <b>{asset} FORECAST</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Bias: <b>{bias}</b>"
        f"{cot_line}"
        f"{price_str}"
        f"{pat_summary}"
    )
