import numpy as np
import pandas as pd


def test_sharpe_sortino_zero_when_empty():
    from evaluation import sharpe_sortino
    out = sharpe_sortino(pd.Series([], dtype=float))
    assert out["sharpe"] == 0


def test_max_drawdown_basic():
    from evaluation import max_drawdown
    series = pd.Series([100, 110, 95, 102, 90, 105])
    out = max_drawdown(series)
    assert out["max_dd_pct"] < 0
    assert out["max_dd_duration_days"] >= 1


def test_expectancy_no_trades():
    from evaluation import expectancy
    out = expectancy([])
    assert out["expectancy_rm"] == 0
    assert out["n_wins"] == 0


def test_expectancy_with_trades():
    from evaluation import expectancy
    trades = [
        {"outcome": "WIN", "realized_pnl": 100,
         "risk_per_share": 0.1, "shares": 100},
        {"outcome": "LOSS", "realized_pnl": -50,
         "risk_per_share": 0.1, "shares": 100},
        {"outcome": "WIN", "realized_pnl": 200,
         "risk_per_share": 0.1, "shares": 100},
    ]
    e = expectancy(trades)
    assert e["n_wins"] == 2 and e["n_losses"] == 1
    assert e["profit_factor"] > 1
    assert e["expectancy_rm"] > 0
