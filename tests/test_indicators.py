"""Indicator math sanity tests (no network)."""
import numpy as np
import pandas as pd


def _synthetic_df(n=300):
    rng = np.random.default_rng(42)
    prices = 3.0 + np.cumsum(rng.normal(0, 0.02, n))
    prices = np.maximum(prices, 0.5)
    df = pd.DataFrame({
        "Open":  prices + rng.normal(0, 0.01, n),
        "High":  prices + np.abs(rng.normal(0.02, 0.01, n)),
        "Low":   prices - np.abs(rng.normal(0.02, 0.01, n)),
        "Close": prices,
        "Volume": rng.integers(1_000, 100_000, n),
    }, index=pd.date_range("2024-01-01", periods=n))
    return df


def test_rsi_bounded():
    from screener import calculate_rsi
    df = _synthetic_df()
    rsi = calculate_rsi(df["Close"], 14).dropna()
    # 0 and 100 are valid degenerate values when the series has no gains/losses
    assert rsi.min() >= 0
    assert rsi.max() <= 100
    # ... but the *typical* value should sit in mid-range
    assert 20 < rsi.iloc[50:].mean() < 80


def test_atr_positive():
    from screener import calculate_atr
    df = _synthetic_df()
    atr = calculate_atr(df, 14).dropna()
    assert (atr > 0).all()


def test_compute_indicators_columns():
    from screener import compute_indicators
    from repository import load_parameters
    df = _synthetic_df()
    out = compute_indicators(df, load_parameters())
    for col in ("EMA_Fast", "EMA_Slow", "EMA_Trend",
                "RSI", "MACD_Hist", "ATR", "Vol_Ratio",
                "Support_20", "Resistance_20", "BB_Upper", "BB_Lower"):
        assert col in out.columns, f"missing {col}"
