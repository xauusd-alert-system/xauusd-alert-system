"""
Persistent Forex Factory calendar browser service.

One real Chromium window (offscreen) is launched ONCE and reused for all
fetches. The backend, the trader, and anything else call it over HTTP, so
the browser is never started/stopped repeatedly.

Architecture: the Chromium lives in a CHILD process
(`scripts.news_feed_browser_worker`) that talks to this HTTP process over
stdin/stdout JSON lines. THIS process is pure HTTP and can never hang: if
the child wedges (stuck renderer, dead driver, ...), the parent kills it
after a timeout and spawns a fresh one - the next request starts clean.

Endpoints:
    GET /health    -> {"ok": true}
    GET /calendar  -> {"events": [...], "source": "forexfactory_www_browser",
                       "fetched_at_utc": "..."}   (HTTP 502 on failure)

Usage:
    python -m scripts.news_feed_server --port 8765
"""
import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("news_feed_server")


class _BrowserWorkerProcess:
    """Manages the child browser process. Single-flight: concurrent
    /calendar requests wait on the SAME in-flight job instead of piling up
    separate fetches."""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._in_flight = False
        self._result: Dict = {}
        self._done = threading.Event()

    def _spawn(self):
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "scripts.news_feed_browser_worker"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # inherit: child logs land in our log capture file
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logger.info("Browser worker spawned (pid=%d)", self._proc.pid)

    def _kill(self):
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                logger.warning("Browser worker killed (pid=%d)", proc.pid)
            except Exception as exc:
                logger.warning("Could not kill browser worker: %s", exc)
        self._in_flight = False

    def _reader(self):
        try:
            line = self._proc.stdout.readline()
            with self._lock:
                if not self._in_flight:
                    return
                try:
                    self._result = json.loads(line.decode("utf-8").strip())
                except Exception:
                    self._result = {"error": "worker returned an invalid response"}
                self._in_flight = False
                self._done.set()
        except Exception as exc:
            with self._lock:
                if self._in_flight:
                    self._result = {"error": f"worker read failed: {exc}"}
                    self._in_flight = False
                    self._done.set()

    def fetch(self, timeout: float = 85.0) -> Dict:
        with self._lock:
            if not self._in_flight:
                self._in_flight = True
                self._done.clear()
                self._result = {}
                self._spawn()
                if self._proc is None or self._proc.poll() is not None:
                    self._in_flight = False
                    return {"error": "browser worker unavailable"}
                try:
                    self._proc.stdin.write(b'{"cmd": "calendar"}\n')
                    self._proc.stdin.flush()
                except Exception as exc:
                    self._in_flight = False
                    self._kill()
                    return {"error": f"could not reach browser worker: {exc}"}
                threading.Thread(target=self._reader, daemon=True,
                                 name="worker-reader").start()
        if not self._done.wait(timeout):
            # Hard backstop: the child is wedged. Kill it and respawn on the
            # next request - the NEXT /calendar call starts clean.
            with self._lock:
                if self._in_flight:
                    self._result = {"error": "calendar fetch timed out"}
                    self._done.set()
                    self._kill()
        return dict(self._result)

    def healthy(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


WORKER = _BrowserWorkerProcess()

# 2026-08-19: serve the last successful calendar while the worker is failing
# (network/firewall outages), so the backend/trader always have events. The
# stale payload is flagged (`stale`, `stale_seconds`) and carries its original
# fetched_at_utc, so consumers know it is not fresh.
LAST_GOOD: Dict = {}
LAST_GOOD_LOCK = threading.Lock()


def _serve_calendar(self: BaseHTTPRequestHandler):
    result = WORKER.fetch()
    if result.get("error"):
        with LAST_GOOD_LOCK:
            cached = dict(LAST_GOOD)
        if cached:
            cached["stale"] = True
            cached["stale_seconds"] = int(
                time.monotonic() - cached.pop("_monotonic"))
            logger.warning(
                "calendar fetch failed (%s); serving stale copy %ss old",
                result["error"][:120], cached["stale_seconds"])
            self._send_json(200, cached)
            return
        self._send_json(502, {"error": result["error"]})
        return
    payload = {
        "events": result.get("events", []),
        "source": "forexfactory_www_browser",
        "fetched_at_utc": result.get(
            "fetched_at_utc",
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")),
    }
    with LAST_GOOD_LOCK:
        LAST_GOOD.clear()
        LAST_GOOD.update(payload)
        LAST_GOOD["_monotonic"] = time.monotonic()
    self._send_json(200, payload)


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True, "worker_ok": WORKER.healthy()})
            return
        if self.path == "/calendar":
            _serve_calendar(self)
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


def _port_in_use(port: int) -> bool:
    """Refuse to run twice: two services on one port means TWO Chromium windows."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def main():
    parser = argparse.ArgumentParser(description="Persistent Forex Factory calendar browser service")
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765)")
    args = parser.parse_args()

    if _port_in_use(args.port):
        logger.error(
            "Port %d already in use - a news feed service is ALREADY running. "
            "Only ONE instance is allowed (one Chromium window). Exiting.", args.port)
        raise SystemExit(1)

    WORKER._spawn()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    logger.info("News feed server listening on http://127.0.0.1:%d (calendar endpoint: /calendar)", args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        WORKER._kill()


if __name__ == "__main__":
    main()
