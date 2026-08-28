"""
48-hour full-power trial window (owner request 2026-08-18).

Unlocks trading on the FxPro DEMO account for `--hours` (default 48):
  - deployment.mode          research -> demo_systematic
  - execution.enabled_assets [] -> all 5 assets (XAUUSD/BTCUSD/XAGUSD/EURUSD/GBPUSD)
  - XAGUSD                   shadow re-enabled (enabled: true)
  - max_open_positions_per_asset  1 -> 2
  - max_concurrent_positions_global 3 -> 6
  - max_daily_trades_per_asset    10 -> 20
  - alerts.cooldown_minutes       15 -> 5
  - alerts.max_alerts_per_day     30 -> 60
  - risk_per_trade_pct       0.0025 -> 0.01
  - cluster_risk_cap         0.004  -> 0.02
  - total_open_risk_cap      0.0075 -> 0.03

After the window ends the watcher:
  1. stops the running MT5 trader (python -m execution.mt5_trader),
  2. generates a full report (all trades, signals, execution attempts, ledger
     integrity, per-asset stats) into docs/TRIAL_WINDOW_REPORT_<ts>.md,
  3. restores the pre-trial config.yaml from the snapshot.

Usage:
  python -m scripts.trial_window start --hours 48
  python -m scripts.trial_window watch          # run in background
  python -m scripts.trial_window report         # generate report now
  python -m scripts.trial_window revert         # restore config now (manual)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

CONFIG_PATH = os.path.join(ROOT, "config", "config.yaml")
BACKUP_DIR = os.path.join(ROOT, "config", "backup")
STATE_PATH = os.path.join(BACKUP_DIR, "trial_window_state.json")
LOG_PATH = os.path.join(ROOT, "logs", "trial_window.log")
HEARTBEAT_PATH = os.path.join(ROOT, "logs", "trial_window_heartbeat.txt")
REPORT_DIR = os.path.join(ROOT, "docs")

# (source line, target line) — applied to the live config at `start`.
TRIAL_EDITS = [
    ("  mode: research", "  mode: demo_systematic"),
    (
        "  mode: research  # simulation | research | paper | human_confirmed | demo_systematic | live_systematic",
        "  mode: demo_systematic  # simulation | research | paper | human_confirmed | demo_systematic | live_systematic",
    ),
    ("  max_open_positions_per_asset: 1", "  max_open_positions_per_asset: 2"),
    ("  max_concurrent_positions_global: 3", "  max_concurrent_positions_global: 6"),
    ("  max_daily_trades_per_asset: 10", "  max_daily_trades_per_asset: 20"),
    ("  cooldown_minutes: 15", "  cooldown_minutes: 5"),
    ("  max_alerts_per_day: 30", "  max_alerts_per_day: 60"),
    ("  risk_per_trade_pct: 0.0025", "  risk_per_trade_pct: 0.01"),
    ("  cluster_risk_cap: 0.004", "  cluster_risk_cap: 0.02"),
    ("  total_open_risk_cap: 0.0075", "  total_open_risk_cap: 0.03"),
]

XAGUSD_EDIT = (
    "XAGUSD:\n    # Quant audit 2026-08-07: XAGUSD moved to SHADOW (enabled: false).",
    "XAGUSD:\n    # Quant audit 2026-08-07: XAGUSD moved to SHADOW "
    "(enabled: false).\n    # TRIAL-48H (2026-08-18): temporarily "
    "re-enabled.",
)

ASSETS_EDIT = ("  enabled_assets: []", "  enabled_assets: [BTCUSD, XAUUSD, XAGUSD, EURUSD, GBPUSD]")


def _log(line: str) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(UTC).isoformat()} {line}\n")
    print(line)


def _now_utc() -> str:
    return datetime.now(UTC).isoformat()


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _apply_single(text: str, src: str, dst: str) -> tuple[str, str]:
    """Return (new_text, status) where status is 'applied' | 'already' | 'missing'."""
    if dst in text:
        return text, "already"
    if src in text:
        return text.replace(src, dst, 1), "applied"
    return text, "missing"


def _trial_values_present(cfg: dict) -> bool:
    exec_cfg = cfg.get("execution") or {}
    risk = cfg.get("risk") or {}
    alerts = cfg.get("alerts") or {}
    checks = [
        (cfg.get("deployment") or {}).get("mode") == "demo_systematic",
        exec_cfg.get("enabled_assets") == ["BTCUSD", "XAUUSD", "XAGUSD", "EURUSD", "GBPUSD"],
        (cfg.get("assets") or {}).get("XAGUSD", {}).get("enabled") is True,
        exec_cfg.get("max_open_positions_per_asset") == 2,
        exec_cfg.get("max_concurrent_positions_global") == 6,
        exec_cfg.get("max_daily_trades_per_asset") == 20,
        alerts.get("cooldown_minutes") == 5,
        alerts.get("max_alerts_per_day") == 60,
        risk.get("risk_per_trade_pct") == 0.01,
        risk.get("cluster_risk_cap") == 0.02,
        risk.get("total_open_risk_cap") == 0.03,
    ]
    return all(checks)


def apply_trial_config() -> list[str]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if _trial_values_present(cfg):
        return ["all trial params already active - no changes"]
    text = _read_text(CONFIG_PATH)
    statuses = []
    for src, dst in TRIAL_EDITS:
        text, status = _apply_single(text, src, dst)
        statuses.append(f"{dst.split(':')[0].strip():35s} -> {status}")
    text, status = _apply_single(text, ASSETS_EDIT[0], ASSETS_EDIT[1])
    statuses.append(f"enabled_assets            -> {status}")
    if (cfg.get("assets") or {}).get("XAGUSD", {}).get("enabled") is not True:
        text, status = _apply_single(text, XAGUSD_EDIT[0], XAGUSD_EDIT[1])
        if status == "applied":
            text = text.replace(XAGUSD_EDIT[0] + "\n    enabled: false", XAGUSD_EDIT[1] + "\n    enabled: true", 1)
        statuses.append(f"XAGUSD.enabled            -> {status}")
    yaml.safe_load(text)  # must parse, else abort
    _write_text(CONFIG_PATH, text)
    return statuses


def snapshot_config() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    snap = os.path.join(BACKUP_DIR, f"config.yaml.pre_trial_48h_{ts}.yaml")
    shutil.copy2(CONFIG_PATH, snap)
    return snap


def load_state() -> dict | None:
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def stop_trader() -> list[str]:
    """Stop python processes running execution.mt5_trader (Windows + POSIX)."""
    stopped = []
    try:
        import psutil  # type: ignore

        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
                if "execution.mt5_trader" in cmd:
                    proc.terminate()
                    stopped.append(f"pid={proc.info['pid']} ({cmd[:120]})")
            except Exception:
                continue
        time.sleep(3)
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
                if "execution.mt5_trader" in cmd:
                    proc.kill()
            except Exception:
                continue
        return stopped
    except ImportError:
        pass
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*execution.mt5_trader*' } "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
        "Write-Output ('pid=' + $_.ProcessId) }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return [line.strip() for line in (out.stdout or "").splitlines() if line.strip()]
    except Exception as exc:
        _log(f"stop_trader failed: {exc}")
        return []


def db_paths() -> tuple[str, str]:
    from config.loader import get_env, load_config

    cfg = load_config()
    trade_db = str(
        get_env("TRADE_LOG_DB_PATH", default=cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite"))
    )
    signal_db = str(get_env("SIGNAL_LOG_DB_PATH", default="data/signal_log.db"))
    return trade_db, signal_db


def _fmt_ts(ts) -> str:
    if ts is None:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts)


def generate_report(state: dict) -> str:
    from data.execution_ledger import read_execution_ledger
    from data.signal_log import read_signal_history
    from data.trade_logger import read_executed_trades
    from data.trading_event_ledger import read_trading_events, verify_event_chain

    trade_db, signal_db = db_paths()
    started_at = state.get("started_at_utc", _now_utc())
    ends_at = state.get("ends_at_utc", _now_utc())
    start_ts = int(datetime.fromisoformat(started_at).timestamp())
    end_ts = int(datetime.fromisoformat(ends_at).timestamp())

    lines: list[str] = []
    w = lines.append
    w("# TRIAL WINDOW REPORT — 48h full-power demo run (XAUUSD Alert System)")
    w("")
    w(f"- **Окно:** {started_at} → {ends_at} UTC")
    w(f"- **Длительность:** {state.get('hours', 48)} ч")
    w("- **Счёт:** FxPro-MT5 Demo (MT5_SERVER из .env)")
    w(f"- **Snapshot config:** `{state.get('snapshot')}`")
    w(f"- **Trade DB:** `{trade_db}`")
    w(f"- **Signal DB:** `{signal_db}`")
    w(f"- **Сгенерирован:** {_now_utc()} UTC")

    w("")
    w("## 1. Конфигурация окна (что было разблокировано)")
    w("")
    w("| Параметр | Было | Стало |")
    w("|---|---|---|")
    w("| deployment.mode | research | demo_systematic |")
    w("| execution.enabled_assets | [] | BTCUSD, XAUUSD, XAGUSD, EURUSD, GBPUSD |")
    w("| XAGUSD.enabled | false | true |")
    w("| max_open_positions_per_asset | 1 | 2 |")
    w("| max_concurrent_positions_global | 3 | 6 |")
    w("| max_daily_trades_per_asset | 10 | 20 |")
    w("| alerts.cooldown_minutes | 15 | 5 |")
    w("| alerts.max_alerts_per_day | 30 | 60 |")
    w("| risk.risk_per_trade_pct | 0.0025 | 0.01 |")
    w("| risk.cluster_risk_cap | 0.004 | 0.02 |")
    w("| risk.total_open_risk_cap | 0.0075 | 0.03 |")
    w("")
    w("Per-asset параметры (бары уверенности, сетки, таймфреймы) НЕ менялись.")

    # ---- Ledger integrity ----
    w("")
    w("## 2. Целостность immutable-леджера")
    w("")
    try:
        chain_ok = verify_event_chain(trade_db)
        w(f"- verify_event_chain: {'✅ VALID' if chain_ok else '❌ BROKEN'}")
    except Exception as exc:
        w(f"- verify_event_chain: ❌ error — {exc}")

    # ---- Trading events ----
    w("")
    w("## 3. События торгового леджера за окно")
    w("")
    try:
        events = read_trading_events(trade_db)
        in_window = (
            events[(events["event_timestamp_utc"] >= start_ts) & (events["event_timestamp_utc"] <= end_ts)]
            if len(events)
            else events
        )
        if len(in_window) == 0:
            w("Событий за окно нет.")
        else:
            counts = in_window.groupby(["asset_key", "event_type"]).size().reset_index(name="n")
            w("| Актив | Тип события | Кол-во |")
            w("|---|---|---|")
            for _, row in counts.iterrows():
                w(f"| {row['asset_key']} | {row['event_type']} | {int(row['n'])} |")
            closed = in_window[in_window["event_type"] == "position_closed"]
            if len(closed):
                w("")
                w("### 3.1 Закрытые позиции (position_closed, из леджера)")
                w("")
                w("| Время (UTC) | Актив | Ticket | Signal | PnL | Reason |")
                w("|---|---|---|---|---|---|")
                for _, row in closed.iterrows():
                    payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
                    pnl = payload.get("realized_pnl")
                    w(
                        f"| {_fmt_ts(row['event_timestamp_utc'])} | {row['asset_key']} | "
                        f"{row['position_ticket']} | {row['signal_id']} | {pnl} | {row['reason'] or ''} |"
                    )
    except Exception as exc:
        w(f"Ошибка чтения леджера: {exc}")

    # ---- Executed trades (trade_logger) ----
    w("")
    w("## 4. Сделки (executed_trades) за окно")
    w("")
    try:
        trades = read_executed_trades(trade_db)
        trades = trades[trades["entry_time"] >= start_ts] if len(trades) else trades
        if len(trades) == 0:
            w("Сделок за окно нет.")
        else:
            w("| Ticket | Символ | Направление | Вход (UTC) | Выход (UTC) | Вход | Выход | PnL | Исход |")
            w("|---|---|---|---|---|---|---|---|---|")
            for _, t in trades.iterrows():
                w(
                    f"| {t['ticket']} | {t['symbol']} | {t['bias']} | {_fmt_ts(t['entry_time'])} | "
                    f"{_fmt_ts(t.get('close_time'))} | {t['entry_price']} | {t.get('close_price') or '—'} | "
                    f"{t.get('pnl') or '—'} | "
                    f"{'WIN' if t.get('outcome') == 1 else 'LOSS' if t.get('outcome') == 0 else 'OPEN'} |"
                )
            w("")
            stats = trades[trades["pnl"].notna()]
            wins = stats[stats["pnl"] >= 0]
            losses = stats[stats["pnl"] < 0]
            total_pnl = float(stats["pnl"].sum()) if len(stats) else 0.0
            gross_win = float(wins["pnl"].sum()) if len(wins) else 0.0
            gross_loss = float(losses["pnl"].sum()) if len(losses) else 0.0
            w("### 4.1 Итоговая статистика")
            w("")
            w(
                f"- Всего сделок: **{len(trades)}** (закрыто: {len(stats)}, "
                f"открыто на конец окна: {len(trades) - len(stats)})"
            )
            w(f"- Win rate: **{(100 * len(wins) / len(stats)):.1f}%**" if len(stats) else "- Win rate: —")
            w(f"- Общий PnL: **${total_pnl:+.2f}**")
            w(f"- Средний выигрыш: ${(gross_win / len(wins)):+.2f}" if len(wins) else "- Средний выигрыш: —")
            w(f"- Средний проигрыш: ${(gross_loss / len(losses)):+.2f}" if len(losses) else "- Средний проигрыш: —")
            w(
                f"- Profit factor: **{(gross_win / abs(gross_loss)):.2f}**"
                if gross_loss
                else "- Profit factor: ∞ (нет убытков)"
            )
            w("")
            w("### 4.2 По активам")
            w("")
            w("| Актив | Сделок | Win rate | PnL |")
            w("|---|---|---|---|")
            for symbol, grp in trades.groupby("symbol"):
                gstats = grp[grp["pnl"].notna()]
                gw = len(gstats[gstats["pnl"] >= 0])
                gp = float(gstats["pnl"].sum()) if len(gstats) else 0.0
                wr = f"{100 * gw / len(gstats):.1f}%" if len(gstats) else "—"
                w(f"| {symbol} | {len(grp)} | {wr} | ${gp:+.2f} |")
    except Exception as exc:
        w(f"Ошибка чтения executed_trades: {exc}")

    # ---- Execution ledger ----
    w("")
    w("## 5. Попытки исполнения (execution_ledger)")
    w("")
    try:
        exec_df = read_execution_ledger(trade_db)
        if len(exec_df) == 0:
            w("Попыток исполнения за окно нет.")
        else:
            col = "recorded_at_ms" if "recorded_at_ms" in exec_df.columns else None
            if col is not None:
                exec_df = exec_df[(exec_df[col] >= start_ts * 1000) & (exec_df[col] <= end_ts * 1000)]
            if len(exec_df) == 0:
                w("Попыток исполнения за окно нет.")
            else:
                w(f"- Всего попыток: {len(exec_df)}")
                for c in ("asset_key", "retcode"):
                    if c in exec_df.columns:
                        w("")
                        w(f"### По {c}")
                        w("")
                        counts = exec_df[c].value_counts()
                        for k, v in counts.items():
                            w(f"- {k}: {int(v)}")
                if "spread_points" in exec_df.columns:
                    sp = exec_df["spread_points"].dropna()
                    if len(sp):
                        w("")
                        w(f"- Spread points: min={sp.min():.1f} med={sp.median():.1f} max={sp.max():.1f}")
                if "latency_ms" in exec_df.columns:
                    lat = exec_df["latency_ms"].dropna()
                    if len(lat):
                        w(f"- Latency ms: min={lat.min():.0f} med={lat.median():.0f} max={lat.max():.0f}")
    except Exception as exc:
        w(f"Ошибка чтения execution_ledger: {exc}")

    # ---- Signals ----
    w("")
    w("## 6. Сигналы за окно (signal_log)")
    w("")
    try:
        from data.signal_log import init_schema as init_signal_schema

        init_signal_schema(signal_db)
        sig = read_signal_history(signal_db, start_ts=start_ts, end_ts=end_ts)
        if len(sig) == 0:
            w("Сигналов за окно нет.")
        else:
            w(f"- Всего сигналов: {len(sig)}")
            for symbol, grp in sig.groupby("symbol"):
                conf = grp["confidence"].dropna()
                w(
                    f"- **{symbol}**: {len(grp)} сигналов · bias: "
                    f"{grp['bias'].value_counts().to_dict()} · avg conf: "
                    f"{float(conf.mean()):.3f}"
                    if len(conf)
                    else f"- **{symbol}**: {len(grp)} сигналов"
                )
    except Exception as exc:
        w(f"Ошибка чтения signal_log: {exc}")

    # ---- Open positions at window end ----
    w("")
    w("## 7. Позиции, открытые на конец окна")
    w("")
    try:
        trades = read_executed_trades(trade_db)
        opened = trades[trades["entry_time"] >= start_ts] if len(trades) else trades
        open_pos = opened[opened["pnl"].isna()] if len(opened) else opened
        if len(open_pos) == 0:
            w("Открытых позиций на конец окна нет.")
        else:
            for _, t in open_pos.iterrows():
                w(
                    f"- Ticket {t['ticket']} · {t['symbol']} · {t['bias']} · "
                    f"вход {_fmt_ts(t['entry_time'])} @ {t['entry_price']}"
                )
    except Exception as exc:
        w(f"Ошибка: {exc}")

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(REPORT_DIR, f"TRIAL_WINDOW_REPORT_{ts}.md")
    _write_text(report_path, "\n".join(lines) + "\n")
    _log(f"Report written: {report_path}")
    return report_path


def cmd_start(args) -> int:
    if load_state() and not args.force:
        print("Trial window already active. Use --force to re-start.")
        return 1
    existing = (
        sorted(
            os.path.join(BACKUP_DIR, name)
            for name in os.listdir(BACKUP_DIR)
            if name.startswith("config.yaml.pre_trial_48h_") and name.endswith(".yaml")
        )
        if os.path.isdir(BACKUP_DIR)
        else []
    )
    snap = existing[0] if existing else snapshot_config()
    statuses = apply_trial_config()
    _log("Trial config applied:")
    for s in statuses:
        _log(f"  {s}")
    started = datetime.now(UTC)
    ends = started + timedelta(hours=args.hours)
    state = {
        "snapshot": snap,
        "started_at_utc": started.isoformat(),
        "ends_at_utc": ends.isoformat(),
        "hours": args.hours,
        "reverted": False,
        "report_path": None,
        "edits": statuses,
    }
    save_state(state)
    _log(f"Trial window started: {state['started_at_utc']} -> {state['ends_at_utc']} UTC")
    print(f"Trial window active until {ends.isoformat()} UTC. Start the trader now:")
    print("  python -m execution.mt5_trader")
    print(f"Watcher: python -m scripts.trial_window watch   (state: {STATE_PATH})")
    return 0


def cmd_watch(args) -> int:
    state = load_state()
    if not state:
        print("No active trial window. Run `start` first.")
        return 1
    _log(f"Watcher started; ends_at={state['ends_at_utc']}")
    while True:
        _write_text(HEARTBEAT_PATH, f"alive {_now_utc()} ends_at={state['ends_at_utc']}\n")
        now = datetime.now(UTC)
        end = datetime.fromisoformat(state["ends_at_utc"])
        if now >= end:
            _log("Window ended; finalizing (stop trader -> report -> revert config)")
            stopped = stop_trader()
            _log(f"Trader processes stopped: {stopped or 'none'}")
            time.sleep(5)
            report = generate_report(state)
            state["report_path"] = report
            state["reverted"] = True
            save_state(state)
            shutil.copy2(state["snapshot"], CONFIG_PATH)
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
            _log(f"Config reverted to {state['snapshot']}")
            _log(f"REPORT: {report}")
            print(f"REVERTED. Report: {report}")
            return 0
        time.sleep(60)


def cmd_report(args) -> int:
    state = load_state()
    if not state:
        print("No active trial window state; generating from defaults.")
        state = {"started_at_utc": _now_utc(), "ends_at_utc": _now_utc(), "hours": 48, "snapshot": "unknown"}
    path = generate_report(state)
    print(f"Report: {path}")
    return 0


def cmd_revert(args) -> int:
    state = load_state()
    if not state:
        print("No trial window state; nothing to revert.")
        return 1
    shutil.copy2(state["snapshot"], CONFIG_PATH)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        yaml.safe_load(f)
    state["reverted"] = True
    save_state(state)
    _log(f"Config manually reverted to {state['snapshot']}")
    print(f"Config restored from {state['snapshot']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="48h full-power trial window (demo).")
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start", help="snapshot + apply trial config")
    p_start.add_argument("--hours", type=float, default=48)
    p_start.add_argument("--force", action="store_true")
    p_start.set_defaults(func=cmd_start)
    p_watch = sub.add_parser("watch", help="wait until the window ends, then revert + report")
    p_watch.set_defaults(func=cmd_watch)
    p_report = sub.add_parser("report", help="generate the report now")
    p_report.set_defaults(func=cmd_report)
    p_revert = sub.add_parser("revert", help="restore the original config now")
    p_revert.set_defaults(func=cmd_revert)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
