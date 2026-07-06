"""
All technical indicators — base + advanced quant math.
Used by every layer (scalp/swing/council/forecast).
"""

import numpy as np
import pandas as pd


# ── Base indicators ────────────────────────────────────────────────────────────

def add_base(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    out = df.copy()

    out["ema9"]  = c.ewm(span=9,  adjust=False).mean()
    out["ema20"] = c.ewm(span=20, adjust=False).mean()
    out["ema50"] = c.ewm(span=50, adjust=False).mean()
    out["ema200"]= c.ewm(span=200,adjust=False).mean()

    prev = c.shift(1)
    tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    out["atr"] = tr.ewm(com=13, adjust=False).mean()

    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss  = (-delta).clip(lower=0).ewm(com=13, adjust=False).mean()
    out["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, float("nan"))))

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    out["macd"]      = ema12 - ema26
    out["macd_sig"]  = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_sig"]

    # ADX
    up, dn = h.diff(), -l.diff()
    dmp = up.where((up > dn) & (up > 0), 0.0).ewm(com=13, adjust=False).mean()
    dmm = dn.where((dn > up) & (dn > 0), 0.0).ewm(com=13, adjust=False).mean()
    atr_e = tr.ewm(com=13, adjust=False).mean()
    dip = 100 * dmp / atr_e
    dim = 100 * dmm / atr_e
    out["adx"] = (100 * (dip - dim).abs() / (dip + dim).replace(0, float("nan")))\
                   .ewm(com=13, adjust=False).mean()

    # Stochastic K/D
    low14  = l.rolling(14).min()
    high14 = h.rolling(14).max()
    out["stoch_k"] = 100 * (c - low14) / (high14 - low14).replace(0, float("nan"))
    out["stoch_d"] = out["stoch_k"].rolling(3).mean()

    # Bollinger Bands
    ma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    out["bb_mid"]   = ma20
    out["bb_upper"] = ma20 + 2 * sd20
    out["bb_lower"] = ma20 - 2 * sd20

    # Support / Resistance (rolling extremes)
    out["support"]    = l.rolling(40).min()
    out["resistance"] = h.rolling(40).max()

    # Volume ratio
    if v.sum() > 0:
        out["vol_sma"]   = v.rolling(20).mean()
        out["vol_ratio"] = v / out["vol_sma"].replace(0, float("nan"))
    else:
        out["vol_ratio"] = pd.Series(1.0, index=df.index)

    # Candle structure
    out["body"]      = (c - df["open"]).abs()
    out["range_"]    = h - l
    rng_safe = out["range_"].replace(0, float("nan"))
    out["bull_pin"]  = ((c - l) / rng_safe > 0.7) & (df["open"] > l + out["range_"] * 0.6)
    out["bear_pin"]  = ((h - c) / rng_safe > 0.7) & (df["open"] < h - out["range_"] * 0.6)

    return out


# ── Quant "Magic Math" ─────────────────────────────────────────────────────────

def hurst_exponent(series: pd.Series, max_lag: int = 20) -> float:
    """Hurst exponent: ~0.5 random, <0.5 mean-reverting, >0.5 trending."""
    try:
        lags = range(2, max_lag)
        tau = [np.std(np.subtract(series.values[lag:], series.values[:-lag]))
               for lag in lags]
        valid = [(l, t) for l, t in zip(lags, tau) if t > 0]
        if len(valid) < 3:
            return 0.5
        log_lags = np.log([v[0] for v in valid])
        log_tau  = np.log([v[1] for v in valid])
        return float(np.polyfit(log_lags, log_tau, 1)[0])
    except Exception:
        return 0.5


def ou_half_life(series: pd.Series) -> float:
    """Ornstein-Uhlenbeck mean-reversion half-life in bars."""
    try:
        delta = series.diff().dropna()
        lag1  = series.shift(1).dropna()
        delta, lag1 = delta.align(lag1, join="inner")
        beta = np.polyfit(lag1, delta, 1)[0]
        if beta >= 0:
            return float("inf")
        return float(-np.log(2) / beta)
    except Exception:
        return float("inf")


def shannon_entropy(series: pd.Series, bins: int = 10) -> float:
    """Shannon entropy of return distribution (higher = more random)."""
    try:
        rets = series.pct_change().dropna()
        counts, _ = np.histogram(rets, bins=bins)
        probs = counts / counts.sum()
        probs = probs[probs > 0]
        return float(-np.sum(probs * np.log2(probs)))
    except Exception:
        return 3.0


def kaufman_efficiency(series: pd.Series, period: int = 10) -> float:
    """Kaufman Efficiency Ratio: 1=perfect trend, 0=random."""
    try:
        net  = abs(series.iloc[-1] - series.iloc[-period])
        path = series.diff().abs().tail(period).sum()
        return float(net / path) if path > 0 else 0.0
    except Exception:
        return 0.0


def vwap_z(df: pd.DataFrame, period: int = 20) -> float:
    """VWAP z-score: how many std devs price is from VWAP."""
    try:
        if df["volume"].sum() == 0:
            return 0.0
        tp = (df["high"] + df["low"] + df["close"]) / 3
        vwap = (tp * df["volume"]).rolling(period).sum() / df["volume"].rolling(period).sum()
        std  = (df["close"] - vwap).rolling(period).std()
        return float((df["close"].iloc[-1] - vwap.iloc[-1]) / std.iloc[-1]) if std.iloc[-1] > 0 else 0.0
    except Exception:
        return 0.0


def squeeze_momentum(df: pd.DataFrame) -> tuple[bool, float]:
    """Bollinger inside Keltner = squeeze. Returns (is_squeezed, momentum_value)."""
    try:
        c, h, l = df["close"], df["high"], df["low"]
        ma20 = c.rolling(20).mean()
        sd   = c.rolling(20).std()
        atr  = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1)\
                 .max(axis=1).ewm(com=13, adjust=False).mean()
        squeezed = (ma20 + 2*sd).iloc[-1] < (ma20 + 1.5*atr).iloc[-1] and \
                   (ma20 - 2*sd).iloc[-1] > (ma20 - 1.5*atr).iloc[-1]
        mom = float(c.iloc[-1] - ((h.rolling(20).max() + l.rolling(20).min()) / 2).iloc[-1])
        return squeezed, mom
    except Exception:
        return False, 0.0


def fib_confluence(df: pd.DataFrame, lookback: int = 100) -> dict:
    """Where does price sit in the last swing leg, and is it near a fib level?"""
    try:
        h, l, c = df["high"], df["low"], df["close"]
        hi_i = h.tail(lookback).idxmax(); lo_i = l.tail(lookback).idxmin()
        hi_p, lo_p = h.tail(lookback).max(), l.tail(lookback).min()
        leg_up = lo_i < hi_i
        span = max(hi_p - lo_p, 1e-12)
        retr = (hi_p - c.iloc[-1]) / span if leg_up else (c.iloc[-1] - lo_p) / span
        fibs = {0.382: "38.2%", 0.5: "50%", 0.618: "61.8%", 0.705: "OTE 70.5%", 0.786: "78.6%"}
        near = min(fibs, key=lambda f: abs(retr - f))
        return {"leg_up": leg_up, "retr_pct": float(retr * 100),
                "near_fib": fibs[near], "at_fib": abs(retr - near) < 0.03}
    except Exception:
        return {"leg_up": True, "retr_pct": 0.0, "near_fib": "n/a", "at_fib": False}


def add_quant(df: pd.DataFrame) -> dict:
    """Returns quant metrics dict (not added to df — used in council toolkit)."""
    c = df["close"]
    w = min(len(c), 100)
    squeezed, mom = squeeze_momentum(df)
    r4 = c.pct_change(4)
    prob_osc = float((r4.tail(250) < r4.iloc[-1]).mean() * 100) if len(r4) >= 250 else 50.0
    atr_now  = df["atr"].iloc[-1] if "atr" in df else 0
    atr_mean = df["atr"].rolling(50).mean().iloc[-1] if "atr" in df else 1
    vi = atr_now / atr_mean if atr_mean else 1.0
    z_score = (c.iloc[-1] - c.tail(200).mean()) / c.tail(200).std() if len(c) >= 200 else 0.0
    buy_p = 0.5
    if df["volume"].sum() > 0:
        buy = df["volume"].where(c >= df["open"], 0.0).tail(24).sum()
        tot = df["volume"].tail(24).sum()
        buy_p = buy / tot if tot > 0 else 0.5

    return {
        "hurst":     hurst_exponent(c.tail(w)),
        "ou_hl":     ou_half_life(c.tail(w)),
        "entropy":   shannon_entropy(c.tail(w)),
        "kaufman":   kaufman_efficiency(c),
        "vwap_z":    vwap_z(df),
        "squeezed":  squeezed,
        "sq_mom":    mom,
        "prob_osc":  prob_osc,
        "vol_imp":   vi,
        "z_score":   z_score,
        "buy_pct":   buy_p * 100,
        "fib":       fib_confluence(df),
    }
