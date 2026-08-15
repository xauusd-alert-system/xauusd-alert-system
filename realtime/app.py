"""
FastAPI inference and web dashboard service exposing real-time signals,
correlation matrix, active positions, Monte Carlo risk analytics,
Macro AI news sentiment, visual charts, and interactive bot controls.
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
import pandas as pd
import numpy as np

from config.loader import load_config, get_env, get_signal_grid
from realtime.pipeline import RealtimePipeline
from realtime.dashboard import DASHBOARD_HTML
from backtest.monte_carlo import MonteCarloSimulator
from alerts.chart_renderer import ChartRenderer
from features.smart_money_metrics import compute_institutional_metrics, format_institutional_metrics_report
from alerts import status_commands as sc

logger = logging.getLogger("realtime_app")

app = FastAPI(title="XAUUSD Multi-Asset Predictive Trading System", version="2.1.0")

CFG = load_config()
MODEL_PATH = get_env("MODEL_PATH", default=None)
DATA_MODE = get_env("DATA_MODE", default="mock")

# Initialize default pipeline (XAUUSD flagship)
pipeline = RealtimePipeline(cfg=CFG, model_path=MODEL_PATH, data_mode=DATA_MODE)

# Track trading paused state
TRADING_PAUSED = False


class SignalResponse(BaseModel):
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
    """Returns current system and account metrics (real MT5 when available)."""
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
    return {
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
    }


@app.get("/api/metrics")
def get_metrics(period: str = "week"):
    """Real closed-trade statistics (owner request 2026-08-11) for the dashboard.

    period in {today, week, 2week, month, 3month, all}. Reuses the read-only
    status_commands pipeline (history_deals_get) so it reflects actual executed
    trades when MT5 is connected, else returns an empty result.
    """
    if period not in ("today", "week", "2week", "month", "3month", "all"):
        period = "week"
    if not sc.ensure_mt5_connection():
        return {"period": period, "period_label": sc.PERIODS.get(period, ""),
                "n": 0, "available": False, "source": "unavailable",
                "mode": "implemented_not_live_verified",
                "as_of_utc": datetime.now(timezone.utc).isoformat()}
    dt_from, dt_to, label = sc.period_range(period)
    deals = sc.fetch_deals_between(dt_from, dt_to) if dt_from else sc.fetch_deals_between(
        datetime(1970, 1, 1, tzinfo=timezone.utc), dt_to)
    contexts = sc.load_position_contexts()
    m = sc.compute_deal_metrics(deals, contexts=contexts, cfg=CFG)
    return {"period": period, "period_label": label, "available": True,
            "source": "mt5_history_deals", "mode": "live_verified",
            "as_of_utc": datetime.now(timezone.utc).isoformat(), **m}


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
    """Generates signals across all 5 enabled assets."""
    assets = ["XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD"]
    if DATA_MODE != "live":
        as_of = datetime.now(timezone.utc).isoformat()
        return {
            "signals": [{
                "asset": asset, "bias": None, "confidence": None,
                "regime": None, "session": None, "targets": [],
                "invalidation": None, "available": False,
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
                "bias": sig.get("bias", "neutral"),
                "confidence": float(sig.get("confidence", 0.5)),
                "regime": sig.get("regime", "range"),
                "session": sig.get("session", "london"),
                "targets": sig.get("targets", []),
                "invalidation": sig.get("invalidation", None),
                "available": True,
                "source": "realtime_pipeline",
                "mode": "live" if DATA_MODE == "live" else "mock",
                "as_of_utc": sig.get("generated_at") or datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.warning(f"Matrix signal generation fallback for {sym}: {e}")
            signals.append({
                "asset": sym,
                "bias": "neutral",
                "confidence": 0.50,
                "regime": "range",
                "session": "london",
                "targets": [],
                "invalidation": None,
                "available": False,
                "source": "error_fallback",
                "mode": "unavailable",
                "as_of_utc": datetime.now(timezone.utc).isoformat(),
            })
    return {"signals": signals, "source": "per_asset_realtime_pipeline",
            "mode": DATA_MODE, "as_of_utc": datetime.now(timezone.utc).isoformat()}


@app.get("/api/correlation")
def get_correlation_matrix():
    """Rolling close-return correlation from real MT5 candles only."""
    as_of = datetime.now(timezone.utc).isoformat()
    if DATA_MODE != "live":
        return {"available": False, "assets": [], "matrix": [],
                "source": "unavailable", "mode": DATA_MODE, "as_of_utc": as_of,
                "reason": "real_market_data_required"}

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
        return {"available": False, "assets": [], "matrix": [],
                "source": "mt5_closed_candles", "mode": "live",
                "as_of_utc": as_of, "reason": "fewer_than_two_assets_available"}
    aligned = pd.concat(returns, axis=1, join="inner").dropna()
    aligned = aligned.loc[:, aligned.std(ddof=0) > 0]
    if len(aligned) < 20 or aligned.shape[1] < 2:
        return {"available": False, "assets": [], "matrix": [],
                "source": "mt5_closed_candles", "mode": "live",
                "as_of_utc": as_of, "reason": "insufficient_aligned_returns"}
    corr = aligned.corr()
    return {"available": True, "assets": corr.columns.tolist(),
            "matrix": corr.to_numpy(dtype=float).tolist(),
            "source": "mt5_closed_candle_returns", "mode": "live_verified",
            "as_of_utc": as_of, "n_aligned_returns": int(len(aligned))}


@app.get("/api/sentiment")
def get_sentiment():
    """No live-news adapter is configured; never present samples as current news."""
    return {"available": False, "score": None, "bias": None, "confidence": None,
            "matched_terms": [], "source": "unavailable",
            "mode": "implemented_not_live_verified",
            "as_of_utc": datetime.now(timezone.utc).isoformat(),
            "reason": "no_live_news_source_configured"}


@app.get("/api/monte-carlo")
def get_monte_carlo():
    """Monte Carlo from persisted executed-trade PnL only; no hypothetical sample."""
    as_of = datetime.now(timezone.utc).isoformat()
    db_path = str(get_env(
        "TRADE_LOG_DB_PATH",
        default=CFG.get("general", {}).get("db_path", "data/market_data_mt5.sqlite"),
    ))
    if not os.path.exists(db_path):
        return {"available": False, "source": "executed_trades",
                "mode": "live_history", "as_of_utc": as_of,
                "reason": "trade_ledger_missing"}
    try:
        from data.trade_logger import read_executed_trades
        trades = read_executed_trades(db_path)
        pnls = pd.to_numeric(trades.get("pnl"), errors="coerce").dropna().to_numpy(dtype=float)
    except Exception as exc:
        logger.warning("Could not load executed trades for Monte Carlo: %s", exc)
        return {"available": False, "source": "executed_trades",
                "mode": "live_history", "as_of_utc": as_of,
                "reason": "trade_ledger_unreadable"}
    if len(pnls) < 2:
        return {"available": False, "source": "executed_trades",
                "mode": "live_history", "as_of_utc": as_of,
                "reason": "at_least_two_closed_trades_required",
                "n_trades": int(len(pnls))}

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
    result.update({"available": True, "source": "executed_trades.pnl",
                   "mode": "live_history", "as_of_utc": as_of,
                   "n_trades": int(len(pnls))})
    return result


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
    if not sc.ensure_mt5_connection():
        return {"available": False, "positions": [], "source": "unavailable",
                "mode": "implemented_not_live_verified", "as_of_utc": as_of}
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
        return {"available": True, "positions": positions, "source": "mt5_positions",
                "mode": "live_verified", "as_of_utc": as_of}
    except Exception as exc:
        return {"available": False, "positions": [], "source": "mt5_positions",
                "mode": "live", "as_of_utc": as_of, "reason": str(exc)}


@app.post("/api/control/{action}")
def handle_control(action: str):
    """Dashboard-process controls; broker mutation is intentionally not wired here."""
    global TRADING_PAUSED
    action_lower = action.lower()
    if action_lower == "pause":
        TRADING_PAUSED = True
        return {"status": "ok", "scope": "dashboard_api_process_only",
                "message": "⏸️ API generation paused; MT5 trader state was not changed"}
    if action_lower == "resume":
        TRADING_PAUSED = False
        return {"status": "ok", "scope": "dashboard_api_process_only",
                "message": "▶️ API generation resumed; MT5 trader state was not changed"}
    if action_lower == "closeall":
        raise HTTPException(
            status_code=501,
            detail=("closeall is not wired to the trader from this web process; "
                    "use the authenticated Telegram control bot"),
        )
    raise HTTPException(status_code=400, detail=f"Unknown action: {action}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Real-time push WebSocket for streaming live ticks and signal updates."""
    await websocket.accept()
    try:
        while True:
            # Echo heartbeat or receive client commands
            data = await websocket.receive_text()
            await websocket.send_json({"status": "live", "echo": data, "paused": TRADING_PAUSED})
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
