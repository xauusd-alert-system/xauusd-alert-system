# -*- coding: utf-8 -*-
"""CLI for the analysis-only US Stocks Headliners manual workflow.

The commands read local candle files, calculate advisory risk and record manual
facts. They contain no terminal URL, browser automation, token or order-routing
capability.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from challenge.manual import journal as journal_mod
from challenge.manual import risk as risk_mod
from challenge.manual import scanner as scanner_mod

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manual_config.yaml")
CANDLES_DIR = os.path.join(ROOT, "data", "manual", "candles")


def load_cfg() -> dict:
    if not os.path.exists(CFG_PATH):
        return {}
    with open(CFG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_candles(symbol: str, path: str | None = None) -> list:
    source = path or os.path.join(CANDLES_DIR, symbol.upper() + ".json")
    if not os.path.exists(source):
        sys.exit(f"no local candle file for {symbol}: {source}")
    with open(source, encoding="utf-8") as f:
        return json.load(f)


def cmd_risk_calc(args) -> int:
    cfg = load_cfg()
    stage = int(args.stage or cfg.get("default_stage", 1))
    profile = args.profile or cfg.get("default_profile", "B")
    reference = float(args.balance or cfg.get("reference_balance", 1000.0))
    total_pnl = float(args.stage_pnl or 0.0)
    params = risk_mod.profile_params(stage, profile, total_pnl, reference)
    shares = risk_mod.max_safe_shares(float(args.price), float(args.stop), params["risk_usd"],
                                      float(args.buying_power) if args.buying_power else None)
    fees = risk_mod.estimated_round_trip_fees(float(args.price), float(args.stop), shares)
    price_risk = shares * abs(float(args.price) - float(args.stop))
    print(f"Profile {profile}, stage {stage}; advisory only, manual terminal confirmation required.")
    print(f"Shares {shares}; price risk ${price_risk:.2f}; estimated round-trip fee ${fees:.2f}; "
          f"planned loss ${price_risk + fees:.2f}; profile risk ${params['risk_usd']:.2f}.")
    if not shares:
        print("NO-GO: even one whole share plus estimated fees exceeds the selected risk budget or buying power.")
    return 0


def cmd_risk_show(args) -> int:
    cfg = load_cfg()
    stage = int(args.stage or cfg.get("default_stage", 1))
    profile = args.profile or cfg.get("default_profile", "B")
    reference = float(args.balance or cfg.get("reference_balance", 1000.0))
    params = risk_mod.profile_params(stage, profile, float(args.stage_pnl or 0.0), reference)
    for key, value in params.items():
        print(f"{key}: {value}")
    return 0


def cmd_day_start(args) -> int:
    sm = risk_mod.DailyStateMachine()
    result = sm.start_day(int(args.stage), args.profile.upper(), float(args.balance),
                          float(args.stage_start_balance or args.balance), dt.datetime.now(),
                          profile_a_confirmed=args.confirm_profile_a)
    if not result["ok"]:
        sys.exit(f"cannot start day: {result['reason']}")
    print(f"Day state saved. Effective risk ${sm.state.effective_risk_usd:.2f}; "
          f"max trades {sm.state.effective_max_trades}; A-only {sm.state.effective_only_a}.")
    return 0


def cmd_day_status(args) -> int:
    sm = risk_mod.DailyStateMachine()
    state = sm.state
    print(f"Stage {state.stage}; profile {state.profile}; date {state.date}")
    print(f"Equity {state.current_equity:.2f}; day-start balance {state.day_start_balance:.2f}; "
          f"stage-start balance {state.total_start_balance:.2f}")
    print(f"Daily PnL {state.daily_pnl():+.2f}; trades {state.trades_today}/{state.effective_max_trades}; "
          f"losses {state.losses_today}; B trades {state.b_trades_today}")
    print(f"Status: {state.status} — {state.status_reason}")
    return 0


def cmd_day_equity(args) -> int:
    sm = risk_mod.DailyStateMachine()
    action = sm.update_equity(float(args.value), open_positions=int(args.open_positions))
    print(f"Advisory action: {action}; status: {sm.state.status} — {sm.state.status_reason}")
    return 0


def cmd_day_trade(args) -> int:
    sm = risk_mod.DailyStateMachine()
    sm.record_trade(float(args.result), setup_class=args.setup_class.upper(),
                    was_planned=args.by_plan.lower() in ("да", "yes", "1"),
                    violation=args.violation or "")
    print(f"Manual record saved. Status: {sm.state.status} — {sm.state.status_reason}")
    return 0


def cmd_scan(args) -> int:
    cfg = load_cfg()
    if args.all_watchlist:
        symbols = cfg.get("watchlist") or []
    elif args.symbol:
        symbols = [args.symbol.upper()]
    else:
        sys.exit("provide --symbol or --all-watchlist")
    if not symbols:
        sys.exit("NO-GO: the local watchlist is intentionally empty; select one to three symbols manually first")
    if len(symbols) > int(cfg.get("max_watchlist_size", 3)):
        sys.exit("NO-GO: watchlist exceeds configured maximum")
    date = dt.date.fromisoformat(args.date)
    as_of = int(args.as_of_utc) if args.as_of_utc else None
    for symbol in symbols:
        candles = load_candles(symbol, args.candles)
        setup = scanner_mod.scan_setup(symbol, date, candles, cfg=cfg, as_of_ts=as_of)
        print(f"{symbol}: {setup.grade} {setup.bias}; trend 15m={setup.trend15} 30m={setup.trend30}; "
              f"entry={setup.entry:.4f} stop={setup.stop:.4f} target={setup.target:.4f} R={setup.rr:.2f}")
        print("NO-GO: " + "; ".join(setup.no_go) if setup.no_go else "CANDIDATE: human risk review required")
    return 0


def cmd_journal(args) -> int:
    path = args.path or journal_mod.DEFAULT_JOURNAL
    if args.sub == "add":
        number = journal_mod.add_trade(path, args.date, args.time, args.instrument.upper(),
                                       args.direction.upper(), args.setup_class.upper(), args.entry,
                                       args.stop, args.target, args.risk_usd, args.risk_pct,
                                       comment=args.comment or "")
        print(f"manual trade #{number} recorded")
    elif args.sub == "close":
        ok = journal_mod.close_trade(path, int(args.num), float(args.result), float(args.r),
                                     args.outcome.upper(), by_plan=args.by_plan or "да",
                                     violation=args.violation or "", comment=args.comment or "")
        print("closed" if ok else f"trade #{args.num} not found")
    elif args.sub == "summary":
        for row in journal_mod.daily_summary(path):
            print(row)
    elif args.sub == "weekly":
        for row in journal_mod.weekly_metrics(path):
            print(row)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Analysis-only US Stocks Headliners manual workflow")
    root = parser.add_subparsers(dest="command", required=True)

    risk = root.add_parser("risk")
    risk_sub = risk.add_subparsers(dest="sub", required=True)
    calc = risk_sub.add_parser("calc")
    calc.add_argument("--price", required=True)
    calc.add_argument("--stop", required=True)
    calc.add_argument("--profile")
    calc.add_argument("--stage")
    calc.add_argument("--balance")
    calc.add_argument("--stage-pnl")
    calc.add_argument("--buying-power")
    calc.set_defaults(func=cmd_risk_calc)
    show = risk_sub.add_parser("show")
    show.add_argument("--profile")
    show.add_argument("--stage")
    show.add_argument("--balance")
    show.add_argument("--stage-pnl")
    show.set_defaults(func=cmd_risk_show)

    day = root.add_parser("day")
    day_sub = day.add_subparsers(dest="sub", required=True)
    start = day_sub.add_parser("start")
    start.add_argument("--stage", required=True)
    start.add_argument("--profile", required=True)
    start.add_argument("--balance", required=True, help="manually verified start-of-day Balance")
    start.add_argument("--stage-start-balance")
    start.add_argument("--confirm-profile-a", action="store_true")
    start.set_defaults(func=cmd_day_start)
    status = day_sub.add_parser("status")
    status.set_defaults(func=cmd_day_status)
    equity = day_sub.add_parser("equity")
    equity.add_argument("value")
    equity.add_argument("--open-positions", default=0)
    equity.set_defaults(func=cmd_day_equity)
    trade = day_sub.add_parser("trade")
    trade.add_argument("--result", required=True)
    trade.add_argument("--class", dest="setup_class", default="A")
    trade.add_argument("--by-plan", default="да")
    trade.add_argument("--violation")
    trade.set_defaults(func=cmd_day_trade)

    scan = root.add_parser("scan")
    scan.add_argument("--symbol")
    scan.add_argument("--all-watchlist", action="store_true")
    scan.add_argument("--date", required=True)
    scan.add_argument("--as-of-utc")
    scan.add_argument("--candles", help="optional local JSON candle path")
    scan.set_defaults(func=cmd_scan)

    journal = root.add_parser("journal")
    journal.add_argument("sub", choices=["add", "close", "summary", "weekly"])
    journal.add_argument("--path")
    journal.add_argument("--date")
    journal.add_argument("--time")
    journal.add_argument("--instrument")
    journal.add_argument("--direction")
    journal.add_argument("--class", dest="setup_class")
    journal.add_argument("--entry", type=float)
    journal.add_argument("--stop", type=float)
    journal.add_argument("--target", type=float)
    journal.add_argument("--risk-usd", type=float)
    journal.add_argument("--risk-pct", type=float)
    journal.add_argument("--num")
    journal.add_argument("--result", type=float)
    journal.add_argument("--r", type=float)
    journal.add_argument("--outcome")
    journal.add_argument("--by-plan")
    journal.add_argument("--violation")
    journal.add_argument("--comment")
    journal.set_defaults(func=cmd_journal)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
