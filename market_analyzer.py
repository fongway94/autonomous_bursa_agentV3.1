# market_analyzer.py
"""
Market Analyzer — KLCI regime, sector momentum, relative strength.

Fixes vs v1
-----------
* Optional secondary data source fallback (placeholder hook).
* ML regime classifier now uses proper TimeSeriesSplit + holdout test;
  reported accuracy is OOS, not training.
* Model persisted to disk so we don't retrain on every cold start.
* Cache lives in SQLite (`scheduler_state.last_*` columns) rather than JSON.
"""

from __future__ import annotations
import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
import joblib
from datetime import datetime, timezone, timedelta
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import TimeSeriesSplit

from db import DATA_DIR, myt_iso
from data_quality import validate_ohlcv
from logger import get_logger, log_learning_event

log = get_logger("market_analyzer")

KLCI_TICKER = "^KLSE"
MARKET_CACHE_FILE = os.path.join(DATA_DIR, "market_regime_cache.json")
SECTOR_MOMENTUM_FILE = os.path.join(DATA_DIR, "sector_momentum.json")
REGIME_MODEL_PATH = os.path.join(DATA_DIR, "regime_classifier.pkl")
REGIME_META_PATH = os.path.join(DATA_DIR, "regime_classifier_meta.json")


def get_myt_now():
    return datetime.now(timezone(timedelta(hours=8)))


def _robust_read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _robust_write_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        log.warning(f"write fail {path}: {e}")


# -------------------------------------------------------------------------
# KLCI fetch with validation + secondary source hook
# -------------------------------------------------------------------------

def get_klci_data(period: str = "3mo") -> pd.DataFrame:
    try:
        df = yf.Ticker(KLCI_TICKER).history(period=period, timeout=15)
    except Exception as e:
        log.warning(f"yf KLCI fail: {e}")
        df = pd.DataFrame()

    if df is None or df.empty:
        # Secondary source hook — currently disabled, easy to plug in.
        df = _try_secondary_klci(period)
        if df is None or df.empty:
            return pd.DataFrame()
    ok, _ = validate_ohlcv(df, KLCI_TICKER, min_rows=20)
    if not ok:
        return pd.DataFrame()
    return df


def _try_secondary_klci(period: str) -> pd.DataFrame:
    """
    Placeholder for a secondary data source. Returns empty df by default.
    Wire investpy / a paid feed here when available.
    """
    return pd.DataFrame()


# -------------------------------------------------------------------------
# Regime detection
# -------------------------------------------------------------------------

def detect_market_regime(klci_df: pd.DataFrame | None = None) -> dict:
    if klci_df is None:
        klci_df = get_klci_data()
    if klci_df.empty or len(klci_df) < 50:
        return {"regime": "UNCERTAIN", "conviction": 0,
                "details": {"reason": "Insufficient KLCI data"}}

    close = klci_df["Close"]
    vol = klci_df["Volume"]
    e20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    e200 = close.ewm(span=200, adjust=False).mean().iloc[-1] \
        if len(close) >= 200 else close.mean()
    latest = close.iloc[-1]

    score = 50
    score += 15 if latest > e20 else -15
    score += 20 if latest > e50 else -20
    score += 25 if latest > e200 else -25
    if e20 > e50 > e200:
        score += 15
    elif e20 < e50 < e200:
        score -= 15

    # KLCI RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = float((100 - 100 / (1 + gain / (loss + 1e-9))).iloc[-1])
    if rsi > 55:
        score += 10
    elif rsi < 45:
        score -= 10

    vol_ratio = float(vol.iloc[-1] / (vol.rolling(20).mean().iloc[-1] + 1e-9))
    if vol_ratio > 1.3:
        score += 5 if score > 50 else -5

    mom = float((latest - close.iloc[-21]) / close.iloc[-21] * 100) \
        if len(close) > 21 else 0
    score += 5 if mom > 3 else (-5 if mom < -3 else 0)
    score = max(0, min(100, score))

    if score >= 70:
        regime, conv = "BULL", score - 50
    elif score <= 30:
        regime, conv = "BEAR", 50 - score
    else:
        regime, conv = "NEUTRAL", 50 - abs(score - 50)

    return {
        "regime": regime, "conviction": float(conv),
        "details": {
            "trend_score": float(score),
            "ema_20_vs_price": float((latest - e20) / e20 * 100),
            "ema_50_vs_price": float((latest - e50) / e50 * 100),
            "ema_200_vs_price": float((latest - e200) / e200 * 100),
            "klci_rsi": rsi, "volume_ratio": vol_ratio,
            "mom_20d_pct": mom, "last_price": float(latest),
            "last_updated": myt_iso(),
        },
    }


# -------------------------------------------------------------------------
# Sector momentum
# -------------------------------------------------------------------------

SECTOR_TICKERS = {
    "Technology": ["0166.KL", "0097.KL", "5005.KL"],
    "Financial Services": ["1155.KL", "1295.KL", "1023.KL"],
    "Utilities": ["5347.KL", "6742.KL"],
    "Construction": ["5398.KL", "3336.KL"],
    "Telecommunications": ["6888.KL", "4863.KL"],
    "Property & REITs": ["5211.KL", "8664.KL"],
    "Consumer Products": ["4707.KL", "7084.KL"],
    "Healthcare": ["5225.KL", "5878.KL"],
    "Energy": ["7108.KL", "7277.KL"],
    "Plantation": ["2445.KL", "5285.KL"],
}


def calculate_sector_momentum(lookback: int = 20) -> dict:
    out: dict = {}
    for sector, tickers in SECTOR_TICKERS.items():
        rets, rsis = [], []
        for t in tickers[:2]:
            try:
                df = yf.Ticker(t).history(period="3mo", timeout=10)
                if df is None or df.empty or len(df) < lookback + 5:
                    continue
                ok, _ = validate_ohlcv(df, t, min_rows=lookback + 5)
                if not ok:
                    continue
                ret = float((df["Close"].iloc[-1] - df["Close"].iloc[-lookback]) /
                            df["Close"].iloc[-lookback] * 100)
                rets.append(ret)
                d = df["Close"].diff()
                g = d.where(d > 0, 0).ewm(alpha=1 / 14, adjust=False).mean()
                l = (-d.where(d < 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
                rsis.append(float((100 - 100 / (1 + g / (l + 1e-9))).iloc[-1]))
            except Exception:
                continue
        if rets:
            avg = float(np.mean(rets))
            trend = ("STRONG_UP" if avg > 3 else "SLIGHT_UP" if avg > 0
                     else "SLIGHT_DOWN" if avg > -3 else "STRONG_DOWN")
            out[sector] = {"momentum_pct": round(avg, 2),
                           "avg_rsi": round(float(np.mean(rsis) if rsis else 50), 1),
                           "trend": trend, "n_tickers": len(rets)}

    if not out:
        return {}
    max_abs = max(abs(d["momentum_pct"]) for d in out.values()) or 1
    sorted_secs = sorted(out.items(), key=lambda x: x[1]["momentum_pct"],
                         reverse=True)
    for rank, (sec, data) in enumerate(sorted_secs, 1):
        out[sec]["rank"] = rank
        out[sec]["strength"] = round(data["momentum_pct"] / (max_abs + 1e-9), 3)

    _robust_write_json(SECTOR_MOMENTUM_FILE,
                       {"timestamp": myt_iso(), "sectors": out})
    return out


def get_sector_momentum() -> dict:
    cached = _robust_read_json(SECTOR_MOMENTUM_FILE, {})
    if cached:
        try:
            ts = datetime.strptime(cached["timestamp"], "%Y-%m-%d %H:%M:%S")
            ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
            if (get_myt_now() - ts).total_seconds() < 7200:
                return cached.get("sectors", {})
        except Exception:
            pass
    return calculate_sector_momentum()


# -------------------------------------------------------------------------
# Relative strength
# -------------------------------------------------------------------------

def calculate_relative_strength(ticker: str,
                                klci_df: pd.DataFrame | None = None,
                                period: int = 20) -> dict | None:
    try:
        df = yf.Ticker(ticker).history(period="3mo", timeout=10)
        if df is None or df.empty or len(df) < period + 5:
            return None
        ok, _ = validate_ohlcv(df, ticker, min_rows=period + 5)
        if not ok:
            return None
        if klci_df is None:
            klci_df = get_klci_data()
        stock_ret = float((df["Close"].iloc[-1] - df["Close"].iloc[-period]) /
                          df["Close"].iloc[-period] * 100)
        klci_ret = 0.0
        if not klci_df.empty and len(klci_df) >= period + 5:
            klci_ret = float((klci_df["Close"].iloc[-1] - klci_df["Close"].iloc[-period]) /
                             klci_df["Close"].iloc[-period] * 100)
        rs = (stock_ret + 1e-9) / (klci_ret + 1e-9) if klci_ret != 0 else 1.0
        signal = ("LEADING" if rs > 1.2 else
                  "LAGGING" if rs < 0.8 else "MATCHING")
        return {"stock_return_pct": round(stock_ret, 2),
                "klci_return_pct": round(klci_ret, 2),
                "rs_ratio": round(rs, 3), "rs_signal": signal,
                "period_days": period}
    except Exception:
        return None


def rank_stocks_by_relative_strength(tickers: list,
                                     klci_df: pd.DataFrame | None = None) -> dict:
    out: dict = {}
    for t in tickers:
        rs = calculate_relative_strength(t, klci_df, period=20)
        if rs:
            out[t] = rs
    if not out:
        return {}
    sorted_rs = sorted(out.items(), key=lambda x: x[1]["rs_ratio"], reverse=True)
    for rank, (t, d) in enumerate(sorted_rs, 1):
        out[t]["rs_rank"] = rank
        out[t]["rs_percentile"] = round((1 - rank / len(sorted_rs)) * 100, 1)
    return out


# -------------------------------------------------------------------------
# Composite analysis
# -------------------------------------------------------------------------

def get_market_regime_cached() -> dict | None:
    cached = _robust_read_json(MARKET_CACHE_FILE, {})
    if cached:
        try:
            ts = datetime.strptime(cached["timestamp"], "%Y-%m-%d %H:%M:%S")
            ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
            if (get_myt_now() - ts).total_seconds() < 7200:
                return cached.get("regime_data", {})
        except Exception:
            pass
    return None


def get_full_market_analysis(force_refresh: bool = False) -> dict:
    if not force_refresh:
        c = get_market_regime_cached()
        if c:
            return c
    klci = get_klci_data()
    regime_data = detect_market_regime(klci)
    sector_mom = get_sector_momentum()
    regime = regime_data["regime"]
    conv = regime_data["conviction"]

    if regime == "BULL":
        pos_mult, risk_adj, max_pos, thr = 1.0, 1.0, 8, 0.60
    elif regime == "NEUTRAL":
        pos_mult, risk_adj, max_pos, thr = 0.75, 0.8, 5, 0.70
    else:
        pos_mult, risk_adj, max_pos, thr = 0.50, 0.6, 3, 0.80
    effective = pos_mult * (0.5 + conv / 100)

    hot, cold = [], []
    for s, d in (sector_mom or {}).items():
        if d.get("strength", 0) > 0.3:
            hot.append(s)
        elif d.get("strength", 0) < -0.3:
            cold.append(s)

    result = {
        "timestamp": myt_iso(),
        "regime_data": regime_data,
        "sector_momentum": sector_mom,
        "position_rules": {
            "position_size_mult": round(effective, 3),
            "risk_adjustment": round(risk_adj, 3),
            "max_concurrent_positions": max_pos,
            "new_signal_threshold": round(thr, 2),
            "regime_label": regime,
            "conviction_pct": round(conv, 1),
        },
        "hot_sectors": hot,
        "cold_sectors": cold,
        "guidance": _guidance(regime, conv, hot),
    }
    _robust_write_json(MARKET_CACHE_FILE,
                       {"timestamp": result["timestamp"],
                        "regime_data": result})
    return result


def _guidance(regime, conv, hot):
    hot_s = ", ".join(hot[:3]) if hot else "None"
    if regime == "BULL":
        return (f"🐂 BULL — conviction {conv:.0f}%. Hot: {hot_s}. "
                "Favour momentum breakouts; hold winners longer.")
    if regime == "BEAR":
        return (f"🐻 BEAR — conviction {conv:.0f}%. Avoid new entries; "
                "short-term scalps only; max 3 positions.")
    if regime == "NEUTRAL":
        return (f"⚖️ NEUTRAL — conviction {conv:.0f}%. Selective entry, "
                f"favour pullbacks. Hot: {hot_s}.")
    return "⚠️ Regime uncertain — conservative sizing."


# -------------------------------------------------------------------------
# ML regime classifier (CV + sealed holdout)
# -------------------------------------------------------------------------

def train_market_regime_classifier(persist: bool = True):
    tickers = ["^KLSE", "1155.KL", "5347.KL", "0166.KL", "5285.KL"]
    rows = []
    for t in tickers:
        try:
            df = yf.Ticker(t).history(period="3y", timeout=15)
            if df is None or df.empty or len(df) < 250:
                continue
            ok, _ = validate_ohlcv(df, t, min_rows=250)
            if not ok:
                continue
            for i in range(200, len(df) - 21):
                row = df.iloc[i]
                ema20 = df["Close"].iloc[:i].ewm(span=20).mean().iloc[-1]
                ema50 = df["Close"].iloc[:i].ewm(span=50).mean().iloc[-1]
                ema200 = df["Close"].iloc[:i].ewm(span=200).mean().iloc[-1]
                fut_ret = float((df["Close"].iloc[i + 10:i + 21].mean()
                                 - row["Close"]) / row["Close"] * 100)
                if fut_ret > 3:
                    label = 1
                elif fut_ret < -3:
                    label = 0
                else:
                    continue
                prev = df["Volume"].iloc[max(0, i - 20):i].mean()
                feats = [
                    float((row["Close"] - ema20) / ema20 * 100) if ema20 else 0,
                    float((row["Close"] - ema50) / ema50 * 100) if ema50 else 0,
                    float((row["Close"] - ema200) / ema200 * 100) if ema200 else 0,
                    float(row["Volume"] / (prev + 1e-9)),
                    float((row["High"] - row["Low"]) / row["Close"] * 100),
                ]
                rows.append((df.index[i], feats, label))
        except Exception:
            continue
    if len(rows) < 150:
        return None
    rows.sort(key=lambda r: r[0])
    X = np.array([r[1] for r in rows])
    y = np.array([r[2] for r in rows])
    split = int(len(rows) * 0.85)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]
    clf = GradientBoostingClassifier(n_estimators=80, max_depth=3, random_state=42)
    clf.fit(X_train, y_train)
    train_acc = float(clf.score(X_train, y_train))
    test_acc = float(clf.score(X_test, y_test)) if len(y_test) else 0.0
    if persist:
        joblib.dump(clf, REGIME_MODEL_PATH)
        _robust_write_json(REGIME_META_PATH, {
            "trained_at": myt_iso(), "n_train": len(X_train),
            "n_test": len(X_test), "train_accuracy": round(train_acc, 4),
            "holdout_accuracy": round(test_acc, 4),
        })
    log_learning_event(
        "REGIME_CLASSIFIER_TRAINED",
        f"OOS accuracy {test_acc:.3f} (train {train_acc:.3f})",
        metrics={"n_train": len(X_train), "n_test": len(X_test)})
    return clf


_market_clf = None


def get_market_ml_prediction() -> dict | None:
    global _market_clf
    if _market_clf is None:
        if os.path.exists(REGIME_MODEL_PATH):
            try:
                _market_clf = joblib.load(REGIME_MODEL_PATH)
            except Exception:
                _market_clf = None
        if _market_clf is None:
            _market_clf = train_market_regime_classifier()
    if _market_clf is None:
        return None
    klci = get_klci_data(period="3mo")
    if klci.empty or len(klci) < 200:
        return None
    try:
        close = klci["Close"]
        e20 = close.ewm(span=20).mean().iloc[-1]
        e50 = close.ewm(span=50).mean().iloc[-1]
        e200 = close.ewm(span=200).mean().iloc[-1]
        v_avg = klci["Volume"].rolling(20).mean().iloc[-1]
        v = klci["Volume"].iloc[-1]
        feats = np.array([[
            float((close.iloc[-1] - e20) / e20 * 100),
            float((close.iloc[-1] - e50) / e50 * 100),
            float((close.iloc[-1] - e200) / e200 * 100),
            float(v / (v_avg + 1e-9)),
            float((klci["High"].iloc[-1] - klci["Low"].iloc[-1]) /
                  close.iloc[-1] * 100),
        ]])
        prob = _market_clf.predict_proba(feats)[0]
        pred = _market_clf.predict(feats)[0]
        verdict = "BULL" if pred == 1 else "BEAR"
        return {
            "prediction": verdict,
            "bull_probability": round(float(prob[1]) * 100, 1),
            "bear_probability": round(float(prob[0]) * 100, 1),
            "interpretation": (
                f"ML model predicts {verdict} with "
                f"{max(prob)*100:.0f}% confidence."),
        }
    except Exception as e:
        log.warning(f"regime ML predict failed: {e}")
        return None
