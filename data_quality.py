# data_quality.py
"""
Lightweight validator for OHLCV dataframes pulled from yfinance.

Returns (is_ok, issues_list). Caller decides what to do with `is_ok=False`.
Issues are also persisted to the data_quality_log table via logger.
"""

import pandas as pd
import numpy as np

from logger import log_data_quality


def validate_ohlcv(df: pd.DataFrame, ticker: str,
                   min_rows: int = 50, max_gap_pct: float = 40.0) -> tuple[bool, list[str]]:
    """
    Validate an OHLCV frame.

    Checks
    ------
    1. Non-empty + has all OHLCV columns
    2. >= min_rows
    3. High >= Low, High >= max(Open,Close), Low <= min(Open,Close)
    4. No negative prices or volumes
    5. No single-day move > max_gap_pct (likely a bad bar or unhandled split)
    6. Latest bar is within last 14 calendar days (else stale)
    """
    issues: list[str] = []

    if df is None or df.empty:
        issues.append("empty_dataframe")
        log_data_quality(ticker, "empty_dataframe", "ERROR")
        return False, issues

    required_cols = {"Open", "High", "Low", "Close", "Volume"}
    if not required_cols.issubset(df.columns):
        missing = required_cols - set(df.columns)
        issues.append(f"missing_cols:{','.join(sorted(missing))}")
        log_data_quality(ticker, f"missing_cols:{missing}", "ERROR")
        return False, issues

    if len(df) < min_rows:
        issues.append(f"too_few_rows:{len(df)}")
        log_data_quality(ticker, f"too_few_rows:{len(df)}", "WARN",
                         {"rows": len(df), "min": min_rows})
        return False, issues

    # Drop NaN rows for sanity checks
    clean = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if clean.empty:
        issues.append("all_nan_after_dropna")
        log_data_quality(ticker, "all_nan_after_dropna", "ERROR")
        return False, issues

    # Negative/zero price or volume
    if (clean[["Open", "High", "Low", "Close"]] <= 0).any().any():
        issues.append("non_positive_price")
        log_data_quality(ticker, "non_positive_price", "ERROR")
    if (clean["Volume"] < 0).any():
        issues.append("negative_volume")
        log_data_quality(ticker, "negative_volume", "ERROR")

    # Candle integrity
    bad_high = (clean["High"] < clean[["Open", "Close"]].max(axis=1)).sum()
    bad_low = (clean["Low"] > clean[["Open", "Close"]].min(axis=1)).sum()
    if bad_high > 0:
        issues.append(f"high_below_body:{bad_high}_bars")
        log_data_quality(ticker, f"high_below_body:{bad_high}_bars", "WARN")
    if bad_low > 0:
        issues.append(f"low_above_body:{bad_low}_bars")
        log_data_quality(ticker, f"low_above_body:{bad_low}_bars", "WARN")

    # Gap detection
    pct_change = clean["Close"].pct_change().abs() * 100
    big_gaps = pct_change[pct_change > max_gap_pct]
    if len(big_gaps) > 0:
        issues.append(f"large_single_day_moves:{len(big_gaps)}")
        log_data_quality(
            ticker, f"large_single_day_moves:{len(big_gaps)}", "WARN",
            {"max_pct": float(big_gaps.max())},
        )

    # Staleness
    try:
        last_ts = pd.to_datetime(df.index[-1])
        if last_ts.tzinfo:
            last_ts = last_ts.tz_localize(None)
        days_old = (pd.Timestamp.now() - last_ts).days
        if days_old > 14:
            issues.append(f"stale_data:{days_old}_days")
            log_data_quality(ticker, f"stale_data:{days_old}_days", "WARN")
    except Exception:
        pass

    # Considered OK if no ERROR-level issues
    error_issues = [i for i in issues if i.startswith(
        ("empty_", "missing_", "too_few_", "non_positive_",
         "negative_", "all_nan_")
    )]
    is_ok = not error_issues
    return is_ok, issues
