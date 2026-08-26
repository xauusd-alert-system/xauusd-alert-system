"""Paper-trading batch replay + metrics (ТЗ §12/§13, Этап F).

Runs N sessions from CSV fixtures, simulates the complete signal→risk→journal
flow WITHOUT any Telegram or live data. Produces daily CSV exports and a
summary report with ТЗ §13 metrics: win rate, avg R, MAE, false setups, etc.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from datetime import datetime, timedelta
from typing import List, Optional

from usstocks.data.replay_provider import load_bars
from usstocks.journal import UsJournal
from usstocks.models import RiskState, TradeSignal
from usstocks.notify import PrintNotifier
from usstocks.risk_engine import RiskEngine
from usstocks.scanner_loop import SignalOnlyRunner, load_symbol_ids
from usstocks.session import session_from_cfg

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("usstocks.paper")

DEFAULT_CFG = {
    "risk": {"risk_per_trade_usd": 10.0, "personal_daily_stop_usd": -20.0,
             "max_trades_per_day": 2, "max_consecutive_losses": 2,
             "daily_profit_lock_usd": 20.0,
             "no_new_entries_minutes_before_close": 25},
    "challenge": {"max_notional_usd": 5000.0},
    "strategy": {},
    "us_stocks": {"tech_symbols": ["AMD", "NVDA", "TSLA", "AAPL", "META",
                                     "MSFT", "AMZN", "GOOGL", "AVGO", "NFLX",
                                     "PLTR", "COIN"]},
    "session": {"holidays": []},
    "scanner": {"poll_seconds": 60},
}


class SessionSimulator:
    """Replays one trading day from CSV bars per symbol."""

    def __init__(self, cfg: dict, journal: UsJournal, symbol_ids: dict,
                 watchlist: List[str], session_date: str):
        self.cfg = cfg
        self.journal = journal
        self.session_date = session_date
        self.symbol_ids = symbol_ids
        self.watchlist = watchlist

        self.risk = RiskEngine.from_cfg(cfg)
        self.session = session_from_cfg(cfg)

        # Provider that reads from the loaded bars dict
        class _Prov:
            def __init__(self, bars_by_sym):
                self.bars = bars_by_sym
            def get_bars(self, symbol, count):
                return self.bars[symbol.upper()]

        self.provider = _Prov({})   # will be set per session
        self.notifier = PrintNotifier()

        self.runner = SignalOnlyRunner(
            cfg, self.provider, self.notifier,
            watchlist=watchlist, state=RiskState(session_date=session_date),
            journal=journal, symbol_ids=symbol_ids)

    def run(self, bars_by_symbol: dict) -> dict:
        """Run the full session replay; returns per-session stats."""
        self.provider.bars = bars_by_symbol
        self.journal.ensure_session(self.session_date)

        # Scan at mid-session (11:00 NY) when signals would realistically form
        session_open = self.session.session_open(
            datetime.fromisoformat(self.session_date).date())
        now = session_open + timedelta(hours=2)  # ~11:30 NY

        signals = self.runner.scan_once(now)

        # Compute per-session stats from journal
        return self._compute_stats()

    def _compute_stats(self) -> dict:
        rows = self.journal._conn.execute(
            "SELECT g.*, o.outcome, o.pnl_usd, o.r_multiple"
            " FROM us_signals g LEFT JOIN us_trade_outcomes o"
            " ON o.signal_id=g.signal_id WHERE g.session_date=?"
            " ORDER BY g.created_at", (self.session_date,)).fetchall()

        trades = []
        for r in rows:
            if r["decision"] != "taken":
                continue
            o = r["outcome"]
            if not o:
                continue
            trades.append({
                "symbol": r["symbol"],
                "side": r["side"],
                "outcome": o,
                "pnl_usd": r["pnl_usd"] or 0.0,
                "r_multiple": r["r_multiple"],
                "planned_risk": r["planned_risk_usd"],
                "max_adverse": r["metrics_json"] and json.loads(r["metrics_json"]).get("mae", None),
            })

        n = len(trades)
        if n == 0:
            return {"session": self.session_date, "trades": 0, "win_rate": 0.0,
                    "avg_r": 0.0, "sum_r": 0.0, "avg_mae_r": 0.0, "max_adverse_r": 0.0,
                    "total_pnl": 0.0, "false_setups": 0}

        wins = sum(1 for t in trades if t["outcome"] == "win")
        losses = sum(1 for t in trades if t["outcome"] == "loss")
        sum_r = sum(t["r_multiple"] or 0.0 for t in trades)
        avg_r = sum_r / n
        win_rate = wins / n * 100
        sum_pnl = sum(t["pnl_usd"] for t in trades)

        # MAE: maximum adverse excursion during trade; we don't have intra-trade
        # path in replay, so approximate with the worst-case pullback vs VWAP
        # from the signal's metrics (if stored). Fallback: 0.
        maes = [t["max_adverse"] for t in trades if t["max_adverse"] is not None]
        avg_mae = sum(maes) / len(maes) if maes else 0.0
        max_mae = max(maes) if maes else 0.0

        return {
            "session": self.session_date,
            "trades": n, "wins": wins, "losses": losses,
            "win_rate": round(win_rate, 1), "avg_r": round(avg_r, 3),
            "sum_r": round(sum_r, 3), "avg_mae_r": round(avg_mae, 3),
            "max_adverse_r": round(max_mae, 3), "total_pnl": round(sum_pnl, 2),
            "false_setups": len([r for r in self.journal._conn.execute(
                "SELECT * FROM us_signals WHERE session_date=?", (self.session_date,)).fetchall()
                if r["decision"] == "rejected" or r["decision"] == "pending"])
        }


def _load_session_bars(csv_paths: dict) -> dict:
    """Load all symbols' bars for one session from CSV files."""
    out = {}
    for sym, path in csv_paths.items():
        out[sym.upper()] = load_bars(path, sym)
    return out


def run_batch(csv_root: str, session_dates: List[str],
              universe: List[str], cfg: dict,
              journal_path: str = "data/usstocks_paper.sqlite") -> List[dict]:
    """Run multiple sessions sequentially, accumulating journal."""
    journal = UsJournal(journal_path)
    symbol_ids = load_symbol_ids()

    scfg = {**DEFAULT_CFG, **cfg}
    universe = [s.upper() for s in universe]

    # Always ensure benchmarks are available
    benchmarks = ["QQQ", "SPY"]

    summary = []
    for sd in session_dates:
        logger.info("=== Paper session %s ===", sd)
        bars = {}
        # Load universe symbols
        for sym in universe:
            path = os.path.join(csv_root, f"{sym}_{sd}.csv")
            if not os.path.exists(path):
                logger.warning("%s: missing %s — skipping symbol", sd, path)
                continue
            bars[sym] = load_bars(path, sym)
        # Always load benchmarks for the session
        for bench in ["QQQ", "SPY"]:
            path = os.path.join(csv_root, f"{bench}_{sd}.csv")
            if os.path.exists(path):
                bars[bench] = load_bars(path, bench)
            else:
                logger.warning("%s: missing benchmark %s — may affect signals", sd, bench)
        if not bars:
            logger.warning("%s: no bars loaded — skip", sd)
            continue

        # Filter universe to only symbols that have data
        available = list(bars.keys())
        sim = SessionSimulator(DEFAULT_CFG, journal, load_symbol_ids(),
                               available, sd)
        stats = sim.run(bars)
        summary.append(stats)
        logger.info("%s: trades=%d win_rate=%.1f%% avgR=%.3f sumR=%.3f pnl=%.2f",
                    sd, stats["trades"], stats["win_rate"], stats["avg_r"],
                    stats["sum_r"], stats["total_pnl"])
        journal.export_day_csv(sd, "data/usstocks_export")
    journal.close()
    return summary


def _write_summary_csv(summary: List[dict], out_path: str):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if not summary:
        return
    keys = summary[0].keys()
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(keys))
        w.writeheader()
        for row in summary:
            w.writerow(row)


def main():
    ap = argparse.ArgumentParser(prog="usstocks.paper",
                                 description="Batch paper-trading replay")
    ap.add_argument("--csv-root", default="data/replay",
                    help="Root dir with SYM_DATE.csv files")
    ap.add_argument("--dates", required=True,
                    help="Comma-separated YYYY-MM-DD session dates")
    ap.add_argument("--universe", default="",
                    help="Comma-separated symbols; empty = all found")
    ap.add_argument("--journal", default="data/usstocks_paper.sqlite",
                    help="Journal DB path (paper-specific)")
    ap.add_argument("--summary-csv", default="data/usstocks_export/paper_summary.csv",
                    help="Where to write aggregate summary")
    args = ap.parse_args()

    session_dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    universe = [s.strip().upper() for s in args.universe.split(",") if s.strip()]

    summary = run_batch(args.csv_root, session_dates,
                        universe or None, {}, args.journal)
    _write_summary_csv(summary, args.summary_csv)
    logger.info("Paper batch done. Summary written to %s", args.summary_csv)


if __name__ == "__main__":
    main()