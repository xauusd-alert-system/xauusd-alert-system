"""
Retrain all models based on config.yaml.
- If retraining.enabled == false, exit silently.
- If max_age_hours is exceeded, force retrain.
- Downloads fresh data first if download_data is true.
"""
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("retrain_models")


def get_model_age(model_path: str) -> float:
    """Return age of model file in hours, or -1 if does not exist."""
    if not os.path.exists(model_path):
        return -1.0
    mtime = os.path.getmtime(model_path)
    age_hours = (time.time() - mtime) / 3600.0
    return age_hours


def main():
    cfg = load_config()
    rt_cfg = cfg.get("retraining", {})
    if not rt_cfg.get("enabled", True):
        logger.info("Retraining disabled by config. Exiting.")
        return

    assets = rt_cfg.get("assets", [])
    max_age_hours = rt_cfg.get("max_age_hours", 24)
    retrain_on_startup = rt_cfg.get("retrain_on_startup", False)
    download_data = rt_cfg.get("download_data", True)
    lookback_days = rt_cfg.get("lookback_days", 730)
    db_path = cfg["general"]["db_path"]

    # Проверка: нужно ли переобучать? Если модели достаточно свежие и это не принудительный запуск
    need_retrain = retrain_on_startup
    if not need_retrain:
        for asset_key in assets:
            model_path = cfg["assets"][asset_key]["model_path"]
            age = get_model_age(model_path)
            if age < 0 or age > max_age_hours:
                need_retrain = True
                break

    if not need_retrain:
        logger.info("All models are up to date. Skipping retraining.")
        return

    logger.info("Retraining required. Starting pipeline...")

    # 1. Скачивание свежих данных
    if download_data:
        logger.info("Downloading fresh market data...")
        start_date = "2020-01-01"  # здесь можно взять lookback_days, но для простоты используем 2020
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # backfill_data --all now resolves per-asset timeframes automatically,
        # but we still pass --timeframe=None to let it use the config chain.
        download_cmd = [
            sys.executable, "-m", "scripts.backfill_data",
            "--all", "--start", start_date, "--end", end_date
        ]
        logger.info(f"Running: {download_cmd}")
        subprocess.run(download_cmd, check=True)
        logger.info("Data download finished.")
    else:
        logger.info("Data download skipped (configured).")

    # 2. Обучение моделей
    logger.info("Starting model training...")
    train_cmd = [sys.executable, "-m", "scripts.train_all_assets"]
    subprocess.run(train_cmd, check=True)

    logger.info("Retraining completed successfully.")


if __name__ == "__main__":
    main()
