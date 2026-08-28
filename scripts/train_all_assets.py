"""
Trains ML models for all enabled assets in config.yaml sequentially.

After each successful training the artifact is cataloged in the Model
Registry (ТЗ 8.4, model/registry.py). Registration is intentionally
NON-FATAL: a registry failure is logged as a warning and never aborts the
training pipeline (safety > functionality; the model file itself is written
by scripts/train_mt5.py exactly as before).
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config.loader import load_config


def register_in_registry(model_path: str, asset: str, timeframe: str) -> str:
    """Catalog one freshly trained artifact; returns registry_id.

    Raises on failure so callers can decide policy; wrapped in a non-fatal
    guard by the training loop.
    """
    from model.registry import register_trained_model

    return register_trained_model(model_path, asset, timeframe)


def main():
    cfg = load_config()
    if not cfg.get("retraining", {}).get("enabled", True):
        print("Retraining safety freeze is active; train_all_assets skipped.")
        return
    assets = cfg.get("assets", {})
    db_path = cfg.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")
    timeframe = cfg.get("market_data", {}).get("timeframe", "M5")

    enabled_assets = [k for k, v in assets.items() if v.get("enabled", False)]
    print(f"🚀 Starting Multi-Asset Model Training for: {enabled_assets}")

    for asset in enabled_assets:
        asset_cfg = assets[asset]
        model_path = asset_cfg["model_path"]
        # Per-asset timeframe override (assets.<key>.timeframe), else global.
        asset_tf = asset_cfg.get("timeframe") or timeframe
        print("\n==========================================")
        print(f"Training Model for {asset} ({asset_cfg['mt5_symbol']}) on {asset_tf}...")
        print("==========================================")

        cmd = [
            sys.executable, "-m", "scripts.train_mt5",
            "--symbol", asset,
            "--timeframe", asset_tf,
            "--db-path", db_path,
            "--output", model_path
        ]
        subprocess.run(cmd, check=True)

        # Model Registry (ТЗ 8.4): catalog the artifact after a successful
        # train. Non-fatal by design - a registry outage must never turn a
        # completed training run into a failure.
        if os.path.exists(model_path):
            try:
                registry_id = register_in_registry(model_path, asset, asset_tf)
                print(f"registry: registered {model_path} as {registry_id}")
            except Exception as e:  # noqa: BLE001 - non-fatal by contract
                print(f"registry WARNING: registration failed for {asset} ({e}); "
                      f"training result unaffected")
        else:
            print(f"registry WARNING: model file missing after training: {model_path}")

    print("\n✅ ALL MULTI-ASSET MODELS TRAINED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
