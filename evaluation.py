# evaluation.py
"""
Evaluation harness — honest performance metrics.

Surfaced metrics
----------------
* Equity curve (daily) + drawdown curve
* CAGR, Sharpe, Sortino (252 trading days)
* Max drawdown depth + duration
* Profit factor, expectancy (RM and R)
* Avg MAE / MFE per trade
* Calibration buckets (confidence decile → realized win rate)
* Per-regime hit rate
* Benchmark comparison vs KLCI buy-and-hold and equal-weight watchlist
"""

from __future__ import annotations
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime

from repository import closed_trades, load_account
from watchlist import get_all_tickers
from data_quality import validate_ohlcv


# -------------------------------------------------------------------------
# Equity curve
# -------------------------------------------------------------------------

def build_equity_curve() -> pd.DataFrame:
    """
    Reconstruct a daily equity curve from closed trades.
    Date = trade close date, value = cumulative realized P&L + initial capital.
    """
    trades = closed_trades()
    if not trades:
        return pd.DataFrame(columns=["date", "equity"])
    df = pd.DataFrame(trades)
    df["closed_at"] = pd.to_datetime(df["closed_at"], errors="coerce")
    df = df.dropna(subset=["closed_at"]).sort_values("closed_at")
    df["realized"] = df["realized_pnl"].fillna(df["closed_pnl"]).fillna(0.0)

    acc = load_account()
    cap0 = acc["initial_capital"]
    daily = df.groupby(df["closed_at"].dt.date)["realized"].sum().cumsum()
    out = pd.DataFrame({"date": daily.index,
                        "realized_cum": daily.values,
                        "equity": cap0 + daily.values})
    return out


# -------------------------------------------------------------------------
# Risk-adjusted metrics
# -------------------------------------------------------------------------

def sharpe_sortino(returns: pd.Series, rf: float = 0.0,
                   periods_per_year: int = 252) -> dict:
    if returns is None or returns.empty:
        return {"sharpe": 0, "sortino": 0}
    mean = returns.mean() - rf / periods_per_year
    std = returns.std()
    downside = returns[returns < 0].std()
    sharpe = (mean / std * np.sqrt(periods_per_year)) if std > 1e-9 else 0.0
    sortino = (mean / downside * np.sqrt(periods_per_year)) if downside and downside > 1e-9 else 0.0
    return {"sharpe": round(float(sharpe), 3),
            "sortino": round(float(sortino), 3)}


def max_drawdown(equity: pd.Series) -> dict:
    if equity is None or equity.empty:
        return {"max_dd_pct": 0, "max_dd_duration_days": 0}
    peak = equity.cummax()
    dd = (equity - peak) / peak
    max_dd = float(dd.min()) * 100
    in_dd = dd < 0
    if not in_dd.any():
        return {"max_dd_pct": round(max_dd, 2), "max_dd_duration_days": 0}
    durations = []
    run = 0
    for v in in_dd:
        if v:
            run += 1
        else:
            if run > 0:
                durations.append(run); run = 0
    if run > 0:
        durations.append(run)
    return {"max_dd_pct": round(max_dd, 2),
            "max_dd_duration_days": int(max(durations) if durations else 0)}


def expectancy(trades: list[dict]) -> dict:
    if not trades:
        return {"expectancy_rm": 0, "expectancy_r": 0,
                "avg_win": 0, "avg_loss": 0, "profit_factor": 0,
                "n_wins": 0, "n_losses": 0}
    wins = [t for t in trades if t.get("outcome") == "WIN"]
    losses = [t for t in trades if t.get("outcome") == "LOSS"]
    avg_win = float(np.mean([(t.get("realized_pnl") or 0) for t in wins])) if wins else 0
    avg_loss = float(np.mean([(t.get("realized_pnl") or 0) for t in losses])) if losses else 0
    n = len(trades)
    p_win = len(wins) / n
    p_loss = len(losses) / n
    exp_rm = p_win * avg_win + p_loss * avg_loss
    gross_w = sum(max(t.get("realized_pnl") or 0, 0) for t in trades)
    gross_l = sum(abs(min(t.get("realized_pnl") or 0, 0)) for t in trades)
    pf = (gross_w / gross_l) if gross_l > 1e-9 else float("inf") if gross_w > 0 else 0

    # R-multiples per trade
    r_vals = []
    for t in trades:
        rps = t.get("risk_per_share") or 0
        shr = t.get("shares") or 0
        risk = rps * shr
        if risk > 0:
            r_vals.append((t.get("realized_pnl") or 0) / risk)
    exp_r = float(np.mean(r_vals)) if r_vals else 0

    return {
        "expectancy_rm": round(exp_rm, 2),
        "expectancy_r": round(exp_r, 3),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else None,
        "n_wins": len(wins), "n_losses": len(losses),
    }


def mae_mfe(trades: list[dict]) -> dict:
    if not trades:
        return {"avg_mae_pct": 0, "avg_mfe_pct": 0,
                "wins_mae_pct": 0, "losses_mfe_pct": 0}
    wins = [t for t in trades if t.get("outcome") == "WIN"]
    losses = [t for t in trades if t.get("outcome") == "LOSS"]
    return {
        "avg_mae_pct": round(float(np.mean([t.get("mae_pct") or 0 for t in trades])), 3),
        "avg_mfe_pct": round(float(np.mean([t.get("mfe_pct") or 0 for t in trades])), 3),
        "wins_mae_pct": round(float(np.mean([t.get("mae_pct") or 0 for t in wins])), 3) if wins else 0,
        "losses_mfe_pct": round(float(np.mean([t.get("mfe_pct") or 0 for t in losses])), 3) if losses else 0,
    }


# -------------------------------------------------------------------------
# Calibration: confidence vs realized win rate
# -------------------------------------------------------------------------

def calibration_buckets(trades: list[dict], n_buckets: int = 5) -> list[dict]:
    if not trades:
        return []
    df = pd.DataFrame(trades)
    df = df[df["confidence_score"].notna() & df["outcome"].notna()]
    if df.empty:
        return []
    df["is_win"] = (df["outcome"] == "WIN").astype(int)
    edges = np.linspace(df["confidence_score"].min(),
                        df["confidence_score"].max() + 0.001,
                        n_buckets + 1)
    out = []
    for i in range(n_buckets):
        bucket = df[(df["confidence_score"] >= edges[i]) &
                    (df["confidence_score"] < edges[i + 1])]
        if bucket.empty:
            continue
        out.append({
            "bucket": f"{edges[i]:.0f}–{edges[i+1]:.0f}",
            "predicted_pct": round(float(bucket["confidence_score"].mean()), 1),
            "realized_win_rate_pct": round(float(bucket["is_win"].mean()) * 100, 1),
            "n_trades": len(bucket),
        })
    return out


# -------------------------------------------------------------------------
# Regime-conditional analytics
# -------------------------------------------------------------------------

def per_regime_stats(trades: list[dict]) -> dict:
    out = {}
    for t in trades:
        r = t.get("market_regime") or "UNKNOWN"
        out.setdefault(r, {"total": 0, "wins": 0, "losses": 0, "pnl": 0})
        out[r]["total"] += 1
        out[r]["pnl"] += t.get("realized_pnl") or t.get("closed_pnl") or 0
        if t.get("outcome") == "WIN":
            out[r]["wins"] += 1
        elif t.get("outcome") == "LOSS":
            out[r]["losses"] += 1
    for r, d in out.items():
        d["win_rate_pct"] = round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0
        d["pnl"] = round(d["pnl"], 2)
    return out


# -------------------------------------------------------------------------
# Benchmarks
# -------------------------------------------------------------------------

def klci_buy_hold(equity_dates: pd.DatetimeIndex, initial_capital: float) -> pd.Series:
    if equity_dates is None or len(equity_dates) == 0:
        return pd.Series(dtype=float)
    try:
        start = equity_dates.min()
        # v3.1.10: explicit timeout — was missing, could hang the Performance
        # tab indefinitely on a slow yfinance day.
        df = yf.Ticker("^KLSE").history(
            start=start - pd.Timedelta(days=10),
            end=equity_dates.max() + pd.Timedelta(days=1),
            timeout=30)
        if df.empty:
            return pd.Series(dtype=float)
        df = df["Close"].reindex(pd.date_range(start, equity_dates.max())).ffill()
        shares = initial_capital / float(df.iloc[0])
        eq = (df * shares).reindex(equity_dates).ffill()
        return eq
    except Exception:
        return pd.Series(dtype=float)


def equal_weight_watchlist(equity_dates: pd.DatetimeIndex,
                            initial_capital: float,
                            max_tickers: int = 20) -> pd.Series:
    if equity_dates is None or len(equity_dates) == 0:
        return pd.Series(dtype=float)
    try:
        tickers = get_all_tickers()[:max_tickers]
        start = equity_dates.min()
        end = equity_dates.max()
        per_ticker = initial_capital / len(tickers)
        eq = None
        for t in tickers:
            try:
                # v3.1.10: explicit timeout — was missing, could hang the
                # Performance tab indefinitely on a slow yfinance day.
                df = yf.Ticker(t).history(start=start - pd.Timedelta(days=10),
                                          end=end + pd.Timedelta(days=1),
                                          timeout=15)
                if df.empty:
                    continue
                ok, _ = validate_ohlcv(df, t, min_rows=5)
                if not ok:
                    continue
                df = df["Close"].reindex(pd.date_range(start, end)).ffill()
                shares = per_ticker / float(df.iloc[0])
                contrib = df * shares
                eq = contrib if eq is None else eq.add(contrib, fill_value=0)
            except Exception:
                continue
        if eq is None:
            return pd.Series(dtype=float)
        return eq.reindex(equity_dates).ffill()
    except Exception:
        return pd.Series(dtype=float)


# -------------------------------------------------------------------------
# Top-level report
# -------------------------------------------------------------------------

def full_evaluation_report() -> dict:
    trades = closed_trades()
    acc = load_account()
    eq_df = build_equity_curve()

    if eq_df.empty:
        equity_series = pd.Series([acc["initial_capital"]],
                                   index=[pd.Timestamp.now().normalize()])
    else:
        equity_series = pd.Series(eq_df["equity"].values,
                                   index=pd.to_datetime(eq_df["date"]))
    daily_ret = equity_series.pct_change().dropna()

    klci = klci_buy_hold(equity_series.index, acc["initial_capital"])
    eqw = equal_weight_watchlist(equity_series.index, acc["initial_capital"])

    return {
        "summary": {
            "total_trades": len(trades),
            "current_equity": round(float(equity_series.iloc[-1]), 2),
            "initial_capital": acc["initial_capital"],
            "total_return_pct": round(
                (equity_series.iloc[-1] / acc["initial_capital"] - 1) * 100, 2),
        },
        "risk": {**sharpe_sortino(daily_ret), **max_drawdown(equity_series)},
        "expectancy": expectancy(trades),
        "mae_mfe": mae_mfe(trades),
        "calibration": calibration_buckets(trades),
        "per_regime": per_regime_stats(trades),
        "equity_curve": eq_df,
        "klci_benchmark": klci,
        "equal_weight_benchmark": eqw,
    }
