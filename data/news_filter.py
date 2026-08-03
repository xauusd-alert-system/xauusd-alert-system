"""
Economic Calendar News Guard - fetches High-Impact USD news events from Forex Factory
and suppresses trading during volatile news windows in LIVE mode only.
Completely silent and network-free during historical backtests.
"""
import time
import logging
import requests
from datetime import datetime, timezone
from typing import List, Dict

logger = logging.getLogger("news_guard")

_NEWS_CACHE: List[Dict] = []
_LAST_FETCH_TS: float = 0.0
_FETCH_FAILED_UNTIL: float = 0.0
CACHE_TTL_SECONDS = 6 * 3600  # 6 часов кэша


def fetch_economic_calendar() -> List[Dict]:
    """
    Fetches weekly economic events from Forex Factory JSON API with Chrome User-Agent.
    Suppresses repeated failed attempts for 30 minutes to prevent hangs/log-spam.
    """
    global _NEWS_CACHE, _LAST_FETCH_TS, _FETCH_FAILED_UNTIL
    now = time.time()

    # Если была ошибка сети, не повторяем запросы 30 минут
    if now < _FETCH_FAILED_UNTIL:
        return _NEWS_CACHE

    if _NEWS_CACHE and (now - _LAST_FETCH_TS < CACHE_TTL_SECONDS):
        return _NEWS_CACHE

    url = "https://nfs.forexfactory.com/forexcalendar.json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=3)
        resp.raise_for_status()
        data = resp.json()

        high_impact_events = []
        for event in data:
            if event.get("impact") == "High" and event.get("country") in ("USD", "ALL"):
                date_str = event.get("date")
                if date_str:
                    try:
                        event_dt = datetime.fromisoformat(date_str).astimezone(timezone.utc)
                        high_impact_events.append({
                            "title": event.get("title", "High Impact News"),
                            "country": event.get("country"),
                            "timestamp_utc": int(event_dt.timestamp()),
                            "datetime_str": event_dt.strftime("%Y-%m-%d %H:%M UTC")
                        })
                    except Exception:
                        pass

        _NEWS_CACHE = high_impact_events
        _LAST_FETCH_TS = now
        return _NEWS_CACHE

    except Exception:
        # При ошибке сети/SSL блокируем сетевые попытки на 30 минут, работаем без новостей
        _FETCH_FAILED_UNTIL = now + 1800
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