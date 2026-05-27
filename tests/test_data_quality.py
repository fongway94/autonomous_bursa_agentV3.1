import numpy as np
import pandas as pd
import pytest


def _good_df(n=80):
    rng = np.random.default_rng(0)
    p = 3 + np.cumsum(rng.normal(0, 0.02, n))
    p = np.maximum(p, 0.5)
    df = pd.DataFrame({
        "Open": p, "High": p + 0.05, "Low": p - 0.05,
        "Close": p, "Volume": rng.integers(1000, 50000, n),
    }, index=pd.date_range("2025-01-01", periods=n))
    return df


def test_validate_good_df():
    from data_quality import validate_ohlcv
    ok, issues = validate_ohlcv(_good_df(), "TEST.KL")
    assert ok
    assert not any(i.startswith(("empty_", "missing_", "too_few_")) for i in issues)


def test_validate_empty_fails():
    from data_quality import validate_ohlcv
    ok, issues = validate_ohlcv(pd.DataFrame(), "TEST.KL")
    assert not ok
    assert "empty_dataframe" in issues


def test_validate_negative_price_flagged():
    from data_quality import validate_ohlcv
    df = _good_df()
    df.iloc[10, df.columns.get_loc("Close")] = -1
    ok, issues = validate_ohlcv(df, "TEST.KL")
    assert any("non_positive_price" in i for i in issues)


def test_validate_too_few_rows_fails():
    from data_quality import validate_ohlcv
    ok, issues = validate_ohlcv(_good_df(n=20), "TEST.KL", min_rows=50)
    assert not ok
