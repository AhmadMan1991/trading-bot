"""
=============================================================================
BTC/USDT AI TRADING BOT — FULL PIPELINE
=============================================================================
Data sources (all free, no API key required):
  1. OHLCV      — BTC/USDT 1h candles        (Bybit public API)
  2. FUNDING    — BTC/USDT perpetual rate     (Bybit Futures, every 8h)
  3. COT        — Bitcoin CME positioning     (CFTC Socrata, weekly)

Run modes:
  python pipeline.py            # full pipeline + risk + telegram
  python pipeline.py --once     # same as above (used by GitHub Actions)
  python pipeline.py --schedule # run every 4h forever
=============================================================================
"""

import time
import math
import random
import os
import json
import requests
import pandas as pd
from pathlib import Path


# =============================================================================
# CONFIG
# =============================================================================

SYMBOL          = "BTCUSDT"
TOTAL_CANDLES   = 2000      # ~83 days of 1h history
CANDLES_PER_REQ = 200       # Bybit max per request for linear klines
REQUEST_DELAY   = 0.3       # seconds between paginated requests
COT_LIMIT       = 200       # weekly COT records to fetch

DATA_ROOT = Path(__file__).parent / "data"


# =============================================================================
# HELPERS
# =============================================================================

def _make_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path)
    print(f"   Saved {len(df)} rows → {path}")


def _section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


# =============================================================================
# 1. OHLCV — Bybit V5 linear klines (no geo-restrictions, no API key)
#
#    GET https://api.bybit.com/v5/market/kline
#    params: category=linear, symbol, interval=60 (minutes), limit, end
#    Response: {"result": {"list": [[startTime,open,high,low,close,vol,...]]}}
#    Note: Bybit returns newest candle first — we reverse each batch.
# =============================================================================

def _fetch_from_bybit(total: int) -> pd.DataFrame:
    """Fetch 1h candles from Bybit (works everywhere including GitHub Actions)."""
    url      = "https://api.bybit.com/v5/market/kline"
    all_rows = []
    end_time = None
    remaining = total

    while remaining > 0:
        batch  = min(200, remaining)
        params = {"category":"linear","symbol":SYMBOL,"interval":"60","limit":batch}
        if end_time:
            params["end"] = end_time
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        raw  = resp.json().get("result",{}).get("list",[])
        if not raw:
            break
        for c in reversed(raw):
            all_rows.append({
                "timestamp": pd.to_datetime(int(c[0]), unit="ms", utc=True),
                "open": float(c[1]), "high": float(c[2]),
                "low" : float(c[3]), "close": float(c[4]),
                "volume": float(c[5]),
            })
        end_time   = int(raw[-1][0]) - 1
        remaining -= batch
        time.sleep(REQUEST_DELAY)
    return all_rows


def _fetch_from_okx(total: int) -> list:
    """Fetch 1h candles from OKX (backup, also works on GitHub Actions)."""
    url      = "https://www.okx.com/api/v5/market/history-candles"
    all_rows = []
    end_time = None
    remaining = total

    while remaining > 0:
        batch  = min(100, remaining)
        params = {"instId":"BTC-USDT-SWAP","bar":"1H","limit":batch}
        if end_time:
            params["after"] = end_time
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        raw  = resp.json().get("data",[])
        if not raw:
            break
        for c in reversed(raw):
            all_rows.append({
                "timestamp": pd.to_datetime(int(c[0]), unit="ms", utc=True),
                "open": float(c[1]), "high": float(c[2]),
                "low" : float(c[3]), "close": float(c[4]),
                "volume": float(c[5]),
            })
        end_time   = raw[-1][0]
        remaining -= batch
        time.sleep(REQUEST_DELAY)
    return all_rows


# =============================================================================
# C. 15M CANDLES — OKX (for scalp signal detection)
#
#    Same OKX endpoint, bar=15m instead of 1H.
#    Returns last 500 × 15m candles (~5 days of scalp data).
# =============================================================================

def fetch_15m_candles(total: int = 500) -> pd.DataFrame:
    """
    Fetch BTC/USDT 15m candles from OKX for scalp signal detection.
    Returns DataFrame with same columns as 1h OHLCV.
    """
    print(f"  Fetching {total} × 15m candles from OKX...")
    url      = "https://www.okx.com/api/v5/market/history-candles"
    all_rows = []
    end_time = None
    remaining = total

    while remaining > 0:
        batch  = min(100, remaining)
        params = {"instId": "BTC-USDT-SWAP", "bar": "15m", "limit": batch}
        if end_time:
            params["after"] = end_time
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            raw  = resp.json().get("data", [])
        except Exception as e:
            print(f"  ⚠ 15m fetch error: {e}")
            break
        if not raw:
            break
        for c in reversed(raw):
            all_rows.append({
                "timestamp": pd.to_datetime(int(c[0]), unit="ms", utc=True),
                "open"     : float(c[1]),
                "high"     : float(c[2]),
                "low"      : float(c[3]),
                "close"    : float(c[4]),
                "volume"   : float(c[5]),
            })
        end_time   = raw[-1][0]
        remaining -= batch
        time.sleep(0.2)

    if not all_rows:
        print("  ⚠ No 15m data returned")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df = (df.drop_duplicates(subset="timestamp")
            .set_index("timestamp")
            .sort_index()
            [["open", "high", "low", "close", "volume"]])
    print(f"  ✓ Got {len(df)} × 15m candles")
    return df


def compute_scalp_signals(df_15m: pd.DataFrame) -> dict:
    """
    Compute scalp signals from 15m candle data.
    Returns a dict of signal flags that gets merged into the main signal.

    SCALP_LONG  : RSI oversold on 15m + bullish MACD cross + volume spike
    SCALP_SHORT : RSI overbought on 15m + bearish MACD cross + volume spike
    """
    if df_15m.empty or len(df_15m) < 30:
        return {"scalp_long": False, "scalp_short": False,
                "scalp_rsi": 50.0, "scalp_macd_hist": 0.0,
                "scalp_vol_ratio": 1.0}

    c = df_15m["close"]
    v = df_15m["volume"]

    # RSI on 15m
    delta    = c.diff()
    gain     = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss     = (-delta).clip(lower=0).ewm(com=13, adjust=False).mean()
    rs       = gain / loss.replace(0, float("nan"))
    rsi_15m  = 100 - (100 / (1 + rs))

    # MACD on 15m
    ema12    = c.ewm(span=12, adjust=False).mean()
    ema26    = c.ewm(span=26, adjust=False).mean()
    macd     = ema12 - ema26
    signal   = macd.ewm(span=9,  adjust=False).mean()
    hist     = macd - signal

    # Volume spike on 15m
    vol_sma  = v.rolling(20).mean()
    vol_rat  = v / vol_sma.replace(0, float("nan"))

    last_rsi  = float(rsi_15m.iloc[-1])
    last_hist = float(hist.iloc[-1])
    prev_hist = float(hist.iloc[-2]) if len(hist) > 1 else 0
    last_vol  = float(vol_rat.iloc[-1])

    bullish_cross = (last_hist > 0) and (prev_hist <= 0)
    bearish_cross = (last_hist < 0) and (prev_hist >= 0)

    scalp_long  = (last_rsi < 35) and bullish_cross and (last_vol > 1.5)
    scalp_short = (last_rsi > 65) and bearish_cross and (last_vol > 1.5)

    return {
        "scalp_long"      : scalp_long,
        "scalp_short"     : scalp_short,
        "scalp_rsi"       : round(last_rsi, 1),
        "scalp_macd_hist" : round(last_hist, 4),
        "scalp_vol_ratio" : round(last_vol, 2),
    }


def fetch_ohlcv() -> pd.DataFrame:
    """
    Fetch BTC/USDT 1h candles.
    Tries OKX first (no geo-restrictions anywhere), then Bybit as fallback.
    """
    for source, fetcher in [("OKX",   _fetch_from_okx),
                             ("Bybit", _fetch_from_bybit)]:
        try:
            print(f"  Fetching {TOTAL_CANDLES} × 1h candles from {source}...")
            rows = fetcher(TOTAL_CANDLES)
            if not rows:
                print(f"  ⚠ {source} returned no data, trying next...")
                continue
            df = pd.DataFrame(rows)
            df = (df.drop_duplicates(subset="timestamp")
                    .set_index("timestamp")
                    .sort_index()
                    [["open","high","low","close","volume"]])
            print(f"  ✓ Got {len(df)} candles from {source}")
            return df
        except Exception as e:
            print(f"  ⚠ {source} failed: {e} — trying next source...")

    raise RuntimeError("All OHLCV sources failed (OKX + Bybit)")


def validate_ohlcv(df: pd.DataFrame) -> None:
    """Check for missing hourly candles."""
    full_range  = pd.date_range(df.index.min(), df.index.max(), freq="1h", tz="UTC")
    missing     = full_range.difference(df.index)
    pct_missing = len(missing) / len(full_range) * 100
    print(f"  Gap check : {len(df)}/{len(full_range)} candles present  "
          f"({pct_missing:.2f}% missing)")
    if len(missing) > 0:
        print(f"  ⚠ First 5 missing: {list(missing[:5])}")
    else:
        print("  ✓ No gaps found")


# =============================================================================
# 2. FUNDING RATE — Bybit V5 funding history (no geo-restrictions, no API key)
#
#    GET https://api.bybit.com/v5/market/funding/history
#    params: category=linear, symbol, limit
#    Response: {"result": {"list": [{"fundingRate","fundingRateTimestamp",...}]}}
#    Funding updates every 8h on Bybit perpetuals.
# =============================================================================

def fetch_funding() -> pd.DataFrame:
    """
    Fetch BTC/USDT perpetual funding rate.
    Tries OKX first, falls back to Bybit.
    OKX endpoint: GET https://www.okx.com/api/v5/public/funding-rate-history
    """
    # Try OKX first
    try:
        print(f"  Fetching funding rate from OKX...")
        url    = "https://www.okx.com/api/v5/public/funding-rate-history"
        params = {"instId": "BTC-USDT-SWAP", "limit": 100}
        resp   = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        raw    = resp.json().get("data", [])
        if raw:
            rows = []
            for item in raw:
                rows.append({
                    "timestamp"   : pd.to_datetime(int(item["fundingTime"]),
                                                   unit="ms", utc=True),
                    "funding_rate": float(item["fundingRate"]),
                })
            df = (pd.DataFrame(rows)
                    .set_index("timestamp")
                    .sort_index()
                    [["funding_rate"]])
            latest = df.iloc[-1]["funding_rate"]
            sign   = "🟢 bullish bias" if latest >= 0 else "🔴 bearish bias"
            print(f"  Latest funding rate (OKX): {latest:.6f} ({latest*100:.4f}%)  {sign}")
            return df
    except Exception as e:
        print(f"  ⚠ OKX funding failed: {e} — trying Bybit...")

    # Fallback: Bybit
    print(f"  Fetching funding rate from Bybit...")
    url    = "https://api.bybit.com/v5/market/funding/history"
    params = {"category": "linear", "symbol": SYMBOL, "limit": 200}
    resp   = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    raw    = resp.json().get("result", {}).get("list", [])
    rows   = []
    for item in raw:
        rows.append({
            "timestamp"   : pd.to_datetime(int(item["fundingRateTimestamp"]),
                                           unit="ms", utc=True),
            "funding_rate": float(item["fundingRate"]),
        })
    df = (pd.DataFrame(rows)
            .set_index("timestamp")
            .sort_index()
            [["funding_rate"]])
    latest = df.iloc[-1]["funding_rate"]
    sign   = "🟢 bullish bias" if latest >= 0 else "🔴 bearish bias"
    print(f"  Latest funding rate (Bybit): {latest:.6f} ({latest*100:.4f}%)  {sign}")
    return df


def align_funding_to_1h(funding: pd.DataFrame,
                         ohlcv_index: pd.DatetimeIndex) -> pd.DataFrame:
    """Forward-fill 8h funding rate onto every hourly timestamp."""
    return funding.reindex(ohlcv_index).ffill().bfill()


# =============================================================================
# 3. COT REPORT — CFTC Legacy report (free, no API key)
#
#    Dataset: https://publicreporting.cftc.gov/resource/6dca-aqww.json
#    Contract: BITCOIN - CHICAGO MERCANTILE EXCHANGE (weekly, Tuesday data)
#    Key cols: noncomm_positions_long/short_all, comm_positions_long/short_all
# =============================================================================

def fetch_cot() -> pd.DataFrame:
    """Fetch CFTC Legacy COT report for CME Bitcoin futures."""
    print("  Fetching CFTC COT data for Bitcoin futures (CME only)...")
    url    = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    params = {
        "$limit"  : COT_LIMIT,
        "$order"  : "report_date_as_yyyy_mm_dd DESC",
        "$where"  : "market_and_exchange_names = 'BITCOIN - CHICAGO MERCANTILE EXCHANGE'",
        "$select" : "*",
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        raw  = resp.json()
    except Exception as e:
        print(f"  ⚠ CFTC unreachable: {e}")
        raw = []

    if not raw:
        print("  ⏭ COT unavailable — continuing without it")
        return pd.DataFrame()

    df = pd.DataFrame(raw)
    df["report_date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"], utc=True)

    pos_cols = [c for c in df.columns if any(k in c for k in
                ["noncomm", "comm_pos", "open_interest"])]
    print(f"  COT columns found: {len(pos_cols)}")

    for col in pos_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    long_nc  = next((c for c in df.columns if c.startswith("noncomm") and "long"  in c and c.endswith("all")), None)
    short_nc = next((c for c in df.columns if c.startswith("noncomm") and "short" in c and c.endswith("all")), None)
    long_c   = next((c for c in df.columns if c.startswith("comm_")   and "long"  in c and c.endswith("all")), None)
    short_c  = next((c for c in df.columns if c.startswith("comm_")   and "short" in c and c.endswith("all")), None)
    oi_col   = next((c for c in df.columns if c == "open_interest_all"), None)

    if long_nc and short_nc:
        df["large_spec_net"] = df[long_nc] - df[short_nc]
        print(f"  ✓ large_spec_net = {long_nc} − {short_nc}")
    else:
        df["large_spec_net"] = float("nan")

    if long_c and short_c:
        df["commercial_net"] = df[long_c] - df[short_c]
        print(f"  ✓ commercial_net  = {long_c} − {short_c}")
    else:
        df["commercial_net"] = float("nan")

    df["open_interest_all"] = pd.to_numeric(df[oi_col], errors="coerce") if oi_col else float("nan")

    keep = ["report_date", "market_and_exchange_names",
            "open_interest_all", "large_spec_net", "commercial_net"]
    keep = [c for c in keep if c in df.columns]
    df   = df[keep].set_index("report_date").sort_index()

    print(f"  Contract  : {df['market_and_exchange_names'].iloc[-1]}")
    print(f"  Records   : {len(df)} weekly entries")
    if not df["large_spec_net"].isna().all():
        net  = df["large_spec_net"].iloc[-1]
        bias = "🟢 net long" if net > 0 else "🔴 net short"
        print(f"  Latest large-spec net: {net:,.0f}  {bias}")
    return df


def align_cot_to_1h(cot: pd.DataFrame,
                     ohlcv_index: pd.DatetimeIndex) -> pd.DataFrame:
    """Forward-fill weekly COT data onto the hourly index."""
    cols    = [c for c in ["large_spec_net", "commercial_net", "open_interest_all"]
               if c in cot.columns]
    aligned = cot[cols].reindex(ohlcv_index, method="ffill")
    return aligned.add_prefix("cot_")


# =============================================================================
# MERGE
# =============================================================================

def merge_all(ohlcv: pd.DataFrame,
              funding: pd.DataFrame,
              cot: pd.DataFrame) -> pd.DataFrame:
    """Join OHLCV + funding + COT on the hourly index."""
    merged = ohlcv.join(align_funding_to_1h(funding, ohlcv.index), how="left")

    if not cot.empty:
        merged = merged.join(align_cot_to_1h(cot, ohlcv.index), how="left")
    else:
        merged["cot_large_spec_net"]    = 0.0
        merged["cot_commercial_net"]    = 0.0
        merged["cot_open_interest_all"] = 0.0
        print("  ℹ COT columns set to 0 (data unavailable)")

    return merged


def print_summary(merged: pd.DataFrame) -> None:
    print(f"  Shape         : {merged.shape[0]} rows × {merged.shape[1]} columns")
    print(f"  Date range    : {merged.index.min()} → {merged.index.max()}")
    print(f"  Columns       : {list(merged.columns)}")
    nulls = merged.isnull().sum()
    if nulls.any():
        print(f"  ⚠ Null counts:")
        for col, n in nulls[nulls > 0].items():
            print(f"    {col}: {n}")
    else:
        print("  ✓ No nulls in merged dataset")
    print("\n  Last 3 rows:")
    print(merged.tail(3).to_string())



# STEP 2 — TECHNICAL INDICATORS
# =============================================================================
#
# All computed in pure pandas — no extra libraries needed.
#
# TREND       : EMA 20 / 50 / 200,  price position vs each EMA
# MOMENTUM    : RSI 14,  MACD (12/26/9)
# VOLUME      : Volume SMA 20,  volume spike flag (> 2× SMA)
# VOLATILITY  : ATR 14  (used later for position sizing & stop placement)
# STRUCTURE   : Swing highs / swing lows (rolling 10-bar lookback)
# DIVERGENCE  : Bull & bear RSI divergence detector
#               → feeds BULL_DIVERGENCE / BEAR_DIVERGENCE signal labels
# SIGNAL PREP : Composite columns agents will read directly

def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_macd(close: pd.Series,
                 fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast   = compute_ema(close, fast)
    ema_slow   = compute_ema(close, slow)
    macd_line  = ema_fast - ema_slow
    signal_line = compute_ema(macd_line, signal)
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_atr(high: pd.Series, low: pd.Series,
                close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()


def detect_swing_points(high: pd.Series, low: pd.Series,
                        lookback: int = 10) -> tuple:
    """
    Swing high  = bar whose high is the highest in a ±lookback window.
    Swing low   = bar whose low  is the lowest  in a ±lookback window.
    Returns two boolean Series (is_swing_high, is_swing_low).
    """
    roll_high = high.rolling(lookback * 2 + 1, center=True).max()
    roll_low  = low.rolling(lookback * 2 + 1, center=True).min()
    is_swing_high = (high == roll_high)
    is_swing_low  = (low  == roll_low)
    return is_swing_high, is_swing_low


def detect_rsi_divergence(close: pd.Series, rsi: pd.Series,
                           lookback: int = 30, swing_lookback: int = 5) -> tuple:
    """
    Bullish divergence  : price makes a LOWER low but RSI makes a HIGHER low
                          → potential reversal upward (BULL_DIVERGENCE signal)
    Bearish divergence  : price makes a HIGHER high but RSI makes a LOWER high
                          → potential reversal downward (BEAR_DIVERGENCE signal)

    Returns two boolean Series (bull_div, bear_div).
    Simple rolling-window implementation — sufficient for agent input.
    """
    bull_div = pd.Series(False, index=close.index)
    bear_div = pd.Series(False, index=close.index)

    for i in range(lookback, len(close)):
        window_price = close.iloc[i - lookback: i + 1]
        window_rsi   = rsi.iloc[i - lookback: i + 1]

        # Bullish: current price near window low AND RSI higher than its window low
        price_low_idx = window_price.idxmin()
        rsi_low_idx   = window_rsi.idxmin()
        if (close.iloc[i] <= window_price.quantile(0.15) and
                rsi.iloc[i] > window_rsi.min() * 1.05):
            bull_div.iloc[i] = True

        # Bearish: current price near window high AND RSI lower than its window high
        if (close.iloc[i] >= window_price.quantile(0.85) and
                rsi.iloc[i] < window_rsi.max() * 0.95):
            bear_div.iloc[i] = True

    return bull_div, bear_div


# =============================================================================
# ADVANCED SIGNAL DETECTORS  (B — missing patterns)
# =============================================================================
#
# Each function takes the OHLCV dataframe and returns a boolean Series.
# They feed directly into compute_indicators() and from there to agents.
#
# WYCKOFF_SPRING      — accumulation range + spring below support + volume dry-up
# MM_ABSORPTION       — large volume at support without price dropping
# SILENT_INSTITUTIONAL— low-vol accumulation over many bars (stealth buying)
# STEALTH_ACCUM       — rising OBV diverging from flat price
# ACTIVE_ACCUM        — consistent buy pressure over 20 bars
# DERIVATIVES_TRAP    — extreme funding + OI divergence from price
# SNIPER_SETUP        — 5+ multi-timeframe confluence factors


def detect_wyckoff_spring(high: pd.Series, low: pd.Series,
                           close: pd.Series, volume: pd.Series,
                           atr: pd.Series) -> pd.Series:
    """
    Wyckoff Spring — the classic accumulation pattern:
    1. Price has been ranging (ATR contracting) for 20+ bars
    2. Price dips briefly BELOW the range low (the "spring")
    3. Volume is LOW on the spring (no supply — sellers exhausted)
    4. Price recovers back above the range low within 1-3 bars

    This is the highest-conviction long entry in Wyckoff methodology.
    """
    result = pd.Series(False, index=close.index)

    for i in range(40, len(close)):
        window     = slice(i - 20, i)
        range_low  = low.iloc[window].min()
        range_high = high.iloc[window].max()
        range_size = range_high - range_low

        # 1. Price must have been in a tight range (ATR contracting)
        avg_atr    = atr.iloc[window].mean()
        if range_size > avg_atr * 3:
            continue   # not a range — skip

        # 2. Current bar dips below range low (the spring)
        if low.iloc[i] >= range_low:
            continue

        # 3. Volume on spring is LOW (below 20-bar average)
        avg_vol = volume.iloc[window].mean()
        if volume.iloc[i] > avg_vol * 0.8:
            continue   # too much volume — not a spring, might be breakdown

        # 4. Close recovers above range low (rejection of lower prices)
        if close.iloc[i] >= range_low:
            result.iloc[i] = True

    return result


def detect_mm_absorption(high: pd.Series, low: pd.Series,
                          close: pd.Series, volume: pd.Series,
                          atr: pd.Series) -> pd.Series:
    """
    Market Maker Absorption — MM is absorbing sell orders at support:
    1. Price is at or near a support level
    2. Volume is HIGH (large orders being filled)
    3. But the candle body is SMALL (price barely moves despite volume)
    4. Close is in upper half of candle (buyers winning the battle)

    High volume + tiny price movement = someone absorbing all the sells.
    """
    body       = (close - close.shift(1)).abs()
    candle_range = high - low
    body_ratio = body / candle_range.replace(0, float("nan"))

    vol_sma    = volume.rolling(20).mean()
    vol_spike  = volume > vol_sma * 1.8        # above-average volume
    small_body = body_ratio < 0.35             # candle mostly wick, not body
    upper_close = (close - low) / candle_range.replace(0, float("nan")) > 0.6

    # Near support: price within 1×ATR of 20-bar low
    near_support = (close - low.rolling(20).min()) < atr

    return vol_spike & small_body & upper_close & near_support


def detect_silent_institutional(close: pd.Series, volume: pd.Series,
                                 atr: pd.Series) -> pd.Series:
    """
    Silent Institutional Accumulation — whales buying quietly:
    1. Price is trending slightly up or flat over 30 bars
    2. Volume is consistently BELOW average (stealth — not attracting attention)
    3. Each dip is bought (higher lows forming)
    4. ATR is contracting (volatility compressing as supply is absorbed)

    "The best accumulation happens when nobody is watching."
    """
    vol_sma     = volume.rolling(20).mean()
    low_volume  = volume < vol_sma * 0.7          # consistently quiet

    # Higher lows over last 10 bars
    lows_rising = (close.rolling(10).min() >
                   close.rolling(10).min().shift(10))

    # ATR contracting vs 30-bar mean
    atr_contracting = atr < atr.rolling(30).mean() * 0.8

    # Price not falling (slight upward drift)
    price_drift = close > close.rolling(20).mean() * 0.99

    return low_volume & lows_rising & atr_contracting & price_drift


def detect_stealth_accum(close: pd.Series, volume: pd.Series) -> pd.Series:
    """
    Stealth Accumulation via OBV Divergence:
    OBV (On-Balance Volume) rising while price is flat/falling
    = smart money accumulating while retail is disinterested.

    OBV = cumulative sum of volume when up, minus volume when down.
    Rising OBV + flat price = bullish divergence in volume flow.
    """
    # Compute OBV
    obv = pd.Series(0.0, index=close.index)
    for i in range(1, len(close)):
        if close.iloc[i] > close.iloc[i-1]:
            obv.iloc[i] = obv.iloc[i-1] + volume.iloc[i]
        elif close.iloc[i] < close.iloc[i-1]:
            obv.iloc[i] = obv.iloc[i-1] - volume.iloc[i]
        else:
            obv.iloc[i] = obv.iloc[i-1]

    # OBV trending up over 20 bars
    obv_rising = obv > obv.rolling(20).mean() * 1.02

    # Price flat or slightly down over same period
    price_flat = (close / close.shift(20) - 1).abs() < 0.03

    return obv_rising & price_flat


def detect_active_accum(close: pd.Series, volume: pd.Series,
                         open_: pd.Series) -> pd.Series:
    """
    Active Accumulation — visible buying pressure building up:
    Consistent bullish candles with above-average volume over 20 bars.
    Buy pressure score = ratio of up-volume to total volume.
    """
    up_vol   = volume.where(close >= open_, 0.0)
    buy_pct  = up_vol.rolling(20).sum() / volume.rolling(20).sum()

    # 65%+ of recent volume has been on up-bars
    strong_buy = buy_pct > 0.65

    # Volume trending up (interest increasing)
    vol_trend  = volume.rolling(10).mean() > volume.rolling(20).mean()

    return strong_buy & vol_trend


def detect_derivatives_trap(funding_rate: pd.Series,
                              close: pd.Series,
                              volume: pd.Series) -> pd.Series:
    """
    Derivatives Trap — liquidation cascade setup:
    1. Funding rate is EXTREME (crowded one-sided position)
    2. Price is stalling / not following the crowd's direction
    3. Volume is declining (the squeeze fuel is building, not releasing yet)

    When funding is extreme positive → longs are crowded → short squeeze risk.
    When funding is extreme negative → shorts are crowded → long squeeze risk.
    The "trap" fires when crowd is maximum one-sided.
    """
    # Extreme funding: top or bottom 10% of recent history
    funding_high = funding_rate > funding_rate.rolling(200).quantile(0.90)
    funding_low  = funding_rate < funding_rate.rolling(200).quantile(0.10)
    extreme_fund = funding_high | funding_low

    # Price not moving in direction of crowd (stalling)
    price_change = close.pct_change(3)
    stalling     = price_change.abs() < 0.015    # less than 1.5% in 3 bars

    # Volume declining (squeeze building, not released)
    vol_declining = volume < volume.rolling(10).mean() * 0.8

    return extreme_fund & stalling & vol_declining


def detect_sniper_setup(df: pd.DataFrame) -> pd.Series:
    """
    Sniper Setup — the highest-probability multi-confluence entry.
    Requires 5+ independent factors aligning simultaneously.
    Each factor is weighted — some count more than others.

    Scoring:
      +2  RSI oversold/overbought at extreme (<25 or >75)
      +2  Price at major support/resistance (within 0.5×ATR)
      +2  Wyckoff spring detected
      +2  MM absorption detected
      +1  Bull/bear divergence
      +1  MACD cross in direction
      +1  EMA stack aligned
      +1  Volume spike
      +1  Funding rate contrarian (crowd on wrong side)
      +1  ATR compressing (pre-move)

    SNIPER fires at score >= 7 (out of 13 max)
    """
    score = pd.Series(0, index=df.index)

    # +2 RSI extreme
    score += ((df["rsi"] < 25) | (df["rsi"] > 75)).astype(int) * 2

    # +2 price at support/resistance
    near_sup = (df["close"] - df["support"]).abs() < df["atr"] * 0.5
    near_res = (df["resistance"] - df["close"]).abs() < df["atr"] * 0.5
    score   += (near_sup | near_res).astype(int) * 2

    # +2 Wyckoff spring
    if "sig_wyckoff_spring" in df.columns:
        score += df["sig_wyckoff_spring"].astype(int) * 2

    # +2 MM absorption
    if "sig_mm_absorption" in df.columns:
        score += df["sig_mm_absorption"].astype(int) * 2

    # +1 divergence
    score += df["bull_divergence"].astype(int)
    score += df["bear_divergence"].astype(int)

    # +1 MACD cross
    score += df["macd_bullish_cross"].astype(int)
    score += df["macd_bearish_cross"].astype(int)

    # +1 EMA stack
    score += df["ema_bullish_stack"].astype(int)
    score += df["ema_bearish_stack"].astype(int)

    # +1 volume spike
    score += df["volume_spike"].astype(int)

    # +1 funding contrarian
    score += (df["funding_rate"] < -0.0001).astype(int)   # shorts crowded
    score += (df["funding_rate"] >  0.0008).astype(int)   # longs crowded

    # +1 ATR compressing
    score += (df["atr_pct"] < df["atr_pct"].rolling(20).mean() * 0.7).astype(int)

    return score >= 7


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Master function — takes the merged OHLCV+funding+COT dataframe and
    returns it with all technical indicator columns appended.
    Agents read from this enriched dataframe.
    """
    out = df.copy()
    c   = out["close"]
    h   = out["high"]
    l   = out["low"]
    v   = out["volume"]

    # --- TREND: EMAs ---
    out["ema_20"]  = compute_ema(c, 20)
    out["ema_50"]  = compute_ema(c, 50)
    out["ema_200"] = compute_ema(c, 200)

    # Price position relative to each EMA (% distance)
    out["price_vs_ema20"]  = (c - out["ema_20"])  / out["ema_20"]  * 100
    out["price_vs_ema50"]  = (c - out["ema_50"])  / out["ema_50"]  * 100
    out["price_vs_ema200"] = (c - out["ema_200"]) / out["ema_200"] * 100

    # EMA alignment: True when 20 > 50 > 200 (uptrend stack)
    out["ema_bullish_stack"] = (out["ema_20"] > out["ema_50"]) &                                (out["ema_50"] > out["ema_200"])
    out["ema_bearish_stack"] = (out["ema_20"] < out["ema_50"]) &                                (out["ema_50"] < out["ema_200"])

    # --- MOMENTUM: RSI ---
    out["rsi"] = compute_rsi(c, 14)
    out["rsi_oversold"]  = out["rsi"] < 30   # potential long setup
    out["rsi_overbought"] = out["rsi"] > 70  # potential short setup

    # --- MOMENTUM: MACD ---
    out["macd"], out["macd_signal"], out["macd_hist"] = compute_macd(c)
    out["macd_bullish_cross"] = (
        (out["macd"] > out["macd_signal"]) &
        (out["macd"].shift(1) <= out["macd_signal"].shift(1))
    )
    out["macd_bearish_cross"] = (
        (out["macd"] < out["macd_signal"]) &
        (out["macd"].shift(1) >= out["macd_signal"].shift(1))
    )

    # --- VOLUME ---
    out["volume_sma20"] = v.rolling(20).mean()
    out["volume_ratio"] = v / out["volume_sma20"]   # >2 = spike
    out["volume_spike"] = out["volume_ratio"] > 2.0

    # Buying vs selling pressure proxy:
    # if close > open → bullish candle, volume counts as buy pressure
    out["buy_volume"]  = v.where(out["close"] >= out["open"], v * 0.5)
    out["sell_volume"] = v.where(out["close"] <  out["open"], v * 0.5)
    out["buy_pressure_sma"] = out["buy_volume"].rolling(10).mean()

    # --- VOLATILITY: ATR ---
    out["atr"]        = compute_atr(h, l, c, 14)
    out["atr_pct"]    = out["atr"] / c * 100   # ATR as % of price
    out["volatility_high"] = out["atr_pct"] > out["atr_pct"].rolling(50).mean() * 1.5

    # --- STRUCTURE: Swing points ---
    out["swing_high"], out["swing_low"] = detect_swing_points(h, l, lookback=10)

    # Rolling 50-bar support & resistance (last swing high/low)
    out["resistance"] = h.where(out["swing_high"]).rolling(50, min_periods=1).max()
    out["support"]    = l.where(out["swing_low"]).rolling(50, min_periods=1).min()
    out["pct_to_resistance"] = (out["resistance"] - c) / c * 100
    out["pct_to_support"]    = (c - out["support"])    / c * 100

    # --- DIVERGENCE ---
    out["bull_divergence"], out["bear_divergence"] =         detect_rsi_divergence(c, out["rsi"], lookback=30)

    # --- COMPOSITE SIGNAL HELPERS ---
    # These boolean columns are what agents will query directly.
    # Each maps loosely to one of the signal taxonomy labels.

    # BREAKOUT: close above resistance with volume confirmation
    out["sig_breakout"] = (
        (c > out["resistance"].shift(1)) &
        out["volume_spike"]
    )

    # BREAKDOWN: close below support with volume confirmation
    out["sig_breakdown"] = (
        (c < out["support"].shift(1)) &
        out["volume_spike"]
    )

    # STRUCTURAL COMPRESSION: ATR contracting + price near midpoint of range
    range_mid = (out["resistance"] + out["support"]) / 2
    out["sig_compression"] = (
        (out["atr_pct"] < out["atr_pct"].rolling(20).mean() * 0.7) &
        (abs(c - range_mid) / range_mid < 0.02)
    )

    # HIGH CONFLUENCE: 5+ factors aligned bullish simultaneously
    bullish_factors = (
        out["ema_bullish_stack"].astype(int) +
        out["rsi_oversold"].astype(int) +
        out["volume_spike"].astype(int) +
        out["bull_divergence"].astype(int) +
        out["macd_bullish_cross"].astype(int) +
        (out["funding_rate"] < 0).astype(int)   # negative funding = bearish crowd = contrarian bull
    )
    out["sig_high_confluence_bull"] = bullish_factors >= 4

    bearish_factors = (
        out["ema_bearish_stack"].astype(int) +
        out["rsi_overbought"].astype(int) +
        out["volume_spike"].astype(int) +
        out["bear_divergence"].astype(int) +
        out["macd_bearish_cross"].astype(int) +
        (out["funding_rate"] > 0.001).astype(int)  # high positive funding = crowded longs
    )
    out["sig_high_confluence_bear"] = bearish_factors >= 4

    # ── ADVANCED DETECTORS (B — missing patterns) ─────────────────────────────

    # Wyckoff Spring — full phase detection
    out["sig_wyckoff_spring"] = detect_wyckoff_spring(
        h, l, c, v, out["atr"])

    # Market Maker Absorption
    out["sig_mm_absorption"] = detect_mm_absorption(
        h, l, c, v, out["atr"])

    # Silent Institutional Accumulation
    out["sig_silent_institutional"] = detect_silent_institutional(
        c, v, out["atr"])

    # Stealth Accumulation (OBV divergence)
    out["sig_stealth_accum"] = detect_stealth_accum(c, v)

    # Active Accumulation (buy pressure building)
    out["sig_active_accum"] = detect_active_accum(c, v, out["open"])

    # Derivatives Trap (extreme funding + stalling price)
    out["sig_derivatives_trap"] = detect_derivatives_trap(
        out["funding_rate"], c, v)

    # Sniper Setup (multi-confluence score >= 7)
    out["sig_sniper"] = detect_sniper_setup(out)

    # ── APEX PICK: rarest — ALL major signals aligning ────────────────────────
    apex_score = (
        out["sig_wyckoff_spring"].astype(int) +
        out["sig_mm_absorption"].astype(int) +
        out["sig_sniper"].astype(int) +
        out["sig_high_confluence_bull"].astype(int) +
        out["bull_divergence"].astype(int)
    )
    out["sig_apex_pick"] = apex_score >= 4

    return out


def print_indicator_summary(df: pd.DataFrame) -> None:
    """Print a human-readable snapshot of the latest bar for quick sanity check."""
    last = df.iloc[-1]
    ts   = df.index[-1]

    print(f"\n  Latest bar  : {ts}")
    print(f"  Close       : ${last['close']:,.2f}")
    print(f"  RSI 14      : {last['rsi']:.1f}"
          + (" 🔴 OVERBOUGHT" if last["rsi_overbought"] else
             " 🟢 OVERSOLD"  if last["rsi_oversold"]  else ""))
    print(f"  MACD hist   : {last['macd_hist']:.2f}"
          + (" ↑ bullish cross" if last["macd_bullish_cross"] else
             " ↓ bearish cross" if last["macd_bearish_cross"] else ""))
    print(f"  EMA stack   : {'🟢 BULLISH (20>50>200)' if last['ema_bullish_stack'] else '🔴 BEARISH (20<50<200)' if last['ema_bearish_stack'] else '⚪ MIXED'}")
    print(f"  ATR (14)    : ${last['atr']:,.2f}  ({last['atr_pct']:.2f}% of price)")
    print(f"  Volume spike: {'✓ YES' if last['volume_spike'] else 'no'}  (ratio: {last['volume_ratio']:.1f}×)")
    print(f"  Support     : ${last['support']:,.2f}  ({last['pct_to_support']:.2f}% below)")
    print(f"  Resistance  : ${last['resistance']:,.2f}  ({last['pct_to_resistance']:.2f}% above)")
    print(f"  Bull div    : {'✓ YES' if last['bull_divergence'] else 'no'}")
    print(f"  Bear div    : {'✓ YES' if last['bear_divergence'] else 'no'}")
    print(f"  Breakout    : {'🚀 YES' if last['sig_breakout'] else 'no'}")
    print(f"  Breakdown   : {'💥 YES' if last['sig_breakdown'] else 'no'}")
    print(f"  Compression : {'💠 YES' if last['sig_compression'] else 'no'}")
    print(f"  High confluence BULL : {'💎 YES' if last['sig_high_confluence_bull'] else 'no'}")
    print(f"  High confluence BEAR : {'💎 YES' if last['sig_high_confluence_bear'] else 'no'}")
    print(f"  Wyckoff Spring       : {'🌊 YES' if last.get('sig_wyckoff_spring', False) else 'no'}")
    print(f"  MM Absorption        : {'🧲 YES' if last.get('sig_mm_absorption', False) else 'no'}")
    print(f"  Silent Institutional : {'🏛 YES' if last.get('sig_silent_institutional', False) else 'no'}")
    print(f"  Stealth Accum        : {'🔮 YES' if last.get('sig_stealth_accum', False) else 'no'}")
    print(f"  Active Accum         : {'📊 YES' if last.get('sig_active_accum', False) else 'no'}")
    print(f"  Derivatives Trap     : {'📉 YES' if last.get('sig_derivatives_trap', False) else 'no'}")
    print(f"  Sniper Setup         : {'🎯 YES' if last.get('sig_sniper', False) else 'no'}")
    print(f"  Apex Pick            : {'🤖 YES' if last.get('sig_apex_pick', False) else 'no'}")


# =============================================================================
# STEP 3 — TECHNICAL ANALYSIS AGENT  (Claude AI)
# =============================================================================
#
# This is the first real AI agent in the system.
#
# WHAT IT DOES:
#   Reads the last N bars of enriched indicator data, formats a structured
#   market brief, calls the Claude API, and returns a JSON signal matching
#   your signal taxonomy (BREAKOUT, BULL_DIVERGENCE, WYCKOFF_SPRING, etc.)
#
# WHAT IT DOES NOT DO:
#   Execute any trade. It only produces a signal + reasoning + confidence.
#   The Risk Agent (Step 4) and Execution Agent (Step 5) gate actual orders.
#
# HOW TO SET YOUR API KEY:
#   export ANTHROPIC_API_KEY="sk-ant-..."   (Mac/Linux terminal)
#   set ANTHROPIC_API_KEY=sk-ant-...        (Windows cmd)
#   Or paste it directly in the ANTHROPIC_API_KEY variable below (dev only).
#
# OUTPUT FORMAT (JSON):
#   {
#     "signal_label"  : "STRUCTURAL_COMPRESSION",   # from taxonomy
#     "emoji"         : "💠",
#     "direction"     : "LONG" | "SHORT" | "NEUTRAL",
#     "confidence"    : 0.78,                        # 0.0 – 1.0
#     "timeframe"     : "SWING" | "SCALP" | "NONE",
#     "entry_zone"    : [58400, 58600],              # price range
#     "stop_loss"     : 57800,                       # hard stop
#     "target_1"      : 59800,
#     "target_2"      : 61200,
#     "reasoning"     : "...",                       # agent's full CoT
#     "key_factors"   : ["RSI approaching oversold", "price on support", ...],
#     "risk_reward"   : 2.4,
#     "agent"         : "TechnicalAnalysisAgent",
#     "timestamp"     : "2026-06-30T21:00:00+00:00"
#   }

import os
import json

# =============================================================================
# SIGNAL TAXONOMY  (maps label → emoji + description)
# =============================================================================

SIGNAL_TAXONOMY = {
    "APEX_PICK"              : ("🤖", "AI APEX PICK",           "Maximum Conviction — Rare & Powerful"),
    "SNIPER"                 : ("🎯", "SNIPER SETUP",           "High Probability Entry — Multi-Confluence"),
    "WYCKOFF_SPRING"         : ("🌊", "WYCKOFF SPRING",         "Classic Pre-Pump — Best Entry Point"),
    "MM_ABSORPTION"          : ("🧲", "MM ABSORPTION",          "Market Maker Eating All Sells"),
    "SILENT_INSTITUTIONAL"   : ("🏛", "SILENT INSTITUTIONAL",   "Whale Accumulating in Silence"),
    "LIQUIDITY_ABSORPTION"   : ("💎", "LIQUIDITY ABSORPTION",   "Deep Supply Being Consumed"),
    "STEALTH_ACCUM"          : ("🔮", "STEALTH ACCUMULATION",   "Smart Money Positioning Quietly"),
    "STRUCTURAL_COMPRESSION" : ("💠", "STRUCTURAL COMPRESSION", "Spring Loaded — Explosion Imminent"),
    "ACTIVE_ACCUM"           : ("📊", "ACTIVE ACCUMULATION",    "Visible Buying Pressure Building"),
    "DERIVATIVES_TRAP"       : ("📉", "DERIVATIVES TRAP",       "Liquidation Cascade Incoming"),
    "BREAKOUT"               : ("🚀", "BREAKOUT",               "Resistance Broken — Confirmed"),
    "BREAKDOWN"              : ("💥", "BREAKDOWN",              "Support Broken — Bearish Momentum"),
    "BULL_DIVERGENCE"        : ("🔵", "BULL DIVERGENCE",        "Price/RSI Reversal — Going Up"),
    "BEAR_DIVERGENCE"        : ("🟠", "BEAR DIVERGENCE",        "Price/RSI Reversal — Going Down"),
    "HIGH_PROBABILITY"       : ("💠", "HIGH PROBABILITY",       "5+ Factors Aligned Simultaneously"),
    "SWING_LONG"             : ("📈", "SWING LONG",             "Multi-Day Uptrend — Hold 1–7 Days"),
    "SWING_SHORT"            : ("📉", "SWING SHORT",            "Multi-Day Downtrend — Hold 1–7 Days"),
    "SCALP_LONG"             : ("⚡", "SCALP LONG",             "Quick Move Up — 15m Timeframe"),
    "SCALP_SHORT"            : ("⚡", "SCALP SHORT",            "Quick Move Down — 15m Timeframe"),
    "NO_SIGNAL"              : ("⏸",  "NO SIGNAL",             "No clear setup — stand aside"),
}


# =============================================================================
# MARKET BRIEF FORMATTER
# =============================================================================

def format_market_brief(df: pd.DataFrame, lookback_bars: int = 10) -> str:
    """
    Converts the last N rows of the enriched indicators dataframe into a
    structured plain-text brief that the agent can reason over clearly.
    Keeps it concise — LLMs reason better over structured summaries than
    raw CSV dumps.
    """
    recent = df.tail(lookback_bars)
    last   = df.iloc[-1]
    prev   = df.iloc[-2]
    ts     = df.index[-1]

    # Price action over lookback window
    window_high  = recent["high"].max()
    window_low   = recent["low"].min()
    window_range = (window_high - window_low) / last["close"] * 100
    price_change = (last["close"] - recent["close"].iloc[0]) / recent["close"].iloc[0] * 100

    # RSI trend (rising or falling over last 5 bars)
    rsi_delta = last["rsi"] - df["rsi"].iloc[-6]

    # Volume trend
    vol_avg_recent = recent["volume"].mean()
    vol_avg_prior  = df["volume"].iloc[-20:-10].mean()
    vol_trend      = "increasing" if vol_avg_recent > vol_avg_prior * 1.1 else                      "decreasing" if vol_avg_recent < vol_avg_prior * 0.9 else "flat"

    # MACD direction
    macd_dir = "above signal (bullish)" if last["macd"] > last["macd_signal"] else                "below signal (bearish)"

    brief = f"""
=== TECHNICAL ANALYSIS BRIEF — BTC/USDT 1H ===
Generated : {ts}
Price     : ${last['close']:,.2f}

--- TREND ---
EMA 20    : ${last['ema_20']:,.2f}  (price {last['price_vs_ema20']:+.2f}% vs EMA20)
EMA 50    : ${last['ema_50']:,.2f}  (price {last['price_vs_ema50']:+.2f}% vs EMA50)
EMA 200   : ${last['ema_200']:,.2f}  (price {last['price_vs_ema200']:+.2f}% vs EMA200)
EMA Stack : {"🟢 BULLISH (20>50>200)" if last['ema_bullish_stack'] else "🔴 BEARISH (20<50<200)" if last['ema_bearish_stack'] else "⚪ MIXED — no clear stack"}

--- MOMENTUM ---
RSI 14    : {last['rsi']:.1f}  {"⚠ OVERSOLD"  if last['rsi'] < 30 else "⚠ OVERBOUGHT" if last['rsi'] > 70 else ""}
RSI trend : {"rising" if rsi_delta > 0 else "falling"} ({rsi_delta:+.1f} over last 5 bars)
MACD      : {last['macd']:.2f}  {macd_dir}
MACD hist : {last['macd_hist']:.2f}  {"→ bullish cross this bar" if last['macd_bullish_cross'] else "→ bearish cross this bar" if last['macd_bearish_cross'] else ""}

--- VOLUME ---
Volume ratio : {last['volume_ratio']:.1f}×  SMA20  {"🔥 SPIKE" if last['volume_spike'] else ""}
Volume trend : {vol_trend} over last {lookback_bars} bars vs prior 10 bars

--- VOLATILITY ---
ATR 14    : ${last['atr']:,.2f}  ({last['atr_pct']:.2f}% of price)
Volatility: {"HIGH — above 1.5× 50-bar mean" if last['volatility_high'] else "normal"}

--- STRUCTURE (last {lookback_bars} bars) ---
Window high    : ${window_high:,.2f}
Window low     : ${window_low:,.2f}
Window range   : {window_range:.2f}%
Price change   : {price_change:+.2f}%
Resistance     : ${last['resistance']:,.2f}  ({last['pct_to_resistance']:.2f}% above price)
Support        : ${last['support']:,.2f}  ({last['pct_to_support']:.2f}% below price)
Swing high     : {"Yes — this bar" if last['swing_high'] else "No"}
Swing low      : {"Yes — this bar" if last['swing_low'] else "No"}

--- DIVERGENCE ---
Bull divergence (RSI) : {"✓ DETECTED" if last['bull_divergence'] else "None"}
Bear divergence (RSI) : {"✓ DETECTED" if last['bear_divergence'] else "None"}

--- PRE-COMPUTED SIGNAL FLAGS ---
Breakout             : {"✓ YES" if last['sig_breakout']             else "No"}
Breakdown            : {"✓ YES" if last['sig_breakdown']            else "No"}
Structural compression: {"✓ YES" if last['sig_compression']         else "No"}
High confluence BULL : {"✓ YES" if last['sig_high_confluence_bull'] else "No"}
High confluence BEAR : {"✓ YES" if last['sig_high_confluence_bear'] else "No"}

--- MARKET CONTEXT ---
Funding rate  : {last['funding_rate']:.6f}  ({last['funding_rate']*100:.4f}%)  {"🔴 longs paying — crowd is long" if last['funding_rate'] > 0.0005 else "🟢 shorts paying — crowd is short (contrarian bull)" if last['funding_rate'] < -0.0001 else "neutral"}
COT large spec net : {last['cot_large_spec_net']:,.0f}  {"🟢 net long" if last['cot_large_spec_net'] > 0 else "🔴 net short"}
COT commercial net : {last['cot_commercial_net']:,.0f}  {"(hedgers net long — unusual)" if last['cot_commercial_net'] > 0 else "(hedgers net short — normal hedge)"}
COT open interest  : {last['cot_open_interest_all']:,.0f} contracts
"""
    return brief.strip()


def format_market_brief_with_scalp(df: pd.DataFrame,
                                    scalp_flags: dict,
                                    lookback_bars: int = 5) -> str:
    """Extended market brief that includes 15m scalp signals."""
    base  = format_market_brief(df, lookback_bars=lookback_bars)
    scalp = f"""
--- 15M SCALP SIGNALS ---
15m RSI       : {scalp_flags.get('scalp_rsi', 50):.1f}
15m MACD hist : {scalp_flags.get('scalp_macd_hist', 0):.4f}
15m Vol ratio : {scalp_flags.get('scalp_vol_ratio', 1):.1f}×
SCALP LONG    : {"✓ DETECTED" if scalp_flags.get('scalp_long')  else "No"}
SCALP SHORT   : {"✓ DETECTED" if scalp_flags.get('scalp_short') else "No"}
"""
    return base + scalp


def format_market_brief_with_scalp(df: pd.DataFrame,
                                    scalp_flags: dict,
                                    lookback_bars: int = 5) -> str:
    """Extended market brief that includes 15m scalp signals."""
    base  = format_market_brief(df, lookback_bars=lookback_bars)
    scalp = f"""
--- 15M SCALP SIGNALS ---
15m RSI       : {scalp_flags.get('scalp_rsi', 50):.1f}
15m MACD hist : {scalp_flags.get('scalp_macd_hist', 0):.4f}
15m Vol ratio : {scalp_flags.get('scalp_vol_ratio', 1):.1f}×
SCALP LONG    : {"✓ DETECTED" if scalp_flags.get('scalp_long')  else "No"}
SCALP SHORT   : {"✓ DETECTED" if scalp_flags.get('scalp_short') else "No"}
"""
    return base + scalp


# =============================================================================
# SYSTEM PROMPT
# =============================================================================

TA_SYSTEM_PROMPT = """You are a crypto technical analysis agent. Output ONLY a JSON object. No text before or after.

SIGNAL LABELS (pick one):
APEX_PICK|SNIPER|WYCKOFF_SPRING|MM_ABSORPTION|SILENT_INSTITUTIONAL|LIQUIDITY_ABSORPTION|STEALTH_ACCUM|STRUCTURAL_COMPRESSION|ACTIVE_ACCUM|DERIVATIVES_TRAP|BREAKOUT|BREAKDOWN|BULL_DIVERGENCE|BEAR_DIVERGENCE|HIGH_PROBABILITY|SWING_LONG|SWING_SHORT|SCALP_LONG|SCALP_SHORT|NO_SIGNAL

RULES:
- SCALP_LONG/SCALP_SHORT: only if 15m scalp signal is DETECTED in brief
- BREAKOUT/BREAKDOWN: only if those flags are True
- WYCKOFF_SPRING: only if sig_wyckoff_spring is True
- MM_ABSORPTION: only if sig_mm_absorption is True
- SNIPER: only if sig_sniper is True (multi-confluence score ≥7)
- DERIVATIVES_TRAP: only if sig_derivatives_trap is True
- HIGH_PROBABILITY/APEX_PICK: only if 5+ factors align
- NO_SIGNAL: if mixed evidence or R:R < 1.5
- SCALP timeframe: use for 15m signals (hold minutes to 2h)
- SWING timeframe: use for 1h signals (hold 1-7 days)
- Stop loss must be at a structural level
- Keep reasoning under 100 words

OUTPUT (valid JSON only):
{"signal_label":"","direction":"LONG|SHORT|NEUTRAL","confidence":0.0,"timeframe":"SWING|SCALP|NONE","entry_zone":[0,0],"stop_loss":0,"target_1":0,"target_2":0,"risk_reward":0.0,"key_factors":["f1","f2","f3"],"reasoning":"max 100 words"}"""


# =============================================================================
# AGENT CALL
# =============================================================================

def run_ta_agent(df: pd.DataFrame,
                 api_key: str = None,
                 lookback_bars: int = 10,
                 verbose: bool = True,
                 scalp_flags: dict = None) -> dict:
    """
    Run the Technical Analysis Agent against the enriched indicators dataframe.

    Args:
        df          : enriched dataframe from compute_indicators()
        api_key     : Ollama Cloud API key. If None, reads OLLAMA_API_KEY env var.
                        Get one at https://ollama.com/settings/keys
        lookback_bars: how many recent bars to include in the market brief
        verbose     : print the full output to console

    Returns:
        dict with signal, confidence, reasoning, entry/stop/target levels
    """
    key = api_key or os.environ.get("OLLAMA_API_KEY", "")
    if not key:
        raise ValueError(
            "No Ollama API key found.\n"
            "Get one at: https://ollama.com/settings/keys\n"
            "Or paste into OLLAMA_API_KEY_HARDCODED in run()"
        )

    # Build the market brief — include 15m scalp data if available
    if scalp_flags:
        brief = format_market_brief_with_scalp(df, scalp_flags,
                                               lookback_bars=lookback_bars)
    else:
        brief = format_market_brief(df, lookback_bars=lookback_bars)

    if verbose:
        print("\n  Market brief sent to agent:")
        print("  " + "─" * 56)
        for line in brief.splitlines():
            print(f"  {line}")
        print("  " + "─" * 56)

    # ── Ollama Cloud API call ─────────────────────────────────────────────────
    # Ollama Cloud is OpenAI-compatible (same format as OpenAI/Groq).
    # Model: llama3.3 — strong reasoning, good JSON output.
    # Other options visible at ollama.com/models
    OLLAMA_MODEL = "gpt-oss:20b-cloud"   # free cloud model — see ollama.com/search?c=cloud
    OLLAMA_URL   = "https://ollama.com/api/chat"

    payload = {
        "model"   : OLLAMA_MODEL,
        "stream"  : False,
        "options" : {"temperature": 0.2, "num_predict": 2000},
        "messages": [
            {"role": "system", "content": TA_SYSTEM_PROMPT},
            {"role": "user",   "content": brief},
        ]
    }

    resp = requests.post(
        OLLAMA_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type" : "application/json",
        },
        json   = payload,
        timeout= 90,
    )

    # Surface Ollama errors clearly
    if not resp.ok:
        try:
            err = resp.json()
            msg = err.get("error", resp.text)
        except Exception:
            msg = resp.text
        raise RuntimeError(f"Ollama API error {resp.status_code}: {msg}")

    # Ollama /api/chat response: message.content (not OpenAI choices format)
    data     = resp.json()
    raw_text = data["message"]["content"].strip()

    if verbose:
        preview = raw_text[:200] + ("..." if len(raw_text) > 200 else "")
        print(f"\n  Ollama response preview: {preview}")

    # Strip markdown fences, then extract outermost JSON object
    clean = raw_text.replace("```json", "").replace("```", "").strip()
    start = clean.find("{")
    end   = clean.rfind("}")
    if start != -1 and end != -1 and end > start:
        clean = clean[start:end + 1]

    try:
        signal = json.loads(clean)
    except json.JSONDecodeError:
        # Truncated response — return a safe NO_SIGNAL with partial reasoning
        print("  ⚠ JSON truncated — returning NO_SIGNAL with partial reasoning")
        return {
            "signal_label": "NO_SIGNAL",
            "direction"   : "NEUTRAL",
            "confidence"  : 0.0,
            "timeframe"   : "NONE",
            "entry_zone"  : [0, 0],
            "stop_loss"   : 0,
            "target_1"    : 0,
            "target_2"    : 0,
            "risk_reward" : 0.0,
            "key_factors" : ["Response truncated — increase num_predict or shorten prompt"],
            "reasoning"   : raw_text[:500] + "... [TRUNCATED]",
        }

    # Enrich with taxonomy metadata
    label  = signal.get("signal_label", "NO_SIGNAL")
    meta   = SIGNAL_TAXONOMY.get(label, ("❓", label, "Unknown signal"))
    signal["emoji"]       = meta[0]
    signal["label_name"]  = meta[1]
    signal["description"] = meta[2]
    signal["agent"]       = f"TechnicalAnalysisAgent (ollama/gpt-oss:20b-cloud)"
    signal["timestamp"]   = str(df.index[-1])

    return signal


def print_agent_signal(signal: dict) -> None:
    """Pretty-print the agent's output to the console."""
    emoji = signal.get("emoji", "❓")
    label = signal.get("label_name", signal.get("signal_label", "?"))
    desc  = signal.get("description", "")
    conf  = signal.get("confidence", 0)
    dir_  = signal.get("direction", "?")
    tf    = signal.get("timeframe", "?")
    rr    = signal.get("risk_reward", 0)
    entry = signal.get("entry_zone", ["-", "-"])
    sl    = signal.get("stop_loss", "-")
    t1    = signal.get("target_1", "-")
    t2    = signal.get("target_2", "-")

    conf_bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))

    print(f"""
  ┌─────────────────────────────────────────────────────┐
  │  {emoji}  {label:<45}│
  │  {desc:<51}│
  ├─────────────────────────────────────────────────────┤
  │  Direction   : {dir_:<36}│
  │  Timeframe   : {tf:<36}│
  │  Confidence  : {conf:.0%}  [{conf_bar}]          │
  │  Risk/Reward : {rr:.1f}R                                    │
  ├─────────────────────────────────────────────────────┤
  │  Entry zone  : ${entry[0]:,.2f} – ${entry[1]:,.2f}                 │
  │  Stop loss   : ${sl:,.2f}                               │
  │  Target 1    : ${t1:,.2f}                               │
  │  Target 2    : ${t2:,.2f}                               │
  ├─────────────────────────────────────────────────────┤""")

    print(f"  │  Key factors:{'':37}│")
    for factor in signal.get("key_factors", [])[:5]:
        truncated = factor[:49]
        print(f"  │    • {truncated:<47}│")

    print(f"  ├─────────────────────────────────────────────────────┤")
    reasoning = signal.get("reasoning", "")
    words = reasoning.split()
    line  = "  │  "
    for word in words:
        if len(line) + len(word) + 1 > 57:
            print(f"{line:<57}│")
            line = f"  │  {word} "
        else:
            line += word + " "
    if line.strip() != "│":
        print(f"{line:<57}│")
    print(f"  └─────────────────────────────────────────────────────┘")
    print(f"  Agent     : {signal.get('agent','')}")
    print(f"  Timestamp : {signal.get('timestamp','')}")


def save_signal(signal: dict) -> None:
    """Append the signal to a JSON-lines log file (one signal per line)."""
    log_dir  = _make_dir(DATA_ROOT / "signals")
    log_path = log_dir / "ta_agent_signals.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps(signal) + "\n")
    print(f"  Signal logged → {log_path}")



# =============================================================================
# STEP 4 — MULTI-AGENT DEBATE
# =============================================================================
#
# ARCHITECTURE:
#   Round 1 — Three agents analyse independently (no peeking at each other):
#     • TA Agent        (already ran in Step 3)
#     • COT Agent       — focuses only on institutional positioning
#     • Sentiment Agent — focuses only on derivatives & funding dynamics
#
#   Round 2 — Synthesis Agent sees ALL three Round 1 votes and:
#     • Identifies agreement vs conflict
#     • Weighs the evidence
#     • Outputs the FINAL signal from the taxonomy
#
# Each agent outputs a "vote" dict:
#   {"bias": "BULLISH|BEARISH|NEUTRAL", "confidence": 0.x,
#    "reasoning": "...", "key_points": [...]}
#
# The synthesis agent outputs the same full signal format as the TA agent.

# =============================================================================
# SHARED OLLAMA CALL HELPER
# =============================================================================

def _ollama_call(system: str, user: str, api_key: str,
                 max_words_reasoning: int = 80) -> str:
    """
    Single reusable Ollama Cloud call.
    Returns raw text response. Caller handles JSON parsing.
    """
    OLLAMA_MODEL = "gpt-oss:20b-cloud"
    OLLAMA_URL   = "https://ollama.com/api/chat"

    payload = {
        "model"   : OLLAMA_MODEL,
        "stream"  : False,
        "options" : {"temperature": 0.2, "num_predict": 800},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]
    }

    resp = requests.post(
        OLLAMA_URL,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json=payload, timeout=90,
    )
    if not resp.ok:
        try:
            msg = resp.json().get("error", resp.text)
        except Exception:
            msg = resp.text
        raise RuntimeError(f"Ollama error {resp.status_code}: {msg}")

    raw = resp.json()["message"]["content"].strip()
    # Strip markdown fences
    raw = raw.replace("```json", "").replace("```", "").strip()
    # Extract outermost JSON object
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e != -1:
        raw = raw[s:e+1]
    return raw


def _parse_vote(raw: str, agent_name: str) -> dict:
    """Parse a vote JSON safely, returning a neutral vote on failure."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"  ⚠ {agent_name} JSON truncated — using NEUTRAL vote")
        return {
            "bias"       : "NEUTRAL",
            "confidence" : 0.0,
            "key_points" : ["response truncated"],
            "reasoning"  : raw[:200],
        }


def _print_vote(name: str, vote: dict) -> None:
    """Print a compact one-line vote summary."""
    bias  = vote.get("bias", vote.get("direction", "?"))
    conf  = vote.get("confidence", 0)
    label = vote.get("signal_label", "")
    pts   = vote.get("key_points", vote.get("key_factors", []))
    top   = pts[0] if pts else ""
    icon  = "🟢" if "BULL" in str(bias).upper() or bias == "LONG" else \
            "🔴" if "BEAR" in str(bias).upper() or bias == "SHORT" else "⚪"
    tag   = f" [{label}]" if label and label != "NO_SIGNAL" else ""
    print(f"    {icon} {name:<20} {str(bias):<8} conf={conf:.0%}{tag}  [{top}]")


# =============================================================================
# COT AGENT
# =============================================================================

COT_SYSTEM = """You are a COT (Commitment of Traders) analyst. Output ONLY JSON.

Analyse the institutional positioning data and output your directional bias.

JSON format (no other text):
{"bias":"BULLISH|BEARISH|NEUTRAL","confidence":0.0,"key_points":["p1","p2","p3"],"reasoning":"max 60 words"}"""


def _format_cot_brief(df: pd.DataFrame) -> str:
    last = df.iloc[-1]
    spec_net  = last["cot_large_spec_net"]
    comm_net  = last["cot_commercial_net"]
    oi        = last["cot_open_interest_all"]

    # Historical context: is spec net at extreme vs last 20 readings?
    spec_series = df["cot_large_spec_net"].dropna()
    if len(spec_series) > 20:
        pct_rank = (spec_series.iloc[-1] > spec_series.iloc[-20:]).mean()
    else:
        pct_rank = 0.5

    return f"""COT REPORT — BTC CME Futures
Large Spec net : {spec_net:,.0f} contracts  ({'🟢 LONG' if spec_net > 0 else '🔴 SHORT'})
Commercial net : {comm_net:,.0f} contracts  ({'unusual — hedgers long' if comm_net > 0 else 'normal — hedgers hedging'})
Open interest  : {oi:,.0f} contracts
Spec net pct rank (last 20 weeks): {pct_rank:.0%}  ({'crowded long — contrarian warning' if pct_rank > 0.8 else 'crowded short — contrarian bull' if pct_rank < 0.2 else 'neutral positioning'})
Current BTC price: ${last['close']:,.2f}"""


def run_cot_agent(df: pd.DataFrame, api_key: str) -> dict:
    brief  = _format_cot_brief(df)
    raw    = _ollama_call(COT_SYSTEM, brief, api_key)
    vote   = _parse_vote(raw, "COT Agent")
    vote["agent"] = "COTAgent"
    print(f"  ✓ COT Agent done — bias: {vote.get('bias','?')}")
    return vote


# =============================================================================
# SENTIMENT / DERIVATIVES AGENT
# =============================================================================

SENTIMENT_SYSTEM = """You are a crypto derivatives and sentiment analyst. Output ONLY JSON.

Analyse funding rates and open interest to detect crowd positioning extremes
and potential derivatives-driven moves (squeezes, traps, cascades).

JSON format (no other text):
{"bias":"BULLISH|BEARISH|NEUTRAL","confidence":0.0,"key_points":["p1","p2","p3"],"reasoning":"max 60 words"}"""


def _format_sentiment_brief(df: pd.DataFrame) -> str:
    last     = df.iloc[-1]
    recent   = df.tail(24)   # last 24h

    funding_now  = last["funding_rate"]
    funding_avg  = recent["funding_rate"].mean()
    funding_min  = recent["funding_rate"].min()
    funding_max  = recent["funding_rate"].max()
    funding_trend = "rising" if funding_now > funding_avg * 1.1 else \
                    "falling" if funding_now < funding_avg * 0.9 else "flat"

    # Funding interpretation
    if funding_now > 0.001:
        crowd = "🔴 EXTREMELY crowded LONG — squeeze risk"
    elif funding_now > 0.0003:
        crowd = "🟡 Moderately long — mild squeeze risk"
    elif funding_now < -0.0003:
        crowd = "🟢 CROWDED SHORT — short squeeze potential"
    elif funding_now < -0.0001:
        crowd = "🟡 Mildly short — some squeeze potential"
    else:
        crowd = "⚪ Neutral — no strong crowd bias"

    return f"""DERIVATIVES & SENTIMENT BRIEF — BTC/USDT
Current funding rate : {funding_now:.6f} ({funding_now*100:.4f}%)
24h avg funding      : {funding_avg:.6f} ({funding_avg*100:.4f}%)
24h funding range    : {funding_min:.6f} to {funding_max:.6f}
Funding trend        : {funding_trend}
Crowd positioning    : {crowd}
Current price        : ${last['close']:,.2f}
RSI                  : {last['rsi']:.1f}
Volume ratio         : {last['volume_ratio']:.1f}× SMA20"""


def run_sentiment_agent(df: pd.DataFrame, api_key: str) -> dict:
    brief  = _format_sentiment_brief(df)
    raw    = _ollama_call(SENTIMENT_SYSTEM, brief, api_key)
    vote   = _parse_vote(raw, "Sentiment Agent")
    vote["agent"] = "SentimentAgent"
    print(f"  ✓ Sentiment Agent done — bias: {vote.get('bias','?')}")
    return vote


# =============================================================================
# SYNTHESIS AGENT  (Round 2 — sees all votes, makes final call)
# =============================================================================

SYNTHESIS_SYSTEM = """You are a trading synthesis agent. Output ONLY JSON.

You receive 3 independent agent votes. Your job:
1. Identify where agents AGREE (strong signal) vs DISAGREE (weak/no signal)
2. Weight by confidence
3. Output the FINAL trading signal

SIGNAL LABELS:
APEX_PICK|SNIPER|WYCKOFF_SPRING|MM_ABSORPTION|SILENT_INSTITUTIONAL|LIQUIDITY_ABSORPTION|STEALTH_ACCUM|STRUCTURAL_COMPRESSION|ACTIVE_ACCUM|DERIVATIVES_TRAP|BREAKOUT|BREAKDOWN|BULL_DIVERGENCE|BEAR_DIVERGENCE|HIGH_PROBABILITY|SWING_LONG|SWING_SHORT|SCALP_LONG|SCALP_SHORT|NO_SIGNAL

RULES:
- Strong agreement (2-3 agents same direction) → pick matching signal label
- Disagreement → NO_SIGNAL unless one agent has confidence > 0.8
- Keep reasoning under 80 words

OUTPUT (valid JSON only):
{"signal_label":"","direction":"LONG|SHORT|NEUTRAL","confidence":0.0,"timeframe":"SWING|SCALP|NONE","entry_zone":[0,0],"stop_loss":0,"target_1":0,"target_2":0,"risk_reward":0.0,"key_factors":["f1","f2","f3"],"reasoning":"max 80 words","agent_agreement":"AGREE|MIXED|DISAGREE"}"""


def _format_debate_brief(df: pd.DataFrame, votes: list) -> str:
    last = df.iloc[-1]

    lines = [
        f"DEBATE BRIEF — BTC/USDT @ ${last['close']:,.2f}",
        f"EMA stack : {'BULLISH' if last['ema_bullish_stack'] else 'BEARISH' if last['ema_bearish_stack'] else 'MIXED'}",
        f"RSI       : {last['rsi']:.1f}",
        f"ATR       : ${last['atr']:,.2f} ({last['atr_pct']:.2f}%)",
        "",
        "AGENT VOTES:",
    ]

    for v in votes:
        agent = v.get("agent", v.get("signal_label", "Agent"))
        bias  = v.get("bias", v.get("direction", "?"))
        conf  = v.get("confidence", 0)
        pts   = v.get("key_points", v.get("key_factors", []))
        rsn   = v.get("reasoning", "")[:120]
        lines.append(f"  [{agent}] bias={bias} conf={conf:.0%}")
        lines.append(f"    points : {pts[:3]}")
        lines.append(f"    reason : {rsn}")

    return "\n".join(lines)


def run_synthesis_agent(df: pd.DataFrame, votes: list, api_key: str) -> dict:
    brief = _format_debate_brief(df, votes)
    raw   = _ollama_call(SYNTHESIS_SYSTEM, brief, api_key)

    try:
        signal = json.loads(raw)
    except json.JSONDecodeError:
        print("  ⚠ Synthesis JSON truncated — falling back to TA Agent signal")
        signal = votes[0]   # fall back to TA agent signal

    # Enrich with taxonomy metadata
    label  = signal.get("signal_label", "NO_SIGNAL")
    meta   = SIGNAL_TAXONOMY.get(label, ("❓", label, "Unknown"))
    signal["emoji"]       = meta[0]
    signal["label_name"]  = meta[1]
    signal["description"] = meta[2]
    signal["agent"]       = "SynthesisAgent (debate)"
    signal["timestamp"]   = str(df.index[-1])
    signal["votes"]       = votes   # keep full audit trail

    agreement = signal.get("agent_agreement", "?")
    print(f"  ✓ Synthesis done — {label}  agreement={agreement}")
    return signal


# =============================================================================
# UPDATED save_signal — accepts custom filename
# =============================================================================

def save_signal(signal: dict, filename: str = "ta_agent_signals.jsonl") -> None:
    """
    Append signal to a JSON-lines log (one per line).
    Auto-trims to last 500 entries so files don't grow forever.
    """
    log_dir  = _make_dir(DATA_ROOT / "signals")
    log_path = log_dir / filename
    to_log   = {k: v for k, v in signal.items() if k != "votes"}

    # Read existing, trim to last 499, append new
    existing = []
    if log_path.exists():
        try:
            existing = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        except Exception:
            existing = []
    existing.append(to_log)
    existing = existing[-500:]   # keep last 500 signals

    with open(log_path, "w") as f:
        for entry in existing:
            f.write(json.dumps(entry) + "\n")
    print(f"  Signal logged → {log_path}  ({len(existing)} total)")


# =============================================================================
# TRADE LOGGER & WEEKLY/MONTHLY SUMMARY
# =============================================================================
#
# Every signal run is saved to trades_log.jsonl with:
#   - timestamp, signal, direction, confidence, entry, stop, targets
#   - risk metrics (CVaR, vol, regime, throttle)
#   - verdict (APPROVED / BLOCKED)
#   - price at signal time
#
# Weekly summary  → sent every Sunday at 21:00 UTC
# Monthly summary → sent on the 1st of each month at 21:00 UTC
#
# Both summaries are sent to Telegram automatically by GitHub Actions.

def log_trade(signal: dict, risk_result: dict, df: pd.DataFrame) -> None:
    """
    Save every signal run to the master trade log.
    Keeps unlimited history (one line per run, ~500 bytes each).
    """
    log_path = _make_dir(DATA_ROOT / "signals") / "trades_log.jsonl"
    last     = df.iloc[-1]

    entry    = risk_result.get("entry_zone",    [0, 0])
    entry_mid = (entry[0] + entry[1]) / 2 if entry and entry[0] > 0 else last["close"]

    record = {
        "timestamp"      : str(df.index[-1]),
        "price_at_signal": round(float(last["close"]), 2),
        "signal_label"   : risk_result.get("signal_label",  "NO_SIGNAL"),
        "direction"      : risk_result.get("direction",      "NEUTRAL"),
        "confidence"     : risk_result.get("confidence",     0.0),
        "timeframe"      : risk_result.get("timeframe",      "NONE"),
        "entry_mid"      : round(entry_mid, 2),
        "stop_loss"      : risk_result.get("stop_loss",      0),
        "target_1"       : risk_result.get("target_1",       0),
        "target_2"       : risk_result.get("target_2",       0),
        "risk_reward"    : risk_result.get("risk_reward",    0.0),
        "verdict"        : risk_result.get("verdict",        "BLOCKED"),
        "position_size"  : risk_result.get("position_size",  0.0),
        "risk_usd"       : risk_result.get("risk_usd",       0.0),
        "block_reason"   : risk_result.get("block_reason",   ""),
        "agent_agreement": risk_result.get("agent_agreement","?"),
        "regime"         : risk_result.get("regime",         "?"),
        "adx"            : risk_result.get("adx",            0),
        "annual_vol_pct" : risk_result.get("annual_vol_pct", 0),
        "cvar_usd"       : risk_result.get("cvar_usd",       0),
        "dd_throttle"    : risk_result.get("dd_throttle",    1),
        "rsi"            : round(float(last["rsi"]), 1),
        "ema_stack"      : ("BULLISH" if last["ema_bullish_stack"]
                            else "BEARISH" if last["ema_bearish_stack"]
                            else "MIXED"),
        "funding_rate"   : round(float(last["funding_rate"]), 6),
        "key_factors"    : risk_result.get("key_factors", []),
        "reasoning"      : risk_result.get("reasoning",  "")[:200],
    }

    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  Trade logged → {log_path}")


def load_trade_log() -> list:
    """Load all records from trades_log.jsonl."""
    log_path = DATA_ROOT / "signals" / "trades_log.jsonl"
    if not log_path.exists():
        return []
    try:
        return [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    except Exception:
        return []


def build_summary(records: list, period_label: str) -> str:
    """
    Build a text summary of signals over a given period.
    Calculates: total runs, signal distribution, approval rate,
    regime breakdown, avg confidence, avg vol.
    """
    if not records:
        return f"No signals recorded for {period_label}."

    total      = len(records)
    approved   = [r for r in records if r.get("verdict") == "APPROVED"]
    blocked    = [r for r in records if r.get("verdict") == "BLOCKED"]
    signals    = [r for r in records if r.get("signal_label") != "NO_SIGNAL"
                  and r.get("direction") != "NEUTRAL"]

    # Signal label distribution
    from collections import Counter
    label_counts = Counter(r.get("signal_label","?") for r in records)
    top_labels   = label_counts.most_common(5)

    # Direction breakdown
    longs  = sum(1 for r in records if r.get("direction") == "LONG")
    shorts = sum(1 for r in records if r.get("direction") == "SHORT")

    # Regime breakdown
    regime_counts = Counter(r.get("regime","?") for r in records)

    # Averages
    avg_conf = sum(r.get("confidence",0) for r in records) / total
    avg_vol  = sum(r.get("annual_vol_pct",0) for r in records) / total
    avg_rsi  = sum(r.get("rsi",50) for r in records) / total

    # Agreement breakdown
    agree_counts = Counter(r.get("agent_agreement","?") for r in records)

    # Price range
    prices = [r.get("price_at_signal",0) for r in records if r.get("price_at_signal",0) > 0]
    price_low  = min(prices) if prices else 0
    price_high = max(prices) if prices else 0

    lines = [
        f"📊 WEEKLY SUMMARY — {period_label}",
        f"{'='*35}",
        f"",
        f"🔢 RUNS",
        f"Total pipeline runs : {total}",
        f"Signals generated   : {len(signals)} ({len(signals)/total:.0%} of runs)",
        f"Trades APPROVED     : {len(approved)}",
        f"Trades BLOCKED      : {len(blocked)}",
        f"",
        f"📈 DIRECTION BIAS",
        f"LONG signals   : {longs}",
        f"SHORT signals  : {shorts}",
        f"NO_SIGNAL      : {total - longs - shorts}",
        f"",
        f"🏷 TOP SIGNALS",
    ]
    for label, count in top_labels:
        bar = "█" * count
        lines.append(f"  {label:<25} {count:2d}x  {bar}")

    lines += [
        f"",
        f"🌍 MARKET REGIMES",
    ]
    for regime, count in regime_counts.most_common():
        pct = count / total * 100
        lines.append(f"  {regime:<12} {count:2d}x ({pct:.0f}%)")

    lines += [
        f"",
        f"🤝 AGENT AGREEMENT",
    ]
    for ag, count in agree_counts.most_common():
        lines.append(f"  {ag:<10} {count:2d}x")

    lines += [
        f"",
        f"📐 AVERAGES",
        f"Avg confidence  : {avg_conf:.0%}",
        f"Avg annual vol  : {avg_vol:.1f}%",
        f"Avg RSI         : {avg_rsi:.1f}",
        f"",
        f"₿ BTC PRICE RANGE",
        f"Low  : ${price_low:,.2f}",
        f"High : ${price_high:,.2f}",
        f"Range: ${price_high - price_low:,.2f} ({(price_high-price_low)/price_low*100:.1f}% move)" if price_low > 0 else "",
        f"",
        f"⚙️ AI Trading Bot — Auto Summary",
    ]
    return "\n".join(l for l in lines if l is not None)


def send_weekly_summary() -> None:
    """Generate and send weekly summary to Telegram."""
    records = load_trade_log()
    now     = pd.Timestamp.now(tz="UTC")
    week_ago = now - pd.Timedelta(days=7)

    weekly = [r for r in records
              if pd.Timestamp(r["timestamp"]) >= week_ago]

    summary = build_summary(weekly, f"Week ending {now.strftime('%Y-%m-%d')}")
    print("\n" + summary)

    try:
        requests.post(
            TELEGRAM_URL,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": summary},
            timeout=15,
        )
        print("  ✅ Weekly summary sent to Telegram")
    except Exception as e:
        print(f"  ⚠ Telegram error: {e}")


def send_monthly_summary() -> None:
    """Generate and send monthly summary to Telegram."""
    records  = load_trade_log()
    now      = pd.Timestamp.now(tz="UTC")
    month_ago = now - pd.Timedelta(days=30)

    monthly = [r for r in records
               if pd.Timestamp(r["timestamp"]) >= month_ago]

    summary = build_summary(monthly, f"Month ending {now.strftime('%Y-%m-%d')}")
    print("\n" + summary)

    try:
        requests.post(
            TELEGRAM_URL,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": summary},
            timeout=15,
        )
        print("  ✅ Monthly summary sent to Telegram")
    except Exception as e:
        print(f"  ⚠ Telegram error: {e}")


# =============================================================================
# STEP 5A — HEDGE FUND RISK ENGINE
# =============================================================================
#
# Four mathematical layers — inspired by AHL, Winton, Two Sigma, Renaissance:
#
#  Layer 1 — VOLATILITY-TARGETED SIZING  (AHL / Man Group)
#             Target a fixed annualised portfolio volatility (15%).
#             Size = (target_vol / realised_vol) × equity
#             → Calm market: bigger size. Wild market: auto-shrinks.
#
#  Layer 2 — CVaR CONSTRAINT  (quantitative hedge fund standard)
#             Monte Carlo 10,000 paths on last 30 days of returns.
#             Conditional Value at Risk (expected loss in worst 5% scenarios).
#             Block trade if CVaR > 2% of equity.
#
#  Layer 3 — EXPONENTIAL DRAWDOWN THROTTLE  (Two Sigma anti-martingale)
#             Size multiplier = e^(−5 × drawdown_pct)
#             Shrinks size continuously and exponentially as losses accumulate.
#             Never fully stops — just gets progressively more conservative.
#
#  Layer 4 — MARKET REGIME FILTER  (systematic CTA standard)
#             ADX + ATR percentile → classify TRENDING / RANGING / VOLATILE
#             Each regime has its own size multiplier and signal whitelist.
#
# VERDICT: APPROVED | REDUCED | BLOCKED  (+ full audit trail)

import math
import random

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
ACCOUNT_SIZE_USD       = 1000.0   # total capital in USD
TARGET_VOL_ANNUAL      = 0.15     # 15% annualised portfolio volatility target
MAX_POSITION_SIZE_USD  = 300.0    # hard cap regardless of formula output
MIN_CONFIDENCE         = 0.50     # agent must be >= 50% confident
MIN_RISK_REWARD        = 1.5      # R:R must be >= 1.5
CVAR_LIMIT_PCT         = 0.02     # block if tail loss > 2% of equity
DAILY_LOSS_LIMIT_PCT   = 0.03     # hard circuit breaker at -3% daily
MONTE_CARLO_PATHS      = 10_000   # simulations for CVaR calculation
RANDOM_SEED            = 42       # reproducible CVaR results
# ──────────────────────────────────────────────────────────────────────────────


def load_risk_state() -> dict:
    """Load P&L state from disk. Auto-resets daily_loss at midnight UTC."""
    state_path = DATA_ROOT / "risk_state.json"
    today      = pd.Timestamp.now(tz="UTC").date().isoformat()
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("date") != today:
            state["daily_loss_usd"] = 0.0
            state["date"]           = today
    else:
        state = {
            "date"              : today,
            "daily_loss_usd"    : 0.0,
            "peak_equity_usd"   : ACCOUNT_SIZE_USD,
            "current_equity_usd": ACCOUNT_SIZE_USD,
        }
    return state


def save_risk_state(state: dict) -> None:
    (DATA_ROOT / "risk_state.json").write_text(json.dumps(state, indent=2))


# ── LAYER 1: VOLATILITY-TARGETED SIZING ───────────────────────────────────────

def compute_vol_targeted_size(df: pd.DataFrame, equity: float) -> tuple:
    """
    AHL-style volatility targeting.
    Realised vol = 24h ATR as fraction of price, annualised (×√8760 for hourly).
    Target a fixed annualised portfolio vol of TARGET_VOL_ANNUAL.

    Returns (position_size_usd, realised_vol_annual, vol_scalar)
    """
    last         = df.iloc[-1]
    price        = last["close"]
    atr          = last["atr"]

    # Hourly ATR as fraction → annualise (8760 hours/year)
    hourly_vol   = atr / price
    annual_vol   = hourly_vol * math.sqrt(8760)
    annual_vol   = max(annual_vol, 0.001)      # floor to avoid div/0

    # Vol scalar: how many times bigger/smaller than target vol?
    vol_scalar   = TARGET_VOL_ANNUAL / annual_vol
    vol_scalar   = min(vol_scalar, 3.0)        # cap at 3× to prevent oversize

    raw_size     = vol_scalar * equity
    position_usd = min(raw_size, MAX_POSITION_SIZE_USD)

    return position_usd, annual_vol, vol_scalar


# ── LAYER 2: CVaR MONTE CARLO ─────────────────────────────────────────────────

def compute_cvar(df: pd.DataFrame, position_usd: float,
                 equity: float) -> tuple:
    """
    Conditional Value at Risk at 95% confidence.
    Uses last 30 days of 1h returns as the empirical distribution,
    then Monte Carlo-samples 10,000 1-period outcomes.

    CVaR = mean loss in the worst 5% of simulated outcomes.
    If CVaR > CVAR_LIMIT_PCT × equity → BLOCK.

    Returns (cvar_usd, var_95_usd, passes_cvar)
    """
    returns = df["close"].pct_change().dropna().tail(30 * 24).values
    if len(returns) < 50:
        # Not enough history — skip CVaR, approve by default
        return 0.0, 0.0, True

    rng = random.Random(RANDOM_SEED)
    simulated_pnl = [
        position_usd * rng.choice(returns)
        for _ in range(MONTE_CARLO_PATHS)
    ]
    simulated_pnl.sort()   # ascending: worst losses at the start

    cutoff   = int(MONTE_CARLO_PATHS * 0.05)   # worst 5%
    var_95   = abs(simulated_pnl[cutoff])       # VaR at 95%
    cvar_95  = abs(sum(simulated_pnl[:cutoff]) / cutoff)  # CVaR

    limit    = CVAR_LIMIT_PCT * equity
    passes   = cvar_95 <= limit

    return round(cvar_95, 2), round(var_95, 2), passes


# ── LAYER 3: EXPONENTIAL DRAWDOWN THROTTLE ────────────────────────────────────

def compute_drawdown_throttle(peak: float, equity: float) -> tuple:
    """
    Two Sigma-inspired anti-martingale throttle.
    throttle = e^(−5 × drawdown_fraction)

    At  0% drawdown → 1.00 (full size)
    At  5% drawdown → 0.78 (−22%)
    At 10% drawdown → 0.61 (−39%)
    At 20% drawdown → 0.37 (−63%)
    At 30% drawdown → 0.22 (−78%)

    Returns (throttle_multiplier, drawdown_pct)
    """
    drawdown     = max(0.0, (peak - equity) / peak) if peak > 0 else 0.0
    throttle     = math.exp(-5.0 * drawdown)
    return round(throttle, 4), round(drawdown * 100, 2)


# ── LAYER 4: MARKET REGIME FILTER ─────────────────────────────────────────────

def compute_regime(df: pd.DataFrame) -> tuple:
    """
    Classify market regime using ADX (trend strength) + ATR percentile.

    ADX computed from directional movement (Wilder's method).
    ATR percentile vs 50-bar rolling window.

    Regimes:
      TRENDING  → ADX > 25, ATR normal    → full size, swing signals OK
      VOLATILE  → ATR > 90th percentile   → 30% size, block scalps
      RANGING   → ADX < 15, ATR normal    → 50% size, block breakouts
      NEUTRAL   → everything else         → 75% size, no restrictions

    Returns (regime, regime_multiplier, adx, atr_pctile, allowed_signals)
    """
    # Compute ADX (14-period Wilder's smoothing)
    h   = df["high"]
    l   = df["low"]
    c   = df["close"]

    up_move   = h.diff()
    down_move = (-l.diff())
    dm_plus   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    dm_minus  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    period = 14
    atr14  = df["atr"]
    di_plus  = 100 * dm_plus.ewm(com=period-1,  adjust=False).mean() / atr14.replace(0, float("nan"))
    di_minus = 100 * dm_minus.ewm(com=period-1, adjust=False).mean() / atr14.replace(0, float("nan"))
    dx       = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, float("nan"))
    adx      = dx.ewm(com=period-1, adjust=False).mean().iloc[-1]
    adx      = round(float(adx) if not pd.isna(adx) else 20.0, 1)

    # ATR percentile vs last 50 bars
    atr_series  = df["atr_pct"].tail(50)
    atr_now     = df["atr_pct"].iloc[-1]
    atr_pctile  = float((atr_series < atr_now).mean()) * 100

    # Classify
    if atr_pctile >= 90:
        regime     = "VOLATILE"
        multiplier = 0.30
        allowed    = {"SWING_LONG","SWING_SHORT","BREAKDOWN","BREAKOUT",
                      "BULL_DIVERGENCE","BEAR_DIVERGENCE","NO_SIGNAL"}
    elif adx > 25:
        regime     = "TRENDING"
        multiplier = 1.00
        allowed    = None   # all signals OK
    elif adx < 15:
        regime     = "RANGING"
        multiplier = 0.50
        allowed    = {"WYCKOFF_SPRING","STRUCTURAL_COMPRESSION","BULL_DIVERGENCE",
                      "BEAR_DIVERGENCE","STEALTH_ACCUM","ACTIVE_ACCUM",
                      "LIQUIDITY_ABSORPTION","MM_ABSORPTION","NO_SIGNAL"}
    else:
        regime     = "NEUTRAL"
        multiplier = 0.75
        allowed    = None

    return regime, multiplier, adx, round(atr_pctile, 1), allowed


# ── MAIN RISK ENGINE ──────────────────────────────────────────────────────────

def run_risk_agent(signal: dict, df: pd.DataFrame) -> dict:
    """
    Hedge-fund grade risk engine. Four mathematical layers.
    Returns enriched signal dict with verdict + full audit trail.
    """
    last      = df.iloc[-1]
    state     = load_risk_state()
    equity    = state["current_equity_usd"]
    peak      = state["peak_equity_usd"]
    notes     = []
    verdict   = "APPROVED"
    block_reason = None

    direction  = signal.get("direction",    "NEUTRAL")
    confidence = signal.get("confidence",   0.0)
    rr         = signal.get("risk_reward",  0.0)
    entry_zone = signal.get("entry_zone",   [0, 0])
    stop_loss  = signal.get("stop_loss",    0)
    label      = signal.get("signal_label", "NO_SIGNAL")

    # ── GATE 0: No-trade flag (computed last so metrics still show) ─────────
    no_signal = (label == "NO_SIGNAL" or direction == "NEUTRAL")

    # ── GATE 1: Confidence & R:R ──────────────────────────────────────────────
    if confidence < MIN_CONFIDENCE:
        block_reason = f"LOW CONFIDENCE {confidence:.0%} < minimum {MIN_CONFIDENCE:.0%}"
        verdict      = "BLOCKED"
        notes.append(f"⛔ {block_reason}")
    if rr > 0 and rr < MIN_RISK_REWARD:
        block_reason = f"POOR R:R {rr:.1f} < minimum {MIN_RISK_REWARD}"
        verdict      = "BLOCKED"
        notes.append(f"⛔ {block_reason}")

    # ── GATE 2: Daily circuit breaker ────────────────────────────────────────
    daily_loss_pct = abs(state["daily_loss_usd"]) / ACCOUNT_SIZE_USD
    if daily_loss_pct >= DAILY_LOSS_LIMIT_PCT:
        block_reason = f"CIRCUIT BREAKER — daily loss {daily_loss_pct:.1%} >= {DAILY_LOSS_LIMIT_PCT:.1%}"
        verdict      = "BLOCKED"
        notes.append(f"🚨 {block_reason}")
    else:
        notes.append(f"✓ Daily loss OK ({daily_loss_pct:.1%} of {DAILY_LOSS_LIMIT_PCT:.1%} limit)")

    # ── LAYER 1: Volatility-Targeted Sizing ───────────────────────────────────
    raw_size, annual_vol, vol_scalar = compute_vol_targeted_size(df, equity)
    notes.append(f"📐 Vol target: realised={annual_vol:.1%} ann  "
                 f"scalar={vol_scalar:.2f}×  raw=${raw_size:,.0f}")

    # ── LAYER 2: CVaR Monte Carlo ─────────────────────────────────────────────
    cvar, var95, passes_cvar = compute_cvar(df, raw_size, equity)
    cvar_limit = CVAR_LIMIT_PCT * equity
    if not passes_cvar:
        block_reason = (f"CVaR BREACH — tail loss ${cvar:,.2f} "
                        f"> limit ${cvar_limit:,.2f} ({CVAR_LIMIT_PCT:.0%} of equity)")
        verdict      = "BLOCKED"
        notes.append(f"🚨 {block_reason}")
    else:
        notes.append(f"✓ CVaR OK: ${cvar:,.2f} tail loss  "
                     f"(VaR95=${var95:,.2f}  limit=${cvar_limit:,.2f})")

    # ── LAYER 3: Exponential Drawdown Throttle ────────────────────────────────
    throttle, drawdown_pct = compute_drawdown_throttle(peak, equity)
    raw_size_throttled = raw_size * throttle
    notes.append(f"📉 Drawdown throttle: {drawdown_pct:.1f}% DD → "
                 f"throttle={throttle:.2f}× → ${raw_size_throttled:,.0f}")

    # ── LAYER 4: Market Regime Filter ─────────────────────────────────────────
    regime, regime_mult, adx, atr_pctile, allowed = compute_regime(df)
    regime_icons = {"TRENDING":"🚀","VOLATILE":"⚡","RANGING":"↔️","NEUTRAL":"○"}
    notes.append(f"{regime_icons.get(regime,'?')} Regime: {regime}  "
                 f"ADX={adx:.1f}  ATR%ile={atr_pctile:.0f}th  "
                 f"size_mult={regime_mult:.0%}")

    # Block signals not appropriate for regime
    if allowed is not None and label not in allowed:
        block_reason = (f"REGIME BLOCK — {label} not valid in {regime} market. "
                        f"Valid: {', '.join(sorted(allowed) - {'NO_SIGNAL'})[:60]}")
        verdict      = "BLOCKED"
        notes.append(f"⛔ {block_reason}")

    # ── GATE 0 applied here (after metrics computed so they show in output) ──
    if no_signal:
        block_reason = "NO_SIGNAL / NEUTRAL — nothing to trade"
        verdict      = "BLOCKED"
        notes.insert(0, "⏸ No actionable signal from debate round")

    # ── FINAL POSITION SIZE ───────────────────────────────────────────────────
    if verdict != "BLOCKED":
        final_size = raw_size_throttled * regime_mult
        final_size = min(final_size, MAX_POSITION_SIZE_USD)

        # Validate entry / stop
        entry = sum(entry_zone)/2 if entry_zone and entry_zone[0] > 0 else last["close"]
        atr   = last["atr"]

        if stop_loss <= 0 or entry <= 0:
            block_reason = "Invalid entry or stop (zero price)"
            verdict      = "BLOCKED"
            final_size   = 0.0
            notes.append(f"⛔ {block_reason}")
        else:
            stop_dist = abs(entry - stop_loss)
            if stop_dist < atr * 0.5:
                notes.append(f"⚠ Stop widened: {stop_dist:.0f} → {atr:.0f} (1×ATR)")
                stop_loss = entry - atr if direction == "LONG" else entry + atr
                stop_dist = atr
            risk_usd  = final_size / entry * stop_dist
            notes.append(f"✅ Final size: ${final_size:,.2f}  "
                         f"risk=${risk_usd:.2f} ({risk_usd/equity:.2%} of equity)")
    else:
        final_size = 0.0
        risk_usd   = 0.0
        entry      = sum(entry_zone)/2 if entry_zone and entry_zone[0] > 0 else 0

    return {
        **signal,
        "verdict"          : verdict,
        "block_reason"     : block_reason,
        "position_size"    : round(final_size, 2),
        "risk_usd"         : round(risk_usd, 2) if verdict != "BLOCKED" else 0.0,
        "stop_loss"        : round(stop_loss, 2),
        "notes"            : notes,
        "account_equity"   : equity,
        "daily_loss_usd"   : state["daily_loss_usd"],
        "drawdown_pct"     : drawdown_pct,
        # Risk metrics for logging & Telegram
        "annual_vol_pct"   : round(annual_vol * 100, 2),
        "vol_scalar"       : round(vol_scalar, 3),
        "cvar_usd"         : cvar,
        "var95_usd"        : var95,
        "dd_throttle"      : throttle,
        "regime"           : regime,
        "adx"              : adx,
        "atr_percentile"   : atr_pctile,
    }


def print_risk_verdict(result: dict) -> None:
    verdict = result.get("verdict", "?")
    icon    = "✅" if verdict == "APPROVED" else "⚠️ " if verdict == "REDUCED" else "🚫"
    reason  = result.get("block_reason") or "All 4 layers passed"

    regime  = result.get("regime", "?")
    adx     = result.get("adx", 0)
    vol     = result.get("annual_vol_pct", 0)
    scalar  = result.get("vol_scalar", 0)
    cvar    = result.get("cvar_usd", 0)
    thr     = result.get("dd_throttle", 1)
    dd      = result.get("drawdown_pct", 0)

    print(f"""
  ┌─────────────────────────────────────────────────────┐
  │  🏦 HEDGE FUND RISK ENGINE: {icon} {verdict:<21}│
  ├─────────────────────────────────────────────────────┤
  │  Signal       : {result.get('signal_label',''):<35}│
  │  Direction    : {result.get('direction',''):<35}│
  │  Position     : ${result.get('position_size',0):>10,.2f} USD{'':<22}│
  │  Risk (USD)   : ${result.get('risk_usd',0):>10,.2f}{'':<30}│
  ├─────────────────────────────────────────────────────┤
  │  📐 L1 Vol Target  : {vol:>5.1f}% ann  scalar={scalar:.2f}×{'':<12}│
  │  📊 L2 CVaR (95%)  : ${cvar:>8,.2f} tail loss{'':<18}│
  │  📉 L3 DD Throttle : {thr:.3f}×  (drawdown={dd:.1f}%){'':<12}│
  │  🌍 L4 Regime      : {regime:<10} ADX={adx:.0f}{'':<14}│
  ├─────────────────────────────────────────────────────┤
  │  Verdict: {reason[:47]:<47}│
  ├─────────────────────────────────────────────────────┤""")
    for note in result.get("notes", []):
        print(f"  │  {note[:51]:<51}│")
    print(f"  └─────────────────────────────────────────────────────┘")


# =============================================================================
# STEP 5B — TELEGRAM NOTIFIER
# =============================================================================

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  # shared credentials, no hardcoded fallback
TELEGRAM_URL     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def send_telegram(result: dict) -> bool:
    """
    Send signal card to Telegram using HTML formatting (reliable, no escape issues).
    Includes full hedge fund risk metrics in every message.
    """
    verdict    = result.get("verdict",        "BLOCKED")
    label      = result.get("signal_label",   "NO_SIGNAL")
    emoji      = result.get("emoji",          "⏸")
    direction  = result.get("direction",      "NEUTRAL")
    conf       = result.get("confidence",     0)
    rr         = result.get("risk_reward",    0)
    entry      = result.get("entry_zone",     [0, 0])
    sl         = result.get("stop_loss",      0)
    t1         = result.get("target_1",       0)
    t2         = result.get("target_2",       0)
    size       = result.get("position_size",  0)
    risk_usd   = result.get("risk_usd",       0)
    reasoning  = result.get("reasoning",      "")[:250]
    factors    = result.get("key_factors",    [])
    ts         = result.get("timestamp",      "")
    agreement  = result.get("agent_agreement","?")
    reason     = result.get("block_reason",   "")

    # Hedge fund risk metrics
    regime     = result.get("regime",         "?")
    annual_vol = result.get("annual_vol_pct", 0)
    vol_scalar = result.get("vol_scalar",     0)
    cvar       = result.get("cvar_usd",       0)
    throttle   = result.get("dd_throttle",    1)
    adx        = result.get("adx",            0)
    dd_pct     = result.get("drawdown_pct",   0)
    atr_pct    = result.get("atr_percentile", 0)

    # Verdict line
    if verdict == "APPROVED":
        verdict_line = "✅ TRADE APPROVED"
    elif verdict == "REDUCED":
        verdict_line = "⚠️ TRADE APPROVED — reduced size"
    else:
        verdict_line = f"🚫 NO TRADE — {(reason or 'blocked')[:50]}"

    # Regime icon
    regime_icon = {"TRENDING":"🚀","VOLATILE":"⚡","RANGING":"↔️","NEUTRAL":"○"}.get(regime,"?")

    # Factors list
    factors_lines = "\n".join(f"  • {f}" for f in factors[:4]) if factors else "  —"

    # HTML message — reliable, no escape chars needed
    msg = (
        f"🤖 <b>AI TRADING SIGNAL</b>\n"
        f"<code>{ts}</code>\n\n"
        f"{verdict_line}\n\n"
        f"{emoji} <b>{label.replace('_',' ')}</b>\n"
        f"{result.get('description','')}\n\n"
        f"<b>Direction</b>  : {direction}\n"
        f"<b>Confidence</b> : {conf:.0%}\n"
        f"<b>R:R</b>        : {rr:.1f}R\n"
        f"<b>Agreement</b>  : {agreement}\n\n"
        f"📊 <b>LEVELS</b>\n"
        f"Entry   : <code>${entry[0]:,.2f} – ${entry[1]:,.2f}</code>\n"
        f"Stop    : <code>${sl:,.2f}</code>\n"
        f"Target1 : <code>${t1:,.2f}</code>\n"
        f"Target2 : <code>${t2:,.2f}</code>\n\n"
        f"🏦 <b>HEDGE FUND RISK ENGINE</b>\n"
        f"Size       : <code>${size:,.2f}</code> USD\n"
        f"Risk       : <code>${risk_usd:,.2f}</code> USD\n"
        f"──────────────────\n"
        f"📐 L1 Vol   : {annual_vol:.1f}% ann  scalar={vol_scalar:.2f}×\n"
        f"📊 L2 CVaR  : ${cvar:,.2f} tail loss (95%)\n"
        f"📉 L3 Thr   : {throttle:.3f}×  DD={dd_pct:.1f}%\n"
        f"{regime_icon} L4 Regime : {regime}  ADX={adx:.0f}  ATR%={atr_pct:.0f}th\n\n"
        f"📋 <b>KEY FACTORS</b>\n"
        f"{factors_lines}\n\n"
        f"💬 <b>REASONING</b>\n"
        f"{reasoning}\n\n"
        f"⚙️ Agents: TA + COT + Sentiment + Synthesis + Risk"
    )

    try:
        # Strip any HTML-breaking chars from dynamic content
        def clean(s):
            return str(s).replace("<","").replace(">","").replace("&","and")

        # Try HTML first
        resp = requests.post(
            TELEGRAM_URL,
            json={
                "chat_id"   : TELEGRAM_CHAT_ID,
                "text"      : msg,
                "parse_mode": "HTML",
            },
            timeout=15,
        )
        if resp.ok:
            print(f"  ✅ Telegram sent (HTML) → chat {TELEGRAM_CHAT_ID}")
            return True

        # HTML failed — build clean plain text with all metrics
        print(f"  ⚠ HTML failed ({resp.status_code}), trying plain text...")
        plain = (
            f"AI TRADING SIGNAL — {ts}\n"
            f"{'='*35}\n"
            f"{verdict_line}\n\n"
            f"{emoji} {label.replace('_',' ')}\n"
            f"Direction  : {direction}\n"
            f"Confidence : {conf:.0%}\n"
            f"R:R        : {rr:.1f}R\n"
            f"Agreement  : {agreement}\n\n"
            f"LEVELS\n"
            f"Entry  : ${entry[0]:,.2f} - ${entry[1]:,.2f}\n"
            f"Stop   : ${sl:,.2f}\n"
            f"T1     : ${t1:,.2f}\n"
            f"T2     : ${t2:,.2f}\n\n"
            f"HEDGE FUND RISK ENGINE\n"
            f"Size       : ${size:,.2f}\n"
            f"Risk       : ${risk_usd:,.2f}\n"
            f"L1 Vol     : {annual_vol:.1f}% ann  scalar={vol_scalar:.2f}x\n"
            f"L2 CVaR    : ${cvar:,.2f} tail loss\n"
            f"L3 Throttle: {throttle:.3f}x  DD={dd_pct:.1f}%\n"
            f"L4 Regime  : {regime}  ADX={adx:.0f}  ATR%={atr_pct:.0f}\n\n"
            f"FACTORS\n" +
            "\n".join(f"- {f}" for f in factors[:4]) +
            f"\n\nREASONING\n{reasoning[:250]}"
        )
        resp2 = requests.post(
            TELEGRAM_URL,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": plain},
            timeout=15,
        )
        if resp2.ok:
            print("  ✅ Telegram sent (plain text)")
            return True
        print(f"  ⚠ Telegram failed: {resp2.status_code} {resp2.text[:150]}")
        return False
    except Exception as e:
        print(f"  ⚠ Telegram error: {e}")
        return False


def send_telegram_heartbeat() -> None:
    """Send a simple status ping so you know the bot is alive."""
    now = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M UTC")
    try:
        requests.post(
            TELEGRAM_URL,
            json={"chat_id": TELEGRAM_CHAT_ID,
                  "text": f"🟢 Bot heartbeat — {now}"},
            timeout=10,
        )
    except Exception:
        pass


# =============================================================================
# STEP 5C — 4H SCHEDULER
# =============================================================================

def run_once() -> None:
    """Run the full pipeline exactly once and send Telegram notification."""
    import sys
    now = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M UTC")

    # Flush stdout so logs appear immediately in LaunchAgent log files
    sys.stdout.flush()

    print(f"\n{'='*60}")
    print(f"  🔄 PIPELINE RUN — {now}")
    print(f"{'='*60}")
    sys.stdout.flush()

    try:
        result = run()
    except Exception as e:
        print(f"\n❌ Pipeline error: {e}")
        try:
            requests.post(
                TELEGRAM_URL,
                json={"chat_id": TELEGRAM_CHAT_ID,
                      "text": f"❌ Bot pipeline error at {now}:\n{str(e)[:300]}"},
                timeout=10,
            )
        except Exception:
            pass
        return

    # result is (enriched_df, final_signal) from run()
    if isinstance(result, tuple) and len(result) == 2:
        _, final_signal = result

        # Run risk agent
        _section("STEP 5A — RISK AGENT")
        enriched_df = result[0]
        risk_result = run_risk_agent(final_signal, enriched_df)
        print_risk_verdict(risk_result)
        save_signal(risk_result, filename="risk_signals.jsonl")

        # Send Telegram
        _section("STEP 5B — TELEGRAM NOTIFICATION")
        send_telegram(risk_result)

        print(f"\n✅  Full pipeline complete — {now}\n")


def start_scheduler(interval_hours: int = 4) -> None:
    """
    Run the pipeline every `interval_hours` hours indefinitely.
    Runs immediately on start, then waits for the next interval.

    Stop with Ctrl+C.
    """
    print(f"\n🚀 Starting scheduler — running every {interval_hours}h")
    print(f"   Press Ctrl+C to stop\n")
    send_telegram_heartbeat()

    while True:
        run_once()
        next_run = pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=interval_hours)
        print(f"\n⏰ Next run at: {next_run.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"   Sleeping {interval_hours}h...\n")
        time.sleep(interval_hours * 3600)

# =============================================================================
# MAIN
# =============================================================================

def run():
    # -------------------------------------------------------------------------
    # STEP 1 — DATA PIPELINE
    # -------------------------------------------------------------------------
    _section("1 / 3  OHLCV — OKX/Bybit 1h candles")
    ohlcv = fetch_ohlcv()
    validate_ohlcv(ohlcv)
    _save_csv(ohlcv, _make_dir(DATA_ROOT / "ohlcv") / "btcusdt_1h.csv")

    _section("2 / 3  FUNDING RATE — Bybit Futures")
    funding = fetch_funding()
    _save_csv(funding, _make_dir(DATA_ROOT / "funding") / "btcusdt_funding.csv")

    _section("3 / 3  COT REPORT — CFTC Socrata")
    try:
        cot = fetch_cot()
    except Exception as e:
        print(f"  ⚠ COT fetch failed: {e}")
        cot = pd.DataFrame()
    if not cot.empty:
        _save_csv(cot, _make_dir(DATA_ROOT / "cot") / "btc_cot.csv")
    else:
        print("  ⏭ COT skipped — continuing with OHLCV + funding only")

    _section("MERGING ALL SOURCES")
    merged = merge_all(ohlcv, funding, cot)
    _save_csv(merged, _make_dir(DATA_ROOT / "merged") / "btcusdt_1h_merged.csv")
    print_summary(merged)

    # -------------------------------------------------------------------------
    # STEP 2 — TECHNICAL INDICATORS
    # -------------------------------------------------------------------------
    _section("STEP 2 — COMPUTING TECHNICAL INDICATORS")
    enriched = compute_indicators(merged)
    _save_csv(enriched, _make_dir(DATA_ROOT / "indicators") / "btcusdt_1h_indicators.csv")
    print(f"  Indicator columns added  : {len(enriched.columns) - len(merged.columns)}")
    print(f"  Total columns in dataset : {len(enriched.columns)}")
    print_indicator_summary(enriched)

    # -------------------------------------------------------------------------
    # STEP 2B — 15M SCALP SIGNALS
    # -------------------------------------------------------------------------
    _section("STEP 2B — 15M SCALP SIGNALS")
    df_15m      = fetch_15m_candles(total=500)
    scalp_flags = compute_scalp_signals(df_15m)

    print(f"  15m RSI        : {scalp_flags['scalp_rsi']}")
    print(f"  15m MACD hist  : {scalp_flags['scalp_macd_hist']}")
    print(f"  15m Vol ratio  : {scalp_flags['scalp_vol_ratio']}×")
    print(f"  SCALP LONG     : {'✅ YES' if scalp_flags['scalp_long']  else 'no'}")
    print(f"  SCALP SHORT    : {'✅ YES' if scalp_flags['scalp_short'] else 'no'}")

    # -------------------------------------------------------------------------
    # STEP 2B — 15M SCALP SIGNALS
    # -------------------------------------------------------------------------
    _section("STEP 2B — 15M SCALP SIGNALS")
    df_15m      = fetch_15m_candles(total=500)
    scalp_flags = compute_scalp_signals(df_15m)

    print(f"  15m RSI        : {scalp_flags['scalp_rsi']}")
    print(f"  15m MACD hist  : {scalp_flags['scalp_macd_hist']}")
    print(f"  15m Vol ratio  : {scalp_flags['scalp_vol_ratio']}×")
    print(f"  SCALP LONG     : {'✅ YES' if scalp_flags['scalp_long']  else 'no'}")
    print(f"  SCALP SHORT    : {'✅ YES' if scalp_flags['scalp_short'] else 'no'}")

    # -------------------------------------------------------------------------
    # STEP 3 — TECHNICAL ANALYSIS AGENT
    # -------------------------------------------------------------------------
    _section("STEP 3 — TECHNICAL ANALYSIS AGENT")

    # ── Ollama Cloud API key ──────────────────────────────────────────────────
    # On GitHub Actions: stored as a Secret (never in code)
    # Locally on your Mac: paste key below as fallback
    OLLAMA_API_KEY_HARDCODED = ""  # removed — set OLLAMA_API_KEY env var / GitHub secret instead

    api_key = (os.environ.get("OLLAMA_API_KEY", "")   # GitHub Secret (priority)
               or OLLAMA_API_KEY_HARDCODED)            # local fallback

    if not api_key:
        print("  ⚠ No Ollama API key found — skipping agent.")
        print("  Locally : paste into OLLAMA_API_KEY_HARDCODED above")
        print("  GitHub  : add OLLAMA_API_KEY in repo Settings → Secrets")
        print("\n⏸  Steps 1 & 2 complete. Step 3 skipped (no API key).\n")
        return enriched

    print("  Running Technical Analysis Agent...")
    signal = run_ta_agent(enriched, api_key=api_key,
                          lookback_bars=5, scalp_flags=scalp_flags)

    print_agent_signal(signal)
    save_signal(signal)

    print("\n✅  Step 3 complete — TA agent signal ready.\n")

    # -------------------------------------------------------------------------
    # STEP 4 — MULTI-AGENT DEBATE
    # -------------------------------------------------------------------------
    _section("STEP 4 — MULTI-AGENT DEBATE")
    print("  Running COT Agent + Sentiment Agent...")

    cot_vote  = run_cot_agent(enriched,       api_key=api_key)
    sent_vote = run_sentiment_agent(enriched, api_key=api_key)

    print("\n  ── Round 1 votes ──")
    _print_vote("TA Agent",        signal)
    _print_vote("COT Agent",       cot_vote)
    _print_vote("Sentiment Agent", sent_vote)

    print("\n  ── Round 2: Debate (agents see each other) ──")
    final = run_synthesis_agent(
        enriched,
        votes   = [signal, cot_vote, sent_vote],
        api_key = api_key,
    )

    print("\n  ── FINAL SIGNAL (post-debate) ──")
    print_agent_signal(final)
    save_signal(final, filename="debate_signals.jsonl")

    print("\n✅  Step 4 complete — multi-agent debate done.\n")

    # -------------------------------------------------------------------------
    # STEP 5A — RISK AGENT
    # -------------------------------------------------------------------------
    _section("STEP 5A — RISK AGENT")
    risk_result = run_risk_agent(final, enriched)
    print_risk_verdict(risk_result)
    save_signal(risk_result, filename="risk_signals.jsonl")
    log_trade(risk_result, risk_result, enriched)   # ← master trade log
    print("\n✅  Step 5A complete — risk verdict ready.\n")

    # -------------------------------------------------------------------------
    # STEP 5B — TELEGRAM NOTIFICATION
    # -------------------------------------------------------------------------
    _section("STEP 5B — TELEGRAM NOTIFICATION")
    send_telegram(risk_result)
    print("\n✅  Step 5B complete — notification sent.\n")

    return enriched, risk_result


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else ""

    if arg == "--schedule":
        start_scheduler(interval_hours=3)

    elif arg == "--weekly-summary":
        # Send weekly summary to Telegram
        print("\n📊 Generating weekly summary...")
        send_weekly_summary()

    elif arg == "--monthly-summary":
        # Send monthly summary to Telegram
        print("\n📊 Generating monthly summary...")
        send_monthly_summary()

    else:
        # Default / --once: full pipeline
        run()


# =============================================================================
# DATA FOLDER LAYOUT (created automatically when you run this)
# =============================================================================
#
#  trading-bot/
#  ├── pipeline.py               ← this file
#  ├── requirements.txt
#  └── data/
#      ├── ohlcv/
#      │   └── btcusdt_1h.csv
#      ├── funding/
#      │   └── btcusdt_funding.csv
#      ├── cot/
#      │   └── btc_cot.csv
#      ├── merged/
#      │   └── btcusdt_1h_merged.csv
#      └── indicators/
#          └── btcusdt_1h_indicators.csv   ← what agents consume
#
# COLUMNS IN INDICATORS FILE:
#   Raw data  : open, high, low, close, volume, funding_rate,
#               cot_large_spec_net, cot_commercial_net, cot_open_interest_all
#   Trend     : ema_20, ema_50, ema_200, price_vs_ema20/50/200,
#               ema_bullish_stack, ema_bearish_stack
#   Momentum  : rsi, rsi_oversold, rsi_overbought,
#               macd, macd_signal, macd_hist,
#               macd_bullish_cross, macd_bearish_cross
#   Volume    : volume_sma20, volume_ratio, volume_spike,
#               buy_volume, sell_volume, buy_pressure_sma
#   Volatility: atr, atr_pct, volatility_high
#   Structure : swing_high, swing_low, resistance, support,
#               pct_to_resistance, pct_to_support
#   Divergence: bull_divergence, bear_divergence
#   Signals   : sig_breakout, sig_breakdown, sig_compression,
#               sig_high_confluence_bull, sig_high_confluence_bear
#
# =============================================================================
