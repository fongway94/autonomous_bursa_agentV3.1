# BursaAI v3.1 — Live Trigger Setup Guide

How to receive real-time notifications when the robo-agent makes a paper trade, so you can mirror the action in your Moomoo (or any) broker account.

---

## What this gives you

Every time the agent paper-trades (entry, full exit, stop-loss hit, trailing-stop hit), you get a formatted message on Telegram and/or email with:

- Ticker, action (BUY/SELL), exact share count
- Entry price, stop loss, all three TP levels
- Setup type, confidence score, brain mode
- Sector and current market regime
- The agent's reasoning

You decide whether to mirror the trade in your real broker. **No real orders are placed by the system.**

---

## 1. Prerequisites

- BursaAI v3.1 installed and running on Streamlit Cloud (or locally)
- At least one of:
  - A Telegram account (for Telegram alerts)
  - An SMTP-capable email account (e.g. Gmail with app password)

---

## 2. Set up Telegram (5 min)

### A. Create the bot
1. Open Telegram, search **@BotFather**, start a chat
2. Send `/newbot`
3. Choose a name (e.g. *BursaAI Alerts*)
4. Choose a username ending in `bot` (e.g. `my_bursa_alerts_bot`)
5. BotFather replies with a token like `7891234567:AAH...xyz`
6. **Copy this token** — it's your `TELEGRAM_BOT_TOKEN`

### B. Get your chat ID
1. Search **@userinfobot** on Telegram, start a chat with it
2. It replies immediately with your chat ID (e.g. `123456789`)
3. **Copy this** — it's your `TELEGRAM_CHAT_ID`

### C. Send `/start` to your new bot
This step is mandatory — Telegram bots can't message you until you message them first.

### D. Configure in Streamlit Cloud

1. Go to your Streamlit Cloud app dashboard → **Manage app** → **Secrets**
2. Add:
   ```toml
   TELEGRAM_BOT_TOKEN = "7891234567:AAH...xyz"
   TELEGRAM_CHAT_ID = "123456789"
   ```
3. Save. App restarts automatically.

For **local** development, set them in your shell or `.env`:
```bash
export TELEGRAM_BOT_TOKEN="7891234567:AAH...xyz"
export TELEGRAM_CHAT_ID="123456789"
```

---

## 3. Set up Email (optional, 5 min)

For Gmail (recommended):

1. Enable 2-Step Verification on your Google account
2. Generate an **App Password** at https://myaccount.google.com/apppasswords
3. Add to Streamlit Cloud Secrets:
   ```toml
   ALERT_SMTP_HOST = "smtp.gmail.com"
   ALERT_SMTP_PORT = "587"
   ALERT_SMTP_USER = "your.email@gmail.com"
   ALERT_SMTP_PASSWORD = "abcd efgh ijkl mnop"  # the app password, NOT your normal password
   ALERT_SMTP_FROM = "your.email@gmail.com"
   ```

For other providers, set `ALERT_SMTP_HOST` and `ALERT_SMTP_PORT` accordingly.

---

## 4. Enable alerts in the app

1. Open BursaAI dashboard → **🔔 Live Alerts** tab
2. Check the badges at top:
   - **Telegram: ✅ configured** (means env vars are set)
   - **Email: ✅ configured** (if you set up email)
3. Toggle **🔔 Enable live alerts** ON
4. Adjust:
   - **Minimum confidence** (default 70) — only signals at or above this score will alert
   - **Only when brain is in EXPLOIT mode** — strongly recommended for first month live
   - **Which events** to alert on (ENTRY, FULL_EXIT, STOP_LOSS, TRAILING_STOP all ON by default)
   - **Channels** — tick Telegram and/or Email
5. Click **💾 Save settings**
6. Click **🧪 Send test alert** — you should receive the test message immediately

---

## 5. What you'll receive

### Entry alert
```
🟢 ENTRY ALERT — 0166.KL Inari Amertron Berhad
Time: 2026-08-15 10:00:00 MYT
Setup: GOLD BUY (BREAKOUT) | Confidence: 82/100
Brain mode: 🎯 EXPLOIT

Action: BUY 1,000 shares @ RM 3.000
Stop Loss: RM 2.850 (-5.0% risk)
TP1: RM 3.225 | TP2: RM 3.300 | TP3: RM 3.450
Sector: Technology | Regime: BULL

Reasoning: Strong momentum breakout with volume (1.8x). Fast EMA
crossed above Slow EMA. Stock leading market (RS 1.32x).
```

### Stop-loss hit alert
```
🔴 STOP LOSS HIT — 0166.KL
Time: 2026-08-19 11:00:00 MYT
Action: SELL 1,000 shares @ RM 2.850
Result: LOSS RM -152.50 | -5.08% on cost
Held: 4 days
```

### Trailing stop alert
```
🟡 TRAILING STOP HIT — 0166.KL
Time: 2026-08-21 10:00:00 MYT
Action: SELL 1,000 shares @ RM 3.214
Result: WIN RM +199.30 | +6.64% on cost
Held: 6 days
```

### Full exit alert
```
✅ FULL EXIT — 0166.KL
Time: 2026-08-22 14:00:00 MYT
Action: SELL 1,000 shares @ RM 3.450
Result: WIN RM +439.75 | +14.66% on cost
Held: 7 days
```

---

## 6. Mirroring the trade in Moomoo

When you receive an entry alert:

1. **Open Moomoo app**, search the ticker (drop `.KL` suffix → e.g. `0166`)
2. **Place a BUY order** with the same:
   - **Quantity** (multiples of 100 — Bursa board lot)
   - Price = market or limit at the alerted entry
3. **Set a stop-loss order** at the alerted SL price
4. **Optionally set TP orders** at TP1, TP2, TP3 (or wait for the next exit alert)

When you receive an exit alert:

1. **Open Moomoo**, navigate to the position
2. **Sell** the remaining quantity at market

> **Important:** Real fills won't match paper fills exactly. The agent's paper P&L is a simulation. Use the agent's *direction* and *risk levels*, expect real slippage.

---

## 7. Sizing your real trades

The agent sends the share count it used in its paper trade. **You don't have to copy that.** A few sensible options:

| Approach | When to use |
|---|---|
| **Same quantity as alert** | Your real capital ≈ paper capital |
| **Half quantity** | First 30 days live — cap your risk |
| **Recalculate yourself**: `target_shares = (your_real_capital × 0.01) ÷ (entry − stop_loss)` | Standard 1% risk-per-trade sizing |
| **Fixed RM per trade** (e.g. RM 5,000 per position) | Simple, fast |

The alert always includes the entry, SL, and the risk per share, so you can re-size on the fly.

---

## 8. Filters — controlling what fires

In **🔔 Live Alerts** tab:

| Setting | Effect |
|---|---|
| **Master switch** | Kills all alerts if OFF (default OFF — opt-in) |
| **Minimum confidence** | Drops alerts below this score (default 70) |
| **EXPLOIT mode only** | Suppresses alerts until brain has 50+ trades |
| **Per-event toggles** | Turn off any of ENTRY / FULL_EXIT / STOP_LOSS / TRAILING_STOP / PARTIAL_EXIT / RISK_REJECTED individually |
| **Trigger actor** | AGENT only = ignore your own manual clicks; BOTH = include them |

### My recommended setting for your first month live

```
Master:              ON
Min confidence:      75
EXPLOIT mode only:   ON
ENTRY:               ON
FULL_EXIT:           ON
STOP_LOSS:           ON
TRAILING_STOP:       ON
PARTIAL_EXIT:        OFF (too noisy)
RISK_REJECTED:       OFF
Trigger actor:       AGENT only
```

This gives you ~1-3 alerts per day during active markets — all high quality.

---

## 9. Dedup behaviour

Each (trade_id, event_type) pair fires **once and only once**. If the scheduler retries the same cycle (rare, due to network hiccup), you won't get duplicate alerts. But a *new* trade on the *same ticker* an hour later WILL fire — that's a separate trade ID.

---

## 10. Going from notification to real auto-execution (v4, later)

When you've validated 3-6 months of live mirroring and trust the agent enough, the broker adapter is ready to wire up:

1. `pip install moomoo-api`
2. Install Moomoo OpenD on your machine (runs locally)
3. Set env vars `MOOMOO_HOST=127.0.0.1`, `MOOMOO_PORT=11111`
4. Open `broker_adapter.py` → fill in the `NotImplementedError` methods of `MoomooAdapter` using Moomoo OpenAPI calls
5. In `live_trigger.py`, add a `broker_mode` setting; route ENTRY alerts to `MoomooAdapter.place_order()`

The interface is locked in already — there's no refactor needed elsewhere.

---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| Test alert says "Telegram not configured" | Env vars not set, or app needs restart after setting them |
| Telegram says "chat not found" | You haven't sent `/start` to your bot yet |
| Gmail returns auth error | Use an **App Password** (not your normal password) and enable 2FA first |
| Alerts arrive but say "WIN RM +0.00" | Trade closed at breakeven; check Performance tab |
| No alerts despite trades happening | Check **🔔 Live Alerts → Recent alerts** for SKIPPED_FILTER rows — they tell you exactly why |
| Multiple identical alerts | Shouldn't happen due to dedup; if it does, check the `alert_log` table |
| Alert log shows lots of SKIPPED_FILTER | Loosen your filters (lower confidence threshold, turn off EXPLOIT-only) |

---

## 12. Privacy / Security

- Your Telegram chat is private (1:1 with your bot)
- SMTP credentials live in Streamlit Cloud Secrets (encrypted at rest)
- Alert log is stored in your SQLite DB at `~/.bursa_agent_data/bursa_agent.db` (local only)
- The Moomoo adapter never calls the API in v3.1 — there is literally no code path that places a real order

---

## Quick-Reference Card

```
SETUP CREDENTIALS (Streamlit Cloud → Manage app → Secrets):
  TELEGRAM_BOT_TOKEN     = "from BotFather"
  TELEGRAM_CHAT_ID       = "from @userinfobot"
  ALERT_SMTP_HOST        = "smtp.gmail.com"
  ALERT_SMTP_PORT        = "587"
  ALERT_SMTP_USER        = "you@gmail.com"
  ALERT_SMTP_PASSWORD    = "<gmail app password>"

ENABLE IN APP:
  🔔 Live Alerts tab → toggle Enable live alerts → Save → 🧪 Test

RECOMMENDED FIRST-MONTH FILTERS:
  Master ON, min conf 75, EXPLOIT-only ON,
  ENTRY/FULL_EXIT/STOP_LOSS/TRAILING_STOP ON, others OFF.

YOU GET:
  Telegram (and/or email) message on every qualifying trade event.

YOU DO:
  Manually mirror the trade in Moomoo (notification-only mode).

GOING FULLY AUTO (v4 later):
  Implement broker_adapter.MoomooAdapter methods.
  Interface is already locked — no refactor needed.
```

---

**Treat the agent as a senior trader sending you ideas. You are still the one pulling the trigger.** 🎯
