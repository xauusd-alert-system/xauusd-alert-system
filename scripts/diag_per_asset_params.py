"""Print the per-asset backtest parameter resolution (money-scale probe)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config.loader import load_config
from model.ensemble_backtest import EnsembleBacktester


def main() -> None:
    cfg = load_config()
    for a in ["XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD"]:
        bt = EnsembleBacktester(cfg, asset_key=a)
        money_factor = bt.volume * bt.point_value_lot  # money per 1.0 price unit
        print(
            f"{a}: volume={bt.volume} point_value_lot={bt.point_value_lot} "
            f"money_per_unit={money_factor:.6f} slippage={bt.slippage} "
            f"spread={bt.spread} commission={bt.commission_per_trade}"
        )


if __name__ == "__main__":
    main()
