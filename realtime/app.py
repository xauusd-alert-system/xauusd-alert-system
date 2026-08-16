"""
FastAPI inference and web dashboard service exposing real-time signals,
correlation matrix, active positions, Monte Carlo risk analytics,
Macro AI news sentiment, visual charts, and interactive bot controls.
"""
from __future__ import annotations
import asyncio
import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Header
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
import pandas as pd
import numpy as np

from config.loader import load_config, get_env, get_signal_grid
from config.deployment import deployment_mode
from realtime.pipeline import RealtimePipeline
from realtime.dashboard import DASHBOARD_HTML
from backtest.monte_carlo import MonteCarloSimulator
from alerts.chart_renderer import ChartRenderer
from features.smart_money_metrics import compute_institutional_metrics, format_institutional_metrics_report
from alerts import status_commands as sc
from contracts.execution_contracts import event_envelope_from_dict
from data.ledger_bridge import verify_signature
from data.ledger_events import (
    execution_quality_summary,
    latest_ledger_activity_ms,
    lifecycle_trace,
    read_ledger_events,
    upsert_ledger_event,
)
from realtime.data_envelope import freshness_status, stamp

logger = logging.getLogger("realtime_app")

app = FastAPI(title="XAUUSD Multi-Asset Predictive Trading System", version="2.1.0")

CFG = load_config()
MODEL_PATH = get_env("MODEL_PATH", default=None)
DATA_MODE = get_env("DATA_MODE", default="mock")

# Initialize default pipeline (XAUUSD flagship)
pipeline = RealtimePipeline(cfg=CFG, model_path=MODEL_PATH, data_mode=DATA_MODE)
APP_STRATEGY_IDENTITY = pipeline.strategy_identity

# Track trading paused state
TRADING_PAUSED = False


class SignalResponse(BaseModel):
    signal_id: str
    signal_state: str
    strategy_version: str
    strategy_spec_hash: str
    config_hash: str
    model_hash: Optional[str] = None
    feature_snapshot_hash: Optional[str] = None
    setup_timeframe: str
    context_timeframes: List[str] = Field(default_factory=list)
    expires_at_utc: Optional[int] = None
    target_legs: List[Dict[str, Any]] = Field(default_factory=list)
    confirmation_predicates: List[str] = Field(default_factory=list)
    confirmed_by: Optional[str] = None
    confirmation_time_utc: Optional[int] = None
    bias: str
    confidence: float
    entry_zone: Optional[List[float]] = None
    invalidation: Optional[float] = None
    targets: Optional[List[float]] = None
    step: Optional[float] = None
    reasoning_summary: str
    regime: str
    timestamp_utc: int
    session: str
    generated_at: str


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard():
    """Serves the interactive web dashboard."""
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/health")
def health():
    """Basic liveness check."""
    return {
        "status": "ok",
        "data_mode": DATA_MODE,
        "model_loaded": pipeline._predictor is not None,
        "trading_paused": TRADING_PAUSED,
        "deployment_mode": deployment_mode(CFG).value,
        **APP_STRATEGY_IDENTITY,
    }


@app.get("/signal", response_model=SignalResponse)
def get_signal(n_candles: int = 300, asset: str = "XAUUSD"):
    """
    Runs the pipeline for the specified asset and returns the structured signal JSON.
    """
    if TRADING_PAUSED:
        raise HTTPException(status_code=423, detail="Signal generation paused in dashboard API process")
    try:
        if asset == "XAUUSD":
            result = pipeline.generate_signal(n_candles=n_candles)
        else:
            asset_pipe = RealtimePipeline(cfg=CFG, asset_key=asset, data_mode=DATA_MODE)
            result = asset_pipe.generate_signal(n_candles=n_candles)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Signal generation failed: {str(e)}")


@app.get("/api/status")
def get_status():
    """Returns current system and account metrics (real MT5 when available).

    Honesty contract (web-UI spec §6.3 / §12): when MT5 is unavailable the
    payload returns ``available=false`` with ``balance/equity/floating_pnl =
    None`` and ``freshness_status=offline`` — never a fallback balance.
    """
    account = None
    positions = []
    if sc.ensure_mt5_connection():
        try:
            m = sc.get_mt5()
            account = m.account_info()
            positions = list(m.positions_get() or [])
        except Exception as exc:
            logger.warning("Could not fetch live status: %s", exc)
    available = account is not None
    balance = float(getattr(account, "balance", 0.0) or 0.0) if available else None
    equity = float(getattr(account, "equity", 0.0) or 0.0) if available else None
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    payload = {
        "status": "online",
        "data_mode": DATA_MODE,
        "available": available,
        "source": "mt5_account" if available else "unavailable",
        "mode": "live_verified" if available and DATA_MODE == "live" else "implemented_not_live_verified",
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "balance": balance,
        "equity": equity,
        "floating_pnl": (equity - balance) if available else None,
        "open_positions_count": len(positions),
        "circuit_breaker": False,
        "trading_paused": TRADING_PAUSED,
        "execution_enabled_assets": CFG.get("execution", {}).get("enabled_assets", []),
        "require_demo_account": bool(CFG.get("execution", {}).get("require_demo_account", False)),
        "deployment_mode": deployment_mode(CFG).value,
        **APP_STRATEGY_IDENTITY,
    }
    return stamp(
        payload,
        last_activity_ms=now if available else None,
        source="mt5_account" if available else "unavailable",
        mode="live_verified" if available and DATA_MODE == "live" else "implemented_not_live_verified",
        freshness=None if available else "offline",  # producer unreachable, not "no data yet"
    )


@app.get("/api/metrics")
def get_metrics(period: str = "week"):
    """Real closed-trade statistics (owner request 2026-08-11) for the dashboard.

    period in {today, week, 2week, month, 3month, all}. Reuses the read-only
    status_commands pipeline (history_deals_get) so it reflects actual executed
    trades when MT5 is connected, else returns an empty result.
    """
    if period not in ("today", "week", "2week", "month", "3month", "all"):
        period = "week"
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    if not sc.ensure_mt5_connection():
        return stamp(
            {"period": period, "period_label": sc.PERIODS.get(period, ""),
             "n": 0, "available": False,
             "as_of_utc": datetime.now(timezone.utc).isoformat()},
            last_activity_ms=None, source="unavailable",
            mode="implemented_not_live_verified", freshness="offline",
        )
    dt_from, dt_to, label = sc.period_range(period)
    deals = sc.fetch_deals_between(dt_from, dt_to) if dt_from else sc.fetch_deals_between(
        datetime(1970, 1, 1, tzinfo=timezone.utc), dt_to)
    contexts = sc.load_position_contexts()
    m = sc.compute_deal_metrics(deals, contexts=contexts, cfg=CFG)
    return stamp(
        {"period": period, "period_label": label, "available": True,
         "as_of_utc": datetime.now(timezone.utc).isoformat(), **m},
        last_activity_ms=now, source="mt5_history_deals", mode="live_verified",
    )


@app.get("/api/paper-status")
def get_paper_status():
    """Frozen-paper liveness/sample counts only; outcome payloads are not selected."""
    manifest_path = get_env("PAPER_MANIFEST_PATH", default=None)
    db_path = get_env("PAPER_LEDGER_DB", default="data/paper_forward.sqlite")
    if not manifest_path:
        return {
            "available": False, "source": "unconfigured",
            "mode": "paper_frozen", "as_of_utc": datetime.now(timezone.utc).isoformat(),
        }
    try:
        from data.paper_ledger import paper_accumulation_status
        from paper.accumulator import load_frozen_manifest
        manifest = load_frozen_manifest(manifest_path, verify_model=False)
        status = paper_accumulation_status(db_path, manifest["run_id"])
        return {"available": True, "as_of_utc": datetime.now(timezone.utc).isoformat(), **status}
    except Exception as exc:
        logger.warning("Paper status unavailable: %s", exc)
        return {
            "available": False, "source": "paper_status_error",
            "mode": "paper_frozen", "as_of_utc": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }


@app.get("/api/matrix")
def get_signal_matrix():
    """Generates signals across all 5 enabled assets.

    Honesty contract (web-UI spec §12): a per-asset failure is an explicit
    ``status=error`` row with ``bias/confidence = None`` — never a fabricated
    neutral ``confidence=0.50`` that looks model-computed.
    """
    assets = ["XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD"]
    as_of = datetime.now(timezone.utc).isoformat()
    if DATA_MODE != "live":
        return {
            "signals": [{
                "asset": asset, "bias": None, "confidence": None,
                "regime": None, "session": None, "targets": [],
                "invalidation": None, "available": False, "status": "unavailable",
                "freshness_status": "offline",
                "source": "unavailable", "mode": DATA_MODE, "as_of_utc": as_of,
            } for asset in assets],
            "source": "unavailable", "mode": DATA_MODE, "as_of_utc": as_of,
        }
    signals = []
    for sym in assets:
        try:
            pipe = RealtimePipeline(cfg=CFG, asset_key=sym, data_mode=DATA_MODE)
            sig = pipe.generate_signal(n_candles=300)
            signals.append({
                "asset": sym,
                "bias": sig.get("bias"),
                "confidence": sig.get("confidence"),
                "regime": sig.get("regime"),
                "session": sig.get("session"),
                "targets": sig.get("targets", []),
                "invalidation": sig.get("invalidation", None),
                "available": True,
                "status": "ok",
                "source": "realtime_pipeline",
                "mode": DATA_MODE,
                "as_of_utc": sig.get("generated_at") or as_of,
            })
        except Exception as e:
            logger.warning("Matrix signal generation failed for %s: %s", sym, e)
            signals.append({
                "asset": sym,
                "bias": None,
                "confidence": None,
                "regime": None,
                "session": None,
                "targets": [],
                "invalidation": None,
                "available": False,
                "status": "error",
                "reason": str(e),
                "source": "realtime_pipeline",
                "mode": "unavailable",
                "as_of_utc": as_of,
            })
    return {"signals": signals, "source": "per_asset_realtime_pipeline",
            "mode": DATA_MODE, "as_of_utc": as_of}


@app.get("/api/correlation")
def get_correlation_matrix():
    """Rolling close-return correlation from real MT5 candles only."""
    as_of = datetime.now(timezone.utc).isoformat()
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    if DATA_MODE != "live":
        return stamp(
            {"available": False, "assets": [], "matrix": [],
             "as_of_utc": as_of, "reason": "real_market_data_required"},
            last_activity_ms=None, source="unavailable", mode=DATA_MODE,
            freshness="offline",
        )

    returns = []
    for asset, asset_cfg in CFG.get("assets", {}).items():
        if not asset_cfg.get("enabled", False):
            continue
        try:
            pipe = pipeline if asset == pipeline.asset_key else RealtimePipeline(
                cfg=CFG, asset_key=asset, data_mode="live"
            )
            frame = pipe.get_frame(n_candles=300, build_features=False)
            if frame is None or len(frame) < 30:
                continue
            series = pd.Series(
                pd.to_numeric(frame["close"], errors="coerce").pct_change().values,
                index=pd.to_numeric(frame["timestamp_utc"], errors="coerce"),
                name=asset,
            ).dropna()
            returns.append(series)
        except Exception as exc:
            logger.warning("Correlation data unavailable for %s: %s", asset, exc)

    if len(returns) < 2:
        return stamp(
            {"available": False, "assets": [], "matrix": [],
             "as_of_utc": as_of, "reason": "fewer_than_two_assets_available"},
            last_activity_ms=None, source="mt5_closed_candles", mode="live",
        )
    aligned = pd.concat(returns, axis=1, join="inner").dropna()
    aligned = aligned.loc[:, aligned.std(ddof=0) > 0]
    if len(aligned) < 20 or aligned.shape[1] < 2:
        return stamp(
            {"available": False, "assets": [], "matrix": [],
             "as_of_utc": as_of, "reason": "insufficient_aligned_returns"},
            last_activity_ms=None, source="mt5_closed_candles", mode="live",
        )
    corr = aligned.corr()
    return stamp(
        {"available": True, "assets": corr.columns.tolist(),
         "matrix": corr.to_numpy(dtype=float).tolist(),
         "as_of_utc": as_of, "n_aligned_returns": int(len(aligned))},
        last_activity_ms=now, source="mt5_closed_candle_returns",
        mode="live_verified",
    )


@app.get("/api/sentiment")
def get_sentiment():
    """No live-news adapter is configured; never present samples as current news."""
    return stamp(
        {"available": False, "score": None, "bias": None, "confidence": None,
         "matched_terms": [],
         "as_of_utc": datetime.now(timezone.utc).isoformat(),
         "reason": "no_live_news_source_configured"},
        last_activity_ms=None, source="unavailable",
        mode="implemented_not_live_verified",
    )


@app.get("/api/monte-carlo")
def get_monte_carlo():
    """Monte Carlo from persisted executed-trade PnL only; no hypothetical sample."""
    as_of = datetime.now(timezone.utc).isoformat()
    db_path = str(get_env(
        "TRADE_LOG_DB_PATH",
        default=CFG.get("general", {}).get("db_path", "data/market_data_mt5.sqlite"),
    ))
    if not os.path.exists(db_path):
        return stamp(
            {"available": False,
             "as_of_utc": as_of, "reason": "primary_event_ledger_missing"},
            last_activity_ms=None, source="trading_events.position_closed",
            mode="live_history",
        )
    try:
        from data.trading_event_ledger import closed_position_pnls, verify_event_chain
        if not verify_event_chain(db_path):
            raise RuntimeError("event hash chain verification failed")
        pnls = np.asarray(closed_position_pnls(db_path), dtype=float)
    except Exception as exc:
        logger.warning("Could not load primary event ledger for Monte Carlo: %s", exc)
        return stamp(
            {"available": False,
             "as_of_utc": as_of, "reason": "primary_event_ledger_unreadable"},
            last_activity_ms=None, source="trading_events.position_closed",
            mode="live_history",
        )
    if len(pnls) < 2:
        return stamp(
            {"available": False,
             "as_of_utc": as_of, "reason": "at_least_two_closed_trades_required",
             "n_trades": int(len(pnls))},
            last_activity_ms=None, source="trading_events.position_closed",
            mode="live_history",
        )

    account = sc.get_mt5().account_info() if sc.ensure_mt5_connection() else None
    initial_balance = float(getattr(account, "balance", 0.0) or 0.0)
    if initial_balance <= 0:
        initial_balance = float(CFG.get("backtest", {}).get("initial_balance", 100.0))
    mc = MonteCarloSimulator(
        trade_pnls=pnls,
        initial_balance=initial_balance,
        n_simulations=1000,
        horizon_trades=100,
    )
    result = mc.run_simulation()
    last_activity = int(datetime.now(timezone.utc).timestamp() * 1000)
    return stamp(
        {"available": True, "as_of_utc": as_of, "n_trades": int(len(pnls)), **result},
        last_activity_ms=last_activity,
        source="trading_events.position_closed.realized_pnl", mode="live_history",
    )


@app.get("/api/chart/{asset}")
def get_asset_chart(asset: str = "XAUUSD"):
    """Render real closed candles; return 503 when the real feed is unavailable."""
    if DATA_MODE != "live":
        raise HTTPException(status_code=503, detail={
            "available": False, "source": "unavailable", "mode": DATA_MODE,
            "reason": "real_market_data_required",
            "as_of_utc": datetime.now(timezone.utc).isoformat(),
        })
    if asset not in CFG.get("assets", {}):
        raise HTTPException(status_code=404, detail=f"Unknown asset: {asset}")
    try:
        pipe = pipeline if asset == pipeline.asset_key else RealtimePipeline(
            cfg=CFG, asset_key=asset, data_mode="live"
        )
        df = pipe.get_frame(n_candles=100, build_features=True)
        if df is None or len(df) < 10:
            raise RuntimeError("insufficient closed candles")
    except Exception as exc:
        raise HTTPException(status_code=503, detail={
            "available": False, "source": "mt5_closed_candles", "mode": "live",
            "reason": str(exc), "as_of_utc": datetime.now(timezone.utc).isoformat(),
        }) from exc

    latest = df.iloc[-1]
    entry = float(latest["close"])
    atr = float(latest.get("atr", 0.0) or 0.0)
    if not np.isfinite(atr) or atr <= 0:
        raise HTTPException(status_code=503, detail="ATR unavailable for chart levels")
    regime = str(latest.get("regime", ""))
    asset_cfg = CFG["assets"][asset]
    grid = get_signal_grid(CFG, asset_cfg, regime=regime)
    step = atr * float(grid.get("tp1_mult", 1.0))
    step_min, step_max = grid.get("step_min_points"), grid.get("step_max_points")
    if grid.get("step_points") is not None:
        step = float(grid["step_points"])
    if step_min is not None:
        step = max(step, float(step_min))
    if step_max is not None:
        step = min(step, float(step_max))
    sl = entry - step * float(grid.get("stop_mult", 2.0))
    targets = [entry + step * float(grid.get(k, d)) for k, d in (
        ("tp1_mult", 1.0), ("tp2_mult", 1.5), ("tp3_mult", 2.0)
    )]
    svg = ChartRenderer.render_svg_candlestick(
        df=df.tail(35), symbol=asset, entry_price=entry,
        sl_price=sl, tp_prices=targets, width=700, height=320,
    )
    return Response(
        content=svg, media_type="image/svg+xml",
        headers={"X-Data-Source": "mt5-closed-candles",
                 "X-Data-Mode": "live-verified",
                 "X-As-Of-UTC": datetime.now(timezone.utc).isoformat()},
    )


@app.get("/api/institutional-metrics")
def get_institutional_metrics():
    """Institutional metrics computed from real closed candles only."""
    as_of = datetime.now(timezone.utc).isoformat()
    if DATA_MODE != "live":
        return {"available": False, "metrics": {}, "report_text": None,
                "source": "unavailable", "mode": DATA_MODE, "as_of_utc": as_of,
                "reason": "real_market_data_required"}
    try:
        candles = pipeline.get_frame(n_candles=100, build_features=False)
        if candles is None or len(candles) < 10:
            raise RuntimeError("insufficient closed candles")
        metrics = compute_institutional_metrics(candles)
        return {"available": True, "metrics": metrics,
                "report_text": format_institutional_metrics_report(metrics),
                "source": f"mt5_closed_candles:{pipeline.asset_key}",
                "mode": "live_verified", "as_of_utc": as_of}
    except Exception as exc:
        logger.warning("Institutional metrics unavailable: %s", exc)
        return {"available": False, "metrics": {}, "report_text": None,
                "source": "mt5_closed_candles", "mode": "live",
                "as_of_utc": as_of, "reason": str(exc)}


@app.get("/api/positions")
def get_positions():
    """Read-only real MT5 positions; never return a synthetic empty portfolio."""
    as_of = datetime.now(timezone.utc).isoformat()
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    if not sc.ensure_mt5_connection():
        return stamp(
            {"available": False, "positions": [], "as_of_utc": as_of},
            last_activity_ms=None, source="unavailable",
            mode="implemented_not_live_verified", freshness="offline",
        )
    try:
        raw_positions = list(sc.get_mt5().positions_get() or [])
        positions = [{
            "ticket": getattr(p, "ticket", None),
            "symbol": getattr(p, "symbol", None),
            "direction": "buy" if int(getattr(p, "type", 0)) == 0 else "sell",
            "volume": float(getattr(p, "volume", 0.0)),
            "open_price": float(getattr(p, "price_open", 0.0)),
            "current_price": float(getattr(p, "price_current", 0.0)),
            "profit": float(getattr(p, "profit", 0.0)),
            "sl": float(getattr(p, "sl", 0.0) or 0.0),
            "tp": float(getattr(p, "tp", 0.0) or 0.0),
        } for p in raw_positions]
        return stamp(
            {"available": True, "positions": positions, "as_of_utc": as_of},
            last_activity_ms=now, source="mt5_positions", mode="live_verified",
        )
    except Exception as exc:
        return stamp(
            {"available": False, "positions": [], "as_of_utc": as_of, "reason": str(exc)},
            last_activity_ms=None, source="mt5_positions", mode="live",
        )


@app.post("/api/control/{action}")
def handle_control(action: str, authorization: str | None = Header(default=None)):
    """All browser mutation controls are DISABLED (web-UI spec §11/§12).

    ``pause``, ``resume`` and ``closeall`` stay off until a real command bus
    exists (idempotency, typed confirmation, kill-switch semantics and broker
    reconciliation). The only execution control remains the authenticated
    Telegram bot. This endpoint exists so the disabled state is explicit, not
    silent.
    """
    expected = get_env("DASHBOARD_CONTROL_TOKEN", default=None)
    if not expected or authorization != f"Bearer {expected}":
        raise HTTPException(status_code=403, detail="dashboard control authorization required")
    raise HTTPException(
        status_code=501,
        detail=(
            f"control action '{action}' is disabled: browser mutation controls are off "
            "until a command bus with idempotency/confirmation/kill-switch exists; "
            "use the authenticated Telegram control bot"
        ),
    )


def _ledger_rows_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Serialize ledger rows to JSON-safe records (payload_json -> payload)."""
    records = []
    for row in df.to_dict("records"):
        row = {k: (None if pd.isna(v) else v) for k, v in row.items()}
        try:
            row["payload"] = json.loads(row.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            row["payload"] = {}
        records.append(row)
    return records


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str | None = None):
    """Owner-only push stream of normalized ledger events (replaces the old echo).

    Auth: ``?token=<LEDGER_OWNER_TOKEN|LEDGER_INGEST_TOKEN>`` query parameter
    (browser WebSockets cannot set headers). The stream sends an initial
    snapshot of the latest events plus a freshness status, then pushes new
    events as they arrive (2s poll) and a periodic heartbeat. No client
    commands are accepted.
    """
    owner_token = _ledger_owner_token()
    await websocket.accept()
    if not owner_token or token != owner_token:
        await websocket.send_json({"type": "error", "code": "UNAUTHORIZED",
                                   "detail": "owner token required as ?token= query parameter"})
        await websocket.close(code=1008)
        return
    try:
        last_sent_ms = 0
        while True:
            db_path = _ledger_db_path()
            latest_ms = None
            events: list[dict[str, Any]] = []
            try:
                latest_ms = latest_ledger_activity_ms(db_path)
                df = read_ledger_events(db_path, since_ms=last_sent_ms, limit=200)
                events = _ledger_rows_to_records(df)
            except Exception as exc:
                logger.warning("Ledger WS read failed: %s", exc)
            if events:
                # strictly-after cursor: read_ledger_events uses >= since_ms
                last_sent_ms = max(int(e["received_at_utc_ms"]) for e in events) + 1
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            await websocket.send_json({
                "type": "events",
                "count": len(events),
                "events": events,
                "as_of_utc_ms": latest_ms,
                "freshness_status": freshness_status(latest_ms, now),
                "server_time_utc_ms": now,
                "deployment_mode": deployment_mode(CFG).value,
                "data_mode": DATA_MODE,
            })
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info("Ledger WebSocket client disconnected")


# =====================================================================
# Signal Desk ledger bridge (Wave 2 of the MQL5 observer plan).
#
# Producers: Python sender (intent_created / request_result) and the
# MQL5 SignalDeskObserver (deal_added / order_history_added /
# position_modified / execution_reconciled / health_heartbeat). All
# facts are normalized into one append-only ledger_events table
# (data/ledger_events.py); event_id is a deterministic primary key, so
# outbox retries and restart reconciliation dedupe safely.
#
# Auth: POST requires LEDGER_INGEST_TOKEN (bearer); reads require
# LEDGER_OWNER_TOKEN (falls back to the ingest token). When
# LEDGER_INGEST_SECRET is set, the Python bridge must additionally sign
# the body (X-Ledger-Signature, HMAC-SHA256); the MQL5 observer relies
# on HTTPS + bearer only. Unconfigured endpoints fail closed (403).
# =====================================================================

def _ledger_db_path() -> str:
    return str(get_env("TRADE_LOG_DB_PATH",
                       default=CFG.get("general", {}).get("db_path", "data/market_data_mt5.sqlite")))


def _ledger_owner_token() -> str | None:
    return get_env("LEDGER_OWNER_TOKEN", default=None) or get_env("LEDGER_INGEST_TOKEN", default=None)


def _check_bearer(authorization: str | None, expected: str | None) -> bool:
    return bool(expected) and authorization == f"Bearer {expected}"


@app.post("/api/ledger/ingest")
async def ledger_ingest(request: Request, authorization: str | None = Header(default=None)):
    """Owner-only, idempotent fact ingest from the Python bridge / MQL5 observer."""
    ingest_token = get_env("LEDGER_INGEST_TOKEN", default=None)
    if not ingest_token:
        raise HTTPException(status_code=403, detail="ledger ingest is not configured")
    if not _check_bearer(authorization, ingest_token):
        raise HTTPException(status_code=401, detail="ledger ingest authorization required")

    try:
        body = await request.body()
        envelope = event_envelope_from_dict(json.loads(body.decode("utf-8")))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid ledger envelope: {exc}")

    secret = get_env("LEDGER_INGEST_SECRET", default=None)
    signature_valid = True
    if secret:
        signature_valid = verify_signature(body, request.headers.get("X-Ledger-Signature"), secret)
        if not signature_valid:
            raise HTTPException(status_code=401, detail="ledger signature mismatch")

    db_path = _ledger_db_path()
    accepted = 0
    duplicates = 0
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    for event in envelope.events:
        _, inserted = upsert_ledger_event(
            db_path, event, signature_valid=signature_valid, received_at_utc_ms=now
        )
        if inserted:
            accepted += 1
        else:
            duplicates += 1
    return {
        "status": "ok",
        "accepted": accepted,
        "duplicates": duplicates,
        "signature_valid": signature_valid,
        "source": envelope.producer,
        "account_fingerprint": envelope.account_fingerprint,
    }


@app.get("/api/ledger/events")
def ledger_events(
    source: str | None = None,
    event_type: str | None = None,
    asset_key: str | None = None,
    intent_id: str | None = None,
    since_ms: int | None = None,
    limit: int = 200,
    authorization: str | None = Header(default=None),
):
    """Owner-only read of normalized execution facts."""
    owner_token = _ledger_owner_token()
    if not owner_token or not _check_bearer(authorization, owner_token):
        raise HTTPException(status_code=403, detail="ledger owner authorization required")
    df = read_ledger_events(
        _ledger_db_path(), source=source, event_type=event_type, asset_key=asset_key,
        intent_id=intent_id, since_ms=since_ms, limit=min(max(1, limit), 5000),
    )
    records = _ledger_rows_to_records(df)
    db_path = _ledger_db_path()
    latest_ms = None
    try:
        latest_ms = latest_ledger_activity_ms(db_path)
    except Exception as exc:
        logger.warning("Ledger freshness read failed: %s", exc)
    return stamp(
        {"source": "ledger_events", "available": True, "count": len(records),
         "events": records},
        last_activity_ms=latest_ms, source="ledger_events", mode="demo",
    )


@app.get("/api/ledger/execution-quality")
def ledger_execution_quality(
    asset_key: str | None = None,
    since_ms: int | None = None,
    authorization: str | None = Header(default=None),
):
    """Owner-only empirical execution-cost summary (plan Wave 3 view)."""
    owner_token = _ledger_owner_token()
    if not owner_token or not _check_bearer(authorization, owner_token):
        raise HTTPException(status_code=403, detail="ledger owner authorization required")
    summary = execution_quality_summary(_ledger_db_path(), asset_key=asset_key, since_ms=since_ms)
    if summary.get("available"):
        summary = stamp(
            summary,
            last_activity_ms=summary.get("as_of_utc_ms"),
            source="ledger_events", mode=summary.get("mode", "demo"),
        )
    else:
        summary = stamp(
            summary,
            last_activity_ms=None, source="ledger_events", mode="demo",
        )
    return summary


@app.get("/api/ledger/lifecycle/{intent_id}")
def ledger_lifecycle(intent_id: str, authorization: str | None = Header(default=None)):
    """Owner-only lifecycle trace: intent -> request -> deal -> reconciliation."""
    owner_token = _ledger_owner_token()
    if not owner_token or not _check_bearer(authorization, owner_token):
        raise HTTPException(status_code=403, detail="ledger owner authorization required")
    trace = lifecycle_trace(_ledger_db_path(), intent_id)
    latest_ms = None
    try:
        latest_ms = latest_ledger_activity_ms(_ledger_db_path())
    except Exception as exc:
        logger.warning("Ledger freshness read failed: %s", exc)
    return stamp(
        trace,
        last_activity_ms=latest_ms,
        source="ledger_events", mode="demo",
    )


# =====================================================================
# P1.6 provenance audit endpoint (ТЗ §39)
#
# GET /api/provenance/{group_id} — owner-only lineage for a trade group:
# spec provenance (market/feature/inference/profile/broker/cost ids +
# both hashes), execution intent, broker orders/deals, and ledger events.
# Missing nodes are reported as status="missing" — never a synthetic
# placeholder.
# =====================================================================

@app.get("/api/provenance/{group_id}")
def provenance_audit(group_id: str, authorization: str | None = Header(default=None)):
    owner_token = _ledger_owner_token()
    if not owner_token or not _check_bearer(authorization, owner_token):
        raise HTTPException(status_code=403, detail="ledger owner authorization required")
    db_path = _ledger_db_path()
    try:
        from data.trade_group_store import load_group
        from execution.provenance import FRESHNESS_VALUES

        group = load_group(db_path, group_id)
    except Exception as exc:
        logger.warning("Provenance audit read failed: %s", exc)
        group = None
    if group is None:
        return stamp(
            {"group_id": group_id, "available": False,
             "lineage": {"group": {"status": "missing"}}},
            last_activity_ms=None, source="ledger_events", mode="demo",
        )
    spec = group["spec"]
    prov = spec.provenance or {}
    lineage = {
        "group": {
            "status": "present",
            "group_id": spec.group_id,
            "mode": spec.mode,
            "side": spec.side,
            "geometry_hash": spec.geometry_hash(),
            "provenance_hash": spec.provenance_hash() if prov else None,
            "provenance_status": prov.get("provenance_status", "available"),
        },
        "market_snapshot": _provenance_node(prov, "market_snapshot_id"),
        "feature_snapshot": _provenance_node(prov, "feature_snapshot_id"),
        "model_inference": _provenance_node(prov, "model_inference_id"),
        "profile": _provenance_node(prov, "profile_id", prefix="PROFILE"),
        "broker_snapshot": _provenance_node(prov, "broker_snapshot_id"),
        "cost_snapshot": _provenance_node(prov, "cost_snapshot_id"),
    }
    # trading-event ledger for the group (actor vs source separation, §26)
    try:
        from data.trading_event_ledger import read_trading_events
        df = read_trading_events(db_path, signal_id=spec.signal_id)
        records = []
        for row in df.to_dict("records"):
            row = {k: (None if pd.isna(v) else v) for k, v in row.items()}
            try:
                row["payload"] = json.loads(row.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                row["payload"] = {}
            records.append(row)
        lineage["ledger_events"] = {
            "status": "present" if len(records) else "missing",
            "events": records,
        }
    except Exception as exc:
        logger.warning("Provenance ledger read failed: %s", exc)
        lineage["ledger_events"] = {"status": "error", "detail": str(exc)}
    return stamp(
        {"group_id": group_id, "available": True, "lineage": lineage},
        last_activity_ms=None, source="ledger_events", mode=spec.mode,
    )


def _provenance_node(prov: dict, key: str, prefix: str | None = None) -> dict:
    """One lineage node: present with the id, or explicit missing."""
    value = prov.get(key)
    if not value:
        return {"status": "missing"}
    return {"status": "present", "source_id": str(value)}
