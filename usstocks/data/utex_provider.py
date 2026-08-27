"""UTEX market-data provider — reuses the proven challenge/manual feed.

Extracted from challenge/manual/alerter.py so both the legacy manual alerter
and the usstocks signal-only stack share ONE implementation (ТЗ §6.1: adapt,
do not duplicate). Network behaviour is byte-compatible:

- refresh: requests -> Playwright chromium fallback on RF-network TLS errors;
- getCandlesToDate with the same headers/payload;
- integer prices arrive scaled by 1e8 and are normalized here.

Transport is injectable for offline tests (recorded JSON fixtures).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Callable, Dict, List, Optional

import requests

from shared.cache import TTLCache
from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError
from shared.retry import retry_with_backoff
from usstocks.models import Bar
from usstocks.session import NY

logger = logging.getLogger("usstocks.utex")

REFRESH_URL = ("https://api.utex.io/rest/grpc/"
               "com.unitedtraders.luna.sessionservice.api.sso.SsoService.refreshAuthorization")
GRPC_BASE = ("https://demoususdt-api-margin.utex.io/rest/grpc/"
             "com.unitedtraders.luna.utex.protocol.mobile.")
DEFAULT_TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "challenge_tokens.json")
REALM = "aurora"
CLIENT_ID = "utexweb"

_POST: Callable = requests.post


def _is_network_error(e: Exception) -> bool:
    """Same classification as the legacy alerter (RF-route TLS flakiness)."""
    msg = str(e)
    return (
        isinstance(e, (requests.exceptions.SSLError,
                       requests.exceptions.ConnectionError,
                       requests.exceptions.ReadTimeout,
                       requests.exceptions.Timeout))
        or "SSLEOF" in msg or "Read timed out" in msg
        or "handshake" in msg.lower() or "Max retries" in msg
        or "Timeout" in type(e).__name__
    )


def _playwright_post(url: str, payload: str, headers: Dict) -> Dict:
    """Last-resort route through Chromium's network stack."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context()
        resp = ctx.request.post(url, data=payload, headers=headers)
        if not resp.ok:
            raise RuntimeError(f"playwright post {resp.status}: {resp.text()[:200]}")
        data = resp.json()
        browser.close()
        return data


def decode_candles(data) -> List[dict]:
    """Normalize a UTEX candle payload into {time,o,h,l,c,v} dicts."""
    out: List[dict] = []
    for c in (data or {}).get("candles", []):
        def number(key):
            value = c[key]
            return float(value) / 1e8 if isinstance(value, int) else float(value)
        out.append({"time": int(c["time"]), "open": number("open"),
                    "high": number("high"), "low": number("low"),
                    "close": number("close"), "volume": float(c.get("volume", 0))})
    out.sort(key=lambda x: x["time"])
    return out


class UtexClient:
    """Auth + candle fetching against the UTEX mobile gRPC gateway with CircuitBreaker and caching."""

    def __init__(self, token_file: str = DEFAULT_TOKEN_FILE,
                 refresh_url: str = REFRESH_URL, grpc_base: str = GRPC_BASE,
                 post: Optional[Callable] = None,
                 token_ttl_seconds: float = 300.0,
                 circuit_breaker: Optional[CircuitBreaker] = None):
        self.token_file = token_file
        self.refresh_url = refresh_url
        self.grpc_base = grpc_base
        self._post = post or _POST
        self.circuit_breaker = circuit_breaker or CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
        self.token_cache = TTLCache(default_ttl_seconds=token_ttl_seconds)

    # -- auth --------------------------------------------------------------

    def _headers(self, access: Optional[str] = None) -> Dict:
        h = {"Content-Type": "application/json",
             "X-UT-GRPC-METADATA": "{}",
             "Origin": "https://markets-app.hashhedge.com",
             "Referer": "https://markets-app.hashhedge.com/",
             "User-Agent": "Mozilla/5.0"}
        if access:
            h["Authorization"] = "Bearer " + access
            h["X-B3-SpanId"] = uuid.uuid4().hex[:16]
            h["X-B3-TraceId"] = uuid.uuid4().hex[:16]
        return h

    def refresh_access(self) -> str:
        cached = self.token_cache.get("access_token")
        if cached:
            return str(cached)

        with open(self.token_file, encoding="utf-8") as f:
            rt = json.load(f)["refresh_token"]
        payload = {"realm": REALM, "clientId": CLIENT_ID, "refreshToken": rt}
        body = json.dumps(payload)

        def _do_refresh():
            try:
                r = self._post(self.refresh_url, json=payload,
                               headers=self._headers(), timeout=30)
                r.raise_for_status()
                token = r.json()["accessToken"]
                self.token_cache.set("access_token", token)
                return token
            except Exception as e:
                if not _is_network_error(e):
                    raise
                logger.warning("refresh via requests failed (%s), trying Playwright",
                               type(e).__name__)
                try:
                    data = _playwright_post(self.refresh_url, body, self._headers())
                    token = data["accessToken"]
                    self.token_cache.set("access_token", token)
                    return token
                except Exception as e2:
                    logger.error("playwright refresh fallback failed: %s", e2)
                    raise e

        return self.circuit_breaker.call(_do_refresh)

    # -- candles -----------------------------------------------------------

    def fetch_candle_dicts(self, access: str, symbol_id, candles_count: int = 720,
                           to_ts: Optional[int] = None) -> List[dict]:
        """Legacy-compatible output: list of dicts sorted by epoch time."""
        payload = {"to": to_ts if to_ts is not None else int(time.time()),
                   "symbolId": symbol_id, "candlesCount": candles_count,
                   "interval": "Min1"}
        url = self.grpc_base + "MobileDataService.getCandlesToDate"

        def _do_fetch():
            try:
                r = self._post(url, json=payload, headers=self._headers(access),
                               timeout=60)
                if r.status_code != 200:
                    raise RuntimeError(
                        f"getCandlesToDate {symbol_id}: {r.status_code} {r.text[:200]}")
                out = decode_candles(r.json())
            except Exception as e:
                if not _is_network_error(e):
                    raise
                logger.warning("getCandles %s via requests failed (%s), "
                               "trying Playwright", symbol_id, type(e).__name__)
                data = _playwright_post(url, json.dumps(payload), self._headers(access))
                out = decode_candles(data)
            if not out:
                raise RuntimeError(f"getCandles {symbol_id}: empty candle response")
            return out

        return self.circuit_breaker.call(_do_fetch)

    def fetch_bars(self, access: str, symbol_id, candles_count: int = 720,
                   to_ts: Optional[int] = None) -> List[Bar]:
        """Same payload as Bars with tz-aware UTC timestamps."""
        dicts = self.fetch_candle_dicts(access, symbol_id, candles_count, to_ts)
        from datetime import datetime, timezone
        return [Bar(ts=datetime.fromtimestamp(d["time"], timezone.utc),
                    open=d["open"], high=d["high"], low=d["low"],
                    close=d["close"], volume=d["volume"]) for d in dicts]


# ---------------------------------------------------------------------------
# Module-level delegates kept for the legacy manual alerter (same names /
# signatures so its behaviour — including tests — stays unchanged).
# ---------------------------------------------------------------------------

_default_client = UtexClient()


def refresh_access(token_file: str = DEFAULT_TOKEN_FILE) -> str:
    return UtexClient(token_file=token_file).refresh_access()


def fetch_candles(access: str, symbol_id, candles_count: int = 720) -> List[dict]:
    return _default_client.fetch_candle_dicts(access, symbol_id, candles_count)


def bars_to_scanner_dicts(bars: List[Bar]) -> List[dict]:
    """Bridge Bars back into the scanner's {time,...} dict shape (UTC epoch)."""
    return [{"time": int(b.ts.timestamp()), "open": b.open, "high": b.high,
             "low": b.low, "close": b.close, "volume": b.volume} for b in bars]
