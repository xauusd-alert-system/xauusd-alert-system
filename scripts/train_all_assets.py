"""
Trains ML models for all enabled assets in config.yaml sequentially.
"""
import os
import sys
import subprocess

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config.loader import load_config

def main():
    cfg = load_config()
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
        print(f"\n==========================================")
        print(f"Training Model for {asset} ({asset_cfg['mt5_symbol']}) on {asset_tf}...")
        print(f"==========================================")
        
        cmd = [
            sys.executable, "-m", "scripts.train_mt5",
            "--symbol", asset,
            "--timeframe", asset_tf,
            "--db-path", db_path,
            "--output", model_path
        ]
        subprocess.run(cmd, check=True)

    print("\n✅ ALL MULTI-ASSET MODELS TRAINED SUCCESSFULLY!")

if __name__ == "__main__":
    main()