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

    Stage 4  retrain_with_real_trades -> scripts.retrain_with_real_trades
             Final retrain that also folds real executed trades (from the
             executed_trades table) into the training set. This leaves the
             best, most up-to-date model files on disk.

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


def _run(stage: str, cmd: list, timeout: int = None) -> bool:
    """Run a stage as a subprocess. Returns True on success (failure is logged, not fatal)."""
    logger.info("=== STAGE: %s ===", stage)
    logger.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            check=True,
            timeout=timeout,
        )
        logger.info("=== STAGE OK: %s ===", stage)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("=== STAGE FAILED (exit=%s): %s ===", e.returncode, stage)
    except subprocess.TimeoutExpired:
        logger.error("=== STAGE TIMED OUT: %s ===", stage)
    return False


def _capture(cmd: list, timeout: int = None) -> str:
    """Run a stage and capture its stdout for the final summary."""
    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            check=True,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception as e:  # noqa: BLE001 - summary must never crash the pipeline
        logger.error("Could not capture output of %s: %s", " ".join(cmd), e)
        return ""


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

    status: list[str] = []

    # ---- Stage 1: backfill fresh MT5 data (incremental) --------------------
    if _env_flag("OVERNIGHT_NO_BACKFILL"):
        start, end = _backfill_window()
        ok = _run(
            "backfill_fresh_data",
            [
                sys.executable, "-m", "scripts.backfill_data",
                "--all", "--timeframe", timeframe,
                "--start", start, "--end", end,
                "--db-path", db_path,
            ],
        )
        status.append(("backfill_data", ok))
    else:
        logger.info("Skipping backfill (OVERNIGHT_NO_BACKFILL set).")

    # ---- Stage 2: walk-forward backtest per asset --------------------------
    if _env_flag("OVERNIGHT_NO_BACKTEST"):
        all_ok = True
        for asset in enabled_assets:
            ok = _run(
                f"walk_forward_backtest:{asset}",
                [
                    sys.executable, "-m", "scripts.run_backtest",
                    "--asset", asset,
                    "--timeframe", timeframe,
                    "--db-path", db_path,
                ],
            )
            all_ok = all_ok and ok
        status.append(("walk_forward_backtest", all_ok))
    else:
        logger.info("Skipping walk-forward backtest (OVERNIGHT_NO_BACKTEST set).")

    # ---- Stage 3: fresh retrain of all assets ------------------------------
    if _env_flag("OVERNIGHT_NO_RETRAIN"):
        ok = _run(
            "retrain_models",
            [sys.executable, "-m", "scripts.train_all_assets"],
        )
        status.append(("retrain_models", ok))
    else:
        logger.info("Skipping retrain (OVERNIGHT_NO_RETRAIN set).")

    # ---- Stage 4: final retrain with real executed trades ------------------
    if _env_flag("OVERNIGHT_NO_REAL_TRADES"):
        ok = _run(
            "retrain_with_real_trades",
            [sys.executable, "-m", "scripts.retrain_with_real_trades"],
        )
        status.append(("retrain_with_real_trades", ok))
    else:
        logger.info("Skipping retrain_with_real_trades (OVERNIGHT_NO_REAL_TRADES set).")

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
