"""Shared health endpoint for all services (Phase 3, Step 10).

A service reports a dict of named checks; each check is a callable returning
``(ok: bool, detail: str)``. The endpoint aggregates them:

    GET /health -> {"status": "ok" | "degraded", "checks": {"<name>": {...}}}

FastAPI + uvicorn are used (both already project dependencies). A check that
raises is reported as a failed check — a health probe must never 500.
"""
from __future__ import annotations

import threading
from typing import Callable

# A single health check: returns (ok, human-readable detail).
CheckFn = Callable[[], "tuple[bool, str]"]
Checks = "Mapping[str, CheckFn]"

HEALTH_PATH = "/health"


def build_check(name: str, fn: CheckFn) -> dict:
    """Helper: wrap one check into a single-entry mapping for ``checks`` dicts."""
    return {name: fn}


def run_checks(checks) -> dict:
    """Execute all checks and build the health payload.

    ``status`` is "ok" only when every check passed; any exception inside a
    check is converted to a failed check (degraded), never an HTTP error.
    """
    results = {}
    healthy = True
    for name, fn in dict(checks).items():
        try:
            ok, detail = fn()
            ok = bool(ok)
            detail = str(detail)
        except Exception as exc:  # a crashing check must not 500 the endpoint
            ok, detail = False, f"check error: {exc}"
        results[name] = {"ok": ok, "detail": detail}
        if not ok:
            healthy = False
    return {"status": "ok" if healthy else "degraded", "checks": results}


def create_health_app(checks):
    """Build the FastAPI app exposing ``GET /health`` for the given checks."""
    from fastapi import FastAPI

    app = FastAPI(title="xauusd-alert-system service", docs_url=None, redoc_url=None)

    @app.get(HEALTH_PATH)
    def health() -> dict:
        return run_checks(checks)

    return app


def run_health_server(port: int, checks, host: str = "127.0.0.1") -> None:
    """Blocking health server: serve ``GET /health`` until interrupted."""
    import uvicorn

    app = create_health_app(checks)
    uvicorn.run(app, host=host, port=int(port), log_level="warning")


def start_health_server_thread(port: int, checks, host: str = "127.0.0.1"):
    """Run the health server in a daemon thread.

    Returns the ``uvicorn.Server`` instance (call ``.should_exit = True`` to
    stop). Services with a blocking work loop use this so their main thread
    stays free.
    """
    import uvicorn

    app = create_health_app(checks)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=int(port), log_level="warning")
    )
    thread = threading.Thread(
        target=server.run, name="service-health-server", daemon=True
    )
    thread.start()
    return server
