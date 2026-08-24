"""Six-month causal backtest for the MT5/forex strategy.

The runner deliberately uses the existing feature, walk-forward model-scoring,
and EnsembleBacktester code paths.  It evaluates the last six months *before*
the configured locked hold-out (default: 2026-02-08 through 2026-08-08),
using 300 days of preceding history for each 30-day test fold.

Three exit policies are reported on the same out-of-sample predictions:

* ``current`` - the frozen per-asset production grid;
* ``rr_3.5`` - full position to +3.5R, stop at -1R, no early BE/partial;
* ``rr_0.85`` - full position to +0.85R, stop at -1R, no early BE/partial.

``rr_3.5`` and ``rr_0.85`` are research comparisons only.  They do not alter
the live config or production model.  XAUUSD M5 is also trained separately as
a candidate after the backtest, written to a non-production path, and never
switched on automatically.

Run from the repository root::

    python -m scripts.fetch_halfyear_fx
    python -m scripts.fetch_halfyear_fx --no-telegram --assets XAUUSD,EURUSD

The runner writes a compact JSON report to ``data/backtest/forex_halfyear_results.json``
and operational status/logs to ``logs/halfyear_fx/``.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RESULT_PATH = ROOT / "data" / "backtest" / "forex_halfyear_results.json"
STATUS_PATH = ROOT / "logs" / "halfyear_fx" / "status.json"
RUN_LOG_DIR = ROOT / "logs" / "halfyear_fx"

DEFAULT_ASSETS = ("XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "BTCUSD")
DEFAULT_CUTOFF = "2026-08-08"
DEFAULT_TRAIN_DAYS = 300
DEFAULT_TEST_DAYS = 30
DEFAULT_STEP_DAYS = 30


# A target equal to all three TP levels plus zero partial ratios makes the
# existing event-driven engine represent one full-size exit at that target.
# The target/stop relation is the only thing changed; entries and model scores
# remain identical across variants.
EXIT_VARIANTS = {
    "current": {},
    "rr_3.5": {
        "signal_grid": {
            "tp1_mult": 3.5,
            "tp2_mult": 3.5,
            "tp3_mult": 3.5,
            "stop_mult": 1.0,
            "breakeven_trigger_atr": 999.0,
            "scaleout": {"tp1_ratio": 0.0, "tp2_ratio": 0.0},
        }
    },
    "rr_0.85": {
        "signal_grid": {
            "tp1_mult": 0.85,
            "tp2_mult": 0.85,
            "tp3_mult": 0.85,
            "stop_mult": 1.0,
            "breakeven_trigger_atr": 999.0,
            "scaleout": {"tp1_ratio": 0.0, "tp2_ratio": 0.0},
        }
    },
}


def _utc_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def six_month_start(cutoff: str | pd.Timestamp) -> pd.Timestamp:
    """Return the calendar date six months before an exclusive cutoff."""
    return _utc_timestamp(cutoff) - pd.DateOffset(months=6)


def make_windows(
    evaluation_start: str | pd.Timestamp,
    cutoff: str | pd.Timestamp,
    train_days: int = DEFAULT_TRAIN_DAYS,
    test_days: int = DEFAULT_TEST_DAYS,
    step_days: int = DEFAULT_STEP_DAYS,
):
    """Build explicit rolling windows covering the requested evaluation range.

    The first test window starts exactly at ``evaluation_start``.  The last
    window is allowed to be shorter than ``test_days`` so the whole six-month
    interval is represented instead of silently discarding its tail.
    """
    from backtest.walk_forward import WalkForwardWindow

    start = _utc_timestamp(evaluation_start)
    end = _utc_timestamp(cutoff)
    if end <= start:
        raise ValueError("cutoff must be later than evaluation_start")
    if min(train_days, test_days, step_days) <= 0:
        raise ValueError("train_days, test_days and step_days must be positive")

    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    windows = []
    cursor = start_ts
    while cursor < end_ts:
        test_end = min(cursor + test_days * 86400, end_ts)
        windows.append(
            WalkForwardWindow(
                train_start_ts=cursor - train_days * 86400,
                train_end_ts=cursor,
                test_start_ts=cursor,
                test_end_ts=test_end,
            )
        )
        cursor += step_days * 86400
    return windows


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def write_status(payload: dict) -> None:
    _write_json_atomic(STATUS_PATH, payload)


def notify(text: str, enabled: bool = True) -> None:
    """Send a non-polling Telegram message; missing credentials are non-fatal."""
    if not enabled:
        return
    try:
        sys.path.insert(0, str(ROOT))
        from alerts.telegram_bot import TelegramAlertBot
        from config.loader import load_config

        sent = TelegramAlertBot(load_config()).send_text_message(text)
        if not sent:
            print("[telegram] not sent (credentials/network unavailable)", flush=True)
    except Exception as exc:  # notifications must never kill research
        print(f"[telegram] notification failed: {exc}", flush=True)


def _load_raw_window(db_path: str, asset: str, timeframe: str, train_start: int, cutoff: int) -> pd.DataFrame:
    from scripts.run_backtest import load_asset_history

    raw = load_asset_history(db_path, timeframe, asset)
    raw = raw[(raw["timestamp_utc"] >= train_start) & (raw["timestamp_utc"] < cutoff)].reset_index(drop=True)
    if raw.empty:
        raise ValueError(f"no raw candles in requested window for {asset} {timeframe}")
    return raw


def apply_exit_variant(cfg: dict, asset: str, variant: str) -> dict:
    """Copy cfg and apply a research-only exit-policy patch to one asset."""
    if variant not in EXIT_VARIANTS:
        raise KeyError(f"unknown exit variant {variant!r}")
    out = copy.deepcopy(cfg)
    patch = EXIT_VARIANTS[variant]
    if not patch:
        return out
    asset_cfg = out.setdefault("assets", {}).setdefault(asset, {})
    for section, values in patch.items():
        section_cfg = copy.deepcopy(asset_cfg.get(section, {}))
        section_cfg.update(copy.deepcopy(values))
        asset_cfg[section] = section_cfg
    return out


def _profit_factor(pnls: np.ndarray) -> float:
    if len(pnls) == 0:
        return 0.0
    gross_profit = float(pnls[pnls > 0].sum())
    gross_loss = float(-pnls[pnls <= 0].sum())
    return round(gross_profit / gross_loss, 4) if gross_loss > 0 else 999.0


def _drawdown(pnls: np.ndarray) -> float:
    if len(pnls) == 0:
        return 0.0
    curve = np.cumsum(pnls)
    return float(np.min(curve - np.maximum.accumulate(curve)))


def _period_key(ts: int) -> str:
    stamp = pd.Timestamp.fromtimestamp(int(ts), tz="UTC")
    return f"{stamp.year}-Q{((stamp.month - 1) // 3) + 1}"


def summarize_trades(trades_df: pd.DataFrame, point_value_lot: float, volume: float) -> dict:
    """Produce money, R, drawdown, exit, and quarterly summaries."""
    if trades_df is None or trades_df.empty:
        return {
            "n_trades": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "max_drawdown": 0.0,
            "r_metrics": {},
            "quarters": {},
            "exit_reasons": {},
        }

    ordered = trades_df.sort_values(["entry_ts", "exit_ts"], kind="stable").reset_index(drop=True)
    pnls = ordered["pnl"].to_numpy(dtype=float)
    wins = pnls > 0
    quarters: dict[str, dict] = {}
    for key, group in ordered.groupby(ordered["entry_ts"].map(_period_key), sort=True):
        gp = group["pnl"].to_numpy(dtype=float)
        quarters[key] = {
            "n_trades": int(len(group)),
            "win_rate_pct": round(100.0 * float(np.mean(gp > 0)), 2),
            "profit_factor": _profit_factor(gp),
            "total_pnl": round(float(gp.sum()), 4),
            "avg_pnl": round(float(gp.mean()), 4),
        }

    r_metrics = {}
    try:
        from backtest.metrics import compute_r_metrics
        r_metrics = compute_r_metrics(ordered, point_value_lot=point_value_lot, volume=volume)
    except Exception as exc:
        r_metrics = {"error": str(exc)}

    return {
        "n_trades": int(len(ordered)),
        "win_rate_pct": round(100.0 * float(np.mean(wins)), 2),
        "profit_factor": _profit_factor(pnls),
        "total_pnl": round(float(pnls.sum()), 4),
        "avg_pnl": round(float(pnls.mean()), 4),
        "max_drawdown": round(_drawdown(pnls), 4),
        "r_metrics": r_metrics,
        "quarters": quarters,
        "exit_reasons": {
            str(reason): int(count)
            for reason, count in ordered["exit_reason"].value_counts(dropna=False).items()
        },
    }


def _variant_trade_frame(cfg: dict, asset: str, variant: str, scored_frames: list[pd.DataFrame]) -> tuple[pd.DataFrame, list[dict]]:
    """Run one exit variant over already-scored OOS folds."""
    from backtest.metrics import trades_to_dataframe
    from model.ensemble_backtest import EnsembleBacktester
    from scripts.run_backtest import merge_asset_cfg

    variant_cfg = apply_exit_variant(cfg, asset, variant)
    fold_rows = []
    all_trades = []
    for fold_index, scored in enumerate(scored_frames):
        cfg_run = merge_asset_cfg(variant_cfg, asset, "labeling")
        cfg_run = merge_asset_cfg(cfg_run, asset, "ensemble")
        engine = EnsembleBacktester(cfg_run, asset_key=asset)
        trades = engine.run(scored.reset_index(drop=True))
        frame = trades_to_dataframe(trades)
        if not frame.empty:
            frame["fold"] = fold_index
            all_trades.append(frame)
        fold_rows.append({
            "fold": fold_index,
            "n_trades": int(len(frame)),
            "total_pnl": round(float(frame["pnl"].sum()), 4) if not frame.empty else 0.0,
            "win_rate_pct": round(100.0 * float((frame["pnl"] > 0).mean()), 2) if not frame.empty else 0.0,
            "profit_factor": _profit_factor(frame["pnl"].to_numpy(dtype=float)) if not frame.empty else 0.0,
        })
    combined = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    return combined, fold_rows


def run_asset(
    cfg: dict,
    asset: str,
    timeframe: str,
    db_path: str,
    windows: list,
    status: dict,
    telegram: bool,
) -> dict:
    """Build one asset's bounded feature frame and perform honest OOS scoring."""
    from backtest.walk_forward import bar_seconds, split_fold_frames
    from scripts.deflated_sharpe import _score_fold
    from scripts.run_backtest import build_full_df, merge_asset_cfg

    cutoff = windows[-1].test_end_ts
    raw = _load_raw_window(db_path, asset, timeframe, windows[0].train_start_ts, cutoff)
    print(f"[{asset} {timeframe}] raw={len(raw)}", flush=True)
    full = build_full_df(cfg, raw, db_path=db_path, asset_key=asset)
    effective_cfg = merge_asset_cfg(cfg, asset, "labeling")
    seconds = bar_seconds(full)
    scored_frames = []
    fold_meta = []
    for fold_index, window in enumerate(windows):
        train_df, test_df = split_fold_frames(full, effective_cfg, window, bar_secs=seconds)
        if test_df.empty:
            continue
        started = time.time()
        scored = _score_fold(train_df, test_df, cfg, asset)
        scored_frames.append(scored)
        fold_meta.append({
            "fold": fold_index,
            "train_start_utc": datetime.fromtimestamp(window.train_start_ts, timezone.utc).isoformat(),
            "train_end_utc": datetime.fromtimestamp(window.train_end_ts, timezone.utc).isoformat(),
            "test_start_utc": datetime.fromtimestamp(window.test_start_ts, timezone.utc).isoformat(),
            "test_end_utc": datetime.fromtimestamp(window.test_end_ts, timezone.utc).isoformat(),
            "purged_train_rows": int(len(train_df)),
            "test_rows": int(len(test_df)),
            "scoring_seconds": round(time.time() - started, 2),
        })
        status["progress"] = {"asset": asset, "fold": fold_index + 1, "folds_total": len(windows)}
        write_status(status)
        print(f"[{asset}] scored fold {fold_index + 1}/{len(windows)}", flush=True)

    if not scored_frames:
        raise ValueError(f"no non-empty OOS folds for {asset} {timeframe}")

    asset_cfg = cfg.get("assets", {}).get(asset, {})
    volume = float(cfg.get("backtest", {}).get("volume", 0.10))
    point_value = float(asset_cfg.get("point_value_lot", cfg.get("backtest", {}).get("point_value_lot", 100.0)))
    variants = {}
    for name in EXIT_VARIANTS:
        frame, folds = _variant_trade_frame(cfg, asset, name, scored_frames)
        variants[name] = {
            "summary": summarize_trades(frame, point_value, volume),
            "folds": folds,
        }
        print(
            f"[{asset} {name}] n={variants[name]['summary']['n_trades']} "
            f"WR={variants[name]['summary']['win_rate_pct']:.2f}% "
            f"PF={variants[name]['summary']['profit_factor']} "
            f"PnL={variants[name]['summary']['total_pnl']:+.4f}",
            flush=True,
        )

    return {
        "enabled_in_config": bool(asset_cfg.get("enabled", False)),
        "timeframe": timeframe,
        "raw_rows": int(len(raw)),
        "featured_rows": int(len(full)),
        "data_start_utc": datetime.fromtimestamp(int(raw["timestamp_utc"].min()), timezone.utc).isoformat(),
        "data_end_exclusive_utc": datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
        "folds": fold_meta,
        "variants": variants,
    }


def train_xau_m5_candidate(cfg: dict, db_path: str, cutoff: str, timeout: int, telegram: bool) -> dict:
    """Train and validate an isolated XAUUSD M5 candidate artifact."""
    candidate = ROOT / "output" / "models" / "xauusd_m5_candidate.joblib"
    live_path = (ROOT / cfg["assets"]["XAUUSD"]["model_path"]).resolve()
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if candidate.resolve() == live_path:
        raise RuntimeError("refusing to overwrite the production XAUUSD model")

    notify(
        "🧠 XAUUSD M5: начинаю обучение отдельной candidate-модели\n"
        f"Cutoff UTC: {cutoff}\nLive-конфиг пока НЕ переключается.",
        telegram,
    )
    cmd = [
        sys.executable,
        "-m",
        "scripts.train_mt5",
        "--symbol",
        "XAUUSD",
        "--timeframe",
        "M5",
        "--db-path",
        db_path,
        "--end-date",
        cutoff,
        "--output",
        str(candidate),
    ]
    log_path = RUN_LOG_DIR / "xauusd_m5_training.log"
    started = time.time()
    with log_path.open("w", encoding="utf-8") as stream:
        proc = subprocess.run(cmd, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    if proc.returncode != 0 or not candidate.exists():
        raise RuntimeError(f"XAUUSD M5 candidate training failed (rc={proc.returncode}); see {log_path}")

    # Loading the artifact is the explicit readiness check used by the reminder.
    import joblib

    bundle = joblib.load(candidate)
    if not isinstance(bundle, dict) or "model" not in bundle or "feature_cols" not in bundle:
        raise RuntimeError("XAUUSD M5 candidate artifact has an invalid model bundle")
    metadata = bundle.get("metadata", {})
    result = {
        "path": str(candidate.relative_to(ROOT)),
        "loaded": True,
        "seconds": round(time.time() - started, 1),
        "feature_count": len(bundle.get("feature_cols", [])),
        "artifact_timeframe": metadata.get("timeframe", "M5"),
        "effective_config_sha256": metadata.get("effective_config_sha256"),
        "production_switched": False,
        "log": str(log_path.relative_to(ROOT)),
    }
    notify(
        "✅ XAUUSD M5 candidate-модель обучена и загружена\n"
        f"Файл: {result['path']}\n"
        f"Признаки: {result['feature_count']}\n"
        "Live XAUUSD остаётся на M15 до отдельной валидации.",
        telegram,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Causal six-month FX backtest + isolated XAUUSD M5 candidate")
    parser.add_argument("--assets", default=",".join(DEFAULT_ASSETS))
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF, help="exclusive UTC cutoff; must be before the locked hold-out")
    parser.add_argument("--evaluation-start", default=None, help="inclusive UTC start; defaults to six calendar months before cutoff")
    parser.add_argument("--db-path", default="data/market_data_mt5.sqlite")
    parser.add_argument("--train-days", type=int, default=DEFAULT_TRAIN_DAYS)
    parser.add_argument("--test-days", type=int, default=DEFAULT_TEST_DAYS)
    parser.add_argument("--step-days", type=int, default=DEFAULT_STEP_DAYS)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--candidate-timeout", type=int, default=3600)
    parser.add_argument("--skip-m5-model", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--out", default=str(RESULT_PATH))
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    from config.loader import load_config

    cfg = load_config()
    assets = [value.strip() for value in args.assets.split(",") if value.strip()]
    unknown = [asset for asset in assets if asset not in cfg.get("assets", {})]
    if unknown:
        raise SystemExit(f"Unknown asset(s): {unknown}")

    cutoff_ts = _utc_timestamp(args.cutoff)
    evaluation_start = _utc_timestamp(args.evaluation_start) if args.evaluation_start else six_month_start(cutoff_ts)
    # The research run must not consume the configured open-ended hold-out.
    locked = cfg.get("validation", {}).get("locked_holdout", {})
    locked_start = locked.get("start") if locked.get("enabled") else None
    if locked_start and cutoff_ts > _utc_timestamp(locked_start):
        raise SystemExit(
            f"Refusing cutoff {cutoff_ts.date()} after locked_holdout.start={locked_start}; "
            "use an explicit pre-lock cutoff."
        )

    windows = make_windows(evaluation_start, cutoff_ts, args.train_days, args.test_days, args.step_days)
    status = {
        "status": "running",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "evaluation_start_utc": evaluation_start.isoformat(),
        "cutoff_exclusive_utc": cutoff_ts.isoformat(),
        "train_days": args.train_days,
        "test_days": args.test_days,
        "step_days": args.step_days,
        "assets_requested": assets,
        "folds_total": len(windows),
        "causal": True,
        "production_models_touched": False,
        "candidate_model_switched_live": False,
        "assets": {},
    }
    write_status(status)
    notify(
        "▶️ FX полугодовой causal-бэктест запущен\n"
        f"Период теста: {evaluation_start.date()} → {cutoff_ts.date()} (UTC)\n"
        f"Фолды: {len(windows)} × {args.test_days} дней\n"
        f"Активы: {', '.join(assets)}\n"
        "После него — изолированное обучение XAUUSD M5 candidate.",
        not args.no_telegram,
    )

    try:
        for asset in assets:
            timeframe = cfg["assets"][asset].get("timeframe") or cfg.get("market_data", {}).get("timeframe", "M5")
            notify(f"⏳ FX-бэктест: {asset} ({timeframe}) — старт", not args.no_telegram)
            try:
                result = run_asset(cfg, asset, timeframe, args.db_path, windows, status, not args.no_telegram)
                status["assets"][asset] = {"status": "completed", **result}
                notify(
                    f"✅ FX-бэктест {asset} завершён\n"
                    f"Current: n={result['variants']['current']['summary']['n_trades']} "
                    f"avgR={result['variants']['current']['summary']['r_metrics'].get('mean_r', 'n/a')}\n"
                    f"RR 3.5: n={result['variants']['rr_3.5']['summary']['n_trades']} "
                    f"avgR={result['variants']['rr_3.5']['summary']['r_metrics'].get('mean_r', 'n/a')}",
                    not args.no_telegram,
                )
            except Exception as exc:
                status["assets"][asset] = {"status": "failed", "error": str(exc), "timeframe": timeframe}
                write_status(status)
                notify(f"❌ FX-бэктест {asset} не завершён: {exc}", not args.no_telegram)

        if not args.skip_m5_model:
            status["progress"] = {"phase": "xauusd_m5_candidate"}
            write_status(status)
            try:
                status["xauusd_m5_candidate"] = train_xau_m5_candidate(
                    cfg, args.db_path, cutoff_ts.strftime("%Y-%m-%d"), args.candidate_timeout, not args.no_telegram
                )
            except Exception as exc:
                status["xauusd_m5_candidate"] = {"loaded": False, "error": str(exc)}
                notify(f"❌ XAUUSD M5 candidate не готов: {exc}", not args.no_telegram)

        status["status"] = "completed" if all(item.get("status") == "completed" for item in status["assets"].values()) else "failed"
        status["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        status["result_path"] = str(Path(args.out).resolve().relative_to(ROOT)) if Path(args.out).resolve().is_relative_to(ROOT) else str(Path(args.out).resolve())
        _write_json_atomic(Path(args.out), status)
        write_status(status)
        notify(
            f"{'✅' if status['status'] == 'completed' else '⚠️'} FX полугодовой прогон завершён\n"
            f"Статус: {status['status']}\nРезультат: {status['result_path']}\n"
            "M5 candidate не включён в live автоматически.",
            not args.no_telegram,
        )
        return 0 if status["status"] == "completed" else 1
    except Exception as exc:
        status["status"] = "failed"
        status["error"] = str(exc)
        status["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        write_status(status)
        notify(f"❌ FX полугодовой прогон аварийно остановлен: {exc}", not args.no_telegram)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
