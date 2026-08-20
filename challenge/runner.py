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
    header = ["ts", "symbol", "side", "qty", "entry", "stop", "tp",
              "status", "exit_price", "pnl"]
    new = not os.path.exists(TRADES_PATH)
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if new:
            w.writeheader()
        w.writerow(row)


def _manage_positions(conn, state, quotes):
    managed = state["managed"]
    for symbol in list(managed):
        info = managed[symbol]
        last = quotes.get(symbol, {}).get("last")
        if last is None:
            continue
        hit = None
        if info["side"] == "long" and last <= info["stop"]:
            hit = ("stop", last)
        elif info["side"] == "long" and last >= info["tp"]:
            hit = ("tp", last)
        elif info["side"] == "short" and last >= info["stop"]:
            hit = ("stop", last)
        elif info["side"] == "short" and last <= info["tp"]:
            hit = ("tp", last)
        if hit:
            conn.close_position(symbol)
            _log_trade({"ts": datetime.now().isoformat(timespec="seconds"),
                        "symbol": symbol, "side": info["side"], "qty": info["qty"],
                        "entry": info["entry"], "stop": info["stop"],
                        "tp": info["tp"], "status": hit[0],
                        "exit_price": hit[1], "pnl": ""})
            logger.info("CLOSED %s %s at %.2f (%s)", symbol, info["side"], hit[1], hit[0])
            del managed[symbol]


def _handle_signals(conn, risk, strategy, state, snap, now):
    quotes = snap["quotes"]
    positions = snap["positions"]
    signals = strategy.update(quotes, now)
    open_symbols = {p["symbol"] for p in positions} | set(state["managed"])
    for sig in signals:
        if sig.symbol in open_symbols:
            continue
        if len(positions) + len(state["managed"]) >= risk.max_open_positions:
            logger.info("skip %s: position limit reached", sig.symbol)
            continue
        qty = risk.position_size(sig.entry, snap["equity"])
        if qty < 1:
            logger.info("skip %s: qty=%d below 1 share", sig.symbol, qty)
            continue
        ok = conn.place_order(sig.symbol, sig.bias, qty, sig.stop, sig.tp)
        if not ok:
            logger.warning("order rejected: %s %s x%d", sig.bias, sig.symbol, qty)
            continue
        state["managed"][sig.symbol] = {
            "side": sig.bias, "qty": qty, "entry": sig.entry,
            "stop": sig.stop, "tp": sig.tp,
            "opened": datetime.now().isoformat(timespec="seconds"),
        }
        if state["day"] != now.date().isoformat():
            state["trading_days"] += 1
        _log_trade({"ts": datetime.now().isoformat(timespec="seconds"),
                    "symbol": sig.symbol, "side": sig.bias, "qty": qty,
                    "entry": sig.entry, "stop": sig.stop, "tp": sig.tp,
                    "status": "open", "exit_price": "", "pnl": ""})
        logger.info("OPENED %s %s x%d @ %.2f (stop %.2f / tp %.2f)",
                    sig.bias, sig.symbol, qty, sig.entry, sig.stop, sig.tp)


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
                _manage_positions(conn, state, snap["quotes"])
                _handle_signals(conn, risk, strategy, state, snap, now)
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