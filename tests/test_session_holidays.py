# -*- coding: utf-8 -*-
"""Holidays sync check — ensures config and JSON cache are consistent."""
import json
import pathlib

import yaml


def test_holidays_sync():
    """Ensure config and data/us_market_holidays.json are in sync."""
    cfg_path = pathlib.Path("config/us_stocks_challenge.yaml")
    json_path = pathlib.Path("data/us_market_holidays.json")
    if not json_path.exists():
        # If JSON not yet generated, warn but don't fail during initial dev
        # After first generation it must stay in sync
        return
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    sess = (cfg or {}).get("session", {})
    cfg_data = {
        "holidays": sess.get("holidays", []),
        "early_closes": sess.get("early_closes", {}),
    }
    with open(json_path, encoding="utf-8") as f:
        existing = json.load(f)
    assert existing == cfg_data, "data/us_market_holidays.json out of sync — run python scripts/update_holidays.py"
