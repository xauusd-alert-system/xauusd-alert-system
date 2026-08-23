# -*- coding: utf-8 -*-
"""Загрузка конфигурации парного модуля (ТЗ §6): config/pairs_config.yaml."""
from __future__ import annotations

import os

import yaml

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "pairs_config.yaml")


def load_config(path: str = DEFAULT_PATH) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"конфиг пар не найден: {path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
