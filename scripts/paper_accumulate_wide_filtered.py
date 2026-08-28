"""
Paper-trading accumulator for the pre-registered XAUUSD candidate
`wide_trend_filtered`.

Runs the FROZEN production model on live-forward bars (>= 2026-08-08)
with the wide_trend_filtered overrides and saves the resulting CLOSED
paper trades to a separate SQLite table.

Does NOT print performance metrics (PnL/PF/WR). Sends a Telegram status
message with the current trade count and the remaining amount to reach 50.
Metrics stay hidden until the pre-registered threshold is reached.
"""

import os
import sqlite3
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from alerts.telegram_bot import TelegramAlertBot
from config.loader import load_config
from model.ensemble_backtest import EnsembleBacktester
from model.predictor import ModelPredictor
from scripts.deflated_sharpe import _apply_variant
from scripts.run_backtest import build_full_df, load_asset_history

LIVE_START_UTC = "2026-08-08"
PAPER_DB_PATH = os.getenv("PAPER_TRADES_DB_PATH", "data/paper_trades.sqlite")
TARGET_TRADES = 50
# FROZEN MODEL: do NOT use cfg asset.model_path, because nightly retraining
# would change the model mid-accumulation. Use the snapshotted copy.
FROZEN_MODEL_PATH = "output/models/frozen/xauusd_paper_20260815.joblib"


def init_paper_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_key TEXT NOT NULL,
                variant TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_time INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                exit_time INTEGER NOT NULL,
                exit_price REAL NOT NULL,
                pnl REAL NOT NULL,
                exit_reason TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


def clear_paper_trades(db_path: str, asset_key: str, variant: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "DELETE FROM paper_trades WHERE asset_key = ? AND variant = ?",
            (asset_key, variant),
        )
        conn.commit()
    finally:
        conn.close()


def save_paper_trades(db_path: str, asset_key: str, variant: str, trades) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for t in trades:
            if t.exit_ts is None or t.pnl is None:
                continue
            direction = "long" if t.direction == 1 else "short"
            conn.execute(
                """
                INSERT INTO paper_trades
                    (asset_key, variant, direction, entry_time, entry_price,
                     exit_time, exit_price, pnl, exit_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_key,
                    variant,
                    direction,
                    int(t.entry_ts),
                    float(t.entry_price),
                    int(t.exit_ts),
                    float(t.exit_price),
                    float(t.pnl),
                    t.exit_reason,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def send_telegram_status(count: int) -> None:
    """Send a harmless status message with only the trade counter."""
    remaining = max(TARGET_TRADES - count, 0)
    msg = (
        f"📡 Paper XAUUSD wide_trend_filtered\n"
        f"Накоплено сделок: {count}/{TARGET_TRADES}\n"
        f"Осталось до проверки: {remaining}\n"
        f"Метрики скрыты до достижения {TARGET_TRADES}."
    )
    try:
        cfg = load_config()
        bot = TelegramAlertBot(cfg)
        bot.send_text_message(msg)
    except Exception as e:
        print(f"[paper] Telegram status failed (non-critical): {e}")


def main() -> None:
    cfg = load_config()
    asset_key = "XAUUSD"
    variant_name = "wide_trend_filtered"
    timeframe = cfg["assets"][asset_key].get("timeframe", "M15")
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")

    if not os.path.exists(FROZEN_MODEL_PATH):
        print(f"Frozen model not found: {FROZEN_MODEL_PATH}")
        print("Copy the current production model first:")
        print("  New-Item -ItemType Directory -Force output/models/frozen")
        print("  Copy-Item output/models/xauusd_direction_model.joblib "
              "output/models/frozen/xauusd_paper_20260815.joblib")
        return

    print("Loading market data...")
    raw = load_asset_history(db_path, timeframe, asset_key)
    full_df = build_full_df(cfg, raw, db_path=db_path, asset_key=asset_key)

    live_start_ts = int(datetime.fromisoformat(LIVE_START_UTC).replace(tzinfo=UTC).timestamp())
    live_df = full_df[full_df["timestamp_utc"] >= live_start_ts].copy()

    if len(live_df) == 0:
        print("No live-forward bars yet (market closed or no new data).")
        # Still send a status ping so you know the job ran.
        send_telegram_status(0)
        return

    print("Loading frozen model...")
    predictor = ModelPredictor(FROZEN_MODEL_PATH)

    print("Predicting probabilities on live-forward bars...")
    preds = predictor.predict_proba(live_df)
    live_df["ml_p_long"] = preds["p_long"].values
    live_df["ml_p_short"] = preds["p_short"].values

    variant_overrides = {
        "signal_grid": {"stop_mult": 4.0, "breakeven_trigger_atr": 1.0, "tp3_mult": 4.0},
        "ensemble": {"suppress_regimes": ["compression", "reversal_watch", "range", "trend_down"]},
    }
    cfg_paper = _apply_variant(cfg, asset_key, variant_overrides)

    print("Running paper backtest on live-forward data...")
    engine = EnsembleBacktester(cfg_paper, asset_key=asset_key)
    trades = engine.run(live_df.reset_index(drop=True))
    closed_trades = [t for t in trades if t.exit_ts is not None and t.pnl is not None]
    print(f"Closed paper trades: {len(closed_trades)}")

    init_paper_db(PAPER_DB_PATH)
    clear_paper_trades(PAPER_DB_PATH, asset_key, variant_name)
    save_paper_trades(PAPER_DB_PATH, asset_key, variant_name, closed_trades)

    # Send Telegram status with only the counter. No PnL/PF/WR.
    send_telegram_status(len(closed_trades))

    print(f"Saved {len(closed_trades)} paper trades to {PAPER_DB_PATH}")


if __name__ == "__main__":
    main()
