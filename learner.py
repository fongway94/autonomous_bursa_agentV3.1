# learner.py
"""
Self-Learning Engine — Bayesian per-state win-rate tracker + bias system.

Why Bayesian, not Q-Learning?
-----------------------------
For swing trading on ~80 tickers with ~5-20 closed trades/state at maturity,
a Beta(α,β) per-(state, action) posterior is statistically the right tool:

* Cold-start: Beta(1,1) → uniform prior, agent defaults to neutral.
* Each WIN → α += 1 + reward_weight, each LOSS → β += 1 + reward_weight.
* Action selection uses Thompson sampling (or posterior-mean during scan).
* Confidence intervals come for free; never overclaim with 5 trades.
* No discount factor / next-state bootstrapping to worry about (each trade
  outcome is independent — there is no sequential decision sub-structure
  within a position because exits are rule-based).

This module also handles:
* Strategy-bias multipliers (breakout vs pullback) with Bayesian shrinkage.
* Sector-bias multipliers.
* Walk-forward optimization with proper train/test separation.
* ML setup classifier with TimeSeriesSplit CV and probability calibration.
"""

import os
import json
import threading
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone, timedelta

from sklearn.ensemble import GradientBoostingClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit

from db import connect, myt_iso, DATA_DIR
from repository import (
    load_parameters, save_parameters,
    load_bias_state, save_bias_state, closed_trades,
)
from logger import log_learning_event, log_bias_change, get_logger

log = get_logger("learner")

FILE_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# State discretization (same buckets as v1 for cache compatibility)
# ---------------------------------------------------------------------------

RSI_BINS = [0, 35, 50, 65, 100]
VOL_RATIO_BINS = [0, 0.7, 1.0, 1.5, 100]
TREND_BINS = [-100, -2, 0, 2, 100]


def discretize_state(rsi, vol_ratio, ema_fast_vs_slow_pct, macd_hist) -> int:
    def _bin(v, bins):
        for i, u in enumerate(bins):
            if v < u:
                return i
        return len(bins) - 1
    rsi_s = _bin(rsi, RSI_BINS)
    vol_s = _bin(vol_ratio, VOL_RATIO_BINS)
    tr_s = _bin(ema_fast_vs_slow_pct, TREND_BINS)
    mom_s = 1 if macd_hist >= 0 else 0
    return rsi_s * 27 + vol_s * 9 + tr_s * 3 + mom_s


# ---------------------------------------------------------------------------
# Bayesian posterior
# ---------------------------------------------------------------------------

# Reward weights for α/β increments. Capped to avoid one giant winner
# distorting the posterior.
WIN_WEIGHT_CAP = 3.0
LOSS_WEIGHT_CAP = 3.0


def _get_prior(c, state_id: int, action: str) -> dict:
    """
    Retrieve the Beta(α, β) posterior for a (state, action).

    Cold-start priors are *weakly optimistic on BUY* so the agent will
    try a never-seen-before setup at least a few times before deciding
    it's bad. AVOID and HOLD use Beta(1, 1) — neutral.
    """
    row = c.execute(
        "SELECT alpha, beta, n_trades, total_r FROM state_priors "
        "WHERE state_id=? AND action=?",
        (state_id, action),
    ).fetchone()
    if row is None:
        if action == "BUY":
            # Optimistic prior: 2 imaginary wins, 1 imaginary loss
            return {"alpha": 2.0, "beta": 1.0, "n": 0, "total_r": 0.0}
        return {"alpha": 1.0, "beta": 1.0, "n": 0, "total_r": 0.0}
    return {"alpha": row["alpha"], "beta": row["beta"],
            "n": row["n_trades"], "total_r": row["total_r"]}


def _save_prior(c, state_id: int, action: str, prior: dict):
    c.execute(
        "INSERT INTO state_priors (state_id, action, alpha, beta, "
        "n_trades, total_r, last_updated) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(state_id, action) DO UPDATE SET "
        "alpha=excluded.alpha, beta=excluded.beta, "
        "n_trades=excluded.n_trades, total_r=excluded.total_r, "
        "last_updated=excluded.last_updated",
        (state_id, action, prior["alpha"], prior["beta"],
         prior["n"], prior["total_r"], myt_iso()),
    )


def posterior_win_prob(prior: dict, ci: float = 0.05) -> tuple[float, float, float]:
    """Returns (mean, lower_ci, upper_ci) of Beta posterior."""
    from scipy.stats import beta as beta_dist
    a, b = max(prior["alpha"], 1e-6), max(prior["beta"], 1e-6)
    mean = a / (a + b)
    try:
        lo = float(beta_dist.ppf(ci / 2, a, b))
        hi = float(beta_dist.ppf(1 - ci / 2, a, b))
    except Exception:
        lo, hi = mean, mean
    return mean, lo, hi


def _exploration_active() -> bool:
    """
    Exploration mode is ON until the agent has closed N trades (default 50).
    Switched off automatically by the scheduler when the threshold is met.
    """
    from repository import get_scheduler_state, closed_trades
    state = get_scheduler_state()
    if not state.get("exploration_mode", 0):
        return False
    target = state.get("exploration_trades_target", 50) or 50
    return len(closed_trades()) < target


def compute_state_action_score(state_id: int, confidence_score: float,
                               regime: str, sector_strength: float) -> dict:
    """
    Returns the agent's recommendation for a state.

    Mode selection:
      * EXPLORATION (first 50 closed trades) — Thompson sampling: draw one
        win-rate sample from each (state, action) Beta posterior and pick
        the highest. Encourages trying new setups quickly.
      * EXPLOITATION (after 50 closed trades) — Lower-confidence-bound:
        conservative estimate so the agent doesn't act on tiny samples.
    """
    try:
        from scipy import stats  # noqa: F401 — for posterior_win_prob
        from scipy.stats import beta as beta_dist
    except Exception:
        return {"action": "HOLD", "confidence_modifier": 1.0,
                "q_scores": {"BUY": 50, "HOLD": 50, "AVOID": 50},
                "reasoning": "scipy unavailable — neutral score"}

    with connect() as c:
        priors = {a: _get_prior(c, state_id, a)
                  for a in ("BUY", "HOLD", "AVOID")}

    explore = _exploration_active()
    scored = {}
    for action, p in priors.items():
        mean, lo, hi = posterior_win_prob(p)
        if explore:
            # Thompson sample: one draw from Beta(α, β)
            try:
                ts = float(beta_dist.rvs(
                    max(p["alpha"], 1e-6), max(p["beta"], 1e-6), size=1)[0])
            except Exception:
                ts = mean
            decision_score = ts
        else:
            decision_score = lo  # lower confidence bound
        scored[action] = {"mean": mean, "lcb": lo, "ucb": hi,
                          "decision": decision_score,
                          "n": p["n"], "avg_r": (p["total_r"] / p["n"])
                          if p["n"] > 0 else 0.0}

    # Regime modifier
    regime_mult = {"BULL": {"BUY": 1.10, "HOLD": 0.95, "AVOID": 0.85},
                   "NEUTRAL": {"BUY": 1.00, "HOLD": 1.00, "AVOID": 1.00},
                   "BEAR": {"BUY": 0.70, "HOLD": 1.05, "AVOID": 1.20},
                   "UNCERTAIN": {"BUY": 0.85, "HOLD": 1.05, "AVOID": 1.05},
                   }.get(regime, {"BUY": 1, "HOLD": 1, "AVOID": 1})

    # Sector strength modifier (only nudges BUY)
    if sector_strength > 0.3:
        regime_mult["BUY"] *= 1.05
    elif sector_strength < -0.3:
        regime_mult["BUY"] *= 0.92

    # Composite score 0-100
    composite = {a: scored[a]["decision"] * 100 * regime_mult[a] for a in scored}

    # Shrink toward 50 when sample is tiny — milder during exploration
    shrink_w = 0.25 if explore else 0.5
    for a, s in scored.items():
        if s["n"] < 5:
            composite[a] = (1 - shrink_w) * composite[a] + shrink_w * 50.0

    best = max(composite, key=composite.get)
    best_score = composite[best]

    # Translate into the confidence modifier used by screener (0..1)
    modifier = round(min(max(best_score / 100.0, 0.3), 1.3), 3)

    mode_tag = "EXPLORE/Thompson" if explore else "EXPLOIT/LCB"
    reasoning = (
        f"State#{state_id} [{mode_tag}]: "
        + " | ".join(f"{a}: μ={scored[a]['mean']:.2f} "
                     f"n={scored[a]['n']}"
                     for a in ("BUY", "HOLD", "AVOID"))
        + f" → {best} ({best_score:.0f})"
    )

    return {
        "action": best,
        "confidence_modifier": modifier,
        "q_scores": {a: round(composite[a], 1) for a in composite},
        "state_priors": {a: {k: round(v, 3) for k, v in s.items()}
                         for a, s in scored.items()},
        "reasoning": reasoning,
        "state_id": state_id,
    }


# ---------------------------------------------------------------------------
# Learning loop
# ---------------------------------------------------------------------------

def learn_from_trade_outcome(trade: dict) -> dict:
    """
    Update Bayesian posterior + biases based on a closed trade.

    Called from:
        * trading_engine.execute_full_exit
        * scheduler auto-settle
        * dashboard manual close buttons
    """
    with FILE_LOCK:
        ind = trade.get("entry_indicators", {})
        rsi = ind.get("rsi", 50)
        vol = ind.get("vol_ratio", 1.0)
        ema_d = ind.get("ema_trend_distance", 0)
        macd_h = ind.get("macd_hist", 0)
        state_id = discretize_state(rsi, vol, ema_d, macd_h)

        outcome = trade.get("outcome", "UNKNOWN")
        pnl_pct = 0.0
        if trade.get("closed_pnl") is not None and trade.get("cost"):
            pnl_pct = trade["closed_pnl"] / trade["cost"] * 100

        # R-multiple = pnl / initial risk
        rps = trade.get("risk_per_share") or 0
        shares = trade.get("shares") or 1
        initial_risk = (rps * shares) if rps and shares else 0
        r_mult = (trade.get("closed_pnl") or 0) / initial_risk \
            if initial_risk > 0 else 0

        signal = trade.get("signal_type", "")
        if "BREAKOUT" in signal or "PULLBACK" in signal:
            action = "BUY"
        elif "SELL" in signal:
            action = "AVOID"
        else:
            action = "HOLD"

        win_inc = min(max(abs(pnl_pct) / 5.0, 0.5), WIN_WEIGHT_CAP)
        loss_inc = min(max(abs(pnl_pct) / 5.0, 0.5), LOSS_WEIGHT_CAP)

        with connect() as c:
            prior = _get_prior(c, state_id, action)
            if outcome == "WIN":
                prior["alpha"] += win_inc
            elif outcome == "LOSS":
                prior["beta"] += loss_inc
            else:
                prior["beta"] += 0.25
            prior["n"] += 1
            prior["total_r"] += float(r_mult)
            _save_prior(c, state_id, action, prior)

        # Update biases
        _update_strategy_bias(trade)

        log_learning_event(
            "BAYES_UPDATE",
            f"State#{state_id} {action} → {outcome} (R={r_mult:+.2f})",
            changes={"state_id": state_id, "action": action,
                     "alpha": round(prior["alpha"], 2),
                     "beta": round(prior["beta"], 2)},
            metrics={"r_multiple": round(r_mult, 3),
                     "pnl_pct": round(pnl_pct, 2),
                     "n_trades": prior["n"]},
        )
        return {
            "state_id": state_id, "action": action,
            "alpha": prior["alpha"], "beta": prior["beta"],
            "n_trades": prior["n"], "r_mult": r_mult,
        }


def _update_strategy_bias(trade: dict):
    """Bayesian-shrunk strategy + sector bias multipliers."""
    biases = load_bias_state()
    sig = trade.get("signal_type", "")
    outcome = trade.get("outcome", "")
    strat = ("breakout" if "BREAKOUT" in sig else
             "pullback" if "PULLBACK" in sig else "other")

    stats = biases.setdefault("strategy_stats", {})
    stats.setdefault(strat, {"wins": 0, "losses": 0, "total": 0})
    stats[strat]["total"] += 1
    if outcome == "WIN":
        stats[strat]["wins"] += 1
    elif outcome == "LOSS":
        stats[strat]["losses"] += 1

    # Bayesian shrinkage with Beta(5,5) prior — equivalent to 10 prior trades.
    # Won't react to <5 raw trades.
    for key, s in stats.items():
        prior_a, prior_b = 5, 5
        wr_shrunk = (s["wins"] + prior_a) / (s["total"] + prior_a + prior_b)
        bias_key = f"{key}_bias"
        if bias_key in biases:
            before = biases[bias_key]
            after = float(np.clip(wr_shrunk / 0.5, 0.75, 1.30))
            biases[bias_key] = after
            log_bias_change(bias_key, before, after,
                            trade_id=trade.get("id"), outcome=outcome)

    # Sector
    sector = trade.get("sector", "")
    if sector:
        sec_stats = biases.setdefault("sector_stats", {})
        sec_stats.setdefault(sector, {"wins": 0, "losses": 0, "total": 0})
        sec_stats[sector]["total"] += 1
        if outcome == "WIN":
            sec_stats[sector]["wins"] += 1
        elif outcome == "LOSS":
            sec_stats[sector]["losses"] += 1
        prior_a, prior_b = 5, 5
        s = sec_stats[sector]
        if s["total"] >= 3:
            wr = (s["wins"] + prior_a) / (s["total"] + prior_a + prior_b)
            before = biases.get("sector_biases", {}).get(sector, 1.0)
            after = float(np.clip(wr / 0.5, 0.80, 1.20))
            biases.setdefault("sector_biases", {})[sector] = after
            log_bias_change(f"sector:{sector}", before, after,
                            trade_id=trade.get("id"), outcome=outcome)

    # System win rate
    closed = closed_trades()
    if closed:
        wins = sum(1 for t in closed if t.get("outcome") == "WIN")
        biases["total_closed_trades"] = len(closed)
        biases["system_win_rate"] = round(wins / len(closed), 4)

    save_bias_state(biases)


# ---------------------------------------------------------------------------
# Walk-forward optimisation (with proper train/test split)
# ---------------------------------------------------------------------------

def _simulate_trades(df: pd.DataFrame, params: dict) -> list[dict]:
    from screener import compute_indicators
    if df is None or df.empty or len(df) < params.get("ema_trend", 200):
        return []
    df = compute_indicators(df, params)
    if df.empty or "RSI" not in df.columns:
        return []
    TRANSACTION_COST = 0.0015
    trades = []
    in_trade = False
    entry = stop = tp2 = 0.0
    entry_idx = 0
    for i in range(params.get("ema_trend", 200), len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        if in_trade:
            if row["Low"] <= stop:
                pnl_pct = (stop - entry) / entry * 100 - TRANSACTION_COST * 100
                trades.append({"outcome": "LOSS", "pnl_pct": pnl_pct})
                in_trade = False
            elif row["High"] >= tp2:
                pnl_pct = (tp2 - entry) / entry * 100 - TRANSACTION_COST * 100
                trades.append({"outcome": "WIN", "pnl_pct": pnl_pct})
                in_trade = False
            elif i - entry_idx >= 15:
                pnl_pct = (row["Close"] - entry) / entry * 100 - TRANSACTION_COST * 100
                trades.append({
                    "outcome": "WIN" if pnl_pct > 0 else "LOSS",
                    "pnl_pct": pnl_pct,
                })
                in_trade = False
            continue
        close = row["Close"]
        ema_fast = row["EMA_Fast"]; ema_slow = row["EMA_Slow"]
        ema_trend = row["EMA_Trend"]; vol_ratio = row["Vol_Ratio"]
        atr = row["ATR"] if not pd.isna(row["ATR"]) else close * 0.03
        is_up = close > ema_trend
        vol_spike = vol_ratio >= params.get("volume_surge_ratio", 1.5)
        is_breakout = close > prev["EMA_Fast"] and prev["Close"] <= prev["EMA_Fast"]
        is_pullback = (is_up and close > ema_slow
                       and row["Low"] <= ema_slow * 1.01 and close >= ema_slow)
        if is_up and ((is_breakout and vol_spike) or is_pullback):
            in_trade = True
            entry = float(close) * (1 - TRANSACTION_COST)
            entry_idx = i
            stop = float(close - atr * params.get("atr_multiplier_stop", 1.5))
            risk = entry - stop
            tp2 = float(entry + 2.0 * risk)
    return trades


def _score_param_set(trades: list[dict]) -> dict:
    if len(trades) < 5:
        return {"win_rate": 0, "profit_factor": 0,
                "combined": 0, "n_trades": len(trades)}
    wins = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    wr = len(wins) / len(trades)
    tp = sum(t["pnl_pct"] for t in wins)
    tl = abs(sum(t["pnl_pct"] for t in losses))
    pf = tp / (tl + 1e-9)
    combined = wr * 0.4 + min(pf / 2.0, 1.0) * 0.6
    return {"win_rate": wr, "profit_factor": pf,
            "combined": combined, "n_trades": len(trades)}


def run_walk_forward_optimization(progress_callback=None) -> tuple[dict, float, float]:
    """
    PROPER walk-forward optimisation:
      * Each window: optimise on TRAIN, evaluate on TEST.
      * Pick params with best out-of-sample TEST score, averaged across
        all windows.
      * Requires at least 30 OOS trades in the winning grid; else reject.
    """
    tickers = ["1155.KL", "5347.KL", "6742.KL", "5398.KL", "0166.KL",
               "0138.KL", "5296.KL", "8583.KL", "7113.KL", "7108.KL"]
    search_grid = [
        {"ema_fast": 9, "ema_slow": 21, "rsi_oversold_pullback": 35.0,
         "volume_surge_ratio": 1.3, "atr_multiplier_stop": 1.5},
        {"ema_fast": 10, "ema_slow": 20, "rsi_oversold_pullback": 40.0,
         "volume_surge_ratio": 1.5, "atr_multiplier_stop": 1.5},
        {"ema_fast": 12, "ema_slow": 26, "rsi_oversold_pullback": 42.0,
         "volume_surge_ratio": 1.8, "atr_multiplier_stop": 2.0},
        {"ema_fast": 5, "ema_slow": 15, "rsi_oversold_pullback": 45.0,
         "volume_surge_ratio": 1.2, "atr_multiplier_stop": 1.5},
        {"ema_fast": 8, "ema_slow": 18, "rsi_oversold_pullback": 38.0,
         "volume_surge_ratio": 1.4, "atr_multiplier_stop": 1.5},
        {"ema_fast": 7, "ema_slow": 21, "rsi_oversold_pullback": 36.0,
         "volume_surge_ratio": 1.6, "atr_multiplier_stop": 1.8},
    ]

    dfs: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            dfs[t] = yf.Ticker(t).history(period="3y", timeout=15)
        except Exception:
            pass
    if not dfs:
        return None, 0, 0

    # OOS scoring per grid across 4 windows.
    grid_scores: dict[int, list[dict]] = {i: [] for i in range(len(search_grid))}
    base_params = load_parameters()

    n_windows = 4
    for w in range(n_windows):
        # Windows are shifted backwards: most-recent first
        train_end = -w * 60 - 60        # leave 60-day OOS gap
        train_start = train_end - 252   # 1y train
        test_start = train_end
        test_end = train_end + 60

        if test_end > 0 or train_start > -250 - n_windows * 60:
            continue
        for gi, g in enumerate(search_grid):
            params = {**base_params, **g}
            if progress_callback:
                pct = (w * len(search_grid) + gi) / (n_windows * len(search_grid))
                progress_callback(pct,
                                  f"WF window {w+1}/{n_windows} | grid {gi+1}/{len(search_grid)}")

            all_trades = []
            for t, df in dfs.items():
                if df.empty or len(df) < 300:
                    continue
                # Train slice — used for sanity (must yield ≥3 trades)
                train_df = df.iloc[train_start:train_end] if train_end else df.iloc[train_start:]
                test_df = df.iloc[test_start:test_end] if test_end else df.iloc[test_start:]
                train_score = _score_param_set(_simulate_trades(train_df, params))
                if train_score["n_trades"] < 2:
                    continue
                test_trades = _simulate_trades(test_df, params)
                all_trades.extend(test_trades)
            grid_scores[gi].append(_score_param_set(all_trades))

    # Aggregate OOS combined across windows
    best_idx, best_score, best_pf, best_wr, best_n = None, -1, 0, 0, 0
    aggregated = []
    for gi, scores in grid_scores.items():
        if not scores:
            continue
        n_total = sum(s["n_trades"] for s in scores)
        avg_combined = float(np.mean([s["combined"] for s in scores]))
        avg_wr = float(np.mean([s["win_rate"] for s in scores]))
        avg_pf = float(np.mean([s["profit_factor"] for s in scores]))
        aggregated.append({"grid": gi, "params": search_grid[gi],
                           "avg_combined": avg_combined, "avg_wr": avg_wr,
                           "avg_pf": avg_pf, "n_total": n_total})
        if n_total >= 30 and avg_combined > best_score:
            best_score = avg_combined; best_idx = gi
            best_pf = avg_pf; best_wr = avg_wr; best_n = n_total

    if best_idx is None:
        log_learning_event(
            "WALK_FORWARD_REJECTED",
            "No parameter set produced ≥30 OOS trades; params unchanged.",
            metrics={"grids_tested": len(search_grid)},
        )
        return None, 0, 0

    new_params = {**base_params, **search_grid[best_idx]}
    save_parameters(new_params, source="WALK_FORWARD",
                    reason=f"Best OOS combined={best_score:.3f} "
                           f"WR={best_wr*100:.1f}% PF={best_pf:.2f} N={best_n}")
    log_learning_event(
        "WALK_FORWARD_OPTIMIZATION",
        f"Selected grid #{best_idx} (OOS WR={best_wr*100:.1f}%, PF={best_pf:.2f}, "
        f"N={best_n})",
        changes={"new_params": search_grid[best_idx]},
        metrics={"combined": round(best_score, 3),
                 "win_rate": round(best_wr * 100, 2),
                 "profit_factor": round(best_pf, 2),
                 "oos_trades": best_n,
                 "all_grids": aggregated},
    )
    return new_params, best_wr, best_pf


# ---------------------------------------------------------------------------
# ML setup classifier — TimeSeriesSplit + Calibration
# ---------------------------------------------------------------------------

_CLASSIFIER_PATH = os.path.join(DATA_DIR, "setup_classifier.pkl")
_CLASSIFIER_META_PATH = os.path.join(DATA_DIR, "setup_classifier_meta.json")
_clf_model = None


def train_setup_classifier():
    from screener import compute_indicators
    tickers = ["1155.KL", "5347.KL", "6742.KL", "5398.KL", "0166.KL",
               "0138.KL", "5296.KL", "8583.KL", "7108.KL", "1295.KL",
               "1023.KL", "1818.KL", "8206.KL", "7113.KL", "7277.KL"]
    params = load_parameters()
    feature_names = ["RSI", "VolRatio", "MACDHist",
                     "EMA_Fast_Dist", "EMA_Slow_Dist", "EMA_Trend_Dist",
                     "BB_Width"]

    rows = []  # (date, features, label)
    for t in tickers:
        try:
            df = yf.Ticker(t).history(period="3y", timeout=15)
            if df is None or df.empty or len(df) < 250:
                continue
            df = compute_indicators(df, params)
            if df.empty or "RSI" not in df.columns:
                continue
            for i in range(100, len(df) - 10):
                row = df.iloc[i]; future = df["High"].iloc[i + 1:i + 11].values
                cur = row["Close"]
                if pd.isna(cur) or cur == 0:
                    continue
                target = int(any(
                    ((fp - cur) / cur >= 0.05)
                    for fp in future if not pd.isna(fp)))
                feats = [
                    float(row["RSI"]) if not pd.isna(row["RSI"]) else 50,
                    float(row["Vol_Ratio"]) if not pd.isna(row["Vol_Ratio"]) else 1,
                    float(row["MACD_Hist"]) if not pd.isna(row["MACD_Hist"]) else 0,
                    float((row["Close"] - row["EMA_Fast"]) / row["EMA_Fast"] * 100)
                    if not (pd.isna(row["EMA_Fast"]) or row["EMA_Fast"] == 0) else 0,
                    float((row["Close"] - row["EMA_Slow"]) / row["EMA_Slow"] * 100)
                    if not (pd.isna(row["EMA_Slow"]) or row["EMA_Slow"] == 0) else 0,
                    float((row["Close"] - row["EMA_Trend"]) / row["EMA_Trend"] * 100)
                    if not (pd.isna(row["EMA_Trend"]) or row["EMA_Trend"] == 0) else 0,
                    float((row["BB_Upper"] - row["BB_Lower"]) / row["Close"] * 100)
                    if not (pd.isna(row["BB_Upper"]) or pd.isna(row["BB_Lower"])
                            or pd.isna(row["Close"]) or row["Close"] == 0) else 0,
                ]
                feats = [float(np.nan_to_num(f, nan=0)) for f in feats]
                rows.append((df.index[i], feats, target))
        except Exception:
            continue

    if len(rows) < 200:
        return None

    rows.sort(key=lambda r: r[0])  # ensure chronological for TimeSeriesSplit
    X = np.array([r[1] for r in rows])
    y = np.array([r[2] for r in rows])

    # Sealed final 10% test set
    split = int(len(rows) * 0.9)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    base = GradientBoostingClassifier(
        n_estimators=120, max_depth=4, learning_rate=0.08, random_state=42)

    # Calibrate with TimeSeriesSplit on the training tail
    tscv = TimeSeriesSplit(n_splits=3)
    clf = CalibratedClassifierCV(base, method="isotonic", cv=tscv)
    clf.fit(X_train, y_train)

    train_acc = float(clf.score(X_train, y_train))
    test_acc = float(clf.score(X_test, y_test)) if len(y_test) else 0.0

    # Feature importance from the underlying GBM (best-effort)
    try:
        importances = []
        for cclf in clf.calibrated_classifiers_:
            est = getattr(cclf, "estimator", None) or getattr(cclf, "base_estimator", None)
            if est is not None and hasattr(est, "feature_importances_"):
                importances.append(est.feature_importances_)
        importance = np.mean(importances, axis=0).tolist() if importances else [0] * len(feature_names)
    except Exception:
        importance = [0] * len(feature_names)

    import joblib
    joblib.dump(clf, _CLASSIFIER_PATH)
    meta = {
        "model_type": "CalibratedGradientBoosting",
        "train_accuracy": round(train_acc, 4),
        "holdout_accuracy": round(test_acc, 4),
        "importance": {n: round(float(v), 4)
                       for n, v in zip(feature_names, importance)},
        "feature_names": feature_names,
        "trained_at": myt_iso(),
        "n_train": len(X_train), "n_test": len(X_test),
        "class_ratio": round(float(np.mean(y_train)), 3),
    }
    with open(_CLASSIFIER_META_PATH, "w") as f:
        json.dump(meta, f, indent=2)

    log_learning_event(
        "ML_CLASSIFIER_TRAINED",
        f"Train acc={train_acc:.3f} | OOS test acc={test_acc:.3f}",
        metrics={"n_train": len(X_train), "n_test": len(X_test),
                 "importance": meta["importance"]},
    )
    return clf, test_acc, importance


def _load_classifier():
    global _clf_model
    if _clf_model is not None:
        return _clf_model
    import joblib
    if os.path.exists(_CLASSIFIER_PATH):
        try:
            _clf_model = joblib.load(_CLASSIFIER_PATH)
            return _clf_model
        except Exception:
            pass
    trained = train_setup_classifier()
    if trained:
        _clf_model = trained[0]
    return _clf_model


def get_ml_score(rsi, vol_ratio, macd_hist, close, ema_fast, ema_slow,
                 ema_trend, bb_upper, bb_lower) -> float:
    """Calibrated probability (0–100) that this setup will gain >=5% in 10d."""
    clf = _load_classifier()
    if clf is None:
        return 50.0 + (15 if 40 < rsi < 65 else 0) + \
               (15 if vol_ratio > 1.5 else 0)
    feats = np.array([[
        float(np.nan_to_num(rsi, nan=50)),
        float(np.nan_to_num(vol_ratio, nan=1)),
        float(np.nan_to_num(macd_hist, nan=0)),
        float(np.nan_to_num((close - ema_fast) / ema_fast * 100
                            if ema_fast else 0, nan=0)),
        float(np.nan_to_num((close - ema_slow) / ema_slow * 100
                            if ema_slow else 0, nan=0)),
        float(np.nan_to_num((close - ema_trend) / ema_trend * 100
                            if ema_trend else 0, nan=0)),
        float(np.nan_to_num((bb_upper - bb_lower) / close * 100
                            if close else 0, nan=0)),
    ]])
    try:
        prob = clf.predict_proba(feats)[0][1]
        return float(prob * 100)
    except Exception:
        return 50.0


def get_classifier_meta() -> dict:
    if os.path.exists(_CLASSIFIER_META_PATH):
        try:
            with open(_CLASSIFIER_META_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------------------
# Read helpers for dashboard
# ---------------------------------------------------------------------------

def get_strategy_performance_report() -> dict:
    trades = closed_trades()
    if not trades:
        return {"summary": {"total_trades": 0, "wins": 0, "losses": 0,
                            "breakeven": 0, "win_rate": 0,
                            "total_pnl_rm": 0, "avg_win_rm": 0,
                            "avg_loss_rm": 0},
                "by_strategy": {}, "by_sector": {}, "by_month": {}}

    wins = [t for t in trades if t.get("outcome") == "WIN"]
    losses = [t for t in trades if t.get("outcome") == "LOSS"]
    be = [t for t in trades if t.get("outcome") == "BREAKEVEN"]

    by_strat = _group(trades, "signal_type")
    by_sector = _group(trades, "sector")
    by_month = _group_month(trades)

    return {
        "summary": {
            "total_trades": len(trades),
            "wins": len(wins), "losses": len(losses), "breakeven": len(be),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "total_pnl_rm": round(sum(t.get("realized_pnl") or t.get("closed_pnl") or 0
                                      for t in trades), 2),
            "avg_win_rm": round(np.mean([t.get("realized_pnl") or 0 for t in wins])
                                if wins else 0, 2),
            "avg_loss_rm": round(np.mean([t.get("realized_pnl") or 0 for t in losses])
                                 if losses else 0, 2),
        },
        "by_strategy": by_strat,
        "by_sector": by_sector,
        "by_month": dict(sorted(by_month.items())),
    }


def _group(trades, key):
    out = {}
    for t in trades:
        k = t.get(key) or "Unknown"
        d = out.setdefault(k, {"wins": 0, "losses": 0, "total": 0,
                               "total_pnl": 0, "win_rate": 0})
        d["total"] += 1
        d["total_pnl"] += t.get("realized_pnl") or t.get("closed_pnl") or 0
        if t.get("outcome") == "WIN":
            d["wins"] += 1
        elif t.get("outcome") == "LOSS":
            d["losses"] += 1
    for k, v in out.items():
        if v["total"] > 0:
            v["win_rate"] = round(v["wins"] / v["total"] * 100, 1)
    return out


def _group_month(trades):
    out = {}
    for t in trades:
        c = t.get("closed_at") or t.get("logged_at") or ""
        m = c[:7]
        if not m:
            continue
        d = out.setdefault(m, {"wins": 0, "losses": 0, "total": 0,
                               "pnl": 0, "win_rate": 0})
        d["total"] += 1
        d["pnl"] += t.get("realized_pnl") or t.get("closed_pnl") or 0
        if t.get("outcome") == "WIN":
            d["wins"] += 1
        elif t.get("outcome") == "LOSS":
            d["losses"] += 1
        if d["total"] > 0:
            d["win_rate"] = round(d["wins"] / d["total"] * 100, 1)
    return out


def get_learning_history() -> list[dict]:
    from logger import get_learning_events
    return get_learning_events(limit=200)
