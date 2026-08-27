"""
Unified launcher: starts the virtual simulation + Telegram control bot
then runs the real MultiAssetMT5Trader.run_loop() in the main thread.

Usage
-----
    python -m scripts.run_bot

    # Optional env overrides:
    DRY_RUN=1                       # log orders, don't send
    SIMULATION_SEED=42              # reproducible warm-up
    SIMULATION_WARMUP_TICKS=5000   # default 5000

Telegram commands once running
------------------------------
    /start       welcome
    /help        list commands
    /status      trader mode + open positions with P&L in $ and R (read-only)
    /positions   all open positions with PnL (read-only)
    /why ASSET   why the position was opened — verbatim entry context (read-only)
    /metrics     institutional SMC metrics; /metrics today|week closed-trade stats
    /account     balance/equity/margin/floating + today's realized P&L (read-only)
    /pause       switch to dry-run (no live orders)
    /resume      switch back to live
    /closeall    emergency close all positions
"""
import os
import sys
import logging

# --- sys.path injection (must come first) ---
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _PROJECT_ROOT)
_shim_dir = os.path.join(_PROJECT_ROOT, "simulation", "mt5_shim")
if _shim_dir not in sys.path:
    sys.path.insert(0, _shim_dir)

import threading

from simulation.simulator import MarketSimulator, load_simulation_config, shutdown_mt5_shim
# NOTE: import the shim under its plain top-level name (see run_simulation.py
# comment) so _inject() lands on the module object the trader actually uses.
import MetaTrader5 as mt5  # noqa: E402  (resolves to the shim via sys.path)
from simulation.virtual_state import VirtualState
from alerts.control_bot import TelegramControlBot
from scripts.run_simulation import SimulationDriver, _bar_timestamp, build_virtual_cfg  # reuse driver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("run_bot")


def _resolve_db_path() -> str:
    """Main SQLite path per project convention (config general.db_path)."""
    try:
        from config.loader import load_config

        return str(
            load_config().get("general", {}).get(
                "db_path", "data/market_data_mt5.sqlite"
            )
        )
    except Exception:
        return "data/market_data_mt5.sqlite"


def _apply_db_migrations() -> None:
    """Run versioned schema migrations on the main DB before trading (ТЗ 9.3).

    Failures are fatal: trading on an unverified/partially migrated schema is
    unsafe, so the bot refuses to start.
    """
    from data.migrate import apply_migrations

    db_path = _resolve_db_path()
    applied = apply_migrations(db_path)
    logger.info("DB migrations applied (%d) to %s", len(applied), db_path)


def main() -> None:
    _apply_db_migrations()

    cfg = build_virtual_cfg()

    # 1. Build & warm up the virtual market.
    seed = os.getenv("SIMULATION_SEED")
    sim = MarketSimulator(cfg=cfg, seed=int(seed) if seed else None)
    sim.warm_up(n_ticks=int(os.getenv("SIMULATION_WARMUP_TICKS", "5000")))

    state = VirtualState(cfg)
    mt5._inject(state, sim, cfg)
    os.environ["DATA_MODE"] = "live"

    # 2. Start the simulation driver (advances bars in background).
    driver = SimulationDriver(sim, m5_interval_seconds=1.0)
    driver.start()

    # 3. Build the real trader.
    from execution.mt5_trader import MultiAssetMT5Trader
    trader = MultiAssetMT5Trader()

    # 4. Start the Telegram control bot in a daemon thread.
    control_bot = TelegramControlBot(trader)
    control_bot.start()
    logger.info(
        "\n"
        "=================================================\n"
        " Telegram Control Bot running. Commands:\n"
        "   /status  /why <ASSET>  /metrics [today|week]  /account\n"
        "   /positions  /pause  /resume  /closeall\n"
        "================================================="
    )

    # 5. Run the trading loop in the main thread (blocking).
    try:
        logger.info("Starting MultiAssetMT5Trader.run_loop() ...")
        trader.run_loop()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down.")
    finally:
        control_bot.stop()
        driver.stop()
        shutdown_mt5_shim()

    # Final summary
    account = state.account_info()
    if account is not None:
        profit = account.equity - cfg.get("virtual_balance", 10000.0)
        logger.info("=" * 50)
        logger.info("FINAL: balance=%.2f equity=%.2f profit=%+.2f mid=%.2f",
                    account.balance, account.equity, profit, sim.current_mid_price)
        logger.info("=" * 50)


if __name__ == "__main__":
    main()
