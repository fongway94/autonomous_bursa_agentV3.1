# BursaAI Swing Agent v3 — User Guide

A quick-reference manual for running the autonomous Bursa Malaysia paper-trading agent.

> **Important:** This is paper trading only. The agent does **not** place real broker orders. P&L is simulated using realistic Bursa fees and slippage. Always validate with your own research before risking real money.

---

## Table of Contents

1. [Installation](#1-installation)
2. [First Launch (60 seconds)](#2-first-launch-60-seconds)
3. [What the Robo-Trader Does by Default](#3-what-the-robo-trader-does-by-default)
4. [How the Self-Learning Works](#4-how-the-self-learning-works)
5. [Daily Workflow](#5-daily-workflow)
6. [Key Controls — Where to Find Everything](#6-key-controls--where-to-find-everything)
7. [Common Operations (Step-by-Step)](#7-common-operations-step-by-step)
8. [Important Notes & Cautions](#8-important-notes--cautions)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Installation

```bash
git clone <your-repo-url>
cd bursa_comprehensive_ai_v3

# Virtual environment (recommended)
python -m venv venv
source venv/bin/activate         # macOS / Linux
# venv\Scripts\activate          # Windows

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Verify everything works (optional but recommended)
pytest tests/ -q
# Should print: 47 passed
```

**Prerequisites:** Python 3.9+, internet access for yfinance.

---

## 2. First Launch (60 seconds)

```bash
streamlit run app.py
```

Browser opens at `http://localhost:8501`.

**The Robo-Trader starts itself the moment the page loads.** No buttons to click.

What happens in the next 60 seconds:

| Time | What you see |
|---|---|
| 0s | Light-themed dashboard loads, 7 tabs visible at top |
| 1s | Sidebar shows `🤖 Robo-Trader 🟢 RUNNING`, `Brain mode 🔬 EXPLORE` |
| 2s | First HEARTBEAT logged in 📜 Logs → Robo-Trader scheduler |
| up to 1h | Next scan runs at the next top-of-hour (during market hours) |

You can also click **🤖 Robo-Trader tab → ⚡ Run Cycle Now** to fire an immediate scan + auto-trade cycle without waiting.

---

## 3. What the Robo-Trader Does by Default

**Out of the box (v3 defaults):**

| Setting | Default | Effect |
|---|---|---|
| **Auto-exit** | ✅ ON | Closes positions at SL / TP / trailing stop / time exit automatically |
| **Auto-entry** | ✅ ON | **Opens new positions automatically** on high-confidence signals |
| **Cycle interval** | 60 minutes | Runs hourly during market hours |
| **Brain mode** | 🔬 EXPLORE | Thompson sampling for the first 50 closed trades |
| **Max risk per trade** | 1.0% of capital | Conservative for unsupervised trading |
| **Trading window** | 09:15–17:00 MYT | Bursa hours (no entries after 16:00) |
| **Kill-switch** | Off | One-click emergency stop available |

### The full cycle (every hour during market hours)

```
1. HEARTBEAT logged
2. Fetch fresh KLCI regime → BULL / NEUTRAL / BEAR
3. Scan all ~80 Bursa stocks (parallel)
4. Cache results in scan_cache
5. Auto-exit: settle TPs / SLs / trailing / time exits on active trades
6. Auto-entry: open new GOLD BUYs above the regime-adjusted confidence threshold
7. Learn: every closed trade updates the Bayesian brain
8. Sleep until next top-of-hour
```

You can watch each step happen in real time in the **📜 Logs → Robo-Trader scheduler** tab.

---

## 4. How the Self-Learning Works

The agent learns from scratch using a two-phase Bayesian approach:

### Phase 1 — EXPLORATION 🔬 (first 50 closed trades, default)

- Every setup is bucketed into one of 250 states (RSI × volume × trend × MACD).
- For each state, the agent samples one win-rate guess from its Beta(α,β) posterior — **Thompson sampling**.
- This makes the agent **try a wide variety of setups** quickly so the brain learns fast.
- The brain mode badge in the sidebar shows **🔬 EXPLORE** + a progress bar.

### Phase 2 — EXPLOITATION 🎯 (automatic after 50 closed trades)

- The agent switches to using the **lower confidence bound** of each posterior — conservative.
- Only setups with statistical evidence of working get acted on.
- The badge changes to **🎯 EXPLOIT**.

You can manually force either mode from **🤖 Robo-Trader → 🧪 Learning Mode**.

### What every closed trade teaches the brain

Each WIN / LOSS updates **three** things simultaneously:

1. **The state's Beta posterior** — `α += 1+win_weight` on win, `β += 1+loss_weight` on loss
2. **The strategy bias** (breakout_bias / pullback_bias) — shrinks toward 1.0 with Beta(5,5) prior
3. **The sector bias** — same shrinkage, per sector

All changes are visible in:
- **🧠 AI Learning** tab → Bayesian State Priors table
- **📜 Logs** → Bias updates
- **📜 Logs** → Learning & parameter changes

---

## 5. Daily Workflow

The agent is fully autonomous. A typical day:

| Time (MYT) | What happens | What you do |
|---|---|---|
| Pre-market | Robo-Trader sleeps, last_run shows yesterday | Open dashboard, glance at Performance |
| 09:00 | Bursa opens | (nothing) |
| 09:15 | First scheduled cycle | Watch Logs to confirm cycle ran |
| 10:00–15:00 | Hourly cycles, auto-entries and exits | Optional: review trades in 💼 Portfolio |
| 16:00+ | No new entries (robo-only window), but exits still run | (nothing) |
| 17:00 | Bursa closes, scheduler skips cycles | Check 📊 Performance for the day |
| 01:00 | Nightly maintenance — ML retrain + log prune | (nothing) |

**You only need to touch the dashboard for:**
- Reviewing performance (📊 Performance tab)
- Manually closing a position you disagree with (💼 Portfolio tab)
- Adjusting risk (⚙️ Settings tab)
- Emergency stop (🚨 Kill-Switch in 🤖 Robo-Trader tab)

---

## 6. Key Controls — Where to Find Everything

### Sidebar (always visible)

| Control | What it does |
|---|---|
| **Initial Capital (RM)** | Starting paper-trade balance |
| **Risk per Trade (%)** | UI hint only (real cap lives in Risk Parameters) |
| **🤖 Robo-Trader badge** | Status + last/next run + brain mode |
| **Start/Stop button** | Toggle the daemon thread |
| **➕ Add Custom Stock** | Append a ticker outside the curated 80 |

### 🔍 Scanner tab

- **🔥 SCAN MARKET** button — manual scan override (auto runs hourly anyway)
- Filter by signal type
- Click any ticker → chart, 5-day tape, BUY/SELL execute panel

### 💼 Portfolio tab

- Active positions table with live P&L and MAE/MFE
- Sector exposure heatmap
- Manual close (WIN / LOSS / partial)
- Closed trades history

### 🧠 AI Learning tab

- Strategy + sector performance breakdown
- Bayesian state priors (top 20 by sample size)
- ML classifier feature importance + OOS accuracy
- 🔁 **Re-train ML Classifier** button (also runs nightly)
- 📏 **Run Walk-Forward Optimization** button
- Learning journal (last 40 events)

### 📊 Performance tab

- Sharpe / Sortino / Max Drawdown / Profit Factor / Expectancy (R)
- Equity curve vs KLCI + Equal-weight benchmarks
- **Calibration chart** — "do my 80% confidence picks actually win 80%?"
- Per-regime hit rate

### 🤖 Robo-Trader tab

| Control | Action |
|---|---|
| **▶ Start / 🛑 Stop** | Daemon thread on/off |
| **♻ Force Restart** | Stop + start (clears state) |
| **⚡ Run Cycle Now** | Trigger one immediate scan + settle + auto-entry |
| **🚨 Kill-Switch** | Emergency stop; will not restart until cleared |
| **Auto-exit checkbox** | Toggle auto exits |
| **Auto-entry checkbox** | Toggle auto entries |
| **Cycle interval** | 15 / 30 / 60 / 120 min |
| **🧪 Learning Mode** | Force EXPLORE or EXPLOIT |
| Recent Scheduler Log | Last 50 cycle events |

### 📜 Logs tab

Five separate streams, all CSV-downloadable:
- Trade executions (filter by USER vs AGENT)
- Robo-Trader scheduler (filter by INFO / WARN / ERROR)
- Learning & parameter changes
- Bias updates
- Data quality issues

### ⚙️ Settings tab

| Section | Contains |
|---|---|
| **Scanner Parameters** | EMA periods, RSI thresholds, volume ratio, ATR multiplier, price range, **🕌 Shariah-compliant only toggle** |
| **Risk Parameters** | Max DD %, max positions, **`max_risk_per_trade_pct`**, position cap, sector cap, daily trade limit |
| **Custom Watchlist** | Remove user-added tickers |
| **Kill-Switch** | Clear if engaged |
| **Reset Capital / Trades** | Destructive ops (delete all trades, reset capital) |

---

## 7. Common Operations (Step-by-Step)

### A. Turn auto-entry ON (it's already ON by default in v3)

If somehow it's off:

1. Click **🤖 Robo-Trader** tab
2. Scroll to **Auto-Trading Toggles**
3. Check ☑ **Auto-execute new GOLD BUY entries**
4. Click **💾 Save Robo-Trader settings**
5. The scheduler force-restarts automatically; sidebar badge updates to **Auto-entry: ON**

### B. Turn auto-entry OFF (manual approval mode)

1. **🤖 Robo-Trader** tab → uncheck **Auto-execute new GOLD BUY entries**
2. Save
3. Now the agent will only auto-exit; you must manually click EXECUTE on each new entry in the Scanner tab

### C. Adjust `max_risk_per_trade_pct`

1. Click **⚙️ Settings** tab
2. Scroll to **Risk Parameters**
3. Change the **Max risk / trade %** number input
4. Click **💾 Save Risk Parameters**

| Value | Behaviour |
|---|---|
| 0.5 | Very conservative — recommended for the first 30 days |
| **1.0** | **Default v3** — balanced |
| 2.0 | Aggressive — only use after you've validated calibration on 50+ trades |
| 3.0 | Maximum allowed — not recommended |

The change is logged in **📜 Logs → Learning & parameter changes** so you can audit later.

### D. Emergency stop everything

1. **🤖 Robo-Trader** tab
2. Click **🚨 Kill-Switch** (red)
3. Loop exits within 60 seconds; will NOT auto-restart
4. To re-enable: **⚙️ Settings → Kill-Switch section → Clear**

### E. Reset the agent and restart learning from scratch

1. **⚙️ Settings → ⚠️ Destructive actions** expander
2. Click **⛔ Delete all trades + scan cache**
3. State priors persist (learning is preserved). To also wipe the brain:
   - Stop the app
   - Delete `~/.bursa_agent_data/bursa_agent.db`
   - Restart the app — fresh install, exploration mode resets

### F. Switch to Shariah-only universe

1. **⚙️ Settings → Scanner Parameters**
2. Check ☑ **🕌 Shariah-compliant only**
3. Save
4. Next scan will exclude conventional banks, brewers, gaming, etc.

### G. View what the agent learned

1. **🧠 AI Learning** tab
2. Scroll to **Bayesian State Priors (top 20 by sample size)**
3. Read the table:
   - `posterior_mean` — agent's estimated win rate for that state+action
   - `n_trades` — sample size (trust grows with this)
   - `avg_r` — average R-multiple per trade
4. Also see **By strategy** and **By sector** tables above for aggregated stats

### H. Speed up learning (more frequent cycles)

Default is 60 min. You can shorten it:

1. **🤖 Robo-Trader → Auto-Trading Toggles → Cycle interval**
2. Pick 15 min for fastest cycles (more yfinance load)
3. Save → scheduler restarts at new cadence

**Caveat:** yfinance updates daily bars near end-of-day. Sub-hourly scans during the day don't see new price data, only new positions you opened.

### I. Run the agent 24/7 without keeping the browser open

```bash
# Headless mode — no UI, just the loop
python -m scheduler --interval 3600
```

Combine with `nohup` / `systemd` / Docker / Windows Task Scheduler for true unattended operation.

---

## 8. Important Notes & Cautions

### ⚠️ The agent will lose money during early exploration

In EXPLORE mode (first 50 trades), the agent intentionally tries setups it has no evidence about. Expect some losers in the first weeks. **This is by design** — losses are the training data.

To minimise the dollar cost of this learning:
- Start with **small capital** (RM 5,000–10,000 paper) for the first 50 trades
- Keep `max_risk_per_trade_pct` at **0.5–1.0%**
- Watch the brain progress in **🧠 AI Learning → Bayesian State Priors**

### ⚠️ "Sleep" risk on Streamlit Cloud

Streamlit Cloud puts apps to sleep after 7 days of inactivity. While sleeping, the scheduler thread dies. v3 has **self-healing**: when you re-open the app, it detects the stale heartbeat (>2× interval) and force-restarts the loop. But trades that should have closed during sleep won't have closed until reactivation. For 24/7 reliability, run headless via cron / systemd / Docker.

### ⚠️ yfinance data is end-of-day

KLCI intraday data is delayed and unreliable. The agent treats every cycle as "evaluate based on latest daily close." Even at hourly intervals, the price the agent sees during the day is yesterday's close (or today's last bar after market close). This is fine for swing trading — but don't expect the agent to react to mid-day spikes.

### ⚠️ Auto-entry caps and safeguards

Even with auto-entry ON, **every trade still goes through `run_full_risk_check`** which can:
- Reject if drawdown >8%
- Reject if you're at max concurrent positions
- Cap size if cost > 20% of capital
- Cap size if sector exposure > 40%
- Reject if daily trade limit (5) is reached
- Reject if outside trading hours

You cannot accidentally blow up your paper portfolio in one cycle.

### ⚠️ Real trading is NOT enabled

This system places **paper trades only**. To go live, you must edit `trading_engine.py` and integrate a broker API (Interactive Brokers recommended for KLSE coverage). Do not attempt without thorough understanding.

### ⚠️ Learning ≠ profit guarantee

Bayesian updates are statistical — they reflect *what worked in your specific run*. Past performance does not predict future returns. Always validate Sharpe > 1.0 and Profit Factor > 1.5 over 50+ trades **before trusting auto-entry with bigger capital**.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| Scheduler shows STOPPED after reload | Click ♻️ Force Restart in 🤖 Robo-Trader tab |
| Kill-switch stuck on | Settings → Clear kill-switch |
| No GOLD BUY signals appearing | Market might be in BEAR regime (signal threshold rises to 80%). Check regime banner in 🔍 Scanner tab |
| Auto-entries not happening despite signals | Check: market hours? After 16:00 MYT? At max positions? Daily limit hit? See 📜 Logs → Robo-Trader for the exact rejection reason |
| `ModuleNotFoundError: scipy` | `pip install scipy>=1.10` |
| Database file locked | Close other Streamlit instances; SQLite WAL handles concurrency but only within one process |
| Heartbeat shows hours ago | Self-heal triggers on next page refresh. Or click ♻️ Force Restart. |
| Tests fail in CI | Make sure `HOME` env var is writable (`conftest.py` uses it for tmp DB) |
| Want to delete everything | `rm -rf ~/.bursa_agent_data/` then restart |

---

## Quick Reference Card

```
START:          streamlit run app.py
HEADLESS:       python -m scheduler --interval 3600
TESTS:          pytest tests/ -q

DB LOCATION:    ~/.bursa_agent_data/bursa_agent.db
LOG FILE:       ~/.bursa_agent_data/logs/bursa_agent.log

KEY KNOBS:
  Auto-entry         → 🤖 Robo-Trader tab → Auto-Trading Toggles
  Risk per trade %   → ⚙️ Settings tab → Risk Parameters
  Cycle interval     → 🤖 Robo-Trader tab → Auto-Trading Toggles
  Shariah filter     → ⚙️ Settings tab → Scanner Parameters
  Brain mode         → 🤖 Robo-Trader tab → 🧪 Learning Mode
  Kill-Switch        → 🤖 Robo-Trader tab → bottom of Controls

SAFETY DEFAULTS (v3):
  Auto-entry         ON
  Auto-exit          ON
  Risk per trade     1.0 %
  Max positions      8 (3 in BEAR)
  Max drawdown warn  8 %
  Hard DD stop       15 %
  Trading window     09:15–17:00 MYT
  No entries after   16:00 MYT
```

---

**Happy trading. Start small. Trust the data, not the hope.** 🚀
