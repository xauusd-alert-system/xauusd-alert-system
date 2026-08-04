"""
Scheduler/worker loop: runs the realtime pipeline on every newly closed candle of
the primary labeling timeframe, logs every signal (regardless of alert outcome) to
SQLite, and sends a Telegram alert only if the ensemble + bot gating logic qualifies.

Design decision - candle-aligned scheduling, not fixed-interval polling:
Running strictly on a fixed interval (e.g. every 60s) would waste API calls and risk
scoring a candle mid-formation. Instead, this scheduler computes the exact UTC second
when the NEXT candle boundary closes (e.g. next M15 mark) and sleeps until shortly
after that boundary, guaranteeing every run scores a fully closed candle - consistent
with the "never score an in-progress candle" guarantee in realtime/pipeline.py.

Runs as a long-lived process (`python -m scripts.run_scheduler`), intended to be
supervised by systemd/Docker/pm2 in production - this module itself does not daemonize.
"""
import time
import logging
from datetime import datetime, timezone

from config.loader import load_config, get_env
from data.ingestion import TIMEFRAME_TO_SECONDS
from data.signal_log import init_schema, log_signal
from realtime.pipeline import RealtimePipeline
from alerts.telegram_bot import TelegramAlertBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("scheduler")


def seconds_until_next_candle_close(timeframe: str, buffer_seconds: int = 5) -> float:
    """
    Computes how long to sleep so the NEXT wakeup happens `buffer_seconds` after the
    next candle boundary closes (giving the data vendor time to finalize the candle).
    """
    step = TIMEFRAME_TO_SECONDS[timeframe]
    now = time.time()
    next_boundary = ((int(now) // step) + 1) * step
    return max(0.0, (next_boundary - now) + buffer_seconds)


def run_once(pipeline: RealtimePipeline, bot: TelegramAlertBot, db_path: str, n_candles: int) -> dict:
    """
    Single pipeline pass: generate signal, log it unconditionally, send alert if qualified.
    Never raises out of this function during the main loop - errors are logged and
    the loop continues to the next scheduled candle rather than crashing the process.
    """
    signal = pipeline.generate_signal(n_candles=n_candles)
    alert_sent = bot.send_alert_if_qualified(signal)
    log_signal(db_path, signal, alert_sent, symbol=pipeline.asset_key)

    logger.info(
        "Signal generated: bias=%s confidence=%.3f regime=%s alert_sent=%s",
        signal["bias"], signal["confidence"], signal["regime"], alert_sent,
    )
    return signal


def main():
    cfg = load_config()
    timeframe = cfg["market_data"]["timeframe"]
    data_mode = get_env("DATA_MODE", default="mock")
    model_path = get_env("MODEL_PATH", default=None)
    db_path = get_env("SIGNAL_LOG_DB_PATH", default="data/signal_log.db")
    n_candles = int(get_env("PIPELINE_N_CANDLES", default="300"))

    init_schema(db_path)
    pipeline = RealtimePipeline(cfg=cfg, model_path=model_path, data_mode=data_mode)
    bot = TelegramAlertBot(cfg)

    logger.info("Scheduler starting. timeframe=%s data_mode=%s model_loaded=%s",
                timeframe, data_mode, pipeline._predictor is not None)

    while True:
        sleep_s = seconds_until_next_candle_close(timeframe)
        logger.info("Sleeping %.1fs until next %s candle close...", sleep_s, timeframe)
        time.sleep(sleep_s)

        try:
            run_once(pipeline, bot, db_path, n_candles)
        except Exception as e:
            logger.exception("Pipeline run failed, will retry on next scheduled candle: %s", e)


if __name__ == "__main__":
    main()
