# v3 → v3.1 — Live Trigger System

## Why
Prepare the system to bridge from paper trades to real-money execution **without** placing real orders yet. After 3 months of paper-trade maturation, you'll flip a switch and start mirroring trades manually in your Moomoo account.

## What you get
- Real-time **Telegram + Email** notifications on every qualifying paper trade
- Configurable filters: confidence threshold, EXPLOIT-mode-only, per-event toggles, actor scope
- Dedup: each (trade, event) fires at most once
- New **🔔 Live Alerts** tab in the dashboard
- Broker adapter **stub** for Moomoo OpenAPI — ready to fill in for v4

## Files changed

| File | Change |
|---|---|
| `db.py` | New `live_trigger_config` (singleton) + `alert_log` tables; seed disabled-by-default |
| `trading_engine.py` | Fire `ENTRY`, `FULL_EXIT`/`STOP_LOSS`/`TRAILING_STOP`, `PARTIAL_EXIT` hooks after each log_trade_event |
| `scheduler.py` | Fire `RISK_REJECTED` hook in auto-entry block |
| `app.py` | New tab `🔔 Live Alerts` between Logs and Settings |
| `requirements.txt` | Added `requests>=2.28.0` (for Telegram HTTP API) |
| `tests/conftest.py` | Reset `scheduler_state` + `live_trigger_config` + `alert_log` between tests |

## Files added

| File | Purpose |
|---|---|
| `notifier.py` | Telegram + Email senders, alert log writer, dispatch helper |
| `broker_adapter.py` | Abstract `BrokerAdapter` + `MoomooAdapter` stub + `NoopAdapter` |
| `live_trigger.py` | Config IO, filter logic, dedup, formatters, public `fire()` hook |
| `tests/test_live_trigger.py` | 16 new tests covering config, filters, dedup, formats, broker interface |
| `LIVE_TRIGGER_GUIDE.md` | User setup guide (Telegram, Gmail, Moomoo manual mirroring) |
| `CHANGES_V3_TO_V3_1.md` | This file |

## Test count
- v3: 47 passing
- **v3.1: 63 passing** (47 retained + 16 new)

## Defaults / opt-in posture
- Live trigger is **disabled by default** — `live_trigger_config.enabled = 0`
- User must explicitly toggle in the 🔔 Live Alerts tab
- No alerts fire until both:
  - Master switch ON, AND
  - The relevant event-type toggle is ON, AND
  - The trade clears the confidence + actor + mode filters

## What's NOT changed
- All v3 learning logic untouched (Bayesian priors, Thompson sampling, exploration mode)
- All v3 trade engine semantics identical (cash conservation tests still pass)
- All v3 tabs functional and unchanged
- DB is forward-compatible (v3 users get new tables created on first launch)

## Architecture diagram
```
┌──────────────────────────────────────────────────────┐
│  TRADING ENGINE                                       │
│  execute_entry / execute_full_exit / partial_exit     │
└──────────────────────┬───────────────────────────────┘
                       │ after log_trade_event:
                       ▼
              ┌─────────────────┐
              │ live_trigger    │  ← config + filter + dedup
              │     .fire()     │
              └────────┬────────┘
                       │
              ┌────────┴────────┐
              ▼                 ▼
     ┌────────────────┐  ┌──────────────────┐
     │   notifier     │  │ alert_log table  │
     │  Telegram /    │  │  (DASHBOARD view)│
     │  Email / Log   │  └──────────────────┘
     └────────┬───────┘
              │
              ▼
        ┌─────────────┐
        │  YOUR PHONE │ ← Telegram message
        │  YOUR INBOX │ ← Email
        └─────────────┘

   (Later, v4)
              ┌──────────────────────┐
              │ broker_adapter       │
              │  MoomooAdapter       │ ← stubbed, ready for OpenAPI wiring
              │   .place_order()     │
              └──────────────────────┘
```
