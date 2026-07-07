"""
Unified data layer.

  TwelveData  → 15m / 1h / 4h intraday bars (scalp/swing/council/forecast)
  yfinance    → 1d / 1w  daily/weekly bars   (forecast higher TF + COT charts)
  OKX         → BTC/ETH spot OHLCV           (Binance/Bybit blocked on GitHub Actions)
  CFTC API    → COT Index 0-100 (official)  + exact spec net + 20-wk %ile
                (insider-week.com scraping was removed 2026-07 — that site
                 restructured its URLs and every endpoint started 404ing;
                 CFTC's own public Socrata API is the primary source now)
"""

import time
import re
import requests
import pandas as pd
import numpy as np
from config import TWELVEDATA_KEY, MARKETS, COT_LOOKBACK, COT_EXTREME_LONG, COT_EXTREME_SHORT

# ── TwelveData ────────────────────────────────────────────────────────────────

_TD_BASE = "https://api.twelvedata.com/time_series"

def fetch_td(asset: str, interval: str, bars: int = 300) -> pd.DataFrame | None:
    """Fetch OHLCV from TwelveData.  interval: '15min' | '1h' | '4h' | '1day'"""
    symbol = MARKETS[asset]["td"]
    try:
        r = requests.get(_TD_BASE, params={
            "symbol": symbol, "interval": interval,
            "outputsize": bars, "apikey": TWELVEDATA_KEY,
        }, timeout=20)
        r.raise_for_status()
        d = r.json()
        if d.get("status") == "error":
            print(f"  [TD] {asset} {interval}: {d.get('message')}")
            return None
        rows = [{
            "timestamp": pd.Timestamp(v["datetime"], tz="UTC"),
            "open":   float(v["open"]),
            "high":   float(v["high"]),
            "low":    float(v["low"]),
            "close":  float(v["close"]),
            "volume": float(v.get("volume", 0) or 0),
        } for v in d.get("values", [])]
        if not rows:
            return None
        return (pd.DataFrame(rows)
                  .drop_duplicates("timestamp")
                  .set_index("timestamp")
                  .sort_index())
    except Exception as e:
        print(f"  [TD] {asset} {interval} failed: {e}")
        return None


# ── yfinance (daily/weekly) ───────────────────────────────────────────────────

def fetch_yf(asset: str, period: str = "2y", interval: str = "1d") -> pd.DataFrame | None:
    try:
        import yfinance as yf
        symbol = MARKETS[asset]["yf"]
        df = yf.download(symbol, period=period, interval=interval,
                         auto_adjust=True, progress=False)
        if df is None or len(df) < 10:
            return None
        df = df.dropna()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index, utc=True)
        return df
    except Exception as e:
        print(f"  [YF] {asset} {interval} failed: {e}")
        return None


# ── OKX (BTC/ETH — Binance/Bybit blocked on GH Actions) ─────────────────────

_OKX_INT = {"15min": "15m", "1h": "1H", "4h": "4H", "1day": "1D"}

def fetch_okx(asset: str, interval: str, bars: int = 300) -> pd.DataFrame | None:
    inst_id = "BTC-USDT" if asset == "BTCUSD" else "ETH-USDT"
    bar_code = _OKX_INT.get(interval, "1H")
    try:
        r = requests.get("https://www.okx.com/api/v5/market/candles",
                         params={"instId": inst_id, "bar": bar_code, "limit": min(bars, 300)},
                         timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return None
        rows = [{
            "timestamp": pd.Timestamp(int(v[0]), unit="ms", tz="UTC"),
            "open": float(v[1]), "high": float(v[2]),
            "low":  float(v[3]), "close": float(v[4]),
            "volume": float(v[5]),
        } for v in data]
        return (pd.DataFrame(rows)
                  .drop_duplicates("timestamp")
                  .set_index("timestamp")
                  .sort_index())
    except Exception as e:
        print(f"  [OKX] {asset} {interval} failed: {e}")
        return None


def fetch_intraday(asset: str, interval: str, bars: int = 300) -> pd.DataFrame | None:
    """Route to OKX for BTC/ETH, TwelveData for everything else."""
    if asset in ("BTCUSD", "ETHUSD"):
        df = fetch_okx(asset, interval, bars)
        if df is not None:
            return df
    return fetch_td(asset, interval, bars)


# ── Insider-week COT (0-100 Index) ───────────────────────────────────────────

_IW_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)",
}

# ── COT Index (0-100), official CFTC source ──────────────────────────────────
# Replaces the old insider-week.com scrape (site restructured, all URLs 404 now).
# Same min-max-normalized 0-100 index style, same signal thresholds, same
# return shape ({"net","change","cot_index","signal","date"}) — so
# forecast_engine.py / swing_engine.py / telegram.py need no changes.

_cot_index_cache = {"ts": None, "data": {}}
_COT_CACHE_TTL_MIN = 180   # COT reports only update weekly — no need to refetch often


def _fetch_cftc_index(cot_name: str, lookback: int = COT_LOOKBACK) -> dict | None:
    url = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    params = {
        "$limit":  lookback + 5,
        "$order":  "report_date_as_yyyy_mm_dd DESC",
        "$where":  f"market_and_exchange_names = '{cot_name}'",
        "$select": "report_date_as_yyyy_mm_dd,noncomm_positions_long_all,noncomm_positions_short_all",
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        raw = r.json()
        if not raw:
            return None
        df = pd.DataFrame(raw)
        df["report_date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"], utc=True)
        df = df.sort_values("report_date")
        df["noncomm_positions_long_all"]  = pd.to_numeric(df["noncomm_positions_long_all"],  errors="coerce")
        df["noncomm_positions_short_all"] = pd.to_numeric(df["noncomm_positions_short_all"], errors="coerce")
        df["spec_net"] = df["noncomm_positions_long_all"] - df["noncomm_positions_short_all"]
        nets = df["spec_net"].dropna().tolist()
        if not nets:
            return None
        latest_net = nets[-1]
        prev_net   = nets[-2] if len(nets) >= 2 else nets[-1]
        mn, mx = min(nets), max(nets)
        idx = round((latest_net - mn) / (mx - mn) * 100) if mx != mn else 50
        signal = ("BULLISH" if idx <= COT_EXTREME_SHORT else
                  "BEARISH" if idx >= COT_EXTREME_LONG else "NEUTRAL")
        return {
            "net":       int(latest_net),
            "change":    int(latest_net - prev_net),
            "cot_index": int(idx),
            "signal":    signal,
            "date":      str(df["report_date"].iloc[-1].date()),
        }
    except Exception as e:
        print(f"  [CFTC-index] {cot_name} failed: {e}")
        return None


def fetch_all_cot() -> dict:
    """Fetch the COT index for every configured market, cached for
    _COT_CACHE_TTL_MIN minutes so a caller that loops per-asset (like the
    forecast layer) doesn't re-hit the CFTC API once per asset per run."""
    global _cot_index_cache
    now = pd.Timestamp.now(tz="UTC")
    if _cot_index_cache["ts"] and (now - _cot_index_cache["ts"]) < pd.Timedelta(minutes=_COT_CACHE_TTL_MIN):
        return _cot_index_cache["data"]

    results = {}
    for market, cfg in MARKETS.items():
        cot_name = cfg.get("cot_name")
        results[market] = _fetch_cftc_index(cot_name) if cot_name else None

    _cot_index_cache = {"ts": now, "data": results}
    return results


# ── CFTC API (exact spec net + 20-week percentile for swing quality) ─────────

def fetch_cftc_cot(asset: str) -> dict:
    cot_name = MARKETS[asset].get("cot_name")
    if not cot_name:
        return {}
    url = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    params = {
        "$limit":  60, "$order": "report_date_as_yyyy_mm_dd DESC",
        "$where":  f"upper(market_and_exchange_names) like '%{cot_name.split(' - ')[0]}%'",
        "$select": "*",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        raw = r.json()
        if not raw:
            return {}
        df = pd.DataFrame(raw)
        exact = df[df["market_and_exchange_names"] == cot_name]
        if not exact.empty:
            df = exact
        df["report_date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"], utc=True)
        df = df.sort_values("report_date")
        for col in ["noncomm_positions_long_all", "noncomm_positions_short_all",
                    "comm_positions_long_all", "comm_positions_short_all", "open_interest_all"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["spec_net"] = df["noncomm_positions_long_all"] - df["noncomm_positions_short_all"]
        df["comm_net"] = df["comm_positions_long_all"]    - df["comm_positions_short_all"]
        spec = df["spec_net"].dropna()
        latest = float(spec.iloc[-1])
        pct_20 = float((spec.tail(20) < latest).mean())
        return {
            "spec_net":     int(latest),
            "comm_net":     int(df["comm_net"].iloc[-1]),
            "pct_rank_20w": round(pct_20, 2),
            "report_date":  str(df["report_date"].iloc[-1].date()),
        }
    except Exception as e:
        print(f"  [CFTC] {asset} failed: {e}")
        return {}


# ── Macro context (shared across layers) ─────────────────────────────────────

_news_cache     = None
_news_raw_cache = None
_dxy_cache      = None


def _fetch_ff_calendar_raw() -> list:
    """Raw ForexFactory 'this week' calendar feed, all impact levels, all
    fields (title/country/date/impact/forecast/previous/actual). Cached
    in-process — every caller (news gate, dollar bias, news agent) shares
    one HTTP call per run instead of each fetching it separately."""
    global _news_raw_cache
    if _news_raw_cache is not None:
        return _news_raw_cache
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        _news_raw_cache = r.json()
        print(f"  [NEWS] {len(_news_raw_cache)} calendar events loaded")
    except Exception as e:
        print(f"  [NEWS] unavailable ({e})")
        _news_raw_cache = []
    return _news_raw_cache


def fetch_news_events_raw() -> list:
    """All calendar events (any impact) with forecast/previous/actual kept,
    for the news agent's pre/post alerts."""
    out = []
    for e in _fetch_ff_calendar_raw():
        try:
            t = pd.Timestamp(e.get("date")).tz_convert("UTC")
        except Exception:
            continue
        out.append({
            "currency": e.get("country", "").upper(),
            "title":    e.get("title", ""),
            "impact":   str(e.get("impact", "")).lower(),
            "time":     t,
            "forecast": e.get("forecast"),
            "previous": e.get("previous"),
            "actual":   e.get("actual"),
        })
    return out


def fetch_news_events() -> list:
    """High-impact events only, minimal fields — used by the news-block gate
    and dollar-bias helper below."""
    global _news_cache
    if _news_cache is not None:
        return _news_cache
    _news_cache = [
        {"currency": e["currency"], "title": e["title"], "time": e["time"]}
        for e in fetch_news_events_raw() if e["impact"] == "high"
    ]
    return _news_cache


_ASSET_CURRENCIES = {
    "EURUSD": {"EUR", "USD"}, "GBPUSD": {"GBP", "USD"},
    "USDJPY": {"JPY", "USD"}, "DXY": {"USD"},
    "XAUUSD": {"USD"}, "SPX500": {"USD"}, "US100": {"USD"},
    "BTCUSD": set(), "ETHUSD": set(),
}


def news_blocked(asset: str, buffer_min: int = 45) -> str:
    currencies = _ASSET_CURRENCIES.get(asset, set())
    if not currencies:
        return ""
    now = pd.Timestamp.now(tz="UTC")
    for ev in fetch_news_events():
        if ev["currency"] in currencies:
            if abs((ev["time"] - now).total_seconds()) < buffer_min * 60:
                return f"{ev['currency']} {ev['title']}"
    return ""


def dollar_bias() -> str:
    global _dxy_cache
    if _dxy_cache:
        return _dxy_cache
    df = fetch_td("EURUSD", "1h", 60)
    if df is None or len(df) < 25:
        _dxy_cache = "USD_NEUTRAL"
        return _dxy_cache
    c = df["close"]
    ema20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
    chg   = c.iloc[-1] / c.iloc[-24] - 1
    if c.iloc[-1] < ema20 and chg < -0.002:
        _dxy_cache = "USD_STRONG"
    elif c.iloc[-1] > ema20 and chg > 0.002:
        _dxy_cache = "USD_WEAK"
    else:
        _dxy_cache = "USD_NEUTRAL"
    print(f"  [DXY] {_dxy_cache} (EUR 24h {chg:+.2%})")
    return _dxy_cache
