"""
Overnight self-improvement pipeline.

Runs the full "retrain & validate while you sleep" routine in sequence. Every
stage is a thin wrapper around an existing, tested entry point in this repo, so
a failure in one stage is isolated and the rest of the night keeps going.

    Stage 1  backfill_fresh_data      -> scripts.backfill_data
             Refresh the most recent MT5 candles so training/backtest see
             up-to-date bars (incremental window, idempotent upsert).
             Requires a running FxPro MT5 terminal (config market_data.provider: mt5).

    Stage 2  walk_forward_backtest    -> scripts.run_backtest
             Walk-forward backtest of each enabled asset; writes
             logs/backtest_<asset>.csv. Gives a health check that the model
             didn't regress after retraining.

    Stage 3  retrain_models           -> scripts.train_all_assets
             Fresh retrain of every enabled asset on the refreshed history.

    Stage 3b deploy_guard_backup      -> scripts.deploy_guard --backup
             (Part B Phase 6 / audit #25) Snapshot each enabled asset's current
             production model to <model_path>.deploy_guard.bak BEFORE the
             retrains below can overwrite them. Idempotent across nights; a
             failed backup fails the night (you cannot protect what was not
             backed up).

    Stage 4  retrain_with_real_trades -> scripts.retrain_with_real_trades
             Final retrain that also folds real executed trades (from the
             executed_trades table) into the training set. This leaves the
             best, most up-to-date model files on disk.

    Stage 4b deploy_guard_check       -> scripts.deploy_guard --check
             (Part B Phase 6 / audit #25) Walk-forward validates each newly
             retrained model against its nightly backup on the SAME freshly
             backfilled out-of-sample windows. If a new model is no better
             than (or regressed beyond tolerance of) the incumbent, the backed-
             up (good) model is restored and the stage exits 1 -> the night is
             reported FAILED / Telegram ❌. A bad night can never silently
             replace a good production model.

    Stage 5  summary_report           -> scripts.summary_report
             Aggregates logs/backtest_*.csv into a portfolio summary.

    Stage 6  telegram_notify          -> alerts.telegram_bot
             Sends the portfolio summary to Telegram (skipped if
             TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not configured).

Environment knobs (all optional, every stage on by default):

    OVERNIGHT_BACKFILL_DAYS=45     how many recent days of fresh MT5 data to pull
    OVERNIGHT_NO_BACKFILL=1        skip stage 1
    OVERNIGHT_NO_BACKTEST=1        skip stage 2
    OVERNIGHT_NO_RETRAIN=1         skip stage 3
    OVERNIGHT_NO_DEPLOY_GUARD=1    skip stages 3b/4b (deploy guard, Part B Phase 6)
    OVERNIGHT_NO_REAL_TRADES=1     skip stage 4
    OVERNIGHT_NO_SUMMARY=1         skip stage 5
    OVERNIGHT_NO_TELEGRAM=1        skip stage 6

Run:
    python -m scripts.overnight
"""
import os
import sys
import glob
import subprocess
import logging
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config, get_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("overnight")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _env_flag(name: str) -> bool:
    """Return True unless the env var is set to a truthy 'skip' value."""
    return get_env(name, default="0") not in {"1", "true", "yes", "on"}


# Per-stage wall-clock timeouts so a hung subprocess cannot block the night.
_DEFAULT_STAGE_TIMEOUT = 3600  # 1h for slow backtests / retrains
_BACKFILL_TIMEOUT = 900  # 15 min


def _run(stage: str, cmd: list, timeout: int = None) -> bool:
    """Run a stage as a subprocess with a wall-clock timeout.

    Returns True on success, False on any failure (logged, not fatal), so one
    hung/stuck stage cannot block or kill the rest of the overnight pipeline.
    On timeout the whole child process tree is killed.
    """
    if timeout is None:
        timeout = _DEFAULT_STAGE_TIMEOUT
    logger.info("=== STAGE: %s ===", stage)
    logger.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        if result.stdout and result.stdout.strip():
            logger.info("  %s", result.stdout.strip().splitlines()[-1][-400:])
        logger.info("=== STAGE OK: %s ===", stage)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("=== STAGE FAILED (exit=%s): %s ===", e.returncode, stage)
        if e.stdout and e.stdout.strip():
            logger.error("  %s", e.stdout.strip().splitlines()[-1][-400:])
        if e.stderr and e.stderr.strip():
            logger.error("  %s", e.stderr.strip().splitlines()[-1][-400:])
    except subprocess.TimeoutExpired as e:
        logger.error("=== STAGE TIMED OUT after %ss: %s ===", timeout, stage)
        _kill_process_tree(e)
    return False


def _capture(cmd: list, timeout: int = None) -> str:
    """Run a stage and capture its stdout for the final summary."""
    try:
        if timeout is None:
            timeout = _DEFAULT_STAGE_TIMEOUT
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            check=True,
            timeout=timeout,
            capture_output=True,
            text=True,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired as e:
        logger.error("Timed out capturing output of %s: %s", " ".join(cmd), e)
        _kill_process_tree(e)
        return ""
    except Exception as e:  # noqa: BLE001 - summary must never crash the pipeline
        logger.error("Could not capture output of %s: %s", " ".join(cmd), e)
        return ""


def _kill_process_tree(expired: subprocess.TimeoutExpired) -> None:
    """Best-effort kill of the timed-out child and its process group.

    ``subprocess.run`` already waits for the process on timeout, but on Windows
    a spawned console app (python -m ...) may leave grandchildren running.
    Using CREATE_NEW_PROCESS_GROUP lets us terminate the whole tree so a hung
    stage cannot linger after the overnight pipeline has moved on.
    """
    pid = getattr(expired, "process", None)
    if pid is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid.pid)],
                capture_output=True,
                timeout=30,
            )
        else:
            try:
                os.killpg(os.getpgid(pid.pid), 9)
            except ProcessLookupError:
                pass
    except Exception as e:  # noqa: BLE001 - cleanup must never raise
        logger.warning("Could not kill timed-out process %s: %s", pid.pid, e)


def _backfill_window() -> tuple[str, str]:
    days = int(get_env("OVERNIGHT_BACKFILL_DAYS", default="45"))
    today = datetime.now(timezone.utc)
    start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    return start, end


def main() -> int:
    cfg = load_config()
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    timeframe = cfg.get("market_data", {}).get("timeframe", "M5")
    enabled_assets = [
        k for k, v in cfg.get("assets", {}).items() if v.get("enabled", False)
    ]
    retraining_enabled = bool(cfg.get("retraining", {}).get("enabled", True))
    if not retraining_enabled:
        logger.warning(
            "Retraining safety freeze is active: backup/retrain/real-trade/deploy-check "
            "stages will be skipped until pre-lock baselines are rebuilt."
        )

    status: list[str] = []

    # ---- Stage 1: backfill fresh MT5 data (incremental) --------------------
    if _env_flag("OVERNIGHT_NO_BACKFILL"):
        start, end = _backfill_window()
        # Backfill every timeframe any enabled asset trades on (global M5 plus
        # per-asset overrides, e.g. EURUSD/GBPUSD/XAGUSD on M15).
        trade_tfs = sorted(
            {
                (a_cfg.get("timeframe") or timeframe)
                for a_cfg in cfg.get("assets", {}).values()
                if a_cfg.get("enabled", False)
            }
        )
        ok = True
        for tf in trade_tfs:
            ok = _run(
                f"backfill_fresh_data:{tf}",
                [
                    sys.executable, "-m", "scripts.backfill_data",
                    "--all", "--timeframe", tf,
                    "--start", start, "--end", end,
                    "--db-path", db_path,
                ],
                timeout=_BACKFILL_TIMEOUT,
            ) and ok
        status.append(("backfill_data", ok))
    else:
        logger.info("Skipping backfill (OVERNIGHT_NO_BACKFILL set).")

    # ---- Stage 2: walk-forward backtest per asset --------------------------
    if _env_flag("OVERNIGHT_NO_BACKTEST"):
        all_ok = True
        for asset in enabled_assets:
            asset_tf = cfg["assets"][asset].get("timeframe") or timeframe
            ok = _run(
                f"walk_forward_backtest:{asset}",
                [
                    sys.executable, "-m", "scripts.run_backtest",
                    "--asset", asset,
                    "--timeframe", asset_tf,
                    "--db-path", db_path,
                ],
            )
            all_ok = all_ok and ok
        status.append(("walk_forward_backtest", all_ok))
    else:
        logger.info("Skipping walk-forward backtest (OVERNIGHT_NO_BACKTEST set).")

    # ---- Stage 3b: back up current production models (Part B Phase 6, #25) ---
    if retraining_enabled and _env_flag("OVERNIGHT_NO_DEPLOY_GUARD"):
        ok = _run(
            "deploy_guard_backup",
            [sys.executable, "-m", "scripts.deploy_guard", "--backup"],
        )
        status.append(("deploy_guard_backup", ok))
    else:
        logger.info("Skipping deploy_guard backup (safety freeze or env skip).")

    # ---- Stage 3: fresh retrain of all assets ------------------------------
    if retraining_enabled and _env_flag("OVERNIGHT_NO_RETRAIN"):
        ok = _run(
            "retrain_models",
            [sys.executable, "-m", "scripts.train_all_assets"],
        )
        status.append(("retrain_models", ok))
    else:
        logger.info("Skipping retrain (safety freeze or env skip).")

    # ---- Stage 4: final retrain with real executed trades ------------------
    if retraining_enabled and _env_flag("OVERNIGHT_NO_REAL_TRADES"):
        ok = _run(
            "retrain_with_real_trades",
            [sys.executable, "-m", "scripts.retrain_with_real_trades"],
        )
        status.append(("retrain_with_real_trades", ok))
    else:
        logger.info("Skipping retrain_with_real_trades (safety freeze or env skip).")

    # ---- Stage 4b: deploy guard - reject a regressing nightly model --------
    # (Part B Phase 6, #25) Walk-forward-validate the freshly retrained model
    # against the backup from Stage 3b on the SAME OOS windows; if it regressed
    # beyond tolerance, restore the incumbent so a bad night cannot overwrite a
    # good production model. Exit code 1 => stage failed => Telegram ❌.
    if retraining_enabled and _env_flag("OVERNIGHT_NO_DEPLOY_GUARD"):
        ok = _run(
            "deploy_guard_check",
            [sys.executable, "-m", "scripts.deploy_guard", "--check"],
        )
        status.append(("deploy_guard_check", ok))
    else:
        logger.info("Skipping deploy_guard check (safety freeze or env skip).")

    # ---- Stage 5: summary report -------------------------------------------
    summary_text = ""
    if _env_flag("OVERNIGHT_NO_SUMMARY"):
        summary_text = _capture([sys.executable, "-m", "scripts.summary_report"])
        if summary_text:
            status.append(("summary_report", True))
            print("\n" + summary_text + "\n")
        else:
            status.append(("summary_report", False))
    else:
        logger.info("Skipping summary_report (OVERNIGHT_NO_SUMMARY set).")

    # ---- Stage 6: Telegram notification -------------------------------------
    if _env_flag("OVERNIGHT_NO_TELEGRAM"):
        _notify_telegram(cfg, summary_text, status)
    else:
        logger.info("Skipping Telegram notify (OVERNIGHT_NO_TELEGRAM set).")

    failed = [name for name, ok in status if not ok]
    if failed:
        logger.warning("Overnight run finished with FAILED stages: %s", failed)
        return 1
    logger.info("Overnight run finished OK (%d stages).", len(status))
    return 0


def _notify_telegram(cfg: dict, summary_text: str, status: list) -> None:
    """Send a short overnight report to Telegram. Non-fatal."""
    try:
        from alerts.telegram_bot import TelegramAlertBot
    except Exception as e:  # noqa: BLE001
        logger.warning("Telegram unavailable (%s); skipping notify.", e)
        return

    lines = ["🛏️ *Ночная прокачка модели — отчёт*"]
    for name, ok in status:
        emoji = "✅" if ok else "❌"
        lines.append(f"{emoji} {name}: {'ok' if ok else 'FAILED'}")
    if summary_text:
        lines.append("\n```\n" + summary_text + "\n```")

    bot = TelegramAlertBot(cfg)
    if bot.send_text_message("\n".join(lines)):
        logger.info("Telegram summary sent.")
    else:
        logger.warning(
            "Telegram summary not sent (set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)."
        )


if __name__ == "__main__":
    raise SystemExit(main())
