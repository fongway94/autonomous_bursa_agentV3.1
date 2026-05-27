# BursaAI Swing Agent — v2 Setup Guide

**Light-themed · Autonomous · Self-learning · Audited**

Python 3.9+ · Windows / macOS / Linux / Streamlit Cloud

---

## What's new in v2

| Area | v1 (the original) | v2 (this build) |
|---|---|---|
| Autonomy | Scheduler was orphaned — agent never ran by itself | Background thread auto-starts on app launch, hourly cycle in market hours |
| Learning | "Q-learning" was a single-step EMA of reward | Bayesian per-state Beta posteriors + bias shrinkage |
| Walk-forward | Train slice computed but never used → curve-fit | Proper train/test split, rejects if <30 OOS trades |
| ML classifier | Reported training accuracy | TimeSeriesSplit + isotonic calibration + sealed holdout |
| Storage | Scattered JSON files, file-lock races | Single SQLite DB (`bursa_agent.db`) with WAL |
| Logging | Print statements + truncated slices | trade_log, scheduler_log, learning_events, parameter_history, bias_history, data_quality_log — all queryable |
| Risk checks | size_multiplier computed but ignored | Engines actually apply size_multiplier |
| Execution | Accepted 137-share orders | 100-share board-lot enforcement |
| Execution realism | 0.15% flat fee, no slippage | 0.15% fee + size-dependent slippage |
| Evaluation | Win-rate + total P&L | + Sharpe, Sortino, max DD + duration, profit factor, expectancy in R, MAE/MFE, calibration, per-regime, KLCI + equal-weight benchmark |
| Data quality | None | Per-fetch validator → `data_quality_log` |
| Theme | Dark | Forced light theme (config + CSS) |
| Tests | 0 | 36 unit + integration tests |

---

## Prerequisites

```bash
python --version       # 3.9 or higher
pip --version
```

---

## Installation

```bash
# 1. Clone the repo
git clone <your-fork-url>
cd bursa_comprehensive_ai_v2

# 2. Virtual environment
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

# 3. Install
pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt`:
- streamlit, yfinance, pandas, numpy
- plotly, scikit-learn, **scipy** (Beta posterior), joblib
- pytest

---

## Run the dashboard

```bash
streamlit run app.py
```

Open http://localhost:8501. The Robo-Trader starts automatically.

A persistent SQLite DB is created at `~/.bursa_agent_data/bursa_agent.db`.
Rotating text logs at `~/.bursa_agent_data/logs/bursa_agent.log`.

---

## Run the test suite

```bash
pytest tests/ -v
```

All 36 tests should pass in ~2 seconds. The suite uses an isolated tmp
directory and never touches your real DB.

---

## Run the agent without Streamlit (cron / Task Scheduler / Docker)

```bash
python -m scheduler --interval 3600
```

This boots the same loop without launching the UI. Pair with cron/systemd
for "true" 24/7 autonomy.

---

## Tabs at a glance

| Tab | What it does |
|---|---|
| 🔍 **Scanner** | Live market scan; click a row to see chart + 5-day tape + execute |
| 💼 **Portfolio** | Active + closed trades, sector heatmap, manual close, partial exit |
| 🧠 **AI Learning** | Bayesian state priors, biases, ML classifier metrics, walk-forward |
| 📊 **Performance** | Sharpe / Sortino / drawdown / calibration / KLCI benchmark |
| 🤖 **Robo-Trader** | Start/Stop/Restart, kill-switch, auto-trade toggles, last/next run, scheduler log |
| 📜 **Logs** | Trade executions · Scheduler · Learning · Bias updates · Data quality (with CSV download) |
| ⚙️ **Settings** | Scanner params, risk params, custom watchlist, reset capital/trades |

---

## How the Robo-Trader works

* Spawns one background daemon thread when Streamlit boots.
* Records a HEARTBEAT every cycle into `scheduler_log`.
* Skips cleanly outside market hours (09:15–17:00 MYT, weekdays).
* During market hours, each cycle:
  1. Fetch fresh KLCI regime
  2. Scan all ~80 tickers
  3. Cache results to DB
  4. (if **auto-exit ON**) settle TPs / SLs / trailing stops / time exits
  5. (if **auto-entry ON**) place new trades on highest-confidence GOLD BUYs that pass risk checks + size multiplier
  6. Feed every closed trade into the Bayesian learner
* Sleeps until next top-of-hour.
* Honours the kill-switch flag — once engaged, the loop exits and won't restart until cleared in Settings.

**Default**: auto-exit ON, auto-entry OFF. You enable auto-entry only when you trust the agent.

---

## File / module map

```
bursa_comprehensive_ai_v2/
├── app.py                  ← Streamlit dashboard (light themed)
├── scheduler.py            ← Robo-Trader daemon thread + `python -m scheduler`
├── screener.py             ← Indicators + setup analyzer
├── trading_engine.py       ← execute_entry / partial / full / auto_settle
├── risk_manager.py         ← Drawdown / position / sector / time-window checks
├── learner.py              ← Bayesian priors + walk-forward + ML classifier
├── market_analyzer.py      ← KLCI regime + sector momentum + RS
├── evaluation.py           ← Sharpe / drawdown / calibration / benchmarks
├── data_quality.py         ← OHLCV validator
├── repository.py           ← All SQL access for engines
├── db.py                   ← SQLite schema + connection (WAL)
├── logger.py               ← All log tables + text file
├── watchlist.py            ← 80 Bursa tickers + custom user list
├── learning_engine.py      ← Deprecated shim (re-exports new modules)
├── ai_parameters.json      ← Default scanner params (also seeds DB)
├── requirements.txt
├── SETUP_GUIDE.md          ← This file
├── .streamlit/config.toml  ← Light theme enforcement
└── tests/                  ← 36 unit + integration tests
```

---

## SQLite schema (single file at `~/.bursa_agent_data/bursa_agent.db`)

| Table | Purpose |
|---|---|
| trades | Full trade journal |
| partial_exits | TP partial-exit child rows |
| account | Singleton: initial capital, cash, equity |
| parameters | Singleton: scanner params (json blob) |
| parameter_history | Every param change with before/after + source + reason |
| bias_state | Singleton: bias multipliers (json blob) |
| bias_history | Every bias update with before/after |
| state_priors | Per-(state_id, action) Beta(α,β) + n_trades + total_R |
| learning_events | Walk-forward, classifier retrain, Bayes updates |
| scheduler_log | Every robo-trader event |
| scheduler_state | Singleton: running flag, last/next run, kill-switch, toggles |
| trade_log | Every executed trade action (entry/partial/exit/reject) |
| data_quality_log | Per-ticker data issues |
| scan_cache | Most recent scan output |
| custom_watchlist | User-added tickers |
| risk_params | Risk parameter overrides |

Browse it with any SQLite tool. Logs are queryable + CSV-downloadable from the **📜 Logs** tab.

---

## Configuration knobs

### Light theme (locked)
`.streamlit/config.toml`:
```toml
[theme]
base = "light"
primaryColor = "#1f6feb"
backgroundColor = "#ffffff"
secondaryBackgroundColor = "#f5f7fb"
textColor = "#1a1a1a"
```

### Slippage model
`trading_engine.py` constants:
```python
TRANSACTION_COST_PCT = 0.0015     # 0.15 % per leg
SLIPPAGE_BASE_BPS = 5             # 0.05 % base
SLIPPAGE_K_RM = 50_000            # grows with RM size / 50k
LOT_SIZE = 100                    # Bursa board lot
```

### Bayesian prior weights
`learner.py`:
```python
WIN_WEIGHT_CAP = 3.0
LOSS_WEIGHT_CAP = 3.0
```

### Risk defaults
`risk_manager.DEFAULT_RISK_PARAMS` — adjust in the Settings tab, persisted to DB.

---

## Going live (real broker)

The interface to swap is `trading_engine.execute_entry()` and `execute_full_exit()`.
Recommended path for KLSE:
- **Interactive Brokers paper account** → real account (cleanest API, KLSE coverage)
- **Rakuten Trade** — no public API; not viable
- For production, wire a separate `broker_adapter.py` and behind a flag.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: scipy` | `pip install scipy` |
| Scheduler "STOPPED" after refresh | Click ♻️ Restart in 🤖 Robo-Trader tab |
| Kill-switch stuck on | Settings → Clear kill-switch |
| Tests fail in CI | Ensure `HOME` env var is writable — `conftest.py` redirects |
| yfinance returns empty for `^KLSE` | Reduce period to 6mo, retry; or wire a secondary source in `market_analyzer._try_secondary_klci` |
| Reset agent | Settings → "Delete all trades + scan cache"; for nuclear reset, delete `~/.bursa_agent_data/` |

---

## Files written outside the repo

| Path | Contains |
|---|---|
| `~/.bursa_agent_data/bursa_agent.db` | All trades / state / logs |
| `~/.bursa_agent_data/logs/bursa_agent.log` | Rotating text log (5×2 MB) |
| `~/.bursa_agent_data/setup_classifier.pkl` | Calibrated ML model |
| `~/.bursa_agent_data/regime_classifier.pkl` | KLCI regime model |
| `~/.bursa_agent_data/market_regime_cache.json` | 2-hour TTL cache |
| `~/.bursa_agent_data/sector_momentum.json` | 2-hour TTL cache |

---

**Happy trading.** Paper first. Always. 🚀
