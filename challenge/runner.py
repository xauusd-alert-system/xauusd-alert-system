"""python -m challenge.runner — main loop for the HashHedge challenge bot.

Trades the NYSE session (18:30-00:55 local) with opening-range breakouts,
half-platform risk cushions (daily loss -$25, total -$60, profit lock +$20),
1:5 leverage sizing, and an automatic flatten before the session close.
"""

import csv
import json
import logging
import os
import sys
import time
from datetime import datetime

from config.loader import load_config

from challenge.browser import ensure_logged_in, launch
from challenge.connector import HashHedgeConnector
from challenge.risk import ChallengeRisk
from challenge.strategy import OpeningRangeBreakout
from challenge.windows import in_flatten_window, in_session_window

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("challenge_runner")

STATE_PATH = "data/challenge_state.json"
TRADES_PATH = "data/challenge_trades.csv"
OUT_DIR = "logs/challenge"


def _load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {"day": None, "day_start_equity": None, "total_start_equity": None,
            "trading_days": 0, "flattened_today": False, "halted": False,
            "halt_reason": None, "managed": {}}


def _save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _log_trade(row):
    # RESEARCH 2026-08-22: expanded header with management + session metadata
    header = ["ts", "symbol", "side", "qty", "entry", "stop", "tp",
              "status", "exit_price", "pnl", "be_moved", "partial_closed",
              "session_bucket", "volume_ratio"]
    # Ensure row has all keys (backward-compatible with old callers)
    for k in header:
        row.setdefault(k, "")
    new = not os.path.exists(TRADES_PATH)
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in header})


def _manage_positions(conn, state, quotes, cfg=None):
    """Manage open positions: stop/TP check.

    BE + partial close are DISABLED by default (backtest 2026-08-22 showed
    they hurt WR and PF on 1-min timeframe). Enable via
    challenge.risk.manage_positions: true in config.
    """
    managed = state["managed"]
    for symbol in list(managed):
        info = managed[symbol]
        last = quotes.get(symbol, {}).get("last")
        if last is None:
            continue
        entry = info["entry"]
        original_stop = info["stop"]
        tp = info["tp"]
        risk_dist = abs(entry - original_stop)
        if risk_dist <= 0:
            continue
        # BACKTEST 2026-08-22: BE+partial HURT on 1-min timeframe
        # (WR 34%->17%, PF 0.37->0.07). Disabled by default.
        # Enable via config: challenge.risk.manage_positions: true
        manage = (cfg or {}).get("risk", {}).get("manage_positions", False)

        hit = None
        if info["side"] == "long":
            unrealized = last - entry
            if manage:
                # Breakeven at 0.5R
                if not info.get("be_moved") and unrealized >= 0.5 * risk_dist:
                    info["stop"] = entry
                    info["be_moved"] = True
                    logger.info("BE %s: stop moved to %.2f (entry) at 0.5R", symbol, entry)
                    try:
                        conn.modify_stop(symbol, entry)
                    except Exception:
                        pass
                # Partial close 50% at 1R
                if not info.get("partial_closed") and unrealized >= risk_dist:
                    info["partial_closed"] = True
                    half_qty = max(1, int(info["qty"] / 2))
                    logger.info("PARTIAL %s: closing %d/%s shares at 1R (%.2f)",
                                symbol, half_qty, info["qty"], last)
                    try:
                        conn.close_partial(symbol, half_qty)
                    except Exception:
                        pass
            if last <= info["stop"]:
                hit = ("stop", last)
            elif last >= tp:
                hit = ("tp", last)
        else:  # short
            unrealized = entry - last
            if manage:
                if not info.get("be_moved") and unrealized >= 0.5 * risk_dist:
                    info["stop"] = entry
                    info["be_moved"] = True
                    logger.info("BE %s: stop moved to %.2f (entry) at 0.5R", symbol, entry)
                    try:
                        conn.modify_stop(symbol, entry)
                    except Exception:
                        pass
                if not info.get("partial_closed") and unrealized >= risk_dist:
                    info["partial_closed"] = True
                    half_qty = max(1, int(info["qty"] / 2))
                    logger.info("PARTIAL %s: closing %d/%s shares at 1R (%.2f)",
                                symbol, half_qty, info["qty"], last)
                    try:
                        conn.close_partial(symbol, half_qty)
                    except Exception:
                        pass
            if last >= info["stop"]:
                hit = ("stop", last)
            elif last <= tp:
                hit = ("tp", last)
        if hit:
            conn.close_position(symbol)
            _log_trade({"ts": datetime.now().isoformat(timespec="seconds"),
                        "symbol": symbol, "side": info["side"], "qty": info["qty"],
                        "entry": info["entry"], "stop": info["stop"],
                        "tp": info["tp"], "status": hit[0],
                        "exit_price": hit[1], "pnl": "",
                        "be_moved": info.get("be_moved", False),
                        "partial_closed": info.get("partial_closed", False)})
            logger.info("CLOSED %s %s at %.2f (%s) BE=%s partial=%s",
                        symbol, info["side"], hit[1], hit[0],
                        info.get("be_moved"), info.get("partial_closed"))
            del managed[symbol]


def _pretrade_checklist(sig, risk, state, snap, now, cfg):
    """RESEARCH 2026-08-22 (us_stocks audit §6.1): formal pre-trade checklist.

    Every signal must pass ALL checks before an order is sent. Returns
    (ok, reason). ok=False means the signal is rejected.

    Checks (from the research checklist):
    1. Position limit: not at max concurrent positions
    2. Not duplicate: symbol not already open
    3. Equity buffer: equity > personal daily stop
    4. All-in sizing: N≥1 after fees (commission-aware)
    5. Stop-day: not in stop_day or profit_locked status
    6. Max attempts: trades_today < max_trades and losses_today < stop_after_losses
    7. Session time: not in flatten window
    """
    positions = snap["positions"]
    equity = float(snap.get("equity") or 0)
    day_start = state.get("day_start_equity", equity)
    managed = state.get("managed", {})

    # 1. Position limit
    total_positions = len(positions) + len(managed)
    if total_positions >= risk.max_open_positions:
        return False, f"position limit reached ({total_positions}/{risk.max_open_positions})"

    # 2. Not duplicate
    open_symbols = {p["symbol"] for p in positions} | set(managed)
    if sig.symbol in open_symbols:
        return False, f"{sig.symbol} already open"

    # 3. Equity buffer — equity must be above daily stop
    daily_pnl = equity - day_start
    if daily_pnl <= -risk.daily_loss_stop:
        return False, f"daily loss limit ({daily_pnl:+.2f} <= -{risk.daily_loss_stop})"

    # 4. All-in sizing: N≥1 after fees
    # Commission: min $1/side, so $2 round-trip per share for cheap stocks,
    # or 0.04% per side for expensive ones.
    stop_dist = abs(sig.entry - sig.stop)
    if stop_dist <= 0:
        return False, "stop distance is zero"
    # Estimate round-trip commission per share: max($1, 0.04%*entry) * 2
    est_fee_per_share = max(1.0, 0.0004 * sig.entry) * 2
    risk_after_fees = risk.per_trade_risk_usd - est_fee_per_share
    if risk_after_fees <= 0:
        return False, f"fees ${est_fee_per_share:.2f} exceed risk budget ${risk.per_trade_risk_usd}"
    qty = risk_after_fees / stop_dist
    if qty < 1:
        return False, f"qty={qty:.2f} below 1 share after fees (stop={stop_dist:.2f}, risk=${risk_after_fees:.2f})"

    # 5. Stop-day status
    if state.get("flattened_today"):
        return False, "stop-day: already flattened today"

    # 6. Max attempts
    trades_today = state.get("trades_today", 0)
    losses_today = state.get("losses_today", 0)
    # Check if we have a manual risk state (from challenge.manual.risk)
    if trades_today >= 3:
        return False, f"max 3 trades/day reached ({trades_today})"
    if losses_today >= 2:
        return False, f"2 losses today (stop-day rule)"

    # 7. Session time: not in flatten window
    if in_flatten_window(cfg, now):
        return False, "in flatten window (last 10 min of session)"

    return True, "ok"


def _handle_signals(conn, risk, strategy, state, snap, now, cfg=None):
    """Process signals through the pre-trade checklist before execution."""
    quotes = snap["quotes"]
    signals = strategy.update(quotes, now)
    for sig in signals:
        ok, reason = _pretrade_checklist(sig, risk, state, snap, now, cfg or {})
        if not ok:
            logger.info("BLOCKED %s: %s", sig.symbol, reason)
            continue
        # Recompute qty (checklist verified N≥1, now get exact int)
        qty = risk.position_size(sig.entry, snap["equity"])
        if qty < 1:
            logger.info("skip %s: qty=%d below 1 share (final check)", sig.symbol, qty)
            continue
        ok = conn.place_order(sig.symbol, sig.bias, qty, sig.stop, sig.tp)
        if not ok:
            logger.warning("order rejected: %s %s x%d", sig.bias, sig.symbol, qty)
            continue
        state["managed"][sig.symbol] = {
            "side": sig.bias, "qty": qty, "entry": sig.entry,
            "stop": sig.stop, "tp": sig.tp,
            "opened": datetime.now().isoformat(timespec="seconds"),
            "session_bucket": getattr(sig, "session_bucket", "unknown"),
            "volume_ratio": getattr(sig, "volume_ratio", 0.0),
        }
        if state["day"] != now.date().isoformat():
            state["trading_days"] += 1
        _log_trade({"ts": datetime.now().isoformat(timespec="seconds"),
                    "symbol": sig.symbol, "side": sig.bias, "qty": qty,
                    "entry": sig.entry, "stop": sig.stop, "tp": sig.tp,
                    "status": "open", "exit_price": "", "pnl": "",
                    "session_bucket": getattr(sig, "session_bucket", ""),
                    "volume_ratio": f"{getattr(sig, 'volume_ratio', 0):.2f}"})
        logger.info("OPENED %s %s x%d @ %.2f (stop %.2f / tp %.2f) [%s, vol %.1fx]",
                    sig.bias, sig.symbol, qty, sig.entry, sig.stop, sig.tp,
                    getattr(sig, "session_bucket", "?"), getattr(sig, "volume_ratio", 0))


def main():
    cfg = load_config().get("challenge", {})
    if not cfg.get("platform", {}).get("url"):
        logger.error("challenge.platform.url is not configured in config.yaml")
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    pw, context = launch(cfg)
    try:
        page = ensure_logged_in(context, cfg)
        session_id = str(cfg.get("platform", {}).get("session_id") or "")
        conn = HashHedgeConnector(page, session_id)
        watchlist = cfg.get("strategy", {}).get("watchlist") or []
        risk = ChallengeRisk(cfg)
        strategy = OpeningRangeBreakout(cfg)
        state = _load_state()
        poll = int(cfg.get("platform", {}).get("poll_seconds", 5))
        last_outside_log = 0
        while True:
            try:
                now = datetime.now()
                snap = conn.snapshot(watchlist)
                equity = float(snap.get("equity") or 0)
                if equity <= 0:
                    logger.info("Challenge not started yet (equity=$0); "
                                "press 'Начать торговлю' in the terminal.")
                    time.sleep(30)
                    continue
                if state["halted"]:
                    logger.info("HALTED: %s", state["halt_reason"])
                    time.sleep(60)
                    continue
                if state.get("total_start_equity") is None:
                    state["total_start_equity"] = equity
                today = now.date().isoformat()
                if state.get("day") != today and in_session_window(cfg, now):
                    state["day"] = today
                    state["day_start_equity"] = equity
                    state["flattened_today"] = False
                if state.get("day_start_equity") is None:
                    state["day_start_equity"] = equity
                    state["day"] = today
                day_start = state["day_start_equity"]
                total_start = state["total_start_equity"]
                action, reason = risk.evaluate(equity, day_start, total_start)
                if action == "halt":
                    conn.flatten()
                    state["managed"] = {}
                    state["halted"] = True
                    state["halt_reason"] = reason
                    logger.warning("HALT: %s", reason)
                    _save_state(state)
                    time.sleep(60)
                    continue
                if action == "flatten_day":
                    if not state["flattened_today"]:
                        conn.flatten()
                        state["managed"] = {}
                        state["flattened_today"] = True
                        logger.warning("DAY STOP: %s", reason)
                    time.sleep(30)
                    continue
                if not in_session_window(cfg, now):
                    if time.time() - last_outside_log > 300:
                        logger.info("Outside NYSE session (18:30-00:55 local); waiting")
                        last_outside_log = time.time()
                    time.sleep(30)
                    continue
                if in_flatten_window(cfg, now):
                    if state["managed"]:
                        conn.flatten()
                        state["managed"] = {}
                        logger.info("Session-close flatten (00:45)")
                    time.sleep(30)
                    continue
                _manage_positions(conn, state, snap["quotes"], cfg)
                _handle_signals(conn, risk, strategy, state, snap, now, cfg)
                _save_state(state)
                time.sleep(poll)
            except NotImplementedError as e:
                logger.error("Connector not mapped yet: %s", e)
                return 1
            except Exception as e:
                logger.error("Runner error: %s", e)
                time.sleep(10)
    finally:
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())