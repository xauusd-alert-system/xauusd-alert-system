"""
Virtual MT5 simulation entry point (FILE 22).

Boots the *real, unmodified* trading stack (``execution.mt5_trader.MultiAssetMT5Trader``)
against the virtual market produced by ``simulation``:

    - injects ``simulation/mt5_shim`` onto ``sys.path`` so ``import MetaTrader5 as mt5``
      inside the protected modules resolves to the fake MT5 package;
    - builds a VirtualState + MarketSimulator, warms it up and wires it into the shim;
    - drives the simulation forward on a daemon thread so new M5 bars keep closing
      (the trader's ``run_loop`` detects a fresh bar via ``copy_rates_from_pos``);
    - sets ``DATA_MODE=live`` so the pipeline reads candles from the virtual MT5 feed.

The only pre-existing code that changes is this 2-line path injection at the very top
(no snapshot of the protected files is touched).
"""

# --- 2-line sys.path injection: the virtual MT5 shim must be importable before any
# --- protected module does `import MetaTrader5 as mt5`.
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
# 1) Make the project root importable (mirrors the existing sys.path hack).
sys.path.insert(0, _PROJECT_ROOT)
# 2) Prepend the shim package so `import MetaTrader5` resolves to our fake module.
_shim_dir = os.path.join(_PROJECT_ROOT, "simulation", "mt5_shim")
if _shim_dir not in sys.path:
    sys.path.insert(0, _shim_dir)

# ----------------------------------------------------------------------
# The rest of the imports intentionally come AFTER the path injection so
# that execution/data imports resolve to the virtual MT5 shim.
# ----------------------------------------------------------------------
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Optional

# NOTE: must import the shim under its plain top-level name so it is the SAME
# module object that `import MetaTrader5 as mt5` in execution/data resolves to
# (a dotted `from simulation.mt5_shim import MetaTrader5` creates a second
# module object and _inject() would be invisible to the protected modules).
import MetaTrader5 as mt5  # noqa: E402  (resolves to the shim via sys.path)

from simulation.simulator import (
    MarketSimulator,
    load_simulation_config,
    shutdown_mt5_shim,
)
from simulation.virtual_state import VirtualState


def build_virtual_cfg() -> dict:
    """Simulation config whose ``symbol_overrides`` also carries the MT5 symbol
    names from the main config (GOLD, SILVER, BITCOIN, EURUSD, GBPUSD).

    The real trader validates orders/candles by ``assets.<key>.mt5_symbol``
    (e.g. ``validate_symbol("GOLD")``), while the shim's VirtualState registers
    symbols from ``symbol_overrides`` (historically keyed by asset key, e.g.
    XAUUSD). Without this bridge the virtual terminal answers "symbol not
    found" for every live symbol.
    """
    import copy

    from config.loader import load_config as load_main_config

    cfg = load_simulation_config()
    overrides = dict(cfg.get("symbol_overrides", {}) or {})
    main_cfg = load_main_config()
    for asset_key, a_cfg in main_cfg.get("assets", {}).items():
        if not a_cfg.get("enabled", True):
            continue
        mt5_sym = a_cfg.get("mt5_symbol")
        if mt5_sym and mt5_sym not in overrides:
            overrides[mt5_sym] = copy.deepcopy(overrides.get(asset_key, {}))
    cfg["symbol_overrides"] = overrides
    return cfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("run_simulation")


# ----------------------------------------------------------------------
# Background driver: advances the virtual market so the trader sees new M5 bars.
# ----------------------------------------------------------------------
class SimulationDriver:
    """Stepped simulator driver.

    Advances the simulation one M5 bar at a time (paced in real time so the
    trader's ``run_loop`` loop, which sleeps 2s per iteration and watches
    ``copy_rates_from_pos`` for a new bar ``time``, keeps discovering fresh
    closed candles).
    """

    def __init__(
        self,
        simulator: MarketSimulator,
        m5_interval_seconds: float = 2.0,
        max_ticks_per_bar: int = 240,
    ) -> None:
        self.simulator = simulator
        self.m5_interval_seconds = float(m5_interval_seconds)
        self.max_ticks_per_bar = int(max_ticks_per_bar)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="simulation-driver",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        logger.info(
            "Simulation driver started: pacing %.1f s per M5 bar",
            self.m5_interval_seconds,
        )
        while not self._stop_event.is_set():
            try:
                bar = self.simulator.advance_to_next_m5_bar(
                    max_ticks=self.max_ticks_per_bar
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.error("Simulation driver step failed: %s", e)
                bar = None
            if bar is not None:
                logger.info(
                    "Virtual M5 bar closed: %s close=%.2f vol=%.1f (mid=%.2f)",
                    _bar_timestamp(bar),
                    bar["close"],
                    bar["volume"],
                    self.simulator.current_mid_price,
                )
            # Pace to ~one M5 bar per m5_interval_seconds; poll stop every 0.1s.
            deadline = time.monotonic() + self.m5_interval_seconds
            while time.monotonic() < deadline and not self._stop_event.is_set():
                time.sleep(0.1)


def _bar_timestamp(bar: dict) -> str:
    """Format a bar's Unix-second ``time`` field as a readable UTC string."""
    try:
        return datetime.fromtimestamp(bar["time"], tz=UTC).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:  # pragma: no cover - defensive
        return str(bar.get("time"))


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main() -> None:
    cfg = build_virtual_cfg()

    # ---- build the simulated world ------------------------------------
    seed = os.getenv("SIMULATION_SEED")
    sim = MarketSimulator(cfg=cfg, seed=int(seed) if seed else None)
    sim.warm_up(n_ticks=int(os.getenv("SIMULATION_WARMUP_TICKS", "5000")))

    state = VirtualState(cfg)

    # ---- wire the shim so the *real* unmodified trader uses it --------
    mt5._inject(state, sim, cfg)
    os.environ["DATA_MODE"] = "live"

    # ---- run the real trading stack in the main thread -----------------
    driver = SimulationDriver(sim, m5_interval_seconds=1.0)
    trader = None
    try:
        driver.start()

        from execution.mt5_trader import MultiAssetMT5Trader

        trader = MultiAssetMT5Trader()
        logger.info("Starting real MultiAssetMT5Trader.run_loop() against the virtual market...")
        trader.run_loop()
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt - shutting down virtual simulation.")
    finally:
        driver.stop()

    # ---- final summary ------------------------------------------------
    account = state.account_info()
    if account is not None:
        profit = account.equity - cfg.get("virtual_balance", 10000.0)
        logger.info("=" * 60)
        logger.info("VIRTUAL RUN FINAL SUMMARY (tick=%d)", sim.tick)
        logger.info("  balance : %.2f", account.balance)
        logger.info("  equity  : %.2f", account.equity)
        logger.info("  profit  : %+.2f", profit)
        logger.info("  mid     : %.2f", sim.current_mid_price)
        logger.info(
            "  open positions : %d",
            len(state.get_positions()) if state is not None else 0,
        )
        logger.info("=" * 60)

    shutdown_mt5_shim()


if __name__ == "__main__":
    main()
