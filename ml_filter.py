"""
Walk-forward ML filter.

GradientBoostingClassifier trained on historical data predicts P(price up
over HORIZON bars). Only adjusts score when prediction is confident
(outside CONF_BAND dead-zone). Returns 0.5 (neutral) on any failure.
"""

import numpy as np
import pandas as pd

HORIZON    = 8      # bars to look forward for label
CONF_BAND  = 0.60   # dead-zone: abstain if 0.40 <= P <= 0.60
MIN_ROWS   = 200    # minimum bars to attempt training


def _features(df: pd.DataFrame) -> np.ndarray | None:
    """8 features from the last bar of df (must have add_base applied)."""
    try:
        req = ["rsi", "macd_hist", "adx", "stoch_k", "bb_upper", "bb_lower",
               "ema20", "atr", "vol_ratio", "close"]
        if not all(c in df.columns for c in req):
            return None
        last = df.iloc[-1]
        c    = df["close"]
        bb_rng = last["bb_upper"] - last["bb_lower"]
        feat = np.array([
            last["rsi"] / 100.0,
            last["macd_hist"] / (last["atr"] + 1e-9),
            last["adx"] / 100.0,
            last["stoch_k"] / 100.0,
            (last["close"] - last["bb_lower"]) / (bb_rng + 1e-9),
            last["vol_ratio"] if np.isfinite(last["vol_ratio"]) else 1.0,
            (c.iloc[-1] / c.iloc[-5] - 1) if len(c) >= 5 else 0.0,
            (c.iloc[-1] / c.iloc[-20] - 1) if len(c) >= 20 else 0.0,
        ], dtype=float)
        return feat if np.all(np.isfinite(feat)) else None
    except Exception:
        return None


def ml_probability_up(df: pd.DataFrame) -> float:
    """
    Returns P(price higher after HORIZON bars) using walk-forward GBM.
    Falls back to 0.5 on any failure.
    """
    try:
        from sklearn.ensemble import GradientBoostingClassifier

        from indicators import add_base
        df = add_base(df)

        if len(df) < MIN_ROWS + HORIZON:
            return 0.5

        rows, labels = [], []
        for i in range(MIN_ROWS, len(df) - HORIZON):
            sub  = df.iloc[:i]
            feat = _features(sub)
            if feat is None:
                continue
            label = int(df["close"].iloc[i + HORIZON] > df["close"].iloc[i])
            rows.append(feat); labels.append(label)

        if len(rows) < 30 or len(set(labels)) < 2:
            return 0.5

        X, y = np.array(rows), np.array(labels)
        split = int(len(X) * 0.8)
        clf = GradientBoostingClassifier(n_estimators=60, max_depth=2,
                                         learning_rate=0.1, random_state=42)
        clf.fit(X[:split], y[:split])

        feat_now = _features(df)
        if feat_now is None:
            return 0.5
        prob = float(clf.predict_proba(feat_now.reshape(1, -1))[0][1])
        return prob
    except Exception:
        return 0.5


def ml_score_adjustment(df: pd.DataFrame, direction: str) -> tuple[int, float]:
    """
    Returns (score_adjustment, probability).

    direction: 'LONG' or 'SHORT'
    adjustment: +1 (confident with direction), -1 (confident against), 0 (abstain)
    """
    p = ml_probability_up(df)
    if direction == "LONG":
        if p > CONF_BAND:
            return +1, p
        if p < (1 - CONF_BAND):
            return -1, p
    elif direction == "SHORT":
        if p < (1 - CONF_BAND):
            return +1, p
        if p > CONF_BAND:
            return -1, p
    return 0, p
