# AI Chat Handoff — Copy this into a fresh chat to continue work

Paste everything below the line into a new AI conversation when you want to continue developing this project. It gives the new assistant enough context to be immediately useful without you re-explaining.

---

## CONTEXT FOR NEW AI ASSISTANT

I'm building an autonomous AI swing-trading agent for Bursa Malaysia (KLSE). The project is live on Streamlit Cloud and has been through multiple version iterations with previous AI assistants. I need you to act as a senior software engineer and senior swing trader to help me continue maintenance and development.

### Role & expectations
- Senior SWE mindset: ask before assuming, think tradeoffs, call out risks, prefer boring proven tech
- Senior swing trader mindset: Bursa-specific conventions, realistic execution, risk-first
- Always run tests before claiming a fix works
- When fixing bugs, write a regression test for it
- When making changes, output the **complete file** for direct copy-paste to GitHub (no diffs)
- **Question infrastructure assumptions early** — for long-running systems, ask "what kills the data?" and "what kills the loop?" before adding features

### Project: BursaAI Swing Agent v3.1.10

**Mission:** Autonomous paper-trading agent that scans ~80 Bursa stocks hourly, picks GOLD BUY breakout/pullback setups, manages exits via SL/TP/trailing stops, and sends Telegram alerts so I can mirror trades in Moomoo manually. Self-learns from outcomes via Bayesian posteriors. Designed to run **indefinitely** with growing memory — not 3 months, not 1 year — as long as the agent is alive.

**Status:** Live on Streamlit Cloud, **168 tests passing in ~30 s**, **~8,700 LOC** across **19 Python modules**.

**Repo location:** GitHub (originally https://github.com/fongway94/autonomous_bursa_agentV3.1)

### Architecture (high level)
```
Robo-Trader thread (scheduler.py, hourly, PID-owned, self-healing,
boot-debounced, orphan-registry protected v3.1.10)
+ Watchdog thread (scheduler.py, every 60s, evicts runaway cycles v3.1.10)
↓
market_calendar → market_analyzer → screener → risk_manager → trading_engine → learner
↓
SQLite WAL (~/.bursa_agent_data/bursa_agent.db, incl. cycle_started_at column v3.1.10)
↓ (every closed trade + hourly heartbeat)
persistence.py → GitHub Gist (private) ← restored on boot
↓
Streamlit dashboard (8 tabs: Scanner / Portfolio / AI Learning / Performance / Robo-Trader / Logs / Live Alerts / Settings)
↓
notifier → Telegram + Email (when live_trigger fires)
```

### Key design decisions (don't violate without asking)

1. **Bayesian Beta(α,β) posteriors, NOT Q-learning** — correct for swing trading on ~80 tickers. Auto-switches EXPLORE (Thompson sampling) → EXPLOIT (LCB) at 50 closed trades.
2. **SQLite with WAL** over JSON files — kills race conditions, proven 1000+ writes/sec.
3. **PID-based scheduler ownership** — `scheduler_state.owner_pid` evicts ghost threads from Streamlit Cloud redeploys.
4. **`maintenance_state` table** for atomic SQL CAS on daily tasks (ML retrain etc.) — prevents the "8 retrains per night" bug.
5. **Boot debounce (v3.1.3)** — on Streamlit Cloud redeploy, scheduler sleeps until next scheduled boundary before first scan. Prevents GitHub-push-storm scanning. Manual buttons still bypass debounce.
6. **Auto-trade ON, auto-exit ON by default** (user explicit choice).
7. **1% max risk per trade default** (was 2% in v2, lowered because auto-trade is on by default).
8. **Light theme locked** via `.streamlit/config.toml` + CSS override. Never use dark/auto.
9. **100-share lot enforcement** at every entry (Bursa board lot).
10. **Volume-aware slippage** model: 5 bps base + size-linear + liquidity penalty, capped at 80 bps.
11. **Bursa session-aware** market_calendar with public holidays through 2027. Lunch break and pre-open phases correctly treated as "no fills". Safe-entry cutoff at 16:00.
12. **Regime-adjusted thresholds**: BULL 60% / NEUTRAL 70% / BEAR 80% confidence required.
13. **Notification-only live mode** — `broker_adapter.MoomooAdapter` is stubbed, will be wired in v4 after 3-6 months of validation.
14. **Cash conservation invariant must hold** to within RM 1.00 across any trade sequence — there's a test for this.
15. **Every closed trade feeds the learner** — α (wins) or β (losses) updates per (state, action). 250 states × 3 actions = 750 priors.
16. **Every code change requires tests** — 168 currently passing, target 100% on critical paths.
17. **Dead code gets deleted** — backwards-compat shims have an expiration date. v3.1.3 removed `learning_engine.py` shim + several unused helpers.
18. **Regime trend tracking (v3.1.4)** — `regime_history` table feeds cycle-explanation messages so user knows if BEAR is weakening (entries resuming) or strengthening (stay defensive).
19. **Gist backup (v3.1.5) is critical, not optional** — `persistence.py` backs up the whole DB to a private GitHub Gist on every closed trade + hourly heartbeat. Without `GITHUB_TOKEN` (classic PAT, `gist` scope), the brain wipes on every Streamlit Cloud redeploy. **This is non-negotiable for the long-term self-learning value prop.**
20. **Silent ghost exit + duplicate-loop guards (v3.1.8)** — duplicate loops from Streamlit reruns exit silently without log spam. `ensure_started()` does NOT spawn when another live owner is detected.
21. **Crash-recoverable start guards (v3.1.9)** — start() Guards 2/3 only block on local alive threads, not on stale DB state. Lost-handle scenarios self-heal.
22. **Zombie thread recovery via orphan registry (v3.1.10)** ⭐ — `_ORPHANED_THREAD_IDS` lets `start()` skip threads that `stop()` requested to exit but that are still hung in a sleep or network call. Without this, a single stuck cycle would permanently jam the UI on STOPPED.
23. **Runaway-cycle watchdog (v3.1.10)** ⭐ — `bursa-watchdog` thread runs every 60 s, forces a clean handoff if any cycle exceeds 10 min. Soft-warns at 5 min. Closes the autonomous-recovery gap that the orphan registry alone couldn't.
24. **All external network calls MUST have explicit `timeout=` kwargs** (v3.1.10) — the watchdog is the safety net, not the first line of defence. Python `threading` cannot interrupt blocking I/O.

### Defaults (live)

- Initial paper capital: RM 20,000
- Max risk per trade: 1% (RM 200)
- Max concurrent positions: 8 (BULL) / 5 (NEUTRAL) / 3 (BEAR)
- Drawdown warn: 8%, hard stop: 15%
- Daily trade limit: 5 new entries
- Trading window: 09:00-17:00 MYT (Bursa native), safe-entry cutoff at 16:00
- Cycle interval: 60 minutes
- Exploration target: 50 closed trades before EXPLOIT mode
- Auto-trade ON, auto-exit ON, live alerts OFF (user opts in later)
- Gist backup: on every closed trade + hourly heartbeat, rate-limited 30s
- **Watchdog: cycle timeout 10 min, soft warn 5 min, tick every 60 s (v3.1.10)**

### Module map (19 modules in v3.1.10)

| Module | What it does |
|---|---|
| `app.py` | Streamlit UI, 8 tabs, light theme |
| `scheduler.py` | Background daemon thread, hourly cycle, PID-owned, boot-debounced, orphan registry, runaway watchdog (v3.1.10) |
| `screener.py` | Indicators + GOLD BUY classifier |
| `trading_engine.py` | execute_entry/exit, cash math, slippage, lots |
| `risk_manager.py` | run_full_risk_check, drawdown breaker, time windows |
| `learner.py` | Bayesian posteriors, walk-forward, ML classifier |
| `market_analyzer.py` | KLCI regime, sector momentum, RS |
| `market_calendar.py` | Bursa sessions + public holidays |
| `evaluation.py` | Sharpe, drawdown, calibration, benchmarks (all yfinance calls now have explicit timeouts v3.1.10) |
| `data_quality.py` | OHLCV validator |
| `repository.py` | All SQL access |
| `db.py` | SQLite schema + WAL connection (adds `cycle_started_at` v3.1.10) |
| `logger.py` | 6 log streams + dedupe helpers (v3.1.8 per-minute dedup) |
| `watchlist.py` | 80 tickers + Shariah filter |
| `notifier.py` | Telegram (plain text) + Email (HTML) |
| `live_trigger.py` | Filter+dedup+format trade events into alerts |
| `broker_adapter.py` | Moomoo stub (v4-ready) |
| **`persistence.py`** ⭐ | **Gist-backed DB backup + restore (v3.1.5)** |
| **`maintenance_reminders.py`** | **Holiday/PAT/WFO renewal reminder system (v3.1.7)** |

**Note:** `learning_engine.py` was removed in v3.1.3.

### What's working perfectly right now
- Hourly scanning during Bursa sessions (09:00-12:30, 14:30-17:00)
- Lunch break + public holiday awareness
- Auto-exit on SL/TP3/trailing/time
- Bayesian state-prior updates on every closed trade
- Sidebar shows accurate last_run, next_run, heartbeat
- Telegram + Email alerts (user has both set up)
- BEAR regime defensive behaviour (refusing low-conviction entries)
- Cycle outcome explanation in scheduler log (includes regime trend in v3.1.4)
- GitHub push no longer triggers immediate scan (boot debounce)
- Light theme everywhere
- DB backed up to private GitHub Gist after every closed trade and every hour — data survives indefinitely across Streamlit Cloud container resets
- **Scheduler self-recovers from stuck loops within 10 min via watchdog (v3.1.10)**
- **UI can always recover from STOPPED → RUNNING via Start button (v3.1.10)**

### Recent bug history (each has a regression test)

- v3.1: Telegram `<br>` rejection → fixed by switching to plain text default
- v3.1: Ghost threads from Streamlit redeploys → fixed via PID ownership eviction
- v3.1.1: ML classifier retrained 8x/night → fixed via `maintenance_state` SQL CAS
- v3.1.1: Scheduler stale `next_run_at` outside market hours → fixed by always advancing
- v3.1.2: Market open was 09:15 instead of 09:00 → fixed in `market_calendar.py`
- v3.1.2: No lunch break or public holiday handling → fixed in `market_calendar.py`
- v3.1.2: Cycle log didn't explain why 0 entries fired → added `_explain_cycle_outcome()`
- v3.1.3: Every GitHub push triggered immediate market scan → fixed via boot debounce
- v3.1.3: Removed `learning_engine.py` + several unused helpers (deprecation sweep)
- v3.1.4: Cycle explanation didn't show regime trend → added `regime_history` table + `get_regime_trend()`
- v3.1.5: DB wiped on every Streamlit Cloud container reset → fixed via `persistence.py` Gist backup
- v3.1.6: ML classifier .pkl wasn't backed up + had no auto-train on boot → fixed via auto-train + .pkl in Gist
- v3.1.7: Holiday list + GitHub PAT expiry were silent failure modes → added `maintenance_reminders.py`
- v3.1.8: Duplicate worker loops caused "10 SKIPs in 16 seconds" log spam from Streamlit reruns → fixed via silent ghost exit + conservative `ensure_started`
- v3.1.9: start() permanently blocked when local _THREAD handle was lost while DB heartbeat fresh → fixed via crash-aware Guard 2/3
- **v3.1.10: Stuck cycle (yfinance hang) survives stop() → start() Guard 2 adopts the zombie → UI permanently STOPPED with no recovery path → fixed via `_ORPHANED_THREAD_IDS` registry**
- **v3.1.10: No autonomous recovery from runaway cycles (UI bug recovery required human Click) → fixed via `bursa-watchdog` thread + `cycle_started_at` column**
- **v3.1.10: conftest.py didn't reset scheduler module-level state → tests were order-dependent (flaky in isolation) → fixed in `tests/conftest.py`**
- **v3.1.10: Two `evaluation.py` yfinance calls lacked explicit `timeout=` → Performance tab could hang indefinitely → added timeouts**

### Known gaps (deliberately deferred)
- Single data source (yfinance) — user OK'd this, secondary source hook exists
- No corporate actions handling — small caps may have splits/bonuses
- Slippage is heuristic, not real fills
- Moomoo broker adapter is stubbed — awaits user validation period
- Public holiday list expires after 2027 (yearly maintenance task)
- GitHub PAT expires (yearly maintenance task)
- ML classifier .pkl file not in Gist backup (self-rebuilds nightly, so non-critical)
- Python `threading` cannot interrupt blocking I/O — watchdog recovers within 10 min but can't cut a stuck call short. Per-call timeouts are mandatory.

### v4 candidates (when user is ready)
1. Wire `broker_adapter.MoomooAdapter` to OpenAPI for real execution
2. Separate `live_account` table for real-money mirror tracking
3. Calibration-driven auto-mode-switch
4. GitHub Actions CI
5. Telegram inline approve/reject buttons
6. Rolling-window learning (fade priors older than N months)
7. ML classifier .pkl included in Gist backup
8. Multi-revision restore UI (pick which historical Gist version to restore)
9. Subprocess-isolated market scan (hard SIGKILL on stuck yfinance) — currently recovery is 10 min via watchdog, not seconds

### Long-term maintenance items (annual)
**The agent automatically reminds the user via banners above the dashboard tabs** when any of these items needs attention. Full step-by-step runbooks are in `PROJECT_HANDBOOK.md` sections 9.N (holidays) and 9.O (GitHub PAT).

- **January each year:** append next year's Bursa public holidays to `market_calendar.MY_PUBLIC_HOLIDAYS` set.
- **Every ~11 months:** regenerate `GITHUB_TOKEN` and replace in Streamlit Cloud Secrets.
- **Quarterly:** review walk-forward optimization.
- **Monthly:** review Performance tab calibration chart and per-regime stats.
- **Weekly:** verify Gist backup status + scan for CYCLE_TIMEOUT/CYCLE_SLOW events (v3.1.10).

### Working principles I expect from you
- **Read PROJECT_HANDBOOK.md first** for any non-trivial change — it has all the design rationale
- **Run tests before claiming success** — `pytest tests/ -q` should show **168 passing**
- **Bug fix = write failing test first**, then fix, then test passes
- **Output complete files for copy-paste**, not diffs — I deploy by pasting files into GitHub
- **Don't change defaults without asking** — they exist for reasons documented in handbook
- **Add to PROJECT_HANDBOOK.md** when making structural changes
- **Be honest about uncertainty** — say "I'm not sure" rather than guess. If real-money is at stake, double-check.
- **Push back if I ask for something risky** — e.g. raising default risk to 5%, removing risk gates, disabling drawdown breaker
- **Sweep dead code periodically** — backwards-compat shims have an expiration date. Confirm zero imports, delete, verify tests pass.
- **Question infrastructure assumptions early** — the v3.1.5 Gist persistence + v3.1.10 watchdog should both have been v2 day-one. For any long-running system, always ask "where does the data live and what kills it?" AND "if the loop gets stuck, how does the system recover without me?" BEFORE building features on top.
- **Every external HTTP call must have an explicit `timeout=`** (v3.1.10 invariant)

### Files I deploy to (in repo root)
```
app.py
scheduler.py            ← v3.1.10: orphan registry + watchdog
screener.py
trading_engine.py
risk_manager.py
learner.py
market_analyzer.py
market_calendar.py
evaluation.py           ← v3.1.10: yfinance timeouts added
data_quality.py
repository.py
db.py                   ← v3.1.10: cycle_started_at migration
logger.py
watchlist.py
notifier.py
live_trigger.py
broker_adapter.py
persistence.py          ← v3.1.5/3.1.6, CRITICAL for long-term data
maintenance_reminders.py ← v3.1.7, surfaces renewal reminders in UI
requirements.txt
ai_parameters.json
.streamlit/config.toml
.gitignore

tests/
conftest.py             ← v3.1.10: resets scheduler module state between tests
test_*.py (20 files, 168 tests)

HandBook/
  PROJECT_HANDBOOK.md (full design rationale)
  AI_CHAT_HANDOFF.md (this file)
  FINAL_EVALUATION.md
USER_GUIDE.md
SETUP_GUIDE.md
LIVE_TRIGGER_GUIDE.md
CHANGES_V2_TO_V3.md
CHANGES_V3_TO_V3_1.md
```

### Streamlit Cloud Secrets I have configured
- `GITHUB_TOKEN` — classic PAT with `gist` scope (v3.1.5 persistence)
- `TELEGRAM_BOT_TOKEN` — bot from @BotFather
- `TELEGRAM_CHAT_ID` — my chat ID from @userinfobot
- `ALERT_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM` — Gmail app password

### To get full context

Ask me to upload `PROJECT_HANDBOOK.md` and any other `.md` files at the start. They have everything: every design decision, every defaults table, every operational runbook, every bug history with the regression test guarding it, the full SQLite schema, the long-term maintenance calendar, and the v4 roadmap.

---

## NOW HERE'S WHAT I WANT TO WORK ON

[← Replace this with your specific request to the new AI]
