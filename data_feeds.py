"""
Unified data layer.

  TwelveData  → 15m / 1h / 4h intraday bars (scalp/swing/council/forecast)
  yfinance    → 1d / 1w  daily/weekly bars   (forecast higher TF + COT charts)
  OKX         → BTC/ETH spot OHLCV           (Binance/Bybit blocked on GitHub Actions)
  insider-week→ COT Index 0-100              (weekly)
  CFTC API   → exact spec net + 20-wk %ile  (swing_bot quality)
"""

import time
import re
import requests
import pandas as pd
import numpy as np
from config import TWELVEDATA_KEY, MARKETS, COT_LOOKBACK

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

def _fetch_iw_asset(iw_path: str, lookback: int = COT_LOOKBACK) -> dict | None:
    url = f"https://insider-week.com/en/commitment-of-traders/{iw_path}/"
    try:
        resp = requests.get(url, headers=_IW_HEADERS, timeout=20)
        resp.raise_for_status()
        m = re.search(r"var\s+dataGraph\s*=\s*(\[.*?\]);", resp.text, re.DOTALL)
        if not m:
            return None
        entries_raw = re.findall(r"\{[^}]+\}", m.group(1))
        entries = []
        for e in entries_raw:
            dm = re.search(r"new Date\((\d+),(\d+),(\d+)\)", e)
            nm = re.search(r"NonCommercial:\s*(-?\d+)", e)
            if dm and nm:
                d = pd.Timestamp(int(dm[1]), int(dm[2]) + 1, int(dm[3]))
                entries.append({"date": d.strftime("%Y-%m-%d"), "net": int(nm[1])})
        if not entries:
            return None
        entries.sort(key=lambda x: x["date"])
        nets = [e["net"] for e in entries[-lookback:]]
        latest_net = nets[-1]
        prev_net   = nets[-2] if len(nets) >= 2 else nets[-1]
        mn, mx = min(nets), max(nets)
        idx = round((latest_net - mn) / (mx - mn) * 100) if mx != mn else 50
        signal = ("BULLISH" if idx <= 25 else "BEARISH" if idx >= 75 else "NEUTRAL")
        return {
            "net":       latest_net,
            "change":    latest_net - prev_net,
            "cot_index": idx,
            "signal":    signal,
            "date":      entries[-1]["date"],
        }
    except Exception as e:
        print(f"  [IW] {iw_path} failed: {e}")
        return None


def fetch_all_cot() -> dict:
    results = {}
    for market, cfg in MARKETS.items():
        path = cfg.get("iw_path")
        if path:
            results[market] = _fetch_iw_asset(path)
        else:
            results[market] = None
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

_news_cache = None
_dxy_cache  = None


def fetch_news_events() -> list:
    global _news_cache
    if _news_cache is not None:
        return _news_cache
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        _news_cache = [
            {"currency": e.get("country", "").upper(),
             "title":    e.get("title", ""),
             "time":     pd.Timestamp(e.get("date")).tz_convert("UTC")}
            for e in r.json()
            if str(e.get("impact", "")).lower() == "high"
        ]
        print(f"  [NEWS] {len(_news_cache)} high-impact events loaded")
    except Exception as e:
        print(f"  [NEWS] unavailable ({e})")
        _news_cache = []
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
