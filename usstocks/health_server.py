"""Health check and status API endpoints (ТЗ §6, Stage F, P1-10).

Provides `/api/health`, `/api/status`, and `/api/metrics` for monitoring
uptime, risk state, scanner cadence, and provider connectivity.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from usstocks.models import RiskState


def create_health_app(
    *,
    state: Optional[RiskState] = None,
    runner: Any = None,
    journal: Any = None,
    start_time: Optional[float] = None,
) -> FastAPI:
    """Create FastAPI application with health check endpoints."""
    app = FastAPI(title="US Stocks VWAP Scanner Health API")
    _start_ts = start_time or time.time()

    @app.get("/api/health")
    def health() -> JSONResponse:
        uptime = time.time() - _start_ts
        metrics = getattr(runner, "metrics", {}) if runner else {}
        signals_enabled = getattr(runner, "signals_enabled", True) if runner else True
        day_stopped = getattr(state, "day_stopped", False) if state else False

        is_healthy = True
        status_str = "healthy"
        if day_stopped:
            status_str = "day_stopped"

        payload = {
            "status": status_str,
            "healthy": is_healthy,
            "uptime_seconds": round(uptime, 1),
            "profile": "us_stocks_challenge",
            "session_date": getattr(state, "session_date", "") if state else "",
            "signals_enabled": signals_enabled,
            "day_stopped": day_stopped,
            "active_symbol": getattr(state, "active_symbol", None) if state else None,
            "trades_taken": getattr(state, "trades_taken", 0) if state else 0,
            "realized_pnl_usd": round(getattr(state, "realized_pnl_usd", 0.0), 2) if state else 0.0,
            "last_scan_duration_ms": metrics.get("last_scan_duration_ms", 0.0),
            "total_scans": metrics.get("total_scans", 0),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return JSONResponse(content=payload, status_code=200)

    @app.get("/api/status")
    def status_endpoint() -> JSONResponse:
        return health()

    @app.get("/api/metrics")
    def metrics_endpoint() -> JSONResponse:
        uptime = time.time() - _start_ts
        metrics = getattr(runner, "metrics", {}) if runner else {}
        return JSONResponse(
            content={
                "uptime_seconds": round(uptime, 1),
                "total_scans": metrics.get("total_scans", 0),
                "last_scan_duration_ms": metrics.get("last_scan_duration_ms", 0.0),
                "trades_taken": getattr(state, "trades_taken", 0) if state else 0,
                "consecutive_losses": getattr(state, "consecutive_losses", 0) if state else 0,
                "realized_pnl_usd": round(getattr(state, "realized_pnl_usd", 0.0), 2) if state else 0.0,
            },
            status_code=200,
        )

    return app
