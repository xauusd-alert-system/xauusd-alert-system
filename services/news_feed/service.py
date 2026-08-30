"""News Feed service (TZ 8.8) — background calendar-cache refresher.

Thin wrapper around ``news/calendar_feed.py`` (``CalendarFeed``) and the
HTTP server in ``scripts/news_feed_server.py``. This module duplicates no
fetch/parse/cache logic: the refresh loop simply drives the existing
``CalendarFeed.get_all()`` (which refreshes in-memory + disk cache), and the
optional ``--serve-http`` mode starts the existing browser server script.

Health checks (``GET /health``):

* ``cache_file``   — the disk cache ``data/news_calendar_cache.json`` exists
  and parses (format is NOT changed — same ``{"ts": ..., "events": [...]}``);
* ``cache_fresh``  — the cache ``ts`` is younger than
  ``max_cache_age_hours`` (default 6h, matching ``news_feed_max_age_minutes``
  semantics in config) — otherwise degraded.

Run: ``python -m services.news_feed [--max-cache-age-hours 6] [--health-port 8793]``
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.base import start_health_server_thread  # noqa: E402

DEFAULT_HEALTH_PORT = 8793
DEFAULT_MAX_CACHE_AGE_HOURS = 6.0
DEFAULT_REFRESH_INTERVAL_SECONDS = 900.0  # 15 min; feed itself TTLs at 1h

SERVICE_NAME = "news_feed"
DEFAULT_CACHE_PATH = os.path.join("data", "news_calendar_cache.json")


def cache_age_seconds(cache_path: str = DEFAULT_CACHE_PATH) -> Optional[float]:
    """Age of the disk cache in seconds (from its ``ts`` field), or None.

    Uses the cache's own ``ts`` (monotonic freshness of the DATA), not the
    file mtime. Returns None when the file is missing or corrupt.
    """
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = float(data.get("ts", 0))
        if ts <= 0:
            return None
        return max(0.0, time.time() - ts)
    except (OSError, ValueError, TypeError):
        return None


def make_cache_file_check(
    cache_path: str = DEFAULT_CACHE_PATH,
) -> Callable[[], tuple[bool, str]]:
    """Health check: the disk cache exists and parses (format unchanged)."""

    def check() -> tuple[bool, str]:
        if not os.path.exists(cache_path):
            return False, f"cache file missing: {cache_path}"
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            return False, f"cache file unreadable/corrupt: {exc}"
        events = data.get("events")
        if not isinstance(events, list):
            return False, "cache format unexpected ('events' list missing)"
        return True, f"ok ({len(events)} events)"

    return check


def make_cache_freshness_check(
    cache_path: str = DEFAULT_CACHE_PATH,
    max_age_hours: float = DEFAULT_MAX_CACHE_AGE_HOURS,
) -> Callable[[], tuple[bool, str]]:
    """Health check: cache ts is within ``max_age_hours`` (else degraded)."""

    def check() -> tuple[bool, str]:
        age = cache_age_seconds(cache_path)
        if age is None:
            return False, f"cache missing or has no valid ts: {cache_path}"
        if age > float(max_age_hours) * 3600.0:
            return (
                False,
                f"degraded: cache age {age / 3600.0:.1f}h exceeds budget {float(max_age_hours):.1f}h",
            )
        return True, f"ok (cache age {age / 3600.0:.2f}h)"

    return check


def build_checks(
    cache_path: str = DEFAULT_CACHE_PATH,
    max_age_hours: float = DEFAULT_MAX_CACHE_AGE_HOURS,
) -> dict:
    """Assemble the service checks dict (unit-tested without the network)."""
    return {
        "cache_file": make_cache_file_check(cache_path),
        "cache_fresh": make_cache_freshness_check(cache_path, max_age_hours),
    }


def refresh_once() -> int:
    """Run one refresh of the existing CalendarFeed (no logic duplicated).

    ``get_all()`` triggers ``_ensure_cache`` -> API refresh + disk save.
    Returns the number of cached events.
    """
    from news.calendar_feed import CalendarFeed

    return len(CalendarFeed().get_all())


def run(args: argparse.Namespace) -> None:
    """Entry point: health server + periodic cache refresh loop.

    With ``--serve-http`` the existing ``scripts.news_feed_server.main`` is
    exec'd in a child process (the browser HTTP service stays untouched).
    """
    checks = build_checks(args.cache_path, args.max_cache_age_hours)
    server = start_health_server_thread(args.health_port, checks)

    print(f"[{os.getpid()}] news_feed service up (health: http://127.0.0.1:{args.health_port}/health)")
    try:
        while True:
            try:
                n = refresh_once()
                print(f"[{os.getpid()}] refresh ok ({n} events)")
            except Exception as exc:
                print(f"[{os.getpid()}] refresh failed: {exc}")
            time.sleep(args.refresh_interval)
    finally:
        server.should_exit = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {__name__.rsplit('.', 1)[0]}",
        description="News feed service (TZ 8.8): background economic-calendar "
        "cache refresher with a freshness health endpoint.",
    )
    parser.add_argument("--cache-path", default=DEFAULT_CACHE_PATH)
    parser.add_argument("--max-cache-age-hours", type=float, default=DEFAULT_MAX_CACHE_AGE_HOURS)
    parser.add_argument("--refresh-interval", type=float, default=DEFAULT_REFRESH_INTERVAL_SECONDS)
    parser.add_argument("--health-port", type=int, default=DEFAULT_HEALTH_PORT)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
