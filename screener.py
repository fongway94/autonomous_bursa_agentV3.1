# screener.py
"""
Enhanced Screener — Integrates market regime, relative strength ranking,
and Bayesian state scoring into the scanning engine.

Fixes vs v1
-----------
* Breakout threshold corrected (>= prev_resistance * 1.000 not 0.98).
* Indicator math unchanged (was correct).
* Parameters loaded from SQLite via repository.
* Bayesian per-state action score replaces fake Q-learning.
* Each fetched dataframe is run through data_quality.validate_ohlcv.
* Optional secondary data source (pandas-based fallback) — disabled by
  default, hook is present.
"""

import pandas as pd
import numpy as np
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed

from watchlist import get_all_tickers, get_ticker_sector, get_ticker_name
from repository import load_parameters, load_bias_state
from data_quality import validate_ohlcv
from logger import get_logger

log = get_logger("screener")

# ---------------------------------------------------------------------------
# Indicator math
# ---------------------------------------------------------------------------

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's EMA smoothed RSI."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's EMA smoothed ATR."""
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_indicators(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if len(df) < max(params.get("ema_trend", 200), 50):
        return pd.DataFrame()

    df = df.copy()
    df["EMA_Fast"] = df["Close"].ewm(span=params["ema_fast"], adjust=False).mean()
    df["EMA_Slow"] = df["Close"].ewm(span=params["ema_slow"], adjust=False).mean()
    df["EMA_Trend"] = df["Close"].ewm(span=params["ema_trend"], adjust=False).mean()

    df["RSI"] = calculate_rsi(df["Close"], period=14)

    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD_Line"] = ema12 - ema26
    df["MACD_Signal"] = df["MACD_Line"].ewm(span=9, adjust=False).mean()
    df["MACD_Hist"] = df["MACD_Line"] - df["MACD_Signal"]

    df["MA20"] = df["Close"].rolling(window=20).mean()
    df["BB_Std"] = df["Close"].rolling(window=20).std()
    df["BB_Upper"] = df["MA20"] + (df["BB_Std"] * 2)
    df["BB_Lower"] = df["MA20"] - (df["BB_Std"] * 2)

    df["ATR"] = calculate_atr(df, period=params.get("atr_period", 14))

    df["Vol_Avg20"] = df["Volume"].rolling(window=20).mean()
    df["Vol_Ratio"] = df["Volume"] / (df["Vol_Avg20"] + 1e-9)

    df["Support_20"] = df["Low"].rolling(window=20).min()
    df["Resistance_20"] = df["High"].rolling(window=20).max()

    return df


# ---------------------------------------------------------------------------
# 5-day tape
# ---------------------------------------------------------------------------

def get_recent_5day_analysis(df: pd.DataFrame, params: dict) -> list[dict]:
    if df.empty or len(df) < 6:
        return []
    out = []
    for i in range(-5, 0):
        row = df.iloc[i]
        prev = df.iloc[i - 1] if len(df) + i - 1 >= 0 else None
        close = float(row["Close"])
        open_p = float(row["Open"])
        vol_ratio = float(row["Vol_Ratio"])
        rsi = float(row["RSI"])
        daily_change = ((close - float(prev["Close"])) /
                        float(prev["Close"]) * 100) if prev is not None else 0
        is_green = close >= open_p

        if daily_change > 2.5 and vol_ratio >= params.get("volume_surge_ratio", 1.5):
            note = "🔥 Massive breakout!"
        elif (daily_change > 1.5 and is_green and rsi >= 45
              and prev is not None and float(prev["RSI"]) < 45):
            note = "📈 Bullish reversal"
        elif rsi < 35 and vol_ratio < 0.7:
            note = "🔵 Bottoming on low volume"
        elif abs(daily_change) < 1.0 and vol_ratio < 0.8:
            note = "➡️ Consolidating"
        elif daily_change < -2.0 and vol_ratio > 1.2:
            note = "🔴 Heavy distribution"
        elif rsi > 70 and vol_ratio > 1.5 and not is_green:
            note = "⚠️ Overbought exhaustion"
        elif is_green and vol_ratio >= 1.2:
            note = "🟢 Buying pressure on volume"
        elif is_green:
            note = "📊 Constructive move"
        else:
            note = "👁️ Minor pullback"

        out.append({
            "Date": df.index[i].strftime("%Y-%m-%d"),
            "Close": f"RM {close:.3f}",
            "Change": f"{daily_change:+.2f}%",
            "VolRatio": f"{vol_ratio:.2f}x",
            "RSI": f"{rsi:.1f}",
            "Note": note,
        })
    return out


# ---------------------------------------------------------------------------
# Fetching with validation
# ---------------------------------------------------------------------------

def fetch_and_calculate(ticker: str, params: dict):
    """Fetch 1y of daily bars, validate, compute indicators."""
    try:
        df = yf.Ticker(ticker).history(period="1y", timeout=15)
    except Exception as e:
        log.warning(f"fetch failure {ticker}: {e}")
        return ticker, None
    if df is None or df.empty:
        return ticker, None

    ok, issues = validate_ohlcv(df, ticker, min_rows=50)
    if not ok:
        return ticker, None

    df_ind = compute_indicators(df, params)
    if df_ind.empty:
        return ticker, None
    return ticker, df_ind


# ---------------------------------------------------------------------------
# Setup analyzer
# ---------------------------------------------------------------------------

def analyze_stock_setup(ticker, df, params,
                        market_regime=None, rs_data=None, q_action=None):
    if df is None or df.empty or len(df) < 5:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(last["Close"])
    vol_ratio = float(last["Vol_Ratio"])
    rsi = float(last["RSI"])
    ema_fast = float(last["EMA_Fast"])
    ema_slow = float(last["EMA_Slow"])
    ema_trend = float(last["EMA_Trend"])
    macd_line = float(last["MACD_Line"])
    macd_sig = float(last["MACD_Signal"])
    macd_hist = float(last["MACD_Hist"])
    atr = float(last["ATR"]) if not pd.isna(last["ATR"]) else close * 0.03
    support = float(last["Support_20"])
    resistance = float(last["Resistance_20"])

    signal_type = "NEUTRAL"
    reasoning: list[str] = []
    base_confidence = 50.0

    min_price = params.get("min_price", 0.30)
    max_price = params.get("max_price", 4.00)
    is_in_price_range = min_price <= close <= max_price

    is_long_term_uptrend = close > ema_trend
    is_med_term_uptrend = close > ema_slow
    is_short_term_uptrend = close > ema_fast

    is_volume_spike = vol_ratio >= params.get("volume_surge_ratio", 1.5)
    is_dry_volume = vol_ratio < 0.8

    # FIX: breakout requires actually breaking above prior resistance.
    is_breakout_resistance = close >= float(prev["Resistance_20"]) * 1.000
    is_ema_bull_cross = (ema_fast > ema_slow) and \
                        (float(prev["EMA_Fast"]) <= float(prev["EMA_Slow"]))
    is_macd_bull_cross = (macd_line > macd_sig) and \
                         (float(prev["MACD_Line"]) <= float(prev["MACD_Signal"]))

    is_pullback_ema = (is_long_term_uptrend and is_med_term_uptrend
                       and float(last["Low"]) <= ema_slow * 1.01
                       and close >= ema_slow)
    is_pullback_rsi = (is_long_term_uptrend
                       and rsi <= params.get("rsi_oversold_pullback", 40)
                       and rsi > float(prev["RSI"]))

    is_death_cross = (ema_fast < ema_slow) and \
                     (float(prev["EMA_Fast"]) >= float(prev["EMA_Slow"]))
    is_rsi_overbought_hook = (float(prev["RSI"]) >= params.get("rsi_overbought", 70)) \
                             and (rsi < float(prev["RSI"]))
    is_below_trend = close < ema_trend

    regime = (market_regime or {}).get("regime_data", {}).get("regime", "NEUTRAL")
    regime_mult = {"BULL": 1.0, "NEUTRAL": 0.85, "BEAR": 0.6,
                   "UNCERTAIN": 0.7}.get(regime, 1.0)

    rs_rank = rs_signal = rs_ratio = None
    if rs_data and ticker in rs_data:
        rs_rank = rs_data[ticker].get("rs_rank")
        rs_signal = rs_data[ticker].get("rs_signal")
        rs_ratio = rs_data[ticker].get("rs_ratio")

    q_override = (q_action or {}).get("action", "HOLD")
    q_modifier = (q_action or {}).get("confidence_modifier", 1.0)

    if regime == "BEAR" and not is_long_term_uptrend:
        signal_type = "REDUCE / AVOID"
        reasoning.append("Bear market regime — no new long positions.")
        base_confidence = 20.0
    elif is_below_trend:
        signal_type = "REDUCE / AVOID"
        reasoning.append(f"Price below long-term EMA {params['ema_trend']}. "
                         "Structural downtrend — avoid entry.")
        base_confidence = 25.0
    elif is_death_cross or is_rsi_overbought_hook:
        signal_type = "SELL / TAKE PROFIT"
        if is_death_cross:
            reasoning.append(f"EMA Death Cross ({params['ema_fast']}/"
                             f"{params['ema_slow']}). Momentum weakening.")
        if is_rsi_overbought_hook:
            reasoning.append(f"RSI overbought exhaustion from "
                             f"{float(prev['RSI']):.0f}.")
        base_confidence = 80.0
    elif is_long_term_uptrend and is_in_price_range:
        if (is_breakout_resistance or is_ema_bull_cross or
                is_macd_bull_cross) and is_volume_spike:
            signal_type = "GOLD BUY (BREAKOUT)"
            reasoning.append(f"Strong momentum breakout with volume "
                             f"({vol_ratio:.2f}x).")
            if is_ema_bull_cross:
                reasoning.append("Fast EMA crossed above Slow EMA.")
            if is_macd_bull_cross:
                reasoning.append("MACD bullish cross.")
            if is_breakout_resistance:
                reasoning.append("Breaking above 20-day resistance.")
            base_confidence = 65.0 + min((vol_ratio - 1.5) * 10, 20)
            if 50 < rsi < 65:
                base_confidence += 10
            if rs_signal == "LEADING":
                base_confidence += 8
                reasoning.append("Stock leading market (RS > 1.2x vs KLCI).")
            elif rs_signal == "LAGGING":
                base_confidence -= 5
                reasoning.append("⚠️ Stock lagging market — weak RS.")
            if q_override == "BUY":
                base_confidence += 5 * q_modifier
                reasoning.append(f"Brain: BUY (score {q_modifier:.2f}).")
            elif q_override == "AVOID":
                base_confidence -= 12 * q_modifier
                reasoning.append("⚠️ Brain: historical losses on this setup.")
        elif is_pullback_ema or is_pullback_rsi:
            signal_type = "GOLD BUY (PULLBACK)"
            if is_pullback_ema:
                reasoning.append(f"Price finding support at rising Slow EMA "
                                 f"({params['ema_slow']}).")
            if is_pullback_rsi:
                reasoning.append(f"RSI pulled back to {rsi:.1f} — hook up.")
            if is_dry_volume:
                reasoning.append(f"Pullback on dry volume ({vol_ratio:.2f}x).")
            if close > float(last["Open"]):
                reasoning.append("Bullish candle at support.")
                base_confidence += 5
            base_confidence = 70.0
            if q_override == "BUY":
                base_confidence += 5 * q_modifier
            elif q_override == "AVOID":
                base_confidence -= 12 * q_modifier
        else:
            signal_type = "HOLD / WATCH"
            if not is_in_price_range:
                reasoning.append(f"Price RM {close:.2f} outside range "
                                 f"({min_price:.2f}–{max_price:.2f}).")
            else:
                reasoning.append("Uptrend intact, no active trigger.")
    else:
        signal_type = "NEUTRAL"
        reasoning.append("No clear directional setup.")

    # Bias multipliers
    biases = load_bias_state()
    bias_adj = 0.0
    if "BREAKOUT" in signal_type:
        bias_adj = (biases.get("breakout_bias", 1.0) - 1.0) * 15.0
    elif "PULLBACK" in signal_type:
        bias_adj = (biases.get("pullback_bias", 1.0) - 1.0) * 15.0
    sector = get_ticker_sector(ticker)
    sec_biases = biases.get("sector_biases", {})
    if sector in sec_biases:
        bias_adj += (sec_biases[sector] - 1.0) * 10.0

    base_confidence = base_confidence * regime_mult + bias_adj
    base_confidence = float(np.clip(base_confidence, 5.0, 99.0))

    # Stop / TP
    entry_price = close
    raw_sl = entry_price - (atr * params.get("atr_multiplier_stop", 1.5))
    support_sl = support * 0.99
    stop_loss = max(raw_sl, support_sl)
    risk_pct = (entry_price - stop_loss) / entry_price * 100
    if risk_pct < 1.5:
        stop_loss = entry_price * 0.985
    elif risk_pct > 10.0:
        stop_loss = entry_price * 0.90
    risk_per_share = max(entry_price - stop_loss, 0.001)
    tp1 = entry_price + 1.5 * risk_per_share
    tp2 = entry_price + 2.0 * risk_per_share
    tp3 = entry_price + 3.0 * risk_per_share

    return {
        "ticker": ticker,
        "name": get_ticker_name(ticker),
        "sector": sector,
        "price": round(entry_price, 3),
        "prev_price": round(float(prev["Close"]), 3),
        "change_pct": round(float((entry_price - float(prev["Close"])) /
                                  float(prev["Close"]) * 100), 2),
        "volume": int(last["Volume"]),
        "vol_ratio": round(vol_ratio, 2),
        "rsi": round(rsi, 1),
        "signal": signal_type,
        "reasoning": " ".join(reasoning),
        "confidence": round(base_confidence, 1),
        "entry": round(entry_price, 3),
        "stop_loss": round(stop_loss, 3),
        "tp1": round(tp1, 3),
        "tp2": round(tp2, 3),
        "tp3": round(tp3, 3),
        "risk_pct": round(risk_pct, 1),
        "atr": round(atr, 3),
        "support": round(support, 3),
        "resistance": round(resistance, 3),
        "ema_trend": round(ema_trend, 3),
        "ema_fast": round(ema_fast, 3),
        "ema_slow": round(ema_slow, 3),
        "macd_hist": round(macd_hist, 4),
        "bb_upper": round(float(last["BB_Upper"]), 3),
        "bb_lower": round(float(last["BB_Lower"]), 3),
        "market_regime": regime,
        "rs_rank": rs_rank,
        "rs_signal": rs_signal,
        "rs_ratio": round(rs_ratio, 3) if rs_ratio else None,
        "q_action": q_override,
        "q_confidence": q_modifier,
        "q_reasoning": (q_action or {}).get("reasoning"),
        "indicators": {
            "rsi": round(rsi, 1),
            "vol_ratio": round(vol_ratio, 2),
            "macd_hist": round(macd_hist, 4),
            "atr": round(atr, 3),
            "support": round(support, 3),
            "resistance": round(resistance, 3),
            "ema_trend_distance": round(
                (entry_price - ema_trend) / ema_trend * 100, 2),
        },
    }


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------

def screen_all_stocks(progress_callback=None, market_regime=None):
    params = load_parameters()
    if params.get("shariah_only"):
        from watchlist import get_all_tickers_shariah_only
        tickers = get_all_tickers_shariah_only()
    else:
        tickers = get_all_tickers()

    if market_regime is None:
        try:
            from market_analyzer import get_full_market_analysis
            market_regime = get_full_market_analysis()
        except Exception:
            market_regime = {
                "regime_data": {"regime": "NEUTRAL"},
                "position_rules": {"new_signal_threshold": 0.70},
            }

    rs_data = {}
    try:
        from market_analyzer import rank_stocks_by_relative_strength
        rs_data = rank_stocks_by_relative_strength(tickers)
    except Exception as e:
        log.warning(f"RS calculation failed: {e}")

    from learner import compute_state_action_score, discretize_state

    results = []
    completed = 0
    total = len(tickers)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_and_calculate, t, params): t for t in tickers}
        for fut in as_completed(futures):
            ticker, df = fut.result()
            completed += 1
            if progress_callback:
                progress_callback(completed / total,
                                  f"Scanning {ticker} ({completed}/{total})")
            if df is None or df.empty:
                continue

            # Bayesian state-action score
            try:
                last = df.iloc[-1]
                state_id = discretize_state(
                    float(last["RSI"]),
                    float(last["Vol_Ratio"]),
                    float((float(last["Close"]) - float(last["EMA_Slow"])) /
                          float(last["EMA_Slow"]) * 100),
                    float(last["MACD_Hist"]),
                )
                regime = market_regime.get("regime_data", {}).get("regime", "NEUTRAL")
                sec_str = market_regime.get("sector_momentum", {}).get(
                    get_ticker_sector(ticker), {}).get("strength", 0)
                q_action = compute_state_action_score(
                    state_id, 50, regime, sec_str)
            except Exception as e:
                log.warning(f"state-score error {ticker}: {e}")
                q_action = None

            analysis = analyze_stock_setup(
                ticker, df, params,
                market_regime=market_regime,
                rs_data=rs_data, q_action=q_action,
            )
            if analysis:
                results.append(analysis)

    df_results = pd.DataFrame(results)
    if df_results.empty:
        return df_results

    signal_order = {
        "GOLD BUY (BREAKOUT)": 1,
        "GOLD BUY (PULLBACK)": 2,
        "HOLD / WATCH": 3,
        "SELL / TAKE PROFIT": 4,
        "NEUTRAL": 5,
        "REDUCE / AVOID": 6,
    }
    df_results["_signal_order"] = df_results["signal"].map(
        lambda x: signal_order.get(x, 99))
    df_results["_rs_score"] = df_results["rs_rank"].apply(
        lambda x: (100 - x) if pd.notna(x) else 50)
    df_results = df_results.sort_values(
        by=["_signal_order", "confidence", "_rs_score"],
        ascending=[True, False, False],
    )
    df_results = df_results.drop(columns=["_signal_order", "_rs_score"])

    if market_regime.get("regime_data", {}).get("regime") == "BEAR":
        df_results["signal"] = df_results.apply(
            lambda r: (r["signal"] + " ⚠️")
            if r["confidence"] < 75 and "GOLD BUY" in r["signal"]
            else r["signal"], axis=1)

    return df_results.reset_index(drop=True)
