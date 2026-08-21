# -*- coding: utf-8 -*-
"""Read-only Telegram summaries for the US Stocks Headliners manual workflow.

These handlers inspect locally recorded state and journals only. They never fetch
terminal data, use a trading credential, submit an order or change a position.
"""
from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT, "data", "manual", "day_state.json")
JOURNAL_FILE = os.path.join(ROOT, "data", "manual", "journal.csv")
STATS_FILE = os.path.join(ROOT, "data", "manual", "setup_stats.json")
OUTCOMES_CSV = os.path.join(ROOT, "data", "manual", "setup_outcomes.csv")


def _modules():
    try:
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from challenge.manual import journal, outcomes, risk
        return (risk, journal, outcomes), None
    except Exception as exc:
        return None, str(exc)


def cmd_day(send, chat_id, args=()):
    modules, error = _modules()
    if error:
        send(chat_id, f"Challenge system unavailable: {error}")
        return
    risk, _, _ = modules
    if not os.path.exists(STATE_FILE):
        send(chat_id, "Day is not started. Record the manually verified start-of-day Balance first.")
        return
    state = risk.DailyStateMachine().state
    daily_pct = 100 * state.daily_pnl() / state.day_start_balance if state.day_start_balance else 0.0
    send(chat_id, "\n".join([
        f"Challenge day: stage {state.stage}, profile {state.profile}, date {state.date}",
        f"Equity {state.current_equity:.2f}; day-start Balance {state.day_start_balance:.2f}",
        f"Daily PnL {state.daily_pnl():+.2f} ({daily_pct:+.2f}%)",
        f"Trades {state.trades_today}/{state.effective_max_trades}; losses {state.losses_today}; B setups {state.b_trades_today}",
        f"Status: {state.status} — {state.status_reason}",
        "This is a local manual record, not a live terminal snapshot.",
    ]))


def cmd_journal(send, chat_id, args=()):
    modules, error = _modules()
    if error:
        send(chat_id, f"Journal unavailable: {error}")
        return
    _, journal, _ = modules
    if not os.path.exists(JOURNAL_FILE):
        send(chat_id, "Manual journal is empty.")
        return
    try:
        count = max(1, min(int(args[0]), 20)) if args else 5
    except ValueError:
        count = 5
    rows = journal.read(JOURNAL_FILE)
    if not rows:
        send(chat_id, "Manual journal is empty.")
        return
    lines = ["Latest manually recorded trades:"]
    for row in rows[-count:]:
        lines.append(
            f"#{row['num']} {row['date']} {row['time']} {row['instrument']} {row['direction']} "
            f"[{row['setup_class']}] result {row.get('result_usd') or 'open'} "
            f"outcome {row.get('outcome') or 'pending'}"
        )
    send(chat_id, "\n".join(lines))


def cmd_stats(send, chat_id, args=()):
    modules, error = _modules()
    if error:
        send(chat_id, f"Stats unavailable: {error}")
        return
    _, _, outcomes = modules
    stats = None
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, encoding="utf-8") as f:
                stats = json.load(f)
        except Exception:
            stats = None
    if stats is None and os.path.exists(OUTCOMES_CSV):
        stats = outcomes.compute_stats(outcomes.read_journal(OUTCOMES_CSV))
        outcomes.save_stats(STATS_FILE, stats)
    if not stats:
        send(chat_id, "Theoretical setup-outcome journal is empty.")
        return
    send(chat_id, outcomes.format_stats_summary(stats))


def cmd_scan(send, chat_id, args=()):
    send(chat_id, "Live scanning is intentionally disabled in the analysis-only branch. "
                  "Run the local-candle CLI scanner after loading data from an approved source.")


def cmd_alert(send, chat_id, args=()):
    send(chat_id, "Automated terminal-connected alerting is disabled. The branch supports only local, "
                  "human-reviewed analytical records and cannot route orders.")
