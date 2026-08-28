"""Local loopback signing proxy for the MQL5 SignalDeskObserver (strict signed ingress).

Architecture (security contract):

    MQL5 SignalDeskObserver
      │  HTTP только на loopback + отдельный proxy bearer token
      ▼
    127.0.0.1 signing proxy  (this script)
      │  validates observer envelope
      │  HMAC-SHA256 over the EXACT raw body
      │  adds remote ingest bearer + X-Ledger-Signature
      ▼
    HTTPS /api/ledger/ingest
      │  requires bearer AND HMAC
      ▼
    append-only ledger_events

Security properties:

* Binds strictly to 127.0.0.1 (external interfaces are rejected at startup).
* Accepts only ``POST /v1/observer/ingest`` with a JSON body size limit.
* Requires ``Authorization: Bearer <OBSERVER_PROXY_TOKEN>`` (constant-time check).
* Accepts only observer envelopes: producer == "mt5_observer", account_mode in
  {demo, contest}; ``real`` is rejected.
* The HMAC for the remote server is computed over the EXACT raw body bytes
  received from the observer (never re-serialized).
* Remote URL must be https://; remote bearer (LEDGER_INGEST_TOKEN) and HMAC
  secret (LEDGER_INGEST_SECRET) never reach the observer.
* Returns 2xx to the observer ONLY when the remote server confirmed 2xx.
  Remote failure -> non-2xx; the MQL5 durable outbox keeps the event and
  retries later. There is no offline fallback queue here.
* Logs only safe operational metadata (status, event count, producer, account
  mode, batch id). Never logs body, secrets, bearer headers or the HMAC.

Run:
    OBSERVER_PROXY_TOKEN=... LEDGER_INGEST_URL=https://host/api/ledger/ingest \\
    LEDGER_INGEST_TOKEN=... LEDGER_INGEST_SECRET=... \\
    python -m scripts.run_observer_signing_proxy [--port 8787]
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from contracts.execution_contracts import (  # noqa: E402
    check_protocol_version,
    event_envelope_from_dict,
)

logger = logging.getLogger("observer_signing_proxy")

LOOPBACK_HOST = "127.0.0.1"

# Indirection so tests can patch remote delivery without a live network.
def _requests_post(url, data, headers, timeout=10.0):
    import requests
    return requests.post(url, data=data, headers=headers, timeout=timeout)
PROXY_PATH = "/v1/observer/ingest"
MAX_BODY_BYTES = 1_000_000  # 1 MB cap for observer envelopes
ALLOWED_ACCOUNT_MODES = {"demo", "contest"}


class ProxyConfigError(RuntimeError):
    """Invalid proxy configuration (fail-closed at startup)."""


def load_proxy_config(env=None) -> dict:
    env = env or os.environ
    proxy_token = env.get("OBSERVER_PROXY_TOKEN")
    ingest_url = env.get("LEDGER_INGEST_URL")
    ingest_token = env.get("LEDGER_INGEST_TOKEN")
    ingest_secret = env.get("LEDGER_INGEST_SECRET")
    missing = []
    if not proxy_token:
        missing.append("OBSERVER_PROXY_TOKEN")
    if not ingest_url:
        missing.append("LEDGER_INGEST_URL")
    if not ingest_token:
        missing.append("LEDGER_INGEST_TOKEN")
    if not ingest_secret:
        missing.append("LEDGER_INGEST_SECRET")
    if missing:
        raise ProxyConfigError(
            "observer signing proxy configuration incomplete: " + ", ".join(missing)
        )
    if not str(ingest_url).startswith("https://"):
        raise ProxyConfigError(
            "LEDGER_INGEST_URL must be https:// (strict signed remote ingress)"
        )
    return {
        "proxy_token": str(proxy_token),
        "ingest_url": str(ingest_url),
        "ingest_token": str(ingest_token),
        "ingest_secret": str(ingest_secret),
    }


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def validate_observer_envelope(raw: dict) -> tuple[bool, str]:
    """Schema + policy validation of the observer envelope (before forwarding).

    Returns (ok, error). Raises nothing; a rejected envelope must NOT be
    forwarded or signed.
    """
    # ТЗ 10.4: protocol version gate — unknown versions are never signed or
    # forwarded; a missing field counts as v1 (legacy observers).
    version_ok, version_err, _version = check_protocol_version(raw)
    if not version_ok:
        return False, version_err
    try:
        envelope = event_envelope_from_dict(raw)
    except Exception as exc:  # pydantic ValidationError and friends
        return False, f"invalid envelope schema: {exc}"
    if envelope.producer != "mt5_observer":
        return False, f"producer must be mt5_observer, got {envelope.producer!r}"
    for event in envelope.events:
        if event.account_mode not in ALLOWED_ACCOUNT_MODES:
            return False, (
                f"account_mode {event.account_mode!r} not allowed "
                f"(demo/contest only)"
            )
    return True, ""


def sign_raw_body(body: bytes, secret: str) -> str:
    """HMAC-SHA256 hex over the exact raw body bytes (constant-time compare
    happens server-side)."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class ObserverSigningProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # Config (set by the server factory; not class-global secrets)
    # ------------------------------------------------------------------
    proxy_token: str = ""
    ingest_url: str = ""
    ingest_token: str = ""
    ingest_secret: str = ""

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib name
        # Only safe metadata; never body/secrets.
        logger.info("proxy %s", fmt % args)

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status: int, message: str) -> None:
        logger.warning("reject status=%d reason=%s", status, message)
        self._send(status, {"status": "error", "detail": message})

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        if self.path != PROXY_PATH:
            self._reject(404, "not found")
            return

        # 1. observer proxy bearer (constant-time)
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not constant_time_eq(
            auth[len("Bearer "):], self.proxy_token
        ):
            self._reject(401, "proxy authorization required")
            return

        # 2. body size cap
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reject(413, "payload too large")
            return
        raw_body = self.rfile.read(length)

        # 3. schema/policy validation (NO forwarding on failure)
        try:
            raw = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self._reject(422, "invalid json")
            return
        ok, error = validate_observer_envelope(raw)
        if not ok:
            self._reject(422, error)
            return

        # 4. forward EXACT raw body to remote HTTPS with bearer + HMAC

        headers = {
            "Authorization": f"Bearer {self.ingest_token}",
            "Content-Type": "application/json",
            "X-Ledger-Signature": sign_raw_body(raw_body, self.ingest_secret),
        }
        try:
            response = _requests_post(
                self.ingest_url, data=raw_body, headers=headers, timeout=10.0
            )
        except Exception as exc:
            logger.warning("remote transport failure: %s", exc)
            self._reject(502, "remote ingest unavailable")
            return
        remote_status = response.status_code
        batch_id = raw.get("batch_id", "?")
        logger.info(
            "forward producer=mt5_observer account_mode=%s events=%d "
            "batch=%s remote_status=%d",
            raw.get("events", [{}])[0].get("account_mode", "?")
            if raw.get("events") else "?",
            len(raw.get("events", [])),
            batch_id,
            remote_status,
        )
        if 200 <= remote_status < 300:
            self._send(200, {"status": "ok", "remote_status": remote_status})
        else:
            self._reject(502, "remote ingest rejected")


def build_proxy_server(config: dict) -> HTTPServer:
    """Testable factory: returns an HTTPServer bound strictly to 127.0.0.1."""
    handler = ObserverSigningProxyHandler
    handler.proxy_token = config["proxy_token"]
    handler.ingest_url = config["ingest_url"]
    handler.ingest_token = config["ingest_token"]
    handler.ingest_secret = config["ingest_secret"]
    server = HTTPServer((LOOPBACK_HOST, 0), handler)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default=LOOPBACK_HOST,
                        help="MUST stay 127.0.0.1; any other value is rejected")
    args = parser.parse_args()
    if args.host != LOOPBACK_HOST:
        raise ProxyConfigError(
            f"proxy host must be {LOOPBACK_HOST} (loopback only); got {args.host!r}"
        )
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    config = load_proxy_config()
    server = HTTPServer((LOOPBACK_HOST, args.port),
                        ObserverSigningProxyHandler)
    ObserverSigningProxyHandler.proxy_token = config["proxy_token"]
    ObserverSigningProxyHandler.ingest_url = config["ingest_url"]
    ObserverSigningProxyHandler.ingest_token = config["ingest_token"]
    ObserverSigningProxyHandler.ingest_secret = config["ingest_secret"]
    logger.info("observer signing proxy listening on http://%s:%d%s",
                LOOPBACK_HOST, args.port, PROXY_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
