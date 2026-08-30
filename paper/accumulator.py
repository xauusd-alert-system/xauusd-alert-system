"""Frozen candidate manifest and candle-idempotent paper accumulator."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from config.loader import effective_asset_config, get_signal_grid
from data.paper_ledger import (
    append_paper_event,
    paper_accumulation_status,
    read_paper_events,
    register_paper_run,
)
from labeling.label_generator import resolve_label_event
from model.trainer import load_model
from scripts.deflated_sharpe import _apply_variant, _variants_for


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_frozen_manifest(
    cfg: dict,
    *,
    asset_key: str,
    variant: str,
    model_path: str,
    output_path: str,
    start_timestamp_utc: int,
    min_closed_trades: int = 50,
) -> dict:
    """Create a manifest once; an existing path can only match byte-for-byte policy."""
    if asset_key not in cfg.get("assets", {}):
        raise ValueError(f"unknown asset {asset_key!r}")
    family = _variants_for(asset_key)
    if variant not in family or family[variant] is None:
        raise ValueError(f"variant {variant!r} is not a model candidate for {asset_key}")
    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"frozen model artifact not found: {model_path}")
    if min_closed_trades < 1:
        raise ValueError("min_closed_trades must be positive")
    lock = cfg.get("validation", {}).get("locked_holdout", {}) or {}
    if lock.get("enabled") and lock.get("start"):
        expected_start = pd.Timestamp(lock["start"])
        expected_start = (
            expected_start.tz_localize("UTC") if expected_start.tzinfo is None else expected_start.tz_convert("UTC")
        )
        if int(start_timestamp_utc) != int(expected_start.timestamp()):
            raise ValueError(
                "paper start must exactly match validation.locked_holdout.start; "
                "changing the start after preregistration is forbidden"
            )

    frozen_cfg = _apply_variant(cfg, asset_key, family[variant])
    model_sha = _file_hash(model_path)
    bundle = load_model(model_path)
    model_metadata = bundle.get("metadata", {})
    feature_columns = list(bundle.get("feature_cols", []))
    if int(model_metadata.get("bundle_schema_version", 0)) < 2:
        raise ValueError("frozen paper requires an auditable schema-v2 model bundle; retrain with train_mt5")
    if model_metadata.get("asset_key") != asset_key:
        raise ValueError(f"model asset metadata {model_metadata.get('asset_key')!r} != {asset_key!r}")
    expected_event = resolve_label_event(effective_asset_config(frozen_cfg, asset_key))
    if model_metadata.get("label_event") != expected_event:
        raise ValueError(f"model label_event {model_metadata.get('label_event')!r} != frozen policy {expected_event!r}")
    trained_end = (model_metadata.get("data_period") or {}).get("end_timestamp_utc")
    if trained_end is None:
        raise ValueError("frozen model metadata has no training data end timestamp")
    if int(trained_end) >= int(start_timestamp_utc):
        raise ValueError(
            "frozen model training period overlaps the live-forward start; "
            "retrain with --end-date before creating the manifest"
        )
    expected_timeframe = frozen_cfg["assets"][asset_key].get(
        "timeframe", frozen_cfg.get("market_data", {}).get("timeframe", "M5")
    )
    if model_metadata.get("timeframe") != expected_timeframe:
        raise ValueError(f"model timeframe {model_metadata.get('timeframe')!r} != frozen policy {expected_timeframe!r}")
    if not feature_columns:
        raise ValueError("frozen model bundle has no feature manifest")
    created = datetime.now(UTC).isoformat()
    identity = {
        "asset_key": asset_key,
        "variant": variant,
        "model_sha256": model_sha,
        "config_snapshot_sha256": _canonical_hash(frozen_cfg),
        "start_timestamp_utc": int(start_timestamp_utc),
        "min_closed_trades": int(min_closed_trades),
    }
    path = Path(output_path)
    if path.exists():
        existing = load_frozen_manifest(str(path), verify_model=True)
        if any(existing.get(k) != v for k, v in identity.items()):
            raise FileExistsError(f"refusing to overwrite frozen manifest with different policy: {path}")
        return existing

    manifest = {
        **identity,
        "run_id": f"{asset_key.lower()}-{variant}-{_canonical_hash(identity)[:12]}",
        "created_at_utc": created,
        "model_path": str(Path(model_path).resolve()),
        "model_metadata": model_metadata,
        "feature_columns": feature_columns,
        "variant_overrides": copy.deepcopy(family[variant]),
        "config_snapshot": frozen_cfg,
        "policy": {
            "fill_mode": "next_open",
            "intrabar_double_touch": "stop_first",
            "ledger": "append_only_event_sourced",
            "validation_reads_allowed_before_minimum": 0,
        },
    }
    manifest["manifest_sha256"] = _canonical_hash(manifest)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return manifest


def load_frozen_manifest(path: str, *, verify_model: bool = True) -> dict:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = manifest.get("manifest_sha256")
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if not expected or _canonical_hash(unsigned) != expected:
        raise RuntimeError("frozen manifest hash mismatch")
    if _canonical_hash(manifest["config_snapshot"]) != manifest["config_snapshot_sha256"]:
        raise RuntimeError("frozen config snapshot hash mismatch")
    if verify_model:
        if not os.path.isfile(manifest["model_path"]):
            raise FileNotFoundError(f"frozen model disappeared: {manifest['model_path']}")
        if _file_hash(manifest["model_path"]) != manifest["model_sha256"]:
            raise RuntimeError("frozen model SHA-256 mismatch; refusing to accumulate")
    return manifest


def _trade_id(run_id: str, signal_ts: int) -> str:
    return hashlib.sha256(f"{run_id}:{signal_ts}".encode()).hexdigest()[:24]


def _events_by_trade(events: pd.DataFrame) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if events.empty:
        return out
    for row in events.to_dict("records"):
        if row.get("trade_id"):
            out.setdefault(str(row["trade_id"]), []).append(row)
    return out


class FrozenPaperAccumulator:
    """One-candle state transition engine backed only by append events."""

    def __init__(self, manifest: dict, db_path: str):
        self.manifest = manifest
        self.db_path = db_path
        self.run_id = manifest["run_id"]
        self.asset_key = manifest["asset_key"]
        self.cfg = manifest["config_snapshot"]
        self.asset_cfg = self.cfg["assets"][self.asset_key]
        register_paper_run(db_path, manifest)

    def _state(self):
        events = read_paper_events(self.db_path, self.run_id)
        grouped = _events_by_trade(events)
        active = None
        pending = None
        for trade_id, rows in grouped.items():
            types = [r["event_type"] for r in rows]
            if "close" in types or "cancel" in types:
                continue
            latest = rows[-1]
            if "open" in types:
                candidate = (trade_id, latest["payload"])
                if active is None or latest["event_id"] > active[2]:
                    active = (candidate[0], candidate[1], latest["event_id"])
            elif "signal" in types:
                candidate = (trade_id, rows[0]["payload"])
                if pending is None or rows[0]["event_id"] > pending[2]:
                    pending = (candidate[0], candidate[1], rows[0]["event_id"])
        return events, active, pending

    def _costs(self) -> tuple[float, float, float, float, float]:
        bt = self.cfg.get("backtest", {})
        spread = float(self.asset_cfg.get("spread_usd", bt.get("spread_points", 25) / 100.0))
        slip = float(self.asset_cfg.get("slippage_usd", bt.get("slippage_points", 5) / 100.0))
        commission = float(bt.get("commission_per_trade", 0.0))
        volume = float(self.cfg.get("execution", {}).get("volume", bt.get("volume", 0.1)))
        pvl = float(self.asset_cfg.get("point_value_lot", bt.get("point_value_lot", 100.0)))
        return spread, slip, commission, volume, pvl

    def _append_state(self, trade_id: str, bar_ts: int, state: dict, event_type="mark") -> bool:
        return append_paper_event(
            self.db_path,
            run_id=self.run_id,
            trade_id=trade_id,
            event_type=event_type,
            idempotency_key=f"{self.run_id}:{trade_id}:{event_type}:{bar_ts}",
            event_timestamp_utc=bar_ts,
            bar_timestamp_utc=bar_ts,
            payload=state,
        )

    def _open_pending(self, pending, bar: pd.Series) -> bool:
        if pending is None:
            return False
        trade_id, signal, _ = pending
        bar_ts = int(bar["timestamp_utc"])
        signal_ts = int(signal["timestamp_utc"])
        if bar_ts <= signal_ts:
            return False
        if signal_ts < int(self.manifest["start_timestamp_utc"]):
            append_paper_event(
                self.db_path,
                run_id=self.run_id,
                trade_id=trade_id,
                event_type="cancel",
                idempotency_key=f"{self.run_id}:{trade_id}:prestart",
                event_timestamp_utc=bar_ts,
                bar_timestamp_utc=bar_ts,
                payload={"reason": "signal_before_manifest_start"},
            )
            return False

        direction = 1 if signal["bias"] == "long" else -1
        spread, slip, commission, volume, pvl = self._costs()
        entry = float(bar["open"]) + direction * (spread / 2.0 + slip)
        step = float(signal["step"])
        grid = signal["grid"]
        scaleout = grid.get("scaleout") or {}
        state = {
            "timestamp_utc": signal_ts,
            "entry_timestamp_utc": bar_ts,
            "last_bar_timestamp_utc": bar_ts,
            "entry_price": entry,
            "direction": direction,
            "step": step,
            "stop_price": entry - direction * step * float(grid.get("stop_mult", 2.0)),
            "initial_stop_price": entry - direction * step * float(grid.get("stop_mult", 2.0)),
            "tp1_price": entry + direction * step * float(grid.get("tp1_mult", 1.0)),
            "tp2_price": entry + direction * step * float(grid.get("tp2_mult", 2.0)),
            "tp3_price": entry + direction * step * float(grid.get("tp3_mult", 3.0)),
            "be_trigger": float(grid.get("breakeven_trigger_atr", 1.0)),
            "scaleout1": float(scaleout.get("tp1_ratio", 0.5)),
            "scaleout2": float(scaleout.get("tp2_ratio", 0.3)),
            "remaining_ratio": 1.0,
            "tp1_hit": False,
            "tp2_hit": False,
            "be_triggered": False,
            "bars_held": 0,
            "pnl_before_commission": 0.0,
            "execution_cost_money": 0.0,
            "commission": commission,
            "volume": volume,
            "point_value_lot": pvl,
            "spread": spread,
            "slippage": slip,
            "regime": signal.get("regime"),
            "session": signal.get("session"),
            "confidence": signal.get("confidence"),
        }
        return self._append_state(trade_id, bar_ts, state, event_type="open")

    def _process_active(self, active, bar: pd.Series) -> bool:
        if active is None:
            return False
        trade_id, old, _ = active
        state = copy.deepcopy(old)
        ts = int(bar["timestamp_utc"])
        if ts <= int(state["last_bar_timestamp_utc"]):
            return False
        direction = int(state["direction"])
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        state["bars_held"] = int(state["bars_held"]) + 1
        state["last_bar_timestamp_utc"] = ts

        def hit_up(level):
            return high >= level if direction == 1 else low <= level

        def hit_down(level):
            return low <= level if direction == 1 else high >= level

        stop_hit = hit_down(float(state["stop_price"]))
        tp1_hit_now = hit_up(float(state["tp1_price"]))
        tp2_hit_now = hit_up(float(state["tp2_price"]))
        tp3_hit_now = hit_up(float(state["tp3_price"]))
        double_touch = stop_hit and (tp1_hit_now or tp2_hit_now or tp3_hit_now)
        spread, slip = float(state["spread"]), float(state["slippage"])
        unit = float(state["volume"]) * float(state["point_value_lot"])

        def realize(ratio: float, level: float):
            exit_price = level - direction * (spread / 2.0 + slip)
            pnl = ratio * direction * (exit_price - float(state["entry_price"])) * unit
            state["pnl_before_commission"] += pnl
            # Entry+exit adverse costs for this tranche.
            state["execution_cost_money"] += ratio * (spread + 2.0 * slip) * unit
            return exit_price

        reason = None
        exit_price = None
        if double_touch:
            exit_price = realize(float(state["remaining_ratio"]), float(state["stop_price"]))
            reason = "stop"
        else:
            if not state["tp1_hit"] and not state["be_triggered"]:
                be_level = float(state["entry_price"]) + direction * float(state["be_trigger"]) * abs(
                    float(state["tp1_price"]) - float(state["entry_price"])
                )
                if hit_up(be_level):
                    state["stop_price"] = float(state["entry_price"])
                    state["be_triggered"] = True
                    stop_hit = hit_down(float(state["stop_price"]))
            if not state["tp1_hit"] and tp1_hit_now:
                ratio = float(state["scaleout1"])
                realize(ratio, float(state["tp1_price"]))
                state["remaining_ratio"] = 1.0 - ratio
                state["tp1_hit"] = True
                state["stop_price"] = float(state["entry_price"])
            if state["tp1_hit"] and not state["tp2_hit"] and tp2_hit_now:
                ratio = float(state["scaleout2"])
                realize(ratio, float(state["tp2_price"]))
                state["remaining_ratio"] = 1.0 - float(state["scaleout1"]) - ratio
                state["tp2_hit"] = True
            # Match EnsembleBacktester: stop touch is sampled once at the start
            # of the candle. A stop moved to BE by TP1 becomes active next candle;
            # re-testing the same OHLC after the move would invent intrabar order.
            stop_after = stop_hit
            if stop_after and tp3_hit_now:
                exit_price = realize(float(state["remaining_ratio"]), float(state["stop_price"]))
                reason = "breakeven" if (state["tp1_hit"] or state["be_triggered"]) else "stop"
            elif tp3_hit_now:
                exit_price = realize(float(state["remaining_ratio"]), float(state["tp3_price"]))
                reason = "tp3_runner"
            elif stop_after:
                exit_price = realize(float(state["remaining_ratio"]), float(state["stop_price"]))
                reason = "breakeven" if (state["tp1_hit"] or state["be_triggered"]) else "stop"
            elif int(state["bars_held"]) >= int(self.cfg.get("labeling", {}).get("horizon_candles_n", 36)):
                exit_price = realize(float(state["remaining_ratio"]), close)
                reason = "timeout"

        if reason is None:
            return self._append_state(trade_id, ts, state)

        state["exit_timestamp_utc"] = ts
        state["exit_price"] = exit_price
        state["exit_reason"] = reason
        state["execution_cost_money"] += float(state["commission"])
        state["pnl"] = float(state["pnl_before_commission"]) - float(state["commission"])
        risk = abs(float(state["entry_price"]) - float(state["initial_stop_price"])) * unit
        state["r_multiple"] = state["pnl"] / risk if risk > 0 else 0.0
        state["gross_before_execution_cost"] = state["pnl"] + state["execution_cost_money"]
        return self._append_state(trade_id, ts, state, event_type="close")

    def process_once(self, pipeline, n_candles: int = 300) -> dict:
        """Process the latest closed candle and append at most one transition/type."""
        frame = pipeline.get_frame(n_candles=3, build_features=False)
        if frame is None or frame.empty:
            raise RuntimeError("paper accumulator received no closed candles")
        bar = frame.iloc[-1]
        bar_ts = int(bar["timestamp_utc"])
        if bar_ts < int(self.manifest["start_timestamp_utc"]):
            raise RuntimeError("latest candle precedes frozen manifest start")

        _, active, pending = self._state()
        self._process_active(active, bar)
        _, active, pending = self._state()
        if active is None:
            self._open_pending(pending, bar)
        _, active, pending = self._state()

        # One-position semantics: do not queue stale signals while a trade or a
        # next-open signal is active.
        if active is None and pending is None:
            signal = pipeline.generate_signal(n_candles=n_candles)
            signal_ts = int(signal["timestamp_utc"])
            if signal_ts == bar_ts and signal.get("bias") in {"long", "short"}:
                regime = str(signal.get("regime"))
                grid = get_signal_grid(self.cfg, self.asset_cfg, regime=regime)
                payload = copy.deepcopy(signal)
                payload["grid"] = grid
                trade_id = _trade_id(self.run_id, signal_ts)
                append_paper_event(
                    self.db_path,
                    run_id=self.run_id,
                    trade_id=trade_id,
                    event_type="signal",
                    idempotency_key=f"{self.run_id}:signal:{signal_ts}",
                    event_timestamp_utc=signal_ts,
                    bar_timestamp_utc=signal_ts,
                    payload=payload,
                )

        append_paper_event(
            self.db_path,
            run_id=self.run_id,
            event_type="heartbeat",
            idempotency_key=f"{self.run_id}:heartbeat:{bar_ts}",
            event_timestamp_utc=bar_ts,
            bar_timestamp_utc=bar_ts,
            payload={"model_sha256": self.manifest["model_sha256"]},
        )
        return paper_accumulation_status(self.db_path, self.run_id)


def format_accumulation_status(status: dict) -> str:
    """Telegram-safe liveness text, deliberately excluding outcome metrics."""
    readiness = "READY for one-time validation" if status["ready_for_one_time_validation"] else "accumulating"
    return (
        f"Paper {status['asset_key']} / {status['variant']} — {readiness}\n"
        f"Closed: {status['closed_trades']}/{status['minimum_closed_trades']} | "
        f"Opened: {status['opened_trades']} | Signals: {status['signals']}\n"
        f"Mode: {status['mode']} | Source: {status['source']}\n"
        f"Manifest: {status['manifest_sha256'][:12]} | Latest bar: "
        f"{status['latest_bar_timestamp_utc'] or 'n/a'}"
    )
