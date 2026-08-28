"""
Economic Calendar News Guard - fetches High-Impact USD news events from Forex Factory
and suppresses trading during volatile news windows in LIVE mode only.
Completely silent and network-free during historical backtests.
"""
import csv
import logging
import os
import time
from datetime import UTC, datetime
from typing import Dict, List

import requests

from config.loader import get_env

logger = logging.getLogger("news_guard")

_NEWS_CACHE: List[Dict] = []
_LAST_FETCH_TS: float = 0.0
_FETCH_FAILED_UNTIL: float = 0.0
_LAST_FETCH_OK: bool | None = None
_LAST_ERROR: str | None = None
_LAST_SOURCE: str | None = None
CACHE_TTL_SECONDS = 6 * 3600  # 6 часов кэша

_FF_NFS_URL = "https://nfs.forexfactory.com/forexcalendar.json"
_FF_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def _build_event(title: str, country: str, event_dt: datetime) -> Dict:
    event_dt = event_dt.astimezone(UTC)
    return {
        "title": title,
        "country": country,
        "timestamp_utc": int(event_dt.timestamp()),
        "datetime_str": event_dt.strftime("%Y-%m-%d %H:%M UTC"),
    }


def _fetch_ff_json() -> tuple[List[Dict], str]:
    """Forex Factory JSON API (nfs.forexfactory.com). Returns (events, source)."""
    headers = {"User-Agent": _FF_UA}
    proxy = get_env("NEWS_FEED_PROXY", default="")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.get(_FF_NFS_URL, headers=headers, timeout=3, proxies=proxies)
    resp.raise_for_status()
    data = resp.json()
    high_impact_events = []
    for event in data:
        if event.get("impact") == "High" and event.get("country") in ("USD", "ALL"):
            date_str = event.get("date")
            if date_str:
                try:
                    event_dt = datetime.fromisoformat(date_str)
                    high_impact_events.append(
                        _build_event(event.get("title", "High Impact News"),
                                     event.get("country"), event_dt))
                except Exception:
                    pass
    return high_impact_events, "forexfactory_json_api"


def _fetch_ff_service() -> tuple[List[Dict], str]:
    """Persistent background browser service (scripts/news_feed_server.py).
    One shared Chromium lives in its own window; this is just an HTTP call."""
    base = (get_env("NEWS_FEED_SERVICE_URL", default="http://127.0.0.1:8765") or "").rstrip("/")
    resp = requests.get(f"{base}/calendar", timeout=90)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload.get("events", []), payload.get("source", "forexfactory_www_browser")


def fetch_economic_calendar() -> List[Dict]:
    """
    Fetches High-Impact USD news events from Forex Factory.

    Primary path: the shared background browser service (one persistent Chromium
    owned by scripts/news_feed_server.py) - Cloudflare lets real browsers through
    even when the JSON API host resets non-browser clients.
    Fallback: the nfs.forexfactory.com/forexcalendar.json API (with optional
    NEWS_FEED_PROXY). Suppresses repeated failed attempts for 30 minutes.
    """
    global _NEWS_CACHE, _LAST_FETCH_TS, _FETCH_FAILED_UNTIL, _LAST_FETCH_OK, _LAST_ERROR, _LAST_SOURCE
    now = time.time()

    # Если была ошибка сети, не повторяем запросы 30 минут
    if now < _FETCH_FAILED_UNTIL:
        return _NEWS_CACHE

    if _NEWS_CACHE and (now - _LAST_FETCH_TS < CACHE_TTL_SECONDS):
        return _NEWS_CACHE

    method = (get_env("NEWS_FEED_METHOD", default="browser") or "browser").lower()
    attempts: List[tuple] = []
    if method == "browser":
        # 1) shared background browser service (scripts/news_feed_server.py),
        # 2) JSON API. No inline browser: the service owns the single Chromium.
        attempts = [(_fetch_ff_service,), (_fetch_ff_json,)]
    else:
        attempts = [(_fetch_ff_json,)]

    last_error: str | None = None
    for fetcher, in attempts:
        try:
            events, source = fetcher()
            _NEWS_CACHE = events
            _LAST_FETCH_TS = now
            _LAST_FETCH_OK = True
            _LAST_ERROR = None
            _LAST_SOURCE = source
            return _NEWS_CACHE
        except Exception as exc:
            last_error = str(exc)
            logger.warning("news feed fetch failed (%s): %s", getattr(fetcher, "__name__", fetcher), last_error)

    # Обе схемы недоступны: блокируем сетевые попытки на 30 минут, работаем без новостей
    _FETCH_FAILED_UNTIL = now + 1800
    _LAST_FETCH_OK = False
    _LAST_ERROR = last_error or "no successful fetch"
    _LAST_SOURCE = None
    return _NEWS_CACHE


def is_news_red_zone(
    current_ts_utc: int,
    buffer_before_minutes: int = 30,
    buffer_after_minutes: int = 30
) -> tuple[bool, str]:
    """
    Checks if current_ts_utc falls within a High-Impact news window.
    Instantly returns False for historical backtest candles (>7 days old) without network calls.
    """
    if not current_ts_utc:
        return False, ""

    # 🚨 ПЕРВАЯ И ГЛАВНАЯ ПРОВЕРКА: Для истории бэктеста мгновенный пропуск БЕЗ СЕТИ!
    if (time.time() - current_ts_utc) > (7 * 86400):
        return False, ""

    events = fetch_economic_calendar()
    if not events:
        return False, ""

    buffer_before_sec = buffer_before_minutes * 60
    buffer_after_sec = buffer_after_minutes * 60

    for event in events:
        news_ts = event["timestamp_utc"]
        window_start = news_ts - buffer_before_sec
        window_end = news_ts + buffer_after_sec

        if window_start <= current_ts_utc <= window_end:
            title = event["title"]
            dt_str = event["datetime_str"]
            return True, f"RED ZONE: {title} ({dt_str})"

    return False, ""

def news_feed_status() -> dict:
    """Distinguish an available empty calendar from an unavailable feed."""
    age = (time.time() - _LAST_FETCH_TS) if _LAST_FETCH_TS else None
    return {
        "available": bool(_LAST_FETCH_OK),
        "last_success_age_seconds": age if _LAST_FETCH_OK else None,
        "error": _LAST_ERROR,
        "event_count": len(_NEWS_CACHE),
        "source": _LAST_SOURCE,
    }


def news_guard_decision(current_ts_utc: int, buffer_before_minutes: int = 30,
                        buffer_after_minutes: int = 30,
                        failure_policy: str = "fail_closed",
                        historical_calendar_path: str | None = None) -> tuple[bool, str, bool]:
    """Return (blocked, reason, feed_available), with optional dated research CSV."""
    if (time.time() - int(current_ts_utc or 0)) > 7 * 86400:
        if not historical_calendar_path or not os.path.exists(historical_calendar_path):
            return False, "historical_calendar_not_modelled", False
        before, after = buffer_before_minutes * 60, buffer_after_minutes * 60
        with open(historical_calendar_path, encoding="utf-8", newline="") as handle:
            for event in csv.DictReader(handle):
                if event.get("impact") not in {None, "", "High"}:
                    continue
                event_ts = int(event["timestamp_utc"])
                if event_ts - before <= int(current_ts_utc) <= event_ts + after:
                    return True, f"HISTORICAL RED ZONE: {event.get('title', 'High Impact News')}", True
        return False, "historical_calendar_clear", True
    blocked, reason = is_news_red_zone(current_ts_utc, buffer_before_minutes, buffer_after_minutes)
    status = news_feed_status()
    if not status["available"] and failure_policy == "fail_closed":
        return True, f"NEWS FEED UNAVAILABLE: {status.get('error') or 'no successful fetch'}", False
    return blocked, reason, bool(status["available"])
