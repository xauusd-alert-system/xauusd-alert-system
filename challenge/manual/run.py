# -*- coding: utf-8 -*-
"""CLI for the prop-challenge manual trading system (ТЗ).

Commands:
  risk  calc   --price P --stop S [--profile B] [--stage 1]
        show   --profile B [--stage 1] [--equity 1000]
  day   start  --stage 1 --profile B --equity 1000 [--start-equity 1000]
        status
        equity <value>          # feed current equity (floating P&L included)
        trade  --result <usd> [--outcome W|L|BE] [--by-plan да] [--violation <text>]
  scan  --symbol AAPL --date 2026-08-19 [--all-watchlist]
  journal add   --date ... --time ... --instrument AAPL --direction L --class A
                 --entry 300 --stop 297 --target 306 --risk-usd 2.5
        close  --num 1 --result -2.5 --r -1 --outcome L
        summary
        weekly
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
CANDLES_DIR = os.path.join(ROOT, "data", "backtest", "candles")


def load_cfg() -> dict:
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def load_candles(symbol: str) -> list:
    p = os.path.join(CANDLES_DIR, symbol.upper() + ".json")
    if not os.path.exists(p):
        sys.exit(f"no candles file for {symbol}: {p}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def fmt_parsed_date(s: str):
    return dt.date.fromisoformat(s)


def cmd_risk_calc(args) -> int:
    cfg = load_cfg()
    price = float(args.price)
    stop = float(args.stop)
    stage = int(args.stage or cfg.get("default_stage", 1))
    profile = args.profile or cfg.get("default_profile", "B")
    ref = float(args.equity or cfg.get("reference_equity", 1000.0))
    p = risk_mod.profile_params(stage, profile, 0.0, ref)
    stop_dist = abs(price - stop)
    qty = p["risk_usd"] / stop_dist if stop_dist else 0.0
    risk_check = {
        "risk_usd": round(qty * stop_dist, 2),
        "qty": round(qty, 2),
        "cap_usd": p["max_risk_usd"],
        "ok": qty * stop_dist <= p["max_risk_usd"] + 1e-9,
    }
    print(
        f"Profile {profile} (stage {stage}): risk ${p['risk_usd']:.2f}, "
        f"daily limit -${p['daily_limit_usd']:.0f}, max {p['max_trades']} trades, "
        f"only A-setups: {p['only_a']}"
    )
    print(f"Price {price:.2f}, stop {stop:.2f} -> stop distance {stop_dist:.2f}")
    print(
        f"Size: {risk_check['qty']} shares (risk ${risk_check['risk_usd']:.2f}, "
        f"cap ${risk_check['cap_usd']:.2f}) -> {'OK' if risk_check['ok'] else 'REJECT (risk > cap)'}"
    )
    return 0


def cmd_risk_show(args) -> int:
    cfg = load_cfg()
    stage = int(args.stage or cfg.get("default_stage", 1))
    profile = args.profile or cfg.get("default_profile", "B")
    ref = float(args.equity or cfg.get("reference_equity", 1000.0))
    total_pnl = float(args.pnl or 0.0)
    p = risk_mod.profile_params(stage, profile, total_pnl, ref)
    print(f"=== Profile {p['profile']} / stage {p['stage']} ===")
    for k, v in p.items():
        print(f"  {k:18s} {v}")
    print("\nDrawdown scaling (ТЗ §2.3):")
    for step_dd, r, mt, oa in risk_mod.DRAWDOWN_STEPS:
        print(f"  at -{step_dd * 100:.1f}%: risk ${r}, max {mt} trade(s), A-only={oa}")
    print(f"Pause limits: stage {stage} -${p['pause_usd']:.0f}")
    return 0


def cmd_day_start(args) -> int:
    stage = int(args.stage)
    profile = args.profile
    equity = float(args.equity)
    start_eq = float(args.start_equity or equity)
    sm = risk_mod.DailyStateMachine()
    res = sm.start_day(stage, profile, equity, start_eq, dt.datetime.now())
    if not res["ok"]:
        sys.exit(f"cannot start day: {res['reason']}")
    print(
        f"Day started: stage {stage}, profile {profile}, day equity {equity:.2f}, "
        f"stage start {start_eq:.2f}, total PnL {res['total_pnl']:+.2f}"
    )
    print(
        f"Effective risk ${sm.state.effective_risk_usd:.2f}, "
        f"max {sm.state.effective_max_trades} trades, "
        f"A-only={sm.state.effective_only_a} (risk_reduced={sm.state.risk_reduced})"
    )
    return 0


def cmd_day_status(args) -> int:
    sm = risk_mod.DailyStateMachine()
    s = sm.state
    print(f"Stage {s.stage} / profile {s.profile} / date {s.date}")
    print(f"Equity {s.current_equity:.2f} (day start {s.day_start_equity:.2f}, stage start {s.total_start_equity:.2f})")
    print(f"Daily PnL {s.daily_pnl():+.2f} ({100 * s.daily_pnl() / s.day_start_equity:+.2f}%)")
    print(f"Trades {s.trades_today}/{s.effective_max_trades}, losses {s.losses_today}")
    print(f"Status: {s.status} — {s.status_reason}")
    if s.paused_until:
        print(f"Paused until {s.paused_until}")
    return 0


def cmd_day_equity(args) -> int:
    sm = risk_mod.DailyStateMachine()
    action = sm.update_equity(float(args.value))
    s = sm.state
    print(f"Equity {s.current_equity:.2f}, daily PnL {s.daily_pnl():+.2f}, status {s.status} ({s.status_reason})")
    print(f"ACTION: {action}")
    return 0


def cmd_day_trade(args) -> int:
    sm = risk_mod.DailyStateMachine()
    result = float(args.result)
    outcome = (args.outcome or ("L" if result < 0 else "W")).upper()
    violation = args.violation or ""
    sm.record_trade(result, was_planned=(args.by_plan in (None, "да", "yes", "1")), violation=violation)
    s = sm.state
    print(
        f"Trade recorded: result {result:+.2f}, outcome {outcome}, "
        f"trades {s.trades_today}/{s.effective_max_trades}, losses {s.losses_today}"
    )
    print(f"Status: {s.status} — {s.status_reason}")
    return 0


def cmd_scan(args) -> int:
    cfg = load_cfg()
    date = fmt_parsed_date(args.date)
    if args.all_watchlist:
        symbols = cfg.get("watchlist") or []
    else:
        symbols = [args.symbol.upper()] if args.symbol else []
    if not symbols:
        sys.exit("provide --symbol or --all-watchlist")
    ss = dt.time(*map(int, cfg.get("session_start_utc", "13:30").split(":")))
    for sym in symbols:
        candles = load_candles(sym)
        res = scanner_mod.scan_setup(sym, date, candles, ss, cfg)
        print("=" * 70)
        print(
            f"{sym} {res.date} trend15={res.trend15} trend30={res.trend30} "
            f"grade={res.grade} bias={res.bias} rr={res.rr}"
        )
        if res.impulse_bar:
            ib = res.impulse_bar
            print(
                f"  impulse: {dt.datetime.fromtimestamp(ib['time'], dt.UTC).strftime('%H:%M')} "
                f"O{ib['open']:.2f} H{ib['high']:.2f} L{ib['low']:.2f} C{ib['close']:.2f} V{ib.get('volume', 0):.0f}"
            )
        if res.signal_bar:
            sb = res.signal_bar
            print(
                f"  signal:  {dt.datetime.fromtimestamp(sb['time'], dt.UTC).strftime('%H:%M')} "
                f"O{sb['open']:.2f} H{sb['high']:.2f} L{sb['low']:.2f} C{sb['close']:.2f}"
            )
        if res.pullback_bars:
            print(
                f"  pullback: {len(res.pullback_bars)} bars, "
                f"{dt.datetime.fromtimestamp(res.pullback_bars[0]['time'], dt.UTC).strftime('%H:%M')} - "
                f"{dt.datetime.fromtimestamp(res.pullback_bars[-1]['time'], dt.UTC).strftime('%H:%M')}"
            )
        if res.tradable:
            print(
                f"  ENTRY {res.bias.upper()} @ {res.entry:.2f} stop {res.stop:.2f} "
                f"target {res.target:.2f} (R:R {res.rr})"
            )
        if res.no_go:
            print(f"  NO-GO: {', '.join(res.no_go)}")
    return 0


def cmd_journal(args) -> int:
    cfg = load_cfg()
    jpath = args.path or journal_mod.DEFAULT_JOURNAL
    if args.sub == "add":
        num = journal_mod.add_trade(
            jpath,
            args.date,
            args.time,
            args.instrument.upper(),
            args.direction.upper(),
            args.setup_class.upper(),
            args.entry,
            args.stop,
            args.target,
            args.risk_usd,
            args.risk_pct,
            comment=args.comment or "",
        )
        print(f"added trade #{num}")
    elif args.sub == "close":
        ok = journal_mod.close_trade(
            jpath,
            int(args.num),
            float(args.result),
            float(args.r),
            args.outcome.upper(),
            by_plan=args.by_plan or "да",
            violation=args.violation or "",
            comment=args.comment or "",
        )
        print("closed" if ok else f"trade #{args.num} not found")
    elif args.sub == "summary":
        for d in journal_mod.daily_summary(jpath):
            print(d)
    elif args.sub == "weekly":
        for w in journal_mod.weekly_metrics(jpath):
            print(w)
    return 0


def cmd_earnings(args) -> int:
    """earnings add|list|remove — ведение календаря отчётностей (YAML)."""
    cfg = load_cfg()
    path = cfg.get("earnings_calendar_path") or "challenge/manual/earnings_calendar.yaml"
    if not os.path.isabs(path):
        path = os.path.join(ROOT, path)
    cal = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cal = yaml.safe_load(f) or {}

    if args.sub == "add":
        day = cal.setdefault(args.date, [])
        if isinstance(day, str):
            day = [day]
        if args.symbol.upper() not in [s.upper() for s in day]:
            day.append(args.symbol.upper())
        cal[args.date] = sorted(set(s.upper() for s in day))
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cal, f, allow_unicode=True, sort_keys=True)
        print(f"added: {args.symbol.upper()} on {args.date} (блок: день отчёта + следующий)")
        return 0

    if args.sub == "remove":
        if args.date and args.date in cal:
            if args.symbol:
                syms = [s for s in cal[args.date] if s.upper() != args.symbol.upper()]
                if syms:
                    cal[args.date] = syms
                else:
                    del cal[args.date]
            else:
                del cal[args.date]
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(cal, f, allow_unicode=True, sort_keys=True)
            print("removed")
        else:
            print("date not found")
        return 0

    # list
    for d in sorted(cal):
        print(d, "=", ", ".join(cal[d]))
    if not cal:
        print("(пусто) — добавляй: earnings add --symbol NVDA --date 2026-08-28")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m challenge.manual.run", description="Prop-challenge manual system (ТЗ)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("risk")
    rsub = r.add_subparsers(dest="sub", required=True)
    rc = rsub.add_parser("calc")
    rc.add_argument("--price", required=True)
    rc.add_argument("--stop", required=True)
    rc.add_argument("--profile")
    rc.add_argument("--stage")
    rc.add_argument("--equity")
    rc.set_defaults(func=cmd_risk_calc)
    rs = rsub.add_parser("show")
    rs.add_argument("--profile")
    rs.add_argument("--stage")
    rs.add_argument("--equity")
    rs.add_argument("--pnl")
    rs.set_defaults(func=cmd_risk_show)

    d = sub.add_parser("day")
    dsub = d.add_subparsers(dest="sub", required=True)
    ds = dsub.add_parser("start")
    ds.add_argument("--stage", required=True)
    ds.add_argument("--profile", required=True)
    ds.add_argument("--equity", required=True)
    ds.add_argument("--start-equity")
    ds.set_defaults(func=cmd_day_start)
    dstat = dsub.add_parser("status")
    dstat.set_defaults(func=cmd_day_status)
    deq = dsub.add_parser("equity")
    deq.add_argument("value")
    deq.set_defaults(func=cmd_day_equity)
    dtr = dsub.add_parser("trade")
    dtr.add_argument("--result", required=True)
    dtr.add_argument("--outcome")
    dtr.add_argument("--by-plan")
    dtr.add_argument("--violation")
    dtr.set_defaults(func=cmd_day_trade)

    sc = sub.add_parser("scan")
    sc.add_argument("--symbol")
    sc.add_argument("--date", required=True)
    sc.add_argument("--all-watchlist", action="store_true")
    sc.set_defaults(func=cmd_scan)

    e = sub.add_parser("earnings")
    esub = e.add_subparsers(dest="sub", required=True)
    ea = esub.add_parser("add")
    ea.add_argument("--symbol", required=True)
    ea.add_argument("--date", required=True)
    ea.set_defaults(func=cmd_earnings, sub="add")
    el = esub.add_parser("list")
    el.set_defaults(func=cmd_earnings, sub="list")
    er = esub.add_parser("remove")
    er.add_argument("--date", required=True)
    er.add_argument("--symbol")
    er.set_defaults(func=cmd_earnings, sub="remove")

    j = sub.add_parser("journal")
    j.add_argument("sub", choices=["add", "close", "summary", "weekly"])
    j.add_argument("--path")
    j.add_argument("--date")
    j.add_argument("--time")
    j.add_argument("--instrument")
    j.add_argument("--direction")
    j.add_argument("--class", dest="setup_class")
    j.add_argument("--entry", type=float)
    j.add_argument("--stop", type=float)
    j.add_argument("--target", type=float)
    j.add_argument("--risk-usd", type=float)
    j.add_argument("--risk-pct", type=float)
    j.add_argument("--num")
    j.add_argument("--result", type=float)
    j.add_argument("--r", type=float)
    j.add_argument("--outcome")
    j.add_argument("--by-plan")
    j.add_argument("--violation")
    j.add_argument("--comment")
    j.set_defaults(func=cmd_journal)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
