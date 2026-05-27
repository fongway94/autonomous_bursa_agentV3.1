# BursaAI v2 → v3 — Changes Summary

Focused upgrade addressing: **autonomy hardening + faster cold-start learning + your two new requirements** (auto-trade by default + self-learn from scratch).

## Files changed

| File | Why it changed |
|---|---|
| `db.py` | Default `autotrade_enabled=1`; new `exploration_mode` + `exploration_trades_target` columns; column-migration helper for upgrading v2 DBs |
| `learner.py` | Optimistic Beta(2,1) prior on BUY for unseen states; Thompson sampling in EXPLORE mode; LCB in EXPLOIT mode; auto-switch logic |
| `trading_engine.py` | Volume-aware slippage model (uses ADV from scan cache); pass ticker into slippage calls; new `SLIPPAGE_LIQUIDITY_CAP_BPS = 80` cap |
| `scheduler.py` | Self-healing `ensure_started` (force-restart on stale heartbeat); robo-only entry window (no entries after 16:00 MYT); nightly ML retrain at 01:00; auto-disable exploration once threshold reached |
| `screener.py` | Honour `shariah_only` parameter to filter universe |
| `watchlist.py` | New `SHARIAH_NON_COMPLIANT` set + `is_shariah_compliant()` + `get_all_tickers_shariah_only()` |
| `risk_manager.py` | Default `max_risk_per_trade_pct` lowered from 2.0 → 1.0 (safer auto-trade default) |
| `app.py` | Call `ensure_started` every rerun (self-healing); show brain mode badge in sidebar; EXPLORE/EXPLOIT controls + progress bar in Robo-Trader tab; Shariah toggle in Settings; default checkbox state updated for auto-entry |
| `tests/test_v3_features.py` | **NEW** — 11 v3 tests covering defaults, exploration mode, volume-aware slippage, Shariah filter, self-heal |

**Test count: v2 = 36 → v3 = 47 passing.**

## What's NOT changed (kept as-is from v2)

- All existing public APIs of `screener`, `trading_engine`, `risk_manager`, `evaluation`, `logger`, `repository`, `data_quality`, `market_analyzer` are backward-compatible.
- v2 trade logs / state priors / parameter history are preserved on upgrade.
- 36 v2 tests still pass unmodified.
- Light theme and all UI tabs unchanged in layout.

## Behavioral changes a user will notice immediately

1. The Robo-Trader is **trading autonomously from the very first scan**.
2. Sidebar shows new **Brain mode 🔬 EXPLORE** badge with a progress counter.
3. Max risk per trade is **1.0% by default** (was 2.0%).
4. No auto-entries are placed after **16:00 MYT** (preserves time for the trade to develop).
5. ML classifier auto-retrains every night at 01:00 MYT.
6. After 50 closed trades, the brain auto-flips to EXPLOIT mode (conservative).
