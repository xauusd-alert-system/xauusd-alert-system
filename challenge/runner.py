"""python -m challenge.runner — main loop for HashHedge UTEx challenge with stealth.

Confirmed Hash Hedge rules:
- Drawdown static from starting balance $100 max overall Stage1
- Daily loss by floating equity realtime (Equity = Balance + Floating PnL, fees included) limit $50, bot hard stop -$30 floating
- Daily reset 00:00-00:13 UTC+4 -> reset balance_at_day_start
- Daily Loss Protection auto closes all positions but bot should close BEFORE platform
- Consistency rule absent
- Weekend hold allowed, don't force close Friday
- Leverage 1:5 -> buying power $5000
- Device fingerprinting Clause 6.5c (canvas, WebGL, fonts, audio)
- Session recording assumed -> mouse must look organic

ORB strategy: 5-min range 9:30-9:45 ET, 1-min entry 9:45-10:30 ET, filters 0.3-1.5% range, gap >3% skip, volume <1.5x 20d avg skip, earnings skip strategic, trade only gap direction, max 2/day, close all before 15:30 ET.

All timing constants inside stealth modules, not hardcoded here except ET windows which mirror stealth config.
"""

import csv
import json
import logging
import os
import sys
import time
import random
from datetime import datetime, timezone, timedelta, date
from typing import Dict, List, Optional

from config.loader import load_config

from challenge.browser import ensure_logged_in, launch
from challenge.connector import HashHedgeConnector
from challenge.risk import ChallengeRisk
from challenge.strategy import OpeningRangeBreakout
from challenge.orb_strategy import ORBStrategy
from challenge.windows import in_flatten_window, in_session_window
from challenge.manual.discipline_report import generate_report, format_report
from execution.stealth import StealthExecutionEngine, StealthConfig
from execution.stealth.browser_humanizer import BrowserHumanizer
from execution.stealth.humanized_timer import HumanizedTimer
from execution.stealth.humanized_risk_manager import HumanizedRiskManager
from execution.stealth.session_simulator import SessionSimulator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("challenge_runner")

STATE_PATH = "data/challenge_state.json"
TRADES_PATH = "data/challenge_trades.csv"
CHECKLIST_LOG = "data/challenge/checklist_log.csv"
REPORT_DIR = "data/challenge/reports"
OUT_DIR = "logs/challenge"


def _load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {
        "day": None,
        "day_start_equity": None,
        "total_start_equity": None,
        "trading_days": 0,
        "flattened_today": False,
        "halted": False,
        "halt_reason": None,
        "managed": {},
        "report_sent_today": False,
        "balance_at_day_start": 1000.0,
        "closed_pnl_today": 0.0,
        "overall_pnl": -2.90,
        "floating_pnl": 0.0,
        "trading_days_set": [],
    }


def _save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _log_trade(row):
    header = ["ts", "symbol", "side", "qty", "entry", "stop", "tp",
              "status", "exit_price", "pnl", "be_moved", "partial_closed",
              "session_bucket", "volume_ratio", "regime", "floating_pnl", "daily_pnl", "overall_pnl"]
    for k in header:
        row.setdefault(k, "")
    new = not os.path.exists(TRADES_PATH)
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in header})


def _log_checklist_result(now, symbol, passed, reason):
    os.makedirs(os.path.dirname(CHECKLIST_LOG), exist_ok=True)
    header = ["ts", "symbol", "passed", "reason"]
    new = not os.path.exists(CHECKLIST_LOG)
    with open(CHECKLIST_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if new:
            w.writeheader()
        w.writerow({
            "ts": now.isoformat(timespec="seconds"),
            "symbol": symbol,
            "passed": str(passed),
            "reason": reason,
        })


def _generate_and_send_report(cfg, now):
    try:
        today = now.date().isoformat()
        report = generate_report(date_filter=today)
        text = format_report(report)
        os.makedirs(REPORT_DIR, exist_ok=True)
        report_path = os.path.join(REPORT_DIR, f"discipline_{today}.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(text)
        logger.info("Discipline report saved: %s", report_path)
        try:
            from alerts.telegram_bot import TelegramAlertBot
            bot = TelegramAlertBot(cfg or {})
            chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
            for chunk in chunks:
                sent = bot.send_text_message(chunk)
                if sent:
                    logger.info("Discipline report sent to Telegram")
                else:
                    logger.warning("Failed to send discipline report to Telegram")
        except Exception as e:
            logger.error("Telegram send error: %s", e)
    except Exception as e:
        logger.error("Failed to generate discipline report: %s", e)


def _pretrade_checklist(sig, risk, state, snap, now, cfg, stealth_engine=None):
    """Pre-trade checklist with stealth and challenge limits."""
    positions = snap["positions"]
    equity = float(snap.get("equity") or 0)
    floating_pnl = float(snap.get("floating_pnl") or snap.get("pnl") or 0)
    day_start = state.get("balance_at_day_start") or state.get("day_start_equity") or equity
    overall_start = state.get("total_start_equity") or equity
    managed = state.get("managed", {})

    # Daily PnL floating realtime: floating + closed since reset
    closed_today = state.get("closed_pnl_today", 0.0)
    daily_pnl_floating = floating_pnl + closed_today
    overall_pnl_floating = state.get("overall_pnl", 0.0) + floating_pnl

    # 1. Position limit
    total_positions = len(positions) + len(managed)
    if total_positions >= risk.max_open_positions:
        return False, f"position limit reached ({total_positions}/{risk.max_open_positions})"

    # 2. Not duplicate
    open_symbols = {p["symbol"] for p in positions} | set(managed)
    if sig.symbol in open_symbols:
        return False, f"{sig.symbol} already open"

    # 3. Equity buffer — floating equity
    if stealth_engine:
        can, reason = stealth_engine.risk_manager.can_trade(daily_pnl=daily_pnl_floating, overall_pnl=overall_pnl_floating, now=datetime.now(timezone.utc))
        if not can:
            return False, f"stealth risk block: {reason}"
    else:
        # Fallback to old risk
        daily_pnl_simple = equity - day_start
        if daily_pnl_simple <= -risk.daily_loss_stop:
            return False, f"daily loss limit ({daily_pnl_simple:+.2f} <= -{risk.daily_loss_stop})"

    # 4. Buying power check with leverage 1:5
    # notional = price * qty ≤ equity * leverage
    buying_power = equity * 5  # leverage 1:5
    # qty will be computed later, but we can estimate risk-based qty
    # For now, skip detailed check, will check after qty calc

    # 5. Stop-day status
    if state.get("flattened_today"):
        return False, "stop-day: already flattened today"

    # 6. Max attempts: max 2 trades/day for challenge
    trades_today = state.get("trades_today", 0)
    if trades_today >= 2:
        return False, f"max 2 trades/day reached ({trades_today})"

    # 7. Session time: not in flatten window (for old session)
    if in_flatten_window(cfg, now):
        return False, "in flatten window (last 10 min of session)"

    # 8. ORB specific filters are inside ORBStrategy, not here

    # 9. S/R and quality score (optional)
    try:
        from challenge.manual.quality_score import compute_quality_score
        vol_ratio = getattr(sig, "volume_ratio", 1.0)
        regime = getattr(sig, "regime", "unknown")
        quality = compute_quality_score(
            signal_ts=int(now.timestamp()),
            volume_ratio=vol_ratio,
            regime=regime,
            bias=sig.bias,
        )
        sig._quality = quality
        min_grade = (cfg or {}).get("min_quality_grade", "D")
        grade_order = {"A": 4, "B": 3, "C": 2, "D": 1}
        if grade_order.get(quality["grade"], 0) < grade_order.get(min_grade, 0):
            return False, f"quality {quality['total']}/100 [{quality['grade']}] below min {min_grade}"
    except Exception:
        pass

    return True, "ok"


def _manage_positions(conn, state, quotes, cfg=None, stealth_engine=None, orb_strategy=None):
    """Manage open positions with stealth humanizer and ORB trailing."""
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

        # Continuous floating PnL check for stealth force close
        floating_pnl = 0
        # Estimate floating PnL for this position: (last - entry) * qty for long, (entry - last) * qty for short
        side = info["side"]
        qty = info["qty"]
        if side == "long":
            floating_pnl_est = (last - entry) * qty
        else:
            floating_pnl_est = (entry - last) * qty

        # === STEALTH: humanized equity curve management ===
        if stealth_engine is not None and stealth_engine.enabled:
            try:
                now_utc = datetime.now(timezone.utc)
                pos_dict = {
                    "id": symbol,
                    "ticket": symbol,
                    "entry_price": entry,
                    "current_price": last,
                    "stop_price": info["stop"],
                    "tp_price": tp,
                    "side": side,
                    "volume": float(qty),
                    "qty": qty,
                    "symbol": symbol,
                    "asset_key": symbol,
                }
                # Update floating PnL in risk manager
                try:
                    # Overall floating includes all positions, but we pass estimate for this one
                    # The risk manager's update_floating_pnl will be called in continuous monitor loop
                    pass
                except Exception:
                    pass

                actions = stealth_engine.manage_position(pos_dict, now_utc)
                for act in actions:
                    atype = act.get("type")
                    delay = act.get("delay_sec", 0)
                    jitter = act.get("api_jitter_sec", 0)
                    browser_action = act.get("browser_action", "click_dom")
                    try:
                        time.sleep(delay)
                        time.sleep(jitter)
                    except Exception:
                        pass
                    if atype == "partial_exit":
                        if info.get("stealth_partial_done"):
                            continue
                        # Use shares from action if available
                        close_qty = act.get("shares")
                        if close_qty is None:
                            pct = act.get("pct", 0.35)
                            close_qty = max(1, int(qty * pct))
                        close_qty = min(close_qty, qty - 1) if qty > 1 else 1
                        logger.info(f"[STEALTH] Partial exit {close_qty}/{qty} for {symbol} at +1R via {browser_action}")
                        try:
                            # Use hotkey if browser_action is hotkey
                            if browser_action == "hotkey" and conn.browser_humanizer:
                                conn.browser_humanizer.execute_hotkey("close_position")
                                # For partial, we still need qty input
                                conn.close_partial(symbol, close_qty)
                            else:
                                conn.close_partial(symbol, close_qty)
                            info["qty"] = qty - close_qty
                            info["partial_closed"] = True
                            info["stealth_partial_done"] = True
                            qty = info["qty"]
                        except Exception as e:
                            logger.debug(f"Stealth partial close failed for {symbol}: {e}")
                    elif atype == "early_close":
                        logger.info(f"[STEALTH] Early close at 0.6*TP for {symbol} via {browser_action}")
                        try:
                            if browser_action == "hotkey" and conn.browser_humanizer:
                                conn.browser_humanizer.execute_hotkey("close_position")
                            conn.close_position(symbol)
                            _log_trade({
                                "ts": datetime.now().isoformat(timespec="seconds"),
                                "symbol": symbol, "side": side, "qty": qty,
                                "entry": entry, "stop": info["stop"],
                                "tp": tp, "status": "stealth-early-close",
                                "exit_price": last, "pnl": "",
                                "be_moved": info.get("be_moved", False),
                                "partial_closed": info.get("partial_closed", False),
                                "session_bucket": info.get("session_bucket", ""),
                                "volume_ratio": f"{info.get('volume_ratio', 0):.2f}",
                                "regime": info.get("regime", ""),
                                "floating_pnl": floating_pnl_est,
                            })
                            del managed[symbol]
                            break
                        except Exception as e:
                            logger.debug(f"Stealth early close failed for {symbol}: {e}")
                    elif atype == "trailing":
                        dist_dollars = act.get("distance_dollars")
                        if dist_dollars is None:
                            # Fallback to price distance
                            dist_dollars = act.get("distance_price", 1.0)
                        side = info["side"]
                        if side == "long":
                            new_stop = last - dist_dollars
                            if new_stop > info["stop"]:
                                info["stop"] = new_stop
                                logger.info(f"[STEALTH] Trailing LONG {symbol} stop to {new_stop:.2f} (dist ${dist_dollars}) via {browser_action}")
                                try:
                                    conn.modify_stop(symbol, new_stop)
                                except Exception:
                                    pass
                        else:
                            new_stop = last + dist_dollars
                            if new_stop < info["stop"]:
                                info["stop"] = new_stop
                                logger.info(f"[STEALTH] Trailing SHORT {symbol} stop to {new_stop:.2f} (dist ${dist_dollars}) via {browser_action}")
                                try:
                                    conn.modify_stop(symbol, new_stop)
                                except Exception:
                                    pass
            except Exception as e:
                logger.debug(f"Stealth manage_position error for {symbol}: {e}")

        # Legacy BE + partial (if enabled)
        manage = (cfg or {}).get("risk", {}).get("manage_positions", False)
        hit = None
        if side == "long":
            unrealized = last - entry
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
                    half_qty = max(1, int(qty / 2))
                    logger.info("PARTIAL %s: closing %d/%s shares at 1R (%.2f)", symbol, half_qty, qty, last)
                    try:
                        conn.close_partial(symbol, half_qty)
                    except Exception:
                        pass
            if last <= info["stop"]:
                hit = ("stop", last)
            elif last >= tp:
                hit = ("tp", last)
        else:
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
                    half_qty = max(1, int(qty / 2))
                    logger.info("PARTIAL %s: closing %d/%s shares at 1R (%.2f)", symbol, half_qty, qty, last)
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
            _log_trade({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "symbol": symbol, "side": side, "qty": qty,
                "entry": entry, "stop": info["stop"],
                "tp": tp, "status": hit[0],
                "exit_price": hit[1], "pnl": "",
                "be_moved": info.get("be_moved", False),
                "partial_closed": info.get("partial_closed", False),
                "session_bucket": info.get("session_bucket", ""),
                "volume_ratio": f"{info.get('volume_ratio', 0):.2f}",
                "regime": info.get("regime", ""),
                "floating_pnl": floating_pnl_est,
            })
            logger.info("CLOSED %s %s at %.2f (%s) BE=%s partial=%s", symbol, side, hit[1], hit[0], info.get("be_moved"), info.get("partial_closed"))
            del managed[symbol]


def _handle_signals(conn, risk, strategy, state, snap, now, cfg=None, stealth_engine=None, orb_strategy=None):
    """Process signals through checklist and stealth engine."""
    quotes = snap["quotes"]
    # Use ORB strategy if available, else fallback to old
    if orb_strategy is not None:
        # ORB strategy works on 1-min candles, but we have quotes with last price
        # For integration, we will try to get signals from orb_strategy.check_breakout
        signals = []
        for symbol, q in quotes.items():
            last = q.get("last")
            if last is None:
                continue
            # Build 1-min candle from quote (simplified)
            candle_1min = {"open": last, "high": last, "low": last, "close": last, "volume": 1000}
            sig = orb_strategy.check_breakout(symbol, candle_1min, now)
            if sig:
                # Convert ORBSignal to old Signal format for checklist
                # Create a simple object with needed attrs
                class SigObj:
                    def __init__(self, orb_sig):
                        self.symbol = orb_sig.symbol
                        self.bias = orb_sig.bias
                        self.entry = orb_sig.entry
                        self.stop = orb_sig.stop
                        self.tp = orb_sig.tp
                        self.orb_high = orb_sig.orb_high
                        self.orb_low = orb_sig.orb_low
                        self.gap_pct = orb_sig.gap_pct
                        self.range_pct = orb_sig.range_pct
                        self.volume_ratio = orb_sig.volume_ratio
                        self.session_bucket = "prime"
                        self.regime = "trend_up" if orb_sig.bias == "long" else "trend_down"
                        self._quality = {}

                signals.append(SigObj(sig))
        # Also get signals from old strategy for compatibility
        try:
            old_signals = strategy.update(quotes, now)
            signals.extend(old_signals)
        except Exception:
            pass
    else:
        signals = strategy.update(quotes, now)

    for sig in signals:
        ok, reason = _pretrade_checklist(sig, risk, state, snap, now, cfg, stealth_engine=stealth_engine)
        _log_checklist_result(now, sig.symbol, ok, reason)
        if not ok:
            logger.info("BLOCKED %s: %s", sig.symbol, reason)
            continue

        # === STEALTH LAYER: 6-gate check with floating PnL ===
        stealth_plan = None
        if stealth_engine is not None and stealth_engine.enabled:
            try:
                now_utc = datetime.now(timezone.utc)
                equity = float(snap.get("equity") or 0)
                floating_pnl = float(snap.get("floating_pnl") or snap.get("pnl") or 0)
                daily_pnl = state.get("closed_pnl_today", 0.0) + floating_pnl
                overall_pnl = state.get("overall_pnl", -2.90) + floating_pnl

                stealth_signal = {
                    "signal_id": f"{sig.symbol}:{int(now.timestamp())}",
                    "bias": sig.bias,
                    "entry": sig.entry,
                    "stop": sig.stop,
                    "tp": sig.tp,
                    "volume": 1,
                    "ticker": sig.symbol,
                    "symbol": sig.symbol,
                }
                stealth_plan = stealth_engine.process_signal(stealth_signal, now_utc, equity, floating_pnl=floating_pnl, daily_pnl=daily_pnl, overall_pnl=overall_pnl)
            except TypeError:
                # Fallback to old contract without floating_pnl
                try:
                    now_utc = datetime.now(timezone.utc)
                    equity = float(snap.get("equity") or 0)
                    daily_pnl = state.get("closed_pnl_today", 0.0)
                    overall_pnl = state.get("overall_pnl", -2.90)
                    stealth_signal = {
                        "signal_id": f"{sig.symbol}:{int(now.timestamp())}",
                        "bias": sig.bias,
                        "entry": sig.entry,
                        "stop": sig.stop,
                        "tp": sig.tp,
                        "volume": 1,
                        "ticker": sig.symbol,
                    }
                    stealth_plan = stealth_engine.process_signal(stealth_signal, now_utc, equity, daily_pnl=daily_pnl, overall_pnl=overall_pnl)
                except Exception as e:
                    logger.warning(f"Stealth engine error for {sig.symbol}, failing open: {e}")
                    stealth_plan = None
            except Exception as e:
                logger.warning(f"Stealth engine error for {sig.symbol}, failing open: {e}")
                stealth_plan = None

            if stealth_plan is None:
                logger.debug(f"Signal {sig.symbol} skipped by stealth engine (gate)")
                continue

            logger.info(f"[STEALTH] Plan for {sig.symbol}: delay={stealth_plan['delay_sec']}s, risk={stealth_plan['risk_pct']:.4f} (${stealth_plan.get('risk_usd',0):.2f}), "
                        f"shares={stealth_plan.get('shares')}, magic={stealth_plan['magic']}, comment='{stealth_plan['comment']}', "
                        f"profile SL:{stealth_plan['sl_mult']} TP:{stealth_plan['tp_mult']}, action={stealth_plan['browser_action']}, jitter={stealth_plan['api_jitter_ms']}ms")
            try:
                time.sleep(stealth_plan["delay_sec"])
                time.sleep(stealth_plan.get("api_jitter_sec", 0))
            except Exception:
                pass

        # Calculate shares with risk
        qty = risk.position_size(sig.entry, snap["equity"])
        if qty < 1:
            logger.info("skip %s: qty=%d below 1 share (final check)", sig.symbol, qty)
            continue

        # Apply stealth share jitter
        if stealth_plan is not None:
            try:
                stealth_shares = stealth_plan.get("shares")
                if stealth_shares and isinstance(stealth_shares, int):
                    # Use stealth shares if it respects buying power
                    buying_power = snap["equity"] * 5
                    notional = sig.entry * stealth_shares
                    if notional <= buying_power:
                        qty = stealth_shares
            except Exception:
                pass

        # Final buying power check with leverage
        buying_power = snap["equity"] * 5
        notional = sig.entry * qty
        if notional > buying_power:
            # Reduce qty to fit buying power
            qty = max(1, int(buying_power / sig.entry))
            logger.info(f"Adjusted qty for {sig.symbol} to fit buying power: {qty} (notional ${notional:.2f} > ${buying_power:.2f})")

        # Execute via BrowserHumanizer (70% DOM, 30% hotkey)
        browser_action = stealth_plan.get("browser_action", "click_dom") if stealth_plan else "click_dom"
        try:
            if browser_action == "hotkey" and conn.browser_humanizer:
                # Use hotkey for market order
                action_key = "buy_market_best_ask" if sig.bias == "long" else "sell_market_best_bid"
                conn.browser_humanizer.execute_hotkey(action_key)
                # Still need to set qty via DOM? The hotkey may use volume param, but we set qty already via _set_qty in connector
                ok = conn.place_order(sig.symbol, sig.bias, qty, sig.stop, sig.tp)
            else:
                ok = conn.place_order(sig.symbol, sig.bias, qty, sig.stop, sig.tp)
        except Exception as e:
            logger.warning(f"Order placement failed for {sig.symbol}: {e}")
            ok = False

        if not ok:
            logger.warning("order rejected: %s %s x%d", sig.bias, sig.symbol, qty)
            continue

        if stealth_engine is not None and stealth_engine.enabled:
            try:
                now_utc = datetime.now(timezone.utc)
                stealth_engine.record_order_executed(now_utc)
                # Update risk manager PnL tracking
                stealth_engine.risk_manager._balance_at_day_start = state.get("balance_at_day_start", 1000.0)
            except Exception as e:
                logger.debug(f"Stealth record_order failed: {e}")

        state["managed"][sig.symbol] = {
            "side": sig.bias, "qty": qty, "entry": sig.entry,
            "stop": sig.stop, "tp": sig.tp,
            "opened": datetime.now().isoformat(timespec="seconds"),
            "session_bucket": getattr(sig, "session_bucket", "unknown"),
            "volume_ratio": getattr(sig, "volume_ratio", 0.0),
            "regime": getattr(sig, "regime", "unknown"),
        }
        state["trades_today"] = state.get("trades_today", 0) + 1
        if state["day"] != now.date().isoformat():
            state["trading_days"] += 1
            # Track trading days set for min 5 requirement
            if now.date().isoformat() not in state.get("trading_days_set", []):
                state.setdefault("trading_days_set", []).append(now.date().isoformat())

        _log_trade({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "symbol": sig.symbol, "side": sig.bias, "qty": qty,
            "entry": sig.entry, "stop": sig.stop, "tp": sig.tp,
            "status": "open", "exit_price": "", "pnl": "",
            "session_bucket": getattr(sig, "session_bucket", ""),
            "volume_ratio": f"{getattr(sig, 'volume_ratio', 0):.2f}",
            "regime": getattr(sig, "regime", ""),
            "floating_pnl": snap.get("floating_pnl", 0),
            "daily_pnl": state.get("closed_pnl_today", 0),
            "overall_pnl": state.get("overall_pnl", -2.90),
        })
        quality = getattr(sig, "_quality", {})
        q_str = f"Q={quality.get('total', '?')}/{quality.get('grade', '?')}" if quality else "Q=?"
        logger.info("OPENED %s %s x%d @ %.2f (stop %.2f / tp %.2f) [%s, vol %.1fx, %s] via %s",
                    sig.bias, sig.symbol, qty, sig.entry, sig.stop, sig.tp,
                    getattr(sig, "session_bucket", "?"), getattr(sig, "volume_ratio", 0), q_str, browser_action)


def continuous_equity_monitor(conn, state, stealth_engine, cfg):
    """Separate cycle checking floating PnL every 2-5s with jitter, force close if approaching thresholds."""
    try:
        snap = conn.snapshot([])
        equity = float(snap.get("equity") or 0)
        floating_pnl = float(snap.get("floating_pnl") or snap.get("pnl") or 0)
        daily_pnl = state.get("closed_pnl_today", 0.0) + floating_pnl
        overall_pnl = state.get("overall_pnl", -2.90) + floating_pnl

        # Update risk manager floating tracking
        if stealth_engine:
            now_utc = datetime.now(timezone.utc)
            stealth_engine.risk_manager.update_floating_pnl(floating_pnl, equity=equity, now=now_utc)

            should_close, reason = stealth_engine.risk_manager.should_force_close(floating_pnl=floating_pnl, daily_pnl=daily_pnl, overall_pnl=overall_pnl)
            if should_close:
                logger.warning(f"[STEALTH MONITOR] Force close triggered: {reason} (daily {daily_pnl:.2f}, overall {overall_pnl:.2f}, floating {floating_pnl:.2f})")
                # Force close all via BrowserHumanizer with humanized delay
                for symbol in list(state.get("managed", {}).keys()):
                    try:
                        # Humanized delay before close
                        delay = stealth_engine.timer.get_close_delay(now_utc)
                        jitter = stealth_engine.hygiene.get_api_jitter_sec() if stealth_engine.hygiene else 0
                        time.sleep(delay)
                        time.sleep(jitter)
                        # Use browser action
                        action = stealth_engine.browser_humanizer._rng.choice(["click_dom", "hotkey"]) if stealth_engine.browser_humanizer else "click_dom"
                        if action == "hotkey" and conn.browser_humanizer:
                            conn.browser_humanizer.execute_hotkey("close_position")
                        conn.close_position(symbol)
                        logger.info(f"[STEALTH MONITOR] Closed {symbol} due to {reason}")
                    except Exception as e:
                        logger.debug(f"Force close failed for {symbol}: {e}")

                # If overall buffer hit, halt fully
                if overall_pnl <= -(stealth_engine.config.challenge_max_overall_loss - stealth_engine.config.challenge_overall_buffer):
                    state["halted"] = True
                    state["halt_reason"] = f"overall buffer hit {overall_pnl:.2f}"
                else:
                    # Daily hard stop -> flatten day
                    state["flattened_today"] = True

    except Exception as e:
        logger.debug(f"Continuous equity monitor error: {e}")


def main():
    full_cfg = load_config()
    cfg = full_cfg.get("challenge", {})
    if not cfg.get("platform", {}).get("url"):
        logger.error("challenge.platform.url is not configured in config.yaml")
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)

    # Stealth anti-fingerprint layer with ET mode
    stealth_cfg_dict = full_cfg.get("stealth", {}) or {}
    stealth_config = StealthConfig.from_dict(stealth_cfg_dict)
    if stealth_config.seed is None:
        try:
            stealth_config.seed = int(full_cfg.get("model", {}).get("random_seed", 42))
        except Exception:
            stealth_config.seed = 42

    # Ensure challenge-specific overrides
    stealth_config.challenge_daily_cap = 2
    stealth_config.challenge_daily_hard_stop = 30.0
    stealth_config.challenge_overall_buffer = 10.0
    stealth_config.challenge_daily_loss_limit = 50.0
    stealth_config.challenge_max_overall_loss = 100.0

    stealth_engine = StealthExecutionEngine(config=stealth_config, use_et=True)
    logger.info(f"Stealth engine initialized for challenge: enabled={stealth_config.enabled}, seed={stealth_config.seed}, ET mode, tickers={stealth_config.challenge_tickers}")

    # BrowserHumanizer for UTEx
    browser_humanizer = stealth_engine.browser_humanizer

    pw, context = launch(cfg, stealth_config=stealth_config)
    try:
        page = ensure_logged_in(context, cfg)
        # Attach page to humanizers
        if browser_humanizer:
            browser_humanizer.page = page
        stealth_engine.browser_humanizer.page = page
        stealth_engine.timer.page = None  # timer doesn't need page

        session_id = str(cfg.get("platform", {}).get("session_id") or "")
        conn = HashHedgeConnector(page, session_id, browser_humanizer=browser_humanizer, stealth_engine=stealth_engine)
        # Watchlist from stealth tickers
        watchlist = stealth_config.challenge_tickers or cfg.get("strategy", {}).get("watchlist") or ["TSLA", "AAPL", "NVDA", "AMZN", "META"]
        risk = ChallengeRisk(cfg)
        # Update risk limits to match confirmed rules
        risk.daily_loss_stop = 30.0  # bot hard stop -$30 floating, before platform -$50
        risk.total_loss_stop = 90.0  # overall buffer -$90 floating
        strategy = OpeningRangeBreakout(cfg)
        orb_strategy = ORBStrategy(cfg=full_cfg, tickers=watchlist, seed=stealth_config.seed)
        state = _load_state()
        poll = int(cfg.get("platform", {}).get("poll_seconds", 5))
        last_outside_log = 0
        last_equity_check = 0
        last_tab_open_check = 0

        # Tab lifecycle: open ~9:20-9:28 ET random
        tab_open_hour, tab_open_min = stealth_engine.session_sim.get_tab_open_time()
        logger.info(f"Tab lifecycle: open at {tab_open_hour:02d}:{tab_open_min:02d} ET, wind-down 10:30-11:00, close after positions or 15:30 ET")

        while True:
            try:
                now = datetime.now()
                now_utc = datetime.now(timezone.utc)

                # Daily reset check 00:00-00:13 UTC+4
                # If in reset window, update balance_at_day_start
                try:
                    if stealth_engine.risk_manager._is_in_reset_window(now_utc):
                        utc4 = now_utc + timedelta(hours=stealth_engine.config.challenge_daily_reset_offset_hours)
                        utc4_date_str = utc4.date().isoformat()
                        if state.get("day") != utc4_date_str:
                            # Reset daily
                            snap_tmp = conn.snapshot([])
                            equity_tmp = float(snap_tmp.get("equity") or 0)
                            state["balance_at_day_start"] = equity_tmp
                            state["day"] = utc4_date_str
                            state["closed_pnl_today"] = 0.0
                            state["flattened_today"] = False
                            state["trades_today"] = 0
                            logger.info(f"Daily reset in UTC+4 window: new day {utc4_date_str}, balance_at_day_start ${equity_tmp:.2f}")
                            # Reset stealth daily
                            stealth_engine.reset_daily(now_utc=now_utc)
                            _save_state(state)
                except Exception as e:
                    logger.debug(f"Daily reset check error: {e}")

                # Continuous equity monitor every 2-5s with jitter
                if time.time() - last_equity_check >= random.uniform(2, 5):
                    last_equity_check = time.time()
                    continuous_equity_monitor(conn, state, stealth_engine, cfg)

                # Tab lifecycle management
                # If before tab open time, wait
                try:
                    current_et_min = now.hour * 60 + now.minute  # assuming now is ET, but we use UTC for simplicity
                    # For proper ET handling, we would convert, but we use config windows as if local is ET
                    tab_open_min_total = tab_open_hour * 60 + tab_open_min
                    if current_et_min < tab_open_min_total:
                        if time.time() - last_tab_open_check > 60:
                            logger.info(f"Before tab open window ({tab_open_hour:02d}:{tab_open_min:02d} ET), waiting")
                            last_tab_open_check = time.time()
                        time.sleep(10)
                        continue
                except Exception:
                    pass

                snap = conn.snapshot(watchlist)
                equity = float(snap.get("equity") or 0)
                floating_pnl = float(snap.get("floating_pnl") or snap.get("pnl") or 0)

                # Update floating tracking
                try:
                    stealth_engine.risk_manager.update_floating_pnl(floating_pnl, equity=equity, now=now_utc)
                except Exception:
                    pass

                if equity <= 0:
                    logger.info("Challenge not started yet (equity=$0); press 'Начать торговлю' in the terminal.")
                    time.sleep(30)
                    continue
                if state["halted"]:
                    logger.info("HALTED: %s", state["halt_reason"] + f" | overall {state.get('overall_pnl',0):+.2f}")
                    time.sleep(60)
                    continue
                if state.get("total_start_equity") is None:
                    state["total_start_equity"] = equity
                if state.get("balance_at_day_start") is None:
                    state["balance_at_day_start"] = equity

                today = now.date().isoformat()
                # For ET session check, use session simulator
                if not stealth_engine.session_sim.is_in_trading_session(now_utc):
                    # Outside 9:30-10:30 ET, but still manage positions until 15:30
                    if stealth_engine.session_sim.should_close_all(now_utc):
                        if state["managed"]:
                            logger.info("Close all before 15:30 ET")
                            for sym in list(state["managed"].keys()):
                                try:
                                    conn.close_position(sym)
                                except Exception:
                                    pass
                            state["managed"] = {}
                            _save_state(state)
                        time.sleep(30)
                        continue
                    # Wind-down 10:30-11:00: reduce activity
                    if stealth_engine.session_sim.is_in_wind_down(now_utc):
                        if time.time() - last_outside_log > 300:
                            logger.info("In wind-down window 10:30-11:00 ET, no new entries, managing positions")
                            last_outside_log = time.time()
                        # Still manage positions
                        _manage_positions(conn, state, snap["quotes"], cfg, stealth_engine=stealth_engine, orb_strategy=orb_strategy)
                        time.sleep(10)
                        continue
                    # Outside session, just manage
                    _manage_positions(conn, state, snap["quotes"], cfg, stealth_engine=stealth_engine, orb_strategy=orb_strategy)
                    time.sleep(5)
                    continue

                # Check daily/overall floating limits
                daily_pnl_floating = state.get("closed_pnl_today", 0.0) + floating_pnl
                overall_pnl_floating = state.get("overall_pnl", -2.90) + floating_pnl

                if overall_pnl_floating <= -90:
                    conn.flatten()
                    state["managed"] = {}
                    state["halted"] = True
                    state["halt_reason"] = f"overall buffer hit {overall_pnl_floating:+.2f} (limit -$90 floating)"
                    logger.warning("HALT: %s", state["halt_reason"])
                    _save_state(state)
                    time.sleep(60)
                    continue

                if daily_pnl_floating <= -30:
                    if not state["flattened_today"]:
                        # Close all via humanizer
                        for sym in list(state["managed"].keys()):
                            try:
                                delay = stealth_engine.timer.get_close_delay(now_utc)
                                time.sleep(delay)
                                conn.close_position(sym)
                            except Exception:
                                pass
                        state["managed"] = {}
                        state["flattened_today"] = True
                        logger.warning("DAY STOP: daily floating loss %.2f <= -$30", daily_pnl_floating)
                    time.sleep(30)
                    continue

                # ORB range collection 9:30-9:45 ET: need 5-min candles
                # For now, we simulate via quotes: if in range window, collect
                if orb_strategy:
                    try:
                        for symbol, q in snap["quotes"].items():
                            # Build 5-min candle from quote (simplified, real implementation would fetch historical)
                            candle_5min = {
                                "open": q.get("last", 0),
                                "high": q.get("last", 0) * 1.002,
                                "low": q.get("last", 0) * 0.998,
                                "close": q.get("last", 0),
                                "volume": 1000000,
                                "prev_close": q.get("last", 0) * 0.99,
                            }
                            orb_strategy.update_5min_candle(symbol, candle_5min, now_utc)
                    except Exception as e:
                        logger.debug(f"ORB 5-min collection error: {e}")

                _manage_positions(conn, state, snap["quotes"], cfg, stealth_engine=stealth_engine, orb_strategy=orb_strategy)
                _handle_signals(conn, risk, strategy, state, snap, now, cfg, stealth_engine=stealth_engine, orb_strategy=orb_strategy)

                # Track min 5 trading days naturally
                if state.get("trading_days", 0) < 5:
                    logger.info(f"Trading days progress: {state.get('trading_days',0)}/5 min")

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
