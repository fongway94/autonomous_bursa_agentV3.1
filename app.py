# app.py
"""
BursaAI Swing Agent — v2 COMPLETE

Light-themed Streamlit dashboard for an autonomous Bursa Malaysia paper-trading
agent.

Tabs
----
1. 🔍 Scanner       — full market scan + signal cards + chart drill-down
2. 💼 Portfolio     — active positions, manual close, equity, exposure heatmap
3. 🧠 AI Learning   — Bayesian state priors, biases, walk-forward, ML model
4. 📊 Performance   — Sharpe, drawdown, calibration, regime stats, benchmarks
5. 🤖 Robo-Trader   — scheduler status, last/next run, controls, kill-switch
6. 📜 Logs          — trades, scheduler, learning, parameter, data-quality
7. ⚙️ Settings      — risk params, scanner params, capital reset, data mgmt
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import numpy as np
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

# Local modules
from watchlist import (
    get_all_tickers, get_ticker_name, get_ticker_sector,
    add_custom_ticker, load_custom_watchlist_tickers, remove_custom_ticker,
)
from screener import (
    screen_all_stocks, fetch_and_calculate, get_recent_5day_analysis,
)
from repository import (
    load_account, save_account, reset_account,
    load_parameters, save_parameters,
    load_trades, active_trades, closed_trades,
    get_trade, get_scheduler_state, update_scheduler_state,
    load_scan_cache, save_scan_cache,
)
from trading_engine import (
    execute_entry, execute_partial_exit, execute_full_exit,
    calculate_trade_cost, round_to_lot, LOT_SIZE,
    TRANSACTION_COST_PCT,
)
from learner import (
    learn_from_trade_outcome, run_walk_forward_optimization,
    train_setup_classifier, get_ml_score,
    get_strategy_performance_report, get_learning_history,
    get_classifier_meta,
)
from market_analyzer import (
    get_full_market_analysis, get_market_ml_prediction,
)
from risk_manager import (
    run_full_risk_check, get_risk_dashboard_stats,
    load_risk_params, save_risk_params,
    check_trading_time_window,
)
from logger import (
    get_trade_log, get_scheduler_log, get_learning_events,
    get_parameter_history, get_bias_history, get_data_quality_log,
)
from evaluation import full_evaluation_report
import scheduler as sched

from db import get_myt_now, myt_iso

# =========================================================================
# CONFIG
# =========================================================================

st.set_page_config(
    page_title="BursaAI Swing Agent — v2",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Force light theme + light-friendly CSS, even if user has dark OS theme.
LIGHT_CSS = """
<style>
:root {
    --bg: #ffffff;
    --bg-soft: #f5f7fb;
    --bg-card: #ffffff;
    --border: #e5e7eb;
    --text: #1a1a1a;
    --text-soft: #525a67;
    --accent: #1f6feb;
    --good: #16a34a;
    --bad: #dc2626;
    --warn: #d97706;
}
html, body, .stApp, .main, .block-container {
    background-color: var(--bg) !important;
    color: var(--text) !important;
}
section[data-testid="stSidebar"] {
    background-color: var(--bg-soft) !important;
    border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] * { color: var(--text) !important; }
.stMarkdown, .stMarkdown p, .stMarkdown li, .stCaption,
.stTabs [data-baseweb="tab"] p,
.stRadio label, .stCheckbox label, .stSelectbox label,
.stTextInput label, .stNumberInput label, .stSlider label,
.stMetric label, .stExpander summary, h1, h2, h3, h4, h5 {
    color: var(--text) !important;
}
.stMetric [data-testid="stMetricValue"] { color: var(--text) !important; }
.stMetric [data-testid="stMetricDelta"] { font-weight: 600; }
.stButton > button {
    background: var(--bg-card);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
}
.stButton > button:hover { border-color: var(--accent); color: var(--accent); }
.stButton > button[kind="primary"] {
    background: var(--accent); color: white; border: 1px solid var(--accent);
}
.stDataFrame, .stTable { background: var(--bg-card) !important; }
div[data-testid="stDataFrameResizable"] { background: var(--bg-card) !important; }
.stTabs [data-baseweb="tab-list"] {
    border-bottom: 1px solid var(--border);
}
.stTabs [data-baseweb="tab"] {
    background-color: var(--bg-soft);
    color: var(--text) !important;
    border-radius: 8px 8px 0 0;
    padding: 8px 14px;
    margin-right: 4px;
}
.stTabs [data-baseweb="tab"][aria-selected="true"] {
    background: var(--accent); color: white !important;
}
.stTabs [data-baseweb="tab"][aria-selected="true"] p { color: white !important; }
.bursa-card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 16px; margin: 8px 0;
}
.bursa-card-good { border-left: 4px solid var(--good); }
.bursa-card-bad { border-left: 4px solid var(--bad); }
.bursa-card-warn { border-left: 4px solid var(--warn); }
.bursa-card-info { border-left: 4px solid var(--accent); }
.kvp { display:flex; justify-content:space-between; padding:3px 0; }
.kvp .k { color: var(--text-soft); }
.kvp .v { color: var(--text); font-weight: 600; }
hr { border-top: 1px solid var(--border) !important; }
/* Plotly: white background */
.js-plotly-plot .plotly { background: var(--bg-card) !important; }
</style>
"""
st.markdown(LIGHT_CSS, unsafe_allow_html=True)

PLOTLY_TEMPLATE = "plotly_white"
PLOTLY_LAYOUT = dict(
    template=PLOTLY_TEMPLATE,
    paper_bgcolor="white", plot_bgcolor="white",
    font=dict(color="#1a1a1a"),
    margin=dict(l=40, r=20, t=30, b=40),
)


# =========================================================================
# Bootstrap — start scheduler on app launch
# =========================================================================

# v3: call ensure_started on EVERY rerun — it's now self-healing
# (will force-restart if the heartbeat is stale).
try:
    sched.ensure_started(interval_sec=3600)
except Exception as e:
    st.warning(f"Scheduler did not start: {e}")


# =========================================================================
# Sidebar — capital, scheduler badge, custom ticker, quick stats
# =========================================================================

with st.sidebar:
    st.markdown("## 🚀 BursaAI Agent")
    st.caption("Bursa Malaysia · Paper Trading · v3 — auto-trade default ON")

    acc = load_account()
    new_cap = st.number_input(
        "Initial Capital (RM)", min_value=1000.0, max_value=10_000_000.0,
        value=float(acc["initial_capital"]), step=1000.0,
    )
    if abs(new_cap - acc["initial_capital"]) > 0.5:
        if st.button("💾 Update Capital", use_container_width=True):
            save_account(initial_capital=new_cap, cash_balance=new_cap,
                         total_equity=new_cap)
            st.success(f"Capital reset to RM {new_cap:,.0f}")
            st.rerun()

    risk_pct = st.slider("Risk per Trade (%)", 0.25, 3.0, 1.0, 0.25)
    risk_amount = acc["initial_capital"] * (risk_pct / 100.0)

    st.markdown(
        f"""
        <div class="bursa-card bursa-card-info">
          <div class="kvp"><span class="k">Risk per Trade</span>
            <span class="v">RM {risk_amount:,.0f}</span></div>
          <div class="kvp"><span class="k">Cash</span>
            <span class="v">RM {acc['cash_balance']:,.0f}</span></div>
          <div class="kvp"><span class="k">Equity</span>
            <span class="v">RM {acc['total_equity']:,.0f}</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Robo-Trader status badge
    ss = get_scheduler_state()
    running = sched.is_running()
    badge_class = "bursa-card-good" if running else "bursa-card-bad"
    badge_text = "🟢 RUNNING" if running else "🔴 STOPPED"
    st.markdown(
        f"""
        <div class="bursa-card {badge_class}">
          <div style="font-weight:700; margin-bottom:6px;">🤖 Robo-Trader {badge_text}</div>
          <div class="kvp"><span class="k">Last run</span>
            <span class="v">{ss.get('last_run_at') or 'never'}</span></div>
          <div class="kvp"><span class="k">Next run</span>
            <span class="v">{ss.get('next_run_at') or '—'}</span></div>
          <div class="kvp"><span class="k">Heartbeat</span>
            <span class="v">{ss.get('last_heartbeat') or '—'}</span></div>
          <div class="kvp"><span class="k">Interval</span>
            <span class="v">{ss.get('interval_sec', 3600)//60} min</span></div>
          <div class="kvp"><span class="k">Auto-exit</span>
            <span class="v">{'ON' if ss.get('autoexit_enabled') else 'OFF'}</span></div>
          <div class="kvp"><span class="k">Auto-entry</span>
            <span class="v">{'ON' if ss.get('autotrade_enabled') else 'OFF'}</span></div>
          <div class="kvp"><span class="k">Brain mode</span>
            <span class="v">{'🔬 EXPLORE' if ss.get('exploration_mode') else '🎯 EXPLOIT'}</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not running:
        if st.button("♻️ Start Robo-Trader", use_container_width=True):
            sched.start(interval_sec=int(ss.get("interval_sec", 3600)))
            st.success("Robo-Trader started.")
            st.rerun()
    else:
        if st.button("🛑 Stop Robo-Trader", use_container_width=True):
            sched.stop()
            st.warning("Robo-Trader stopped.")
            st.rerun()

    with st.expander("➕ Add Custom Stock"):
        c1, c2 = st.columns(2)
        add_code = c1.text_input("Code (e.g. 5398)", "").strip().upper()
        add_name = c2.text_input("Name", "").strip()
        sectors = ["Technology", "Financial Services", "Utilities",
                   "Construction", "Telecommunications", "Property & REITs",
                   "Consumer Products", "Healthcare", "Energy", "Plantation",
                   "Custom"]
        add_sec = st.selectbox("Sector", sectors, index=10)
        if st.button("Register Ticker", use_container_width=True) \
                and add_code and add_name:
            code = add_code if add_code.endswith(".KL") else add_code + ".KL"
            add_custom_ticker(code, add_name, add_sec)
            st.success(f"✅ {code} added.")
            st.rerun()


# =========================================================================
# Tabs
# =========================================================================

tab_scanner, tab_portfolio, tab_learning, tab_perf, tab_robo, tab_logs, tab_alerts, tab_settings = st.tabs(
    ["🔍 Scanner", "💼 Portfolio", "🧠 AI Learning",
     "📊 Performance", "🤖 Robo-Trader", "📜 Logs",
     "🔔 Live Alerts", "⚙️ Settings"]
)


# =========================================================================
# TAB 1 — Scanner
# =========================================================================
with tab_scanner:
    c1, c2 = st.columns([1, 4])
    run_scan = c1.button("🔥 SCAN MARKET", type="primary",
                          use_container_width=True)
    c2.caption("Background agent scans hourly during market hours. "
               "Manual scan overrides the schedule.")

    if "screener_df" not in st.session_state:
        cached_records, cached_regime, cached_ts = load_scan_cache()
        st.session_state["screener_df"] = (
            pd.DataFrame(cached_records) if cached_records else pd.DataFrame())
        st.session_state["market_regime"] = cached_regime or {}
        if cached_ts:
            st.caption(f"📦 Loaded cached scan from {cached_ts}")

    if run_scan:
        with st.spinner("Scanning Bursa Malaysia…"):
            prog = st.progress(0.0)
            mr = get_full_market_analysis(force_refresh=True)
            df = screen_all_stocks(progress_callback=prog.progress, market_regime=mr)
            prog.empty()
        st.session_state["screener_df"] = df
        st.session_state["market_regime"] = mr
        if not df.empty:
            save_scan_cache(df.to_dict("records"), mr)
        st.rerun()

    mr = st.session_state.get("market_regime", {}) or {}
    regime = mr.get("regime_data", {}).get("regime", "NEUTRAL")
    icons = {"BULL": "🐂", "NEUTRAL": "⚖️", "BEAR": "🐻", "UNCERTAIN": "❓"}
    colors = {"BULL": "var(--good)", "NEUTRAL": "var(--warn)",
              "BEAR": "var(--bad)", "UNCERTAIN": "var(--text-soft)"}
    st.markdown(
        f"""
        <div class="bursa-card" style="border-left:5px solid {colors.get(regime,'gray')};">
            <span style="font-size:1.6rem;">{icons.get(regime,'⚖️')}</span>
            <strong style="font-size:1.05rem; color:{colors.get(regime,'gray')};">
                {regime}</strong>
            <span style="margin-left:8px; color:var(--text-soft);">
                {mr.get('guidance','Market data unavailable.')}</span>
        </div>
        """, unsafe_allow_html=True,
    )

    df = st.session_state.get("screener_df")
    if df is None or df.empty:
        st.info("No scan results yet — click **SCAN MARKET** above.")
    else:
        signal_filter = st.multiselect(
            "Filter signals",
            sorted(df["signal"].unique().tolist()),
            default=[s for s in df["signal"].unique() if "GOLD BUY" in s],
        )
        view = df[df["signal"].isin(signal_filter)] if signal_filter else df

        display_cols = ["ticker", "name", "sector", "signal", "confidence",
                        "price", "change_pct", "vol_ratio", "rsi",
                        "entry", "stop_loss", "tp1", "tp2", "tp3",
                        "risk_pct", "rs_signal"]
        st.dataframe(view[display_cols], use_container_width=True,
                     hide_index=True, height=400)

        st.markdown("---")
        st.markdown("### 🔬 Stock Detail")

        sel = st.selectbox(
            "Choose ticker to analyze",
            view["ticker"].tolist(),
            format_func=lambda t: f"{t} — {get_ticker_name(t)}",
        )
        if sel:
            row = view[view["ticker"] == sel].iloc[0].to_dict()
            params_p = load_parameters()
            _, df_ind = fetch_and_calculate(sel, params_p)
            is_buy = "GOLD BUY" in row.get("signal", "")
            is_sell = "SELL" in row.get("signal", "")

            col_a, col_b = st.columns([1, 1])
            with col_a:
                badge_class = ("bursa-card-good" if is_buy
                               else "bursa-card-bad" if is_sell
                               else "bursa-card-info")
                st.markdown(
                    f"""
                    <div class="bursa-card {badge_class}">
                      <div style="font-weight:700; font-size:1.1rem;">
                        {row['ticker']} · {row['signal']}</div>
                      <div class="kvp"><span class="k">Confidence</span>
                          <span class="v">{row['confidence']:.0f}/100</span></div>
                      <div class="kvp"><span class="k">Price</span>
                          <span class="v">RM {row['price']:.3f}
                          ({row['change_pct']:+.2f}%)</span></div>
                      <div class="kvp"><span class="k">Entry</span>
                          <span class="v">RM {row['entry']:.3f}</span></div>
                      <div class="kvp"><span class="k">Stop Loss</span>
                          <span class="v">RM {row['stop_loss']:.3f}
                          ({row['risk_pct']:.1f}% risk)</span></div>
                      <div class="kvp"><span class="k">TP1 / TP2 / TP3</span>
                          <span class="v">RM {row['tp1']:.3f} ·
                          {row['tp2']:.3f} · {row['tp3']:.3f}</span></div>
                      <div class="kvp"><span class="k">RSI / Vol×</span>
                          <span class="v">{row['rsi']:.1f} ·
                          {row['vol_ratio']:.2f}×</span></div>
                    </div>
                    """, unsafe_allow_html=True,
                )

                if is_buy:
                    risk_per_share = max(row["entry"] - row["stop_loss"], 0.001)
                    suggested_shares = round_to_lot(int(risk_amount / risk_per_share))
                    shares = st.number_input(
                        "Shares (multiples of 100)",
                        min_value=LOT_SIZE, max_value=1_000_000,
                        value=max(suggested_shares, LOT_SIZE),
                        step=LOT_SIZE,
                    )
                    shares = round_to_lot(int(shares))
                    cost_info = calculate_trade_cost(shares, row["entry"])
                    actual_risk = risk_per_share * shares
                    st.caption(
                        f"Outlay ≈ RM {cost_info['gross']:,.0f} "
                        f"+ fee RM {cost_info['fee']:.2f} "
                        f"| Risk RM {actual_risk:,.0f}"
                    )
                    if st.button("✅ EXECUTE BUY ORDER",
                                 use_container_width=True, type="primary"):
                        acc_now = load_account()
                        trades_now = load_trades()
                        rc = run_full_risk_check(
                            trades_now,
                            {"ticker": sel, "sector": row["sector"],
                             "entry": row["entry"], "stop_loss": row["stop_loss"],
                             "cost": cost_info["gross"],
                             "risk_amount": actual_risk},
                            acc_now["cash_balance"], acc_now["initial_capital"],
                        )
                        if rc["pass"]:
                            sized = int(round_to_lot(int(shares * rc["size_multiplier"])))
                            if sized < LOT_SIZE:
                                st.error("Risk check reduced size below 100-share lot.")
                            else:
                                ok, tid, msg = execute_entry(
                                    sel, row["name"], row["sector"],
                                    row["entry"], row["stop_loss"],
                                    row["tp1"], row["tp2"], row["tp3"],
                                    row["signal"], sized,
                                    {"reasoning": row.get("reasoning",""),
                                     "rsi": row.get("rsi"),
                                     "vol_ratio": row.get("vol_ratio"),
                                     "atr": row.get("atr"),
                                     "support": row.get("support"),
                                     "resistance": row.get("resistance"),
                                     "macd_hist": row.get("macd_hist"),
                                     "ema_trend": row.get("ema_trend", row["entry"])},
                                    mr, row["confidence"], execution_type="MANUAL",
                                    actor="USER",
                                )
                                if ok:
                                    st.success(f"Trade #{tid}: {msg}")
                                    st.rerun()
                                else:
                                    st.error(msg)
                        else:
                            st.error(rc["final_verdict"])

                elif is_sell:
                    active_for_t = [t for t in active_trades()
                                    if t["ticker"] == sel]
                    if active_for_t:
                        tid = active_for_t[0]["id"]
                        if st.button(f"🔒 Close {sel} as WIN",
                                     use_container_width=True):
                            ok, msg = execute_full_exit(
                                tid, row["price"], reason="Sell signal",
                                outcome="WIN", actor="USER")
                            if ok:
                                t = get_trade(tid)
                                if t:
                                    learn_from_trade_outcome(t)
                                st.success(msg); st.rerun()
                        if st.button(f"🔒 Close {sel} as LOSS",
                                     use_container_width=True):
                            ok, msg = execute_full_exit(
                                tid, row["price"], reason="Sell signal",
                                outcome="LOSS", actor="USER")
                            if ok:
                                t = get_trade(tid)
                                if t:
                                    learn_from_trade_outcome(t)
                                st.error(msg); st.rerun()
                    else:
                        st.caption("No active position for this ticker.")

            with col_b:
                st.markdown("**🧠 AI Reasoning**")
                st.info(row.get("reasoning", "—"))
                if row.get("q_reasoning"):
                    st.caption(f"🤖 Bayesian brain: {row['q_reasoning']}")
                if df_ind is not None and not df_ind.empty:
                    hist5 = get_recent_5day_analysis(df_ind, params_p)
                    if hist5:
                        st.markdown("**📅 5-Day Tape**")
                        st.dataframe(pd.DataFrame(hist5),
                                     hide_index=True, use_container_width=True)

            # Chart
            if df_ind is not None and not df_ind.empty:
                st.markdown("**📈 Chart (90 days)**")
                d = df_ind.tail(90)
                fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                    vertical_spacing=0.05,
                                    row_heights=[0.72, 0.28])
                fig.add_trace(go.Candlestick(
                    x=d.index, open=d["Open"], high=d["High"],
                    low=d["Low"], close=d["Close"], name="Price",
                    increasing_line_color="#16a34a",
                    decreasing_line_color="#dc2626",
                ), row=1, col=1)
                for nm, col, c, w in [
                    ("EMA-Trend", "EMA_Trend", "#1f6feb", 2),
                    ("EMA-Slow", "EMA_Slow", "#d97706", 1.5),
                    ("EMA-Fast", "EMA_Fast", "#16a34a", 1.5),
                ]:
                    if col in d.columns:
                        fig.add_trace(go.Scatter(x=d.index, y=d[col],
                                                 line=dict(color=c, width=w),
                                                 name=nm), row=1, col=1)
                if is_buy:
                    for y, c, dash, lbl in [
                        (row["entry"], "#16a34a", "dash", "Entry"),
                        (row["stop_loss"], "#dc2626", "dot", "SL"),
                        (row["tp1"], "#16a34a", "longdash", "TP1"),
                        (row["tp2"], "#9333ea", "longdash", "TP2"),
                        (row["tp3"], "#0891b2", "longdash", "TP3"),
                    ]:
                        fig.add_hline(y=y, line_dash=dash, line_color=c,
                                      annotation_text=lbl, row=1, col=1)
                bar_colors = ["#16a34a" if d["Close"].iloc[i] >= d["Open"].iloc[i]
                              else "#dc2626" for i in range(len(d))]
                fig.add_trace(go.Bar(x=d.index, y=d["Volume"],
                                     marker_color=bar_colors, name="Vol"),
                              row=2, col=1)
                fig.update_layout(height=520, xaxis_rangeslider_visible=False,
                                  showlegend=True, **PLOTLY_LAYOUT)
                st.plotly_chart(fig, use_container_width=True)


# =========================================================================
# TAB 2 — Portfolio
# =========================================================================
with tab_portfolio:
    st.markdown("## 💼 Portfolio & Position Management")
    trades = load_trades()
    acc = load_account()

    # Live prices from scan cache
    scan_df = st.session_state.get("screener_df")
    if scan_df is None or scan_df.empty:
        records, _, _ = load_scan_cache()
        scan_df = pd.DataFrame(records) if records else pd.DataFrame()
    price_lookup = ({r["ticker"]: {"price": float(r["price"]),
                                   "change_pct": float(r.get("change_pct", 0))}
                     for _, r in scan_df.iterrows()}
                    if not scan_df.empty else {})

    active = [t for t in trades if t["status"] == "ACTIVE"]
    total_active_cost = sum((t.get("cost") or 0) for t in active)
    total_active_value = 0.0
    enriched = []
    for t in active:
        px = price_lookup.get(t["ticker"], {}).get("price", t["entry_price"])
        shares_r = t.get("shares_remaining") or t.get("shares") or 0
        mv = px * shares_r
        upnl = mv - (t.get("entry_price") * shares_r)
        try:
            d = (get_myt_now().replace(tzinfo=None)
                 - pd.to_datetime(t["logged_at"]).replace(tzinfo=None)).days
        except Exception:
            d = 0
        enriched.append({**t, "live_price": px, "market_val": mv,
                         "unrealized_pnl": upnl, "days_held": d,
                         "shares_remaining": shares_r})
        total_active_value += mv

    equity = acc["cash_balance"] + total_active_value
    returns_pct = (equity / acc["initial_capital"] - 1) * 100

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Equity", f"RM {equity:,.0f}",
              f"{returns_pct:+.2f}% vs start")
    m2.metric("Cash", f"RM {acc['cash_balance']:,.0f}")
    m3.metric("Active Cost", f"RM {total_active_cost:,.0f}")
    m4.metric("Active MV", f"RM {total_active_value:,.0f}",
              f"RM {(total_active_value-total_active_cost):+,.0f}")
    m5.metric("Active Trades", len(active))

    if enriched:
        st.markdown("### 📌 Active Positions")
        cols = ["id", "ticker", "name", "sector", "signal_type",
                "shares_remaining", "entry_price", "live_price",
                "unrealized_pnl", "stop_loss", "tp1", "tp2", "tp3",
                "trailing_stop", "mae_pct", "mfe_pct", "days_held"]
        df_active = pd.DataFrame(enriched)[cols]
        st.dataframe(df_active, hide_index=True, use_container_width=True)

        # Position exposure heatmap (sector)
        sec_exp: dict[str, float] = {}
        for t in active:
            sec = t.get("sector") or "Unknown"
            sec_exp[sec] = sec_exp.get(sec, 0) + (t.get("cost") or 0)
        if sec_exp:
            st.markdown("### 🔥 Sector Exposure")
            df_sec = pd.DataFrame({"sector": list(sec_exp.keys()),
                                   "exposure_rm": list(sec_exp.values())})
            fig = px.bar(df_sec, x="sector", y="exposure_rm",
                         color="exposure_rm", color_continuous_scale="Blues")
            fig.update_layout(**PLOTLY_LAYOUT, height=300)
            st.plotly_chart(fig, use_container_width=True)

        # Manual close panel
        st.markdown("### Manual Close")
        sel_id = st.selectbox("Trade to manage",
                              [t["id"] for t in active],
                              format_func=lambda i: next(
                                  f"#{t['id']} {t['ticker']} "
                                  f"({t['shares_remaining']} sh @ "
                                  f"{t['entry_price']:.3f})"
                                  for t in active if t["id"] == i))
        if sel_id:
            t = get_trade(sel_id)
            px = price_lookup.get(t["ticker"], {}).get("price", t["entry_price"])
            c1, c2, c3 = st.columns(3)
            if c1.button("✅ Close as WIN", use_container_width=True):
                ok, msg = execute_full_exit(sel_id, px, reason="Manual",
                                            outcome="WIN", actor="USER")
                if ok:
                    learn_from_trade_outcome(get_trade(sel_id))
                    st.success(msg); st.rerun()
            if c2.button("❌ Close as LOSS", use_container_width=True):
                ok, msg = execute_full_exit(sel_id, px, reason="Manual",
                                            outcome="LOSS", actor="USER")
                if ok:
                    learn_from_trade_outcome(get_trade(sel_id))
                    st.error(msg); st.rerun()
            half = round_to_lot(t["shares_remaining"] // 2)
            if half > 0 and c3.button(f"½ Partial @ live ({half} sh)",
                                       use_container_width=True):
                ok, msg = execute_partial_exit(sel_id, "MANUAL_PARTIAL", px, half,
                                                reason="Manual partial",
                                                actor="USER")
                if ok:
                    st.info(msg); st.rerun()
    else:
        st.info("No active positions yet.")

    # Closed trades
    st.markdown("### 🗂 Closed Trades")
    closed = [t for t in trades if t["status"] == "CLOSED"]
    if closed:
        cdf = pd.DataFrame(closed)[
            ["id", "ticker", "signal_type", "entry_price", "exit_price",
             "closed_pnl", "outcome", "logged_at", "closed_at",
             "market_regime", "confidence_score", "mae_pct", "mfe_pct"]
        ]
        st.dataframe(cdf.sort_values("closed_at", ascending=False),
                     hide_index=True, use_container_width=True, height=400)
    else:
        st.caption("No closed trades yet.")


# =========================================================================
# TAB 3 — AI Learning
# =========================================================================
with tab_learning:
    st.markdown("## 🧠 Self-Learning Engine")

    perf = get_strategy_performance_report()

    s = perf["summary"]
    a, b, c, d = st.columns(4)
    a.metric("Closed Trades", s["total_trades"])
    b.metric("Win Rate", f"{s['win_rate']}%")
    c.metric("Total P&L", f"RM {s['total_pnl_rm']:,.0f}")
    d.metric("Avg Win / Loss",
             f"{s['avg_win_rm']:.0f} / {s['avg_loss_rm']:.0f}")

    st.markdown("### Strategy & Sector Performance")
    cols = st.columns(2)
    if perf["by_strategy"]:
        cols[0].markdown("**By strategy**")
        cols[0].dataframe(pd.DataFrame.from_dict(
            perf["by_strategy"], orient="index"), use_container_width=True)
    if perf["by_sector"]:
        cols[1].markdown("**By sector**")
        cols[1].dataframe(pd.DataFrame.from_dict(
            perf["by_sector"], orient="index"), use_container_width=True)

    st.markdown("### Bayesian State Priors (top 20 by sample size)")
    from db import connect as _conn
    with _conn(readonly=True) as cn:
        rows = cn.execute(
            "SELECT state_id, action, alpha, beta, n_trades, total_r, last_updated "
            "FROM state_priors ORDER BY n_trades DESC LIMIT 20"
        ).fetchall()
    if rows:
        pri_df = pd.DataFrame([dict(r) for r in rows])
        pri_df["posterior_mean"] = (
            pri_df["alpha"] / (pri_df["alpha"] + pri_df["beta"])).round(3)
        pri_df["avg_r"] = (pri_df["total_r"] / pri_df["n_trades"].clip(lower=1)).round(2)
        st.dataframe(pri_df, hide_index=True, use_container_width=True)
    else:
        st.info("No state priors yet — close some trades to populate the brain.")

    st.markdown("### ML Setup Classifier")
    meta = get_classifier_meta()
    if meta:
        cc = st.columns(3)
        cc[0].metric("OOS Accuracy", f"{meta.get('holdout_accuracy', 0):.3f}")
        cc[1].metric("Train Accuracy", f"{meta.get('train_accuracy', 0):.3f}")
        cc[2].metric("Trained At", meta.get("trained_at", "—"))
        if meta.get("importance"):
            imp = pd.DataFrame({"feature": list(meta["importance"].keys()),
                                "importance": list(meta["importance"].values())})
            fig = px.bar(imp.sort_values("importance"), x="importance",
                         y="feature", orientation="h",
                         color="importance", color_continuous_scale="Blues")
            fig.update_layout(**PLOTLY_LAYOUT, height=350)
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("Classifier not trained yet.")

    cc1, cc2 = st.columns(2)
    if cc1.button("🔁 Re-train ML Classifier", use_container_width=True):
        with st.spinner("Training (TimeSeriesSplit + Isotonic calibration)…"):
            res = train_setup_classifier()
        st.success(f"Done. OOS accuracy: {res[1]:.3f}" if res
                   else "Training failed (insufficient data).")
        st.rerun()
    if cc2.button("📏 Run Walk-Forward Optimization", use_container_width=True):
        with st.spinner("Running walk-forward (train ≠ test)…"):
            prog = st.progress(0.0)
            best, wr, pf = run_walk_forward_optimization(
                progress_callback=prog.progress)
            prog.empty()
        if best is None:
            st.warning("Rejected: not enough OOS trades for any grid.")
        else:
            st.success(f"Selected params (OOS WR={wr*100:.1f}% PF={pf:.2f}).")
            st.json(best)

    st.markdown("### Learning Journal")
    events = get_learning_history()
    if events:
        ev_df = pd.DataFrame(events)[
            ["timestamp", "event_type", "description"]
        ].head(40)
        st.dataframe(ev_df, hide_index=True, use_container_width=True)
    else:
        st.caption("No learning events yet.")


# =========================================================================
# TAB 4 — Performance (Sharpe, drawdown, calibration, benchmarks)
# =========================================================================
with tab_perf:
    st.markdown("## 📊 Performance & Evaluation")
    with st.spinner("Computing metrics…"):
        report = full_evaluation_report()

    s = report["summary"]; r = report["risk"]
    e = report["expectancy"]; mm = report["mae_mfe"]

    cc = st.columns(4)
    cc[0].metric("Total Return",
                 f"{s['total_return_pct']:+.2f}%",
                 f"RM {s['current_equity']:,.0f}")
    cc[1].metric("Sharpe", r["sharpe"])
    cc[2].metric("Sortino", r["sortino"])
    cc[3].metric("Max Drawdown", f"{r['max_dd_pct']:.2f}%",
                 f"{r['max_dd_duration_days']}d duration")

    cc2 = st.columns(4)
    cc2[0].metric("Profit Factor",
                  f"{e['profit_factor']}" if e["profit_factor"] else "∞")
    cc2[1].metric("Expectancy", f"RM {e['expectancy_rm']:.2f}",
                  f"R {e['expectancy_r']:+.2f}")
    cc2[2].metric("Avg MAE / MFE",
                  f"{mm['avg_mae_pct']:+.2f}% / {mm['avg_mfe_pct']:+.2f}%")
    cc2[3].metric("Wins / Losses",
                  f"{e['n_wins']} / {e['n_losses']}")

    eq = report["equity_curve"]
    if not eq.empty:
        st.markdown("### Equity Curve vs Benchmarks")
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=eq["date"], y=eq["equity"],
            mode="lines", line=dict(color="#1f6feb", width=2.5),
            name="BursaAI"))
        klci = report["klci_benchmark"]
        if not klci.empty:
            fig.add_trace(go.Scatter(x=klci.index, y=klci.values,
                                      mode="lines",
                                      line=dict(color="#d97706", width=1.5,
                                                dash="dash"),
                                      name="KLCI buy & hold"))
        eqw = report["equal_weight_benchmark"]
        if not eqw.empty:
            fig.add_trace(go.Scatter(x=eqw.index, y=eqw.values,
                                      mode="lines",
                                      line=dict(color="#16a34a", width=1.5,
                                                dash="dot"),
                                      name="Equal-weight watchlist"))
        fig.update_layout(height=380, **PLOTLY_LAYOUT, hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)

    if report["calibration"]:
        st.markdown("### Calibration (predicted vs realized win rate)")
        cdf = pd.DataFrame(report["calibration"])
        fig = go.Figure()
        fig.add_trace(go.Bar(x=cdf["bucket"], y=cdf["realized_win_rate_pct"],
                              marker_color="#1f6feb", name="Realized win %"))
        fig.add_trace(go.Scatter(x=cdf["bucket"], y=cdf["predicted_pct"],
                                  marker_color="#dc2626", mode="lines+markers",
                                  name="Predicted (mid)"))
        fig.update_layout(height=320, **PLOTLY_LAYOUT,
                          yaxis_title="%", xaxis_title="Confidence bucket")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("If blue bars track the red line, the agent's confidence is well-calibrated.")
        st.dataframe(cdf, hide_index=True, use_container_width=True)

    if report["per_regime"]:
        st.markdown("### Per-Regime Performance")
        st.dataframe(pd.DataFrame.from_dict(report["per_regime"], orient="index"),
                     use_container_width=True)


# =========================================================================
# TAB 5 — Robo-Trader
# =========================================================================
with tab_robo:
    st.markdown("## 🤖 Robo-Trader Control Centre")
    ss = get_scheduler_state()
    running = sched.is_running()

    a, b, c = st.columns(3)
    a.metric("Status", "🟢 RUNNING" if running else "🔴 STOPPED")
    a.caption(f"Heartbeat: {ss.get('last_heartbeat') or '—'}")
    b.metric("Last Run", ss.get("last_run_at") or "never")
    b.caption(f"Failures (recent): {ss.get('consecutive_failures', 0)}")
    c.metric("Next Run", ss.get("next_run_at") or "—")
    c.caption(f"Interval: {ss.get('interval_sec', 3600)//60} min")

    if ss.get("last_error"):
        st.error(f"Last error: `{ss['last_error'][:400]}`")

    st.markdown("### Controls")
    cc = st.columns(4)
    if not running:
        if cc[0].button("▶️ Start", use_container_width=True, type="primary"):
            sched.start(int(ss.get("interval_sec", 3600)))
            st.success("Started."); st.rerun()
    else:
        if cc[0].button("🛑 Stop", use_container_width=True):
            sched.stop(); st.warning("Stopped."); st.rerun()
    if cc[1].button("♻️ Force Restart", use_container_width=True):
        sched.force_restart(int(ss.get("interval_sec", 3600)))
        st.success("Restarted."); st.rerun()
    if cc[2].button("⚡ Run Cycle Now", use_container_width=True):
        with st.spinner("Running cycle…"):
            res = sched.run_once()
        st.success(f"Done: {res}")
    if cc[3].button("🚨 Kill-Switch", use_container_width=True):
        update_scheduler_state(kill_switch=1)
        sched.stop()
        st.error("Kill-switch engaged. Scheduler will not restart "
                 "until cleared in Settings.")
        st.rerun()

    st.markdown("### Auto-Trading Toggles")
    cc2 = st.columns(3)
    cur_autoexit = bool(ss.get("autoexit_enabled", 1))
    cur_autotrade = bool(ss.get("autotrade_enabled", 1))   # v3: default ON
    new_autoexit = cc2[0].checkbox("Auto-settle SL/TP/Trailing/Time exits",
                                    cur_autoexit)
    new_autotrade = cc2[1].checkbox("Auto-execute new GOLD BUY entries",
                                     cur_autotrade)
    new_interval_min = cc2[2].selectbox(
        "Cycle interval",
        [15, 30, 60, 120],
        index=[15, 30, 60, 120].index(
            max(15, min(120, ss.get("interval_sec", 3600) // 60))) if
        ss.get("interval_sec", 3600) // 60 in [15, 30, 60, 120] else 2,
    )
    if (new_autoexit != cur_autoexit
            or new_autotrade != cur_autotrade
            or new_interval_min * 60 != ss.get("interval_sec", 3600)):
        if st.button("💾 Save Robo-Trader settings", type="primary"):
            update_scheduler_state(
                autoexit_enabled=int(new_autoexit),
                autotrade_enabled=int(new_autotrade),
                interval_sec=int(new_interval_min * 60),
            )
            sched.force_restart(int(new_interval_min * 60))
            st.success("Saved + restarted."); st.rerun()

    # ---- v3: Exploration-mode controls ----
    st.markdown("### 🧪 Learning Mode")
    from repository import closed_trades as _ct
    done = len(_ct())
    target = int(ss.get("exploration_trades_target", 50) or 50)
    in_explore = bool(ss.get("exploration_mode", 1))
    if in_explore:
        st.info(
            f"🔬 EXPLORE mode — Thompson sampling. "
            f"Closed trades: {done}/{target}. "
            "Agent tries new setups quickly to build the brain. "
            "Switches to EXPLOIT automatically when target is reached."
        )
        prog_val = min(done / max(target, 1), 1.0)
        st.progress(prog_val)
    else:
        st.success(
            f"🎯 EXPLOIT mode — using lower-confidence-bound. "
            f"Brain has learned from {done} trades."
        )
    cce = st.columns(3)
    new_target = cce[0].number_input(
        "Exploration trade target", min_value=10, max_value=500,
        value=target, step=10)
    if cce[1].button("Force EXPLORE", use_container_width=True):
        update_scheduler_state(exploration_mode=1,
                                exploration_trades_target=int(new_target))
        st.rerun()
    if cce[2].button("Force EXPLOIT", use_container_width=True):
        update_scheduler_state(exploration_mode=0)
        st.rerun()

    st.markdown("### Trading-Time Window")
    tw = check_trading_time_window()
    st.info(f"Window: {tw['window']} — {tw['reason']}")

    st.markdown("### Recent Scheduler Log (50)")
    sl = get_scheduler_log(limit=50)
    if sl:
        st.dataframe(pd.DataFrame(sl)[["timestamp", "level", "event",
                                       "message", "duration_sec"]],
                     hide_index=True, use_container_width=True, height=350)
    else:
        st.caption("No scheduler events yet.")


# =========================================================================
# TAB 6 — Logs
# =========================================================================
with tab_logs:
    st.markdown("## 📜 Complete Audit Log")

    sub = st.radio("Log type",
                   ["Trade executions", "Robo-Trader scheduler",
                    "Learning & parameter changes",
                    "Bias updates", "Data quality"],
                   horizontal=True)

    if sub == "Trade executions":
        c1, c2 = st.columns(2)
        actor = c1.selectbox("Actor", ["ALL", "USER", "AGENT"])
        evt = c2.selectbox("Event", ["ALL", "ENTRY_EXECUTED", "PARTIAL_EXIT",
                                      "FULL_EXIT", "TRAIL_SET",
                                      "RISK_REJECTED"])
        rows = get_trade_log(limit=500,
                              actor_filter=None if actor == "ALL" else actor,
                              event_filter=None if evt == "ALL" else evt)
        if rows:
            df = pd.DataFrame(rows)
            df["payload"] = df["payload_json"].apply(
                lambda x: json.loads(x) if x else {})
            df = df.drop(columns=["payload_json"])
            st.dataframe(df, hide_index=True, use_container_width=True,
                         height=500)
            st.download_button("⬇️ Download CSV",
                                df.to_csv(index=False).encode(),
                                file_name="trade_log.csv")
        else:
            st.caption("No trade events.")

    elif sub == "Robo-Trader scheduler":
        lvl = st.selectbox("Level", ["ALL", "INFO", "WARN", "ERROR"])
        rows = get_scheduler_log(limit=500,
                                  level=None if lvl == "ALL" else lvl)
        if rows:
            df = pd.DataFrame(rows)
            df["payload"] = df["payload_json"].apply(
                lambda x: json.loads(x) if x else {})
            df = df.drop(columns=["payload_json"])
            st.dataframe(df, hide_index=True, use_container_width=True,
                         height=500)
            st.download_button("⬇️ Download CSV",
                                df.to_csv(index=False).encode(),
                                file_name="scheduler_log.csv")
        else:
            st.caption("Scheduler hasn't logged anything yet.")

    elif sub == "Learning & parameter changes":
        st.markdown("**Learning events**")
        ev = get_learning_events(limit=300)
        if ev:
            df = pd.DataFrame(ev)
            df["changes"] = df["changes_json"].apply(
                lambda x: json.loads(x) if x else {})
            df["metrics"] = df["metrics_json"].apply(
                lambda x: json.loads(x) if x else {})
            df = df.drop(columns=["changes_json", "metrics_json"])
            st.dataframe(df, hide_index=True, use_container_width=True,
                         height=300)
            st.download_button("⬇️ Download CSV",
                                df.to_csv(index=False).encode(),
                                file_name="learning_events.csv")
        else:
            st.caption("No learning events.")
        st.markdown("**Parameter change history**")
        ph = get_parameter_history(limit=200)
        if ph:
            df = pd.DataFrame(ph)
            df["before"] = df["before_json"].apply(
                lambda x: json.loads(x) if x else {})
            df["after"] = df["after_json"].apply(
                lambda x: json.loads(x) if x else {})
            df = df.drop(columns=["before_json", "after_json"])
            st.dataframe(df, hide_index=True, use_container_width=True,
                         height=300)
        else:
            st.caption("No parameter changes yet.")

    elif sub == "Bias updates":
        bh = get_bias_history(limit=400)
        if bh:
            st.dataframe(pd.DataFrame(bh), hide_index=True,
                         use_container_width=True, height=500)
        else:
            st.caption("No bias updates yet.")

    elif sub == "Data quality":
        dq = get_data_quality_log(limit=400)
        if dq:
            df = pd.DataFrame(dq)
            df["detail"] = df["detail_json"].apply(
                lambda x: json.loads(x) if x else {})
            df = df.drop(columns=["detail_json"])
            st.dataframe(df, hide_index=True, use_container_width=True,
                         height=500)
        else:
            st.caption("✅ No data quality issues recorded.")


# =========================================================================
# TAB — Live Alerts (v3.1)
# =========================================================================
with tab_alerts:
    st.markdown("## 🔔 Live Alerts — Trigger your real trades from agent signals")
    st.caption(
        "When ON, every paper trade the agent makes will push a notification "
        "to Telegram / Email so you can mirror the action in your broker."
    )

    from live_trigger import (load_config as _lt_load,
                               save_config as _lt_save,
                               send_test_alert as _lt_test,
                               recent_alerts as _lt_recent)
    from notifier import (telegram_configured, email_configured)

    cfg = _lt_load()

    # ---- Status badges ----
    cc = st.columns(4)
    cc[0].metric("Master switch",
                 "🟢 ON" if cfg["enabled"] else "🔴 OFF")
    cc[1].metric("Telegram",
                 "✅ configured" if telegram_configured() else "❌ not set")
    cc[2].metric("Email",
                 "✅ configured" if email_configured() else "❌ not set")
    last_24h = len([a for a in _lt_recent(500)
                    if a.get("status") == "SENT"])
    cc[3].metric("Alerts sent (24h)", last_24h)

    # ---- Main toggles ----
    st.markdown("### Main settings")
    c1, c2, c3 = st.columns(3)
    new_enabled = c1.toggle("🔔 Enable live alerts",
                             value=bool(cfg["enabled"]))
    new_min_conf = c2.slider("Minimum confidence",
                              min_value=50.0, max_value=95.0, step=1.0,
                              value=float(cfg["min_confidence"]))
    new_exploit_only = c3.toggle("Only when brain is in EXPLOIT mode",
                                   value=bool(cfg["exploit_mode_only"]))

    st.markdown("### Which events should alert?")
    e1, e2, e3, e4 = st.columns(4)
    new_entry = e1.checkbox("ENTRY", value=bool(cfg["alert_on_entry"]))
    new_full = e2.checkbox("FULL EXIT", value=bool(cfg["alert_on_full_exit"]))
    new_sl = e3.checkbox("STOP LOSS", value=bool(cfg["alert_on_stop_loss"]))
    new_trail = e4.checkbox("TRAILING STOP",
                             value=bool(cfg["alert_on_trailing_stop"]))
    e5, e6 = st.columns(2)
    new_partial = e5.checkbox("Partial exits (TP2 50%)",
                               value=bool(cfg["alert_on_partial_exit"]))
    new_rej = e6.checkbox("Risk-rejected entries",
                           value=bool(cfg["alert_on_risk_rejected"]))

    st.markdown("### Channels")
    chcol = st.columns(3)
    new_tg = chcol[0].checkbox("Send to Telegram",
                                value=bool(cfg["telegram_enabled"]),
                                disabled=not telegram_configured())
    if not telegram_configured():
        chcol[0].caption("Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars")
    new_em = chcol[1].checkbox("Send to Email",
                                value=bool(cfg["email_enabled"]),
                                disabled=not email_configured())
    if not email_configured():
        chcol[1].caption("Set ALERT_SMTP_* env vars")
    new_emails = chcol[2].text_input("Email recipients (comma-separated)",
                                       value=cfg["email_recipients"])

    actor_choice = st.radio(
        "Trigger on which trades?",
        ["AGENT only (recommended)", "AGENT + manual clicks"],
        index=(0 if (cfg.get("actor_filter") or "AGENT") == "AGENT" else 1),
        horizontal=True)
    new_actor = "AGENT" if actor_choice.startswith("AGENT only") else "BOTH"

    ssave, stest = st.columns(2)
    if ssave.button("💾 Save settings", type="primary",
                     use_container_width=True):
        _lt_save({
            "enabled": new_enabled, "min_confidence": new_min_conf,
            "exploit_mode_only": new_exploit_only,
            "alert_on_entry": new_entry, "alert_on_full_exit": new_full,
            "alert_on_stop_loss": new_sl, "alert_on_trailing_stop": new_trail,
            "alert_on_partial_exit": new_partial,
            "alert_on_risk_rejected": new_rej,
            "telegram_enabled": new_tg, "email_enabled": new_em,
            "email_recipients": new_emails, "actor_filter": new_actor,
        })
        st.success("Saved.")
        st.rerun()
    if stest.button("🧪 Send test alert", use_container_width=True):
        with st.spinner("Sending…"):
            res = _lt_test()
        ok_chans = [ch for ch, (ok, _) in res.items() if ok]
        bad_chans = [(ch, err) for ch, (ok, err) in res.items() if not ok]
        if ok_chans:
            st.success(f"Test sent OK to: {', '.join(ok_chans)}")
        for ch, err in bad_chans:
            st.error(f"{ch} failed: {err}")

    # ---- Broker connection (stub) ----
    st.markdown("### Broker Connection (v4 placeholder)")
    st.info(
        "Currently NOTIFICATION-ONLY mode. The Moomoo broker adapter is "
        "stubbed and ready — to enable direct order placement later, "
        "install `moomoo-api`, set MOOMOO_HOST / MOOMOO_PORT env vars, "
        "and implement the TODO methods in `broker_adapter.py`."
    )

    # ---- Alert log ----
    st.markdown("### Recent alerts (last 100)")
    alerts = _lt_recent(100)
    if alerts:
        df_a = pd.DataFrame(alerts)
        df_a = df_a[["timestamp", "event_type", "ticker", "channel",
                     "status", "message", "error"]]
        st.dataframe(df_a, hide_index=True, use_container_width=True,
                     height=400)
        st.download_button(
            "⬇️ Download alert log CSV",
            df_a.to_csv(index=False).encode(),
            file_name="alert_log.csv",
        )
    else:
        st.caption("No alerts yet. They'll appear here once the trigger fires.")


# =========================================================================
# TAB 7 — Settings
# =========================================================================
with tab_settings:
    st.markdown("## ⚙️ Settings")

    st.markdown("### Scanner Parameters")
    p = load_parameters()
    c1, c2, c3 = st.columns(3)
    ema_trend = c1.number_input("EMA Trend",
                                 value=int(p.get("ema_trend", 200)),
                                 step=10)
    ema_fast = c2.number_input("EMA Fast",
                                value=int(p.get("ema_fast", 10)), step=1)
    ema_slow = c3.number_input("EMA Slow",
                                value=int(p.get("ema_slow", 20)), step=1)
    rsi_pb = c1.number_input("RSI Pullback ≤",
                              value=float(p.get("rsi_oversold_pullback", 40.0)),
                              step=1.0)
    rsi_ov = c2.number_input("RSI Overbought ≥",
                              value=float(p.get("rsi_overbought", 70.0)),
                              step=1.0)
    vsr = c3.number_input("Volume Surge ×",
                           value=float(p.get("volume_surge_ratio", 1.5)),
                           step=0.1)
    atr_mult = c1.number_input("ATR Stop Multiplier",
                                value=float(p.get("atr_multiplier_stop", 1.5)),
                                step=0.1)
    min_price = c2.number_input("Min Price (RM)",
                                 value=float(p.get("min_price", 0.30)),
                                 step=0.05)
    max_price = c3.number_input("Max Price (RM)",
                                 value=float(p.get("max_price", 4.00)),
                                 step=0.5)
    shariah_only = st.checkbox(
        "🕌 Shariah-compliant only (filter out conventional banks, brewers, gaming)",
        value=bool(p.get("shariah_only", False)),
    )
    if st.button("💾 Save Scanner Parameters", type="primary"):
        new = {**p, "ema_trend": int(ema_trend), "ema_fast": int(ema_fast),
               "ema_slow": int(ema_slow),
               "rsi_oversold_pullback": float(rsi_pb),
               "rsi_overbought": float(rsi_ov),
               "volume_surge_ratio": float(vsr),
               "atr_multiplier_stop": float(atr_mult),
               "min_price": float(min_price), "max_price": float(max_price),
               "shariah_only": bool(shariah_only)}
        save_parameters(new, source="USER", reason="Settings panel edit")
        st.success("Saved (logged to parameter_history)."); st.rerun()

    st.markdown("### Risk Parameters")
    rp = load_risk_params()
    c1, c2, c3 = st.columns(3)
    max_dd = c1.number_input("Max DD warn %",
                              value=float(rp["max_drawdown_pct"]), step=1.0)
    max_dd_strict = c2.number_input("Max DD strict %",
                                     value=float(rp["max_drawdown_strict_pct"]),
                                     step=1.0)
    max_pos = c3.number_input("Max concurrent positions",
                               value=int(rp["max_concurrent_positions"]),
                               step=1)
    max_risk_pct = c1.number_input("Max risk / trade %",
                                    value=float(rp["max_risk_per_trade_pct"]),
                                    step=0.25)
    max_pos_pct = c2.number_input("Max position cost %",
                                   value=float(rp["max_position_cost_pct"]),
                                   step=1.0)
    max_sec_pct = c3.number_input("Max sector exposure %",
                                   value=float(rp["max_sector_exposure_pct"]),
                                   step=5.0)
    daily_cap = c1.number_input("Daily trade limit",
                                 value=int(rp["max_trades_per_day"]), step=1)
    if st.button("💾 Save Risk Parameters", type="primary"):
        new_rp = {**rp,
                  "max_drawdown_pct": float(max_dd),
                  "max_drawdown_strict_pct": float(max_dd_strict),
                  "max_concurrent_positions": int(max_pos),
                  "max_risk_per_trade_pct": float(max_risk_pct),
                  "max_position_cost_pct": float(max_pos_pct),
                  "max_sector_exposure_pct": float(max_sec_pct),
                  "max_trades_per_day": int(daily_cap)}
        save_risk_params(new_rp)
        st.success("Saved."); st.rerun()

    st.markdown("### Custom Watchlist")
    custom = load_custom_watchlist_tickers()
    if custom:
        st.dataframe(pd.DataFrame.from_dict(custom, orient="index"),
                     use_container_width=True)
        rm = st.selectbox("Remove ticker", [""] + list(custom.keys()))
        if rm and st.button(f"Remove {rm}"):
            remove_custom_ticker(rm)
            st.rerun()
    else:
        st.caption("No custom tickers — add from the sidebar.")

    st.markdown("### Kill-Switch")
    ss = get_scheduler_state()
    if ss.get("kill_switch"):
        st.error("🚨 Kill-switch is ENGAGED — scheduler will not run.")
        if st.button("✅ Clear kill-switch"):
            update_scheduler_state(kill_switch=0)
            st.success("Cleared."); st.rerun()
    else:
        st.success("Kill-switch is clear.")

    st.markdown("### Reset Capital / Trades")
    with st.expander("⚠️ Destructive actions"):
        new_cap = st.number_input("Reset capital to",
                                   value=float(acc["initial_capital"]),
                                   min_value=1000.0, step=1000.0)
        if st.button("Reset capital (does NOT delete trades)"):
            reset_account(new_cap); st.success("Reset."); st.rerun()
        if st.button("⛔ Delete all trades + scan cache"):
            from db import connect as _c
            with _c() as cc:
                cc.execute("DELETE FROM trades")
                cc.execute("DELETE FROM partial_exits")
                cc.execute("DELETE FROM scan_cache")
            reset_account(new_cap)
            st.warning("All trades cleared."); st.rerun()
