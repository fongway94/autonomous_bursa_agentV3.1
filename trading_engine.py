# trading_engine.py
"""
Trading Engine — paper-trade execution with realistic Bursa Malaysia conventions.

Improvements vs v1
------------------
* Cash accounting verified with property tests (see tests/).
* 100-share lot enforcement on entry.
* Configurable slippage model (linear in trade size / ADV).
* MAE/MFE tracking per active trade.
* Trailing stop set exactly once (idempotent).
* Time-exit handled by regime (5/7/14 days).
* All state in SQLite via repository — no JSON race conditions.
* Every action emits a structured trade_log row.
"""

from __future__ import annotations
import math
import pandas as pd
import yfinance as yf

from db import myt_iso, get_myt_now
from repository import (
    insert_trade, update_trade, get_trade, active_trades,
    insert_partial_exit, load_account, save_account,
)
from logger import log_trade_event, get_logger

log = get_logger("trading_engine")

# -------------------------------------------------------------------------
# COST + SLIPPAGE
# -------------------------------------------------------------------------

TRANSACTION_COST_PCT = 0.0015          # brokerage + stamp duty + clearing
SLIPPAGE_BASE_BPS = 5                  # 0.05 % minimum slippage
SLIPPAGE_K_RM = 50_000                 # adds bps proportional to trade RM
SLIPPAGE_LIQUIDITY_CAP_BPS = 80        # hard cap, even for thin stocks
LOT_SIZE = 100


def estimate_slippage_bps(trade_rm: float, avg_daily_rm: float | None = None,
                          participation_ratio: float | None = None) -> float:
    """
    Volume-aware slippage estimate (v3).

    Components
    ----------
    1. base               5 bps   — minimum market-impact + spread half.
    2. size-impact        trade_rm / 50_000   — linear in order size.
    3. liquidity penalty  if avg_daily_rm known, scale ↑ when our order is
                          >1% of typical daily volume. Penalty = up to
                          (participation_ratio * 50 bps).

    Always capped at SLIPPAGE_LIQUIDITY_CAP_BPS (80 bps = 0.8%).

    Examples
    --------
    RM 2k order, liquid stock (ADV RM 5m): 5 + 0.04 + ~0 = ~5 bps
    RM 20k order, mid-liquid (ADV RM 500k): 5 + 0.4 + 20 = ~25 bps
    RM 100k order, thin (ADV RM 200k): 5 + 2 + 50 = ~57 bps
    """
    base = SLIPPAGE_BASE_BPS + (trade_rm / SLIPPAGE_K_RM)

    if avg_daily_rm is not None and avg_daily_rm > 0:
        pr = trade_rm / avg_daily_rm
    elif participation_ratio is not None:
        pr = max(participation_ratio, 0)
    else:
        pr = 0.0

    # Liquidity penalty: 0% participation → 0 bps; 10%+ → 50 bps
    liq_bps = min(max(pr, 0.0), 0.10) * 500.0
    return min(base + liq_bps, SLIPPAGE_LIQUIDITY_CAP_BPS)


def _lookup_adv_rm(ticker: str | None) -> float | None:
    """
    Try to look up the trailing 20-day average daily traded value (in RM)
    from the most recent scan cache. None if unavailable.
    """
    if not ticker:
        return None
    try:
        from repository import load_scan_cache
        records, _, _ = load_scan_cache()
        for r in records:
            if r.get("ticker") == ticker:
                vol = r.get("volume") or 0
                price = r.get("price") or 0
                if vol and price:
                    return float(vol) * float(price)  # rough proxy
        return None
    except Exception:
        return None


def apply_buy_slippage(price: float, shares: int,
                       ticker: str | None = None) -> tuple[float, float]:
    """Worsen the fill on a buy. Returns (filled_price, slippage_pct)."""
    adv_rm = _lookup_adv_rm(ticker)
    bps = estimate_slippage_bps(price * shares, avg_daily_rm=adv_rm)
    slip = bps / 10_000.0
    return price * (1 + slip), slip * 100


def apply_sell_slippage(price: float, shares: int,
                        ticker: str | None = None) -> tuple[float, float]:
    adv_rm = _lookup_adv_rm(ticker)
    bps = estimate_slippage_bps(price * shares, avg_daily_rm=adv_rm)
    slip = bps / 10_000.0
    return price * (1 - slip), slip * 100


def calculate_trade_cost(shares: int, price: float) -> dict:
    gross = shares * price
    fee = gross * TRANSACTION_COST_PCT
    return {"gross": gross, "fee": fee, "total": gross + fee}


def round_to_lot(shares: int) -> int:
    """Floor shares down to nearest 100-share board lot. Returns 0 if <100."""
    if shares < LOT_SIZE:
        return 0
    return int((shares // LOT_SIZE) * LOT_SIZE)


# -------------------------------------------------------------------------
# ENTRY
# -------------------------------------------------------------------------

def execute_entry(ticker, name, sector, entry_price, stop_loss,
                  tp1, tp2, tp3, signal_type, shares, analysis_data,
                  market_regime, confidence_score,
                  execution_type: str = "MANUAL",
                  actor: str = "USER") -> tuple[bool, int | None, str]:
    """
    Place a paper trade.

    Validates:
      * Positive sane prices, SL < entry
      * Shares is a positive multiple of 100 (auto-round down)
      * Sufficient cash for slippage-adjusted fill + fee
    """
    if entry_price <= 0 or stop_loss <= 0 or entry_price <= stop_loss:
        return False, None, "Invalid entry/stop prices."

    shares = round_to_lot(int(shares))
    if shares <= 0:
        return False, None, f"Position too small (< {LOT_SIZE} share lot)."

    fill_price, slip_pct = apply_buy_slippage(entry_price, shares, ticker=ticker)
    gross = fill_price * shares
    fee = gross * TRANSACTION_COST_PCT
    total_outlay = gross + fee

    acc = load_account()
    cash = acc["cash_balance"]
    if total_outlay > cash + 0.01:
        return False, None, (f"Insufficient cash. Need RM {total_outlay:,.2f} "
                             f"(incl. RM {fee:.2f} fee + {slip_pct:.2f}% slip), "
                             f"have RM {cash:,.2f}.")

    risk_per_share = round(fill_price - stop_loss, 4)
    trade = {
        "ticker": ticker, "name": name, "sector": sector,
        "signal_type": signal_type,
        "entry_price": round(fill_price, 3),
        "stop_loss": round(float(stop_loss), 3),
        "tp1": round(float(tp1), 3),
        "tp2": round(float(tp2), 3),
        "tp3": round(float(tp3), 3),
        "shares": int(shares), "lots": int(shares // LOT_SIZE),
        "cost": round(gross, 2), "fee": round(fee, 2),
        "total_outlay": round(total_outlay, 2),
        "risk_per_share": risk_per_share,
        "actual_risk_pct": round((fill_price - stop_loss) / fill_price * 100, 2),
        "status": "ACTIVE", "phase": "FULL", "outcome": None,
        "logged_at": myt_iso(),
        "execution_type": execution_type,
        "market_regime": market_regime.get("regime_data", {}).get("regime", "UNKNOWN"),
        "regime_conviction": market_regime.get("position_rules", {}).get("conviction_pct", 0),
        "confidence_score": float(confidence_score),
        "entry_reasoning": analysis_data.get("reasoning", ""),
        "entry_indicators": {
            "rsi": analysis_data.get("rsi", 0),
            "vol_ratio": analysis_data.get("vol_ratio", 0),
            "atr": analysis_data.get("atr", 0),
            "support": analysis_data.get("support", 0),
            "resistance": analysis_data.get("resistance", 0),
            "macd_hist": analysis_data.get("macd_hist", 0),
            "ema_trend_distance": round(
                (fill_price - analysis_data.get("ema_trend", fill_price)) /
                analysis_data.get("ema_trend", fill_price) * 100, 2)
            if analysis_data.get("ema_trend") else 0,
        },
        "highest_price": round(fill_price, 3),
        "lowest_price": round(fill_price, 3),
        "mae_pct": 0.0, "mfe_pct": 0.0,
        "unrealized_pnl": 0.0, "realized_pnl": 0.0,
        "shares_remaining": int(shares),
        "slippage_pct": round(slip_pct, 4),
        "tags": [market_regime.get("regime_data", {}).get("regime", "UNKNOWN")],
    }
    trade_id = insert_trade(trade)
    save_account(cash_balance=cash - total_outlay)

    log_trade_event(
        "ENTRY_EXECUTED", trade_id=trade_id, ticker=ticker, actor=actor,
        payload={
            "fill_price": fill_price, "slippage_pct": slip_pct,
            "shares": shares, "gross": gross, "fee": fee,
            "cash_after": cash - total_outlay,
            "signal_type": signal_type, "confidence": confidence_score,
            "execution_type": execution_type,
        },
    )

    # v3.1: live trigger hook (safe — swallows all exceptions)
    try:
        from live_trigger import fire as _live_fire
        _live_fire("ENTRY", trade_id=trade_id, ticker=ticker,
                   actor=actor, payload={"fill_price": fill_price,
                                          "shares": shares})
    except Exception:
        pass

    return True, trade_id, (
        f"Entry executed: {shares} shares of {ticker} @ RM {fill_price:.3f} "
        f"(slip {slip_pct:.2f}%). Total outlay RM {total_outlay:,.2f}. "
        f"SL RM {stop_loss:.3f} | TP1 RM {tp1:.3f} | TP2 RM {tp2:.3f} | TP3 RM {tp3:.3f}.")


# -------------------------------------------------------------------------
# EXITS
# -------------------------------------------------------------------------

def execute_partial_exit(trade_id: int, tp_level: str, current_price: float,
                         shares_to_close: int, reason: str = "Partial TP exit",
                         actor: str = "USER") -> tuple[bool, str]:
    t = get_trade(trade_id)
    if t is None:
        return False, "Trade not found."
    if t["status"] != "ACTIVE":
        return False, f"Trade is {t['status']}, cannot partially exit."

    shares_to_close = round_to_lot(min(shares_to_close, t["shares_remaining"]))
    if shares_to_close <= 0:
        return False, "Nothing to close (lot-size rounding)."

    fill_price, slip_pct = apply_sell_slippage(current_price, shares_to_close, ticker=t['ticker'])
    gross = fill_price * shares_to_close
    fee = gross * TRANSACTION_COST_PCT
    net_proceeds = gross - fee
    entry = t["entry_price"]
    # Proportional entry fee already paid at open — must be netted off P&L
    entry_fee_per_share = (t.get("fee") or 0) / max(t.get("shares") or 1, 1)
    entry_fee_share = entry_fee_per_share * shares_to_close
    pnl = (fill_price - entry) * shares_to_close
    net_pnl = pnl - fee - entry_fee_share

    insert_partial_exit(trade_id, {
        "tp_level": tp_level, "shares_closed": shares_to_close,
        "exit_price": round(fill_price, 3),
        "pnl_rm": round(pnl, 2),
        "net_pnl_after_fees": round(net_pnl, 2),
        "exit_at": myt_iso(), "reason": reason,
    })

    new_remaining = t["shares_remaining"] - shares_to_close
    new_realized = (t.get("realized_pnl") or 0) + net_pnl
    new_phase = "PARTIAL" if new_remaining > 0 else "CLOSED"

    fields = {
        "shares_remaining": new_remaining,
        "realized_pnl": round(new_realized, 2),
        "phase": new_phase,
    }
    if new_remaining <= 0:
        fields.update({
            "status": "CLOSED",
            "outcome": "WIN" if pnl > 0 else "LOSS",
            "closed_pnl": round(new_realized, 2),
            "exit_price": round(fill_price, 3),
            "closed_at": myt_iso(),
        })
    update_trade(trade_id, fields)

    # Cash: we receive net_proceeds (gross − fee)
    acc = load_account()
    save_account(cash_balance=acc["cash_balance"] + net_proceeds)

    log_trade_event(
        "PARTIAL_EXIT", trade_id=trade_id, ticker=t["ticker"], actor=actor,
        payload={"tp_level": tp_level, "shares_closed": shares_to_close,
                 "fill_price": fill_price, "slippage_pct": slip_pct,
                 "pnl": pnl, "net_pnl": net_pnl,
                 "shares_remaining": new_remaining,
                 "reason": reason},
    )

    # v3.1: live trigger hook (off by default — user opts in)
    try:
        from live_trigger import fire as _live_fire
        _live_fire("PARTIAL_EXIT", trade_id=trade_id, ticker=t["ticker"],
                   actor=actor,
                   payload={"fill_price": fill_price,
                            "net_pnl": net_pnl, "tp_level": tp_level})
    except Exception:
        pass

    return True, (f"Partial {tp_level}: {shares_to_close} shares @ RM "
                  f"{fill_price:.3f}. Net P&L RM {net_pnl:+.2f}. "
                  f"{new_remaining} shares remaining.")


def execute_full_exit(trade_id: int, current_price: float,
                      reason: str = "Manual close",
                      outcome: str | None = None,
                      actor: str = "USER") -> tuple[bool, str]:
    t = get_trade(trade_id)
    if t is None:
        return False, "Trade not found."
    if t["status"] != "ACTIVE":
        return False, f"Trade is {t['status']}."

    shares_to_close = t["shares_remaining"]
    if shares_to_close <= 0:
        return False, "No shares remaining."

    fill_price, slip_pct = apply_sell_slippage(current_price, shares_to_close, ticker=t['ticker'])
    gross = fill_price * shares_to_close
    fee = gross * TRANSACTION_COST_PCT
    net_proceeds = gross - fee
    entry = t["entry_price"]
    # Proportional entry fee already paid at open — must be netted off P&L
    entry_fee_per_share = (t.get("fee") or 0) / max(t.get("shares") or 1, 1)
    entry_fee_share = entry_fee_per_share * shares_to_close
    pnl = (fill_price - entry) * shares_to_close
    net_pnl = pnl - fee - entry_fee_share

    new_realized = (t.get("realized_pnl") or 0) + net_pnl
    if outcome is None:
        if pnl > 0:
            outcome = "WIN"
        elif pnl < -(t.get("cost", 1) * 0.01):
            outcome = "LOSS"
        else:
            outcome = "BREAKEVEN"

    update_trade(trade_id, {
        "shares_remaining": 0, "phase": "CLOSED",
        "status": "CLOSED", "outcome": outcome,
        "realized_pnl": round(new_realized, 2),
        "closed_pnl": round(new_realized, 2),
        "exit_price": round(fill_price, 3),
        "closed_at": myt_iso(),
        "notes": (t.get("notes", "") or "") + f" | Exit: {reason}",
    })

    acc = load_account()
    save_account(cash_balance=acc["cash_balance"] + net_proceeds)

    log_trade_event(
        "FULL_EXIT", trade_id=trade_id, ticker=t["ticker"], actor=actor,
        payload={"fill_price": fill_price, "slippage_pct": slip_pct,
                 "pnl": pnl, "net_pnl": net_pnl,
                 "outcome": outcome, "reason": reason},
    )

    # v3.1: live trigger hook — map reason -> event type
    try:
        from live_trigger import fire as _live_fire
        reason_upper = (reason or "").upper()
        if "STOP LOSS" in reason_upper or "SL HIT" in reason_upper:
            ev = "STOP_LOSS"
        elif "TRAILING" in reason_upper:
            ev = "TRAILING_STOP"
        else:
            ev = "FULL_EXIT"
        _live_fire(ev, trade_id=trade_id, ticker=t["ticker"],
                   actor=actor,
                   payload={"fill_price": fill_price,
                            "net_pnl": net_pnl,
                            "outcome": outcome, "reason": reason})
    except Exception:
        pass

    # v3.1.5: backup DB after every full exit (manual or auto) — preserves
    # closed trade + updated cash balance + brain learning that follows
    try:
        from persistence import backup as _pers_backup, is_configured
        if is_configured():
            _pers_backup(reason=f"full exit trade #{trade_id} {outcome}")
    except Exception:
        pass

    return True, (f"Closed {t['ticker']} @ RM {fill_price:.3f}. "
                  f"Net P&L RM {net_pnl:+.2f} ({outcome}).")


# -------------------------------------------------------------------------
# AUTO-SETTLE LOOP (called by scheduler)
# -------------------------------------------------------------------------

def auto_settle_trades(price_lookup: dict, market_regime: dict,
                       actor: str = "AGENT") -> dict:
    """
    Idempotent settlement.

    price_lookup: {ticker: {"price": float, "high": float, "low": float}}
                  (high/low optional but recommended for intraday accuracy)
    """
    regime = market_regime.get("regime_data", {}).get("regime", "NEUTRAL")
    max_hold_days = {"BULL": 14, "NEUTRAL": 7, "BEAR": 5}.get(regime, 7)

    settled, partials = [], []

    for t in active_trades():
        ticker = t["ticker"]
        if ticker not in price_lookup:
            continue
        px = price_lookup[ticker]
        current_price = float(px["price"])
        high_today = float(px.get("high", current_price))
        low_today = float(px.get("low", current_price))

        entry = t["entry_price"]
        sl = t["stop_loss"]
        tp1, tp2, tp3 = t["tp1"], t["tp2"], t["tp3"]
        trailing = t.get("trailing_stop")

        # Track MAE/MFE
        highest = max(t.get("highest_price") or entry, high_today)
        lowest = min(t.get("lowest_price") or entry, low_today)
        mae_pct = (lowest - entry) / entry * 100
        mfe_pct = (highest - entry) / entry * 100

        update_trade(t["id"], {
            "highest_price": round(highest, 3),
            "lowest_price": round(lowest, 3),
            "mae_pct": round(mae_pct, 3),
            "mfe_pct": round(mfe_pct, 3),
            "unrealized_pnl": round(
                (current_price - entry) * t["shares_remaining"], 2),
        })

        # ----- Exit conditions (priority order) -----

        # 1. TP3
        if high_today >= tp3:
            ok, msg = execute_full_exit(t["id"], tp3,
                                        reason="TP3 hit", outcome="WIN",
                                        actor=actor)
            if ok:
                settled.append({"trade_id": t["id"], "type": "TP3", "msg": msg,
                                "ticker": ticker, "outcome": "WIN"})
            continue

        # 2. Trailing stop (only after it is set)
        if trailing is not None and low_today <= trailing:
            outcome = "WIN" if trailing > entry else \
                      ("BREAKEVEN" if abs(trailing - entry) / entry < 0.005
                       else "LOSS")
            ok, msg = execute_full_exit(
                t["id"], trailing,
                reason=f"Trailing stop hit @ {trailing:.3f}",
                outcome=outcome, actor=actor)
            if ok:
                settled.append({"trade_id": t["id"], "type": "TRAIL", "msg": msg,
                                "ticker": ticker, "outcome": outcome})
            continue

        # 3. Hard stop
        if low_today <= sl:
            ok, msg = execute_full_exit(t["id"], sl,
                                        reason="Hard SL hit", outcome="LOSS",
                                        actor=actor)
            if ok:
                settled.append({"trade_id": t["id"], "type": "SL", "msg": msg,
                                "ticker": ticker, "outcome": "LOSS"})
            continue

        # 4. TP2 — partial 50%, set trailing if not yet set
        if high_today >= tp2 and t.get("phase") == "FULL":
            shares_part = round_to_lot(t["shares_remaining"] // 2)
            if shares_part > 0:
                ok, msg = execute_partial_exit(
                    t["id"], "TP2", tp2, shares_part,
                    reason="50% partial at TP2 — runner kept",
                    actor=actor)
                if ok:
                    partials.append({"trade_id": t["id"], "ticker": ticker,
                                     "shares": shares_part, "msg": msg})
                # Reload trade to update fields
                t = get_trade(t["id"])

        # 5. TP1 — set trailing stop (once)
        if high_today >= tp1 and t.get("trailing_stop") is None:
            buffer_pct = 0.5
            new_trail = max(entry * (1 + buffer_pct / 100),
                            current_price * (1 - buffer_pct / 100))
            update_trade(t["id"], {"trailing_stop": round(new_trail, 3)})
            log_trade_event(
                "TRAIL_SET", trade_id=t["id"], ticker=ticker, actor=actor,
                payload={"trailing_stop": new_trail, "tp1": tp1},
            )

        # 6. Time exit
        try:
            logged = pd.to_datetime(t["logged_at"])
            if logged.tzinfo:
                logged = logged.tz_localize(None)
            days_held = (get_myt_now().replace(tzinfo=None) - logged).days
            if days_held >= max_hold_days and t.get("phase") == "FULL":
                outcome = "WIN" if current_price > entry else "LOSS"
                ok, msg = execute_full_exit(
                    t["id"], current_price,
                    reason=f"Max hold {max_hold_days}d reached",
                    outcome=outcome, actor=actor)
                if ok:
                    settled.append({"trade_id": t["id"], "type": "TIME",
                                    "msg": msg, "ticker": ticker,
                                    "outcome": outcome})
        except Exception as e:
            log.warning(f"time-exit calc failed for {t['id']}: {e}")

    # Update equity = cash + sum(active position market values)
    acc = load_account()
    active_val = 0.0
    for t in active_trades():
        px = price_lookup.get(t["ticker"], {}).get("price", t["entry_price"])
        active_val += float(px) * t["shares_remaining"]
    save_account(total_equity=acc["cash_balance"] + active_val)

    return {
        "settled": settled, "partials": partials,
        "cash_balance": round(acc["cash_balance"], 2),
        "total_equity": round(acc["cash_balance"] + active_val, 2),
    }


# -------------------------------------------------------------------------
# Convenience helpers (kept for app.py compatibility)
# -------------------------------------------------------------------------

def add_trade_note(trade_id: int, note: str) -> bool:
    t = get_trade(trade_id)
    if not t:
        return False
    new = (t.get("notes") or "") + f"\n[{myt_iso()}] {note}"
    update_trade(trade_id, {"notes": new})
    return True


def tag_trade(trade_id: int, tag: str) -> bool:
    t = get_trade(trade_id)
    if not t:
        return False
    tags = t.get("tags") or []
    if tag not in tags:
        tags.append(tag)
        update_trade(trade_id, {"tags": tags})
    return True
