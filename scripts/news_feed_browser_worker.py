"""
Browser worker CHILD process for the news feed service.

Owns the single persistent Chromium (headed, offscreen) and executes fetch
jobs issued by the parent `news_feed_server` over stdin/stdout:

    parent -> stdin : {"cmd": "calendar"}\n
    child  -> stdout: {"events": [...], "source": ..., "error": ...}\n

The parent is a pure HTTP process that can never hang: if THIS process
wedges (stuck renderer, dead driver, anything), the parent kills it after a
timeout and spawns a fresh one. Playwright's sync API is thread-bound, so
ALL driver calls run in this process's main thread - never in helper
threads.

Usage (spawned automatically by the parent):
    python -m scripts.news_feed_browser_worker
"""
import json
import logging
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("news_feed_browser_worker")

_FF_WWW_URL = "https://www.forexfactory.com/calendar"
_FF_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
# 2026-08-19: FF/Cloudflare was slow-to-blackhole for hours; the old 25s
# timeout treated every slow page as a dead browser and force-relaunched
# Chromium on every fetch. 60s + a single retry lets transient stalls pass.
_GOTO_TIMEOUT_MS = 60000
_WATCHDOG_SECONDS = 120

_browser = None
_playwright = None
_context = None
_page = None


def _kill_chromium(pid: int):
    """Hard-kill a stuck Chromium so a hanging page.goto raises and the
    worker can relaunch cleanly. Called from a watchdog timer thread."""
    try:
        signal_name = getattr(signal, "SIGTERM", None) or signal.SIGKILL
        import os
        os.kill(pid, signal_name)
        logger.warning("Watchdog: killed stuck Chromium (pid=%d)", pid)
    except Exception as exc:
        logger.warning("Watchdog kill failed (pid=%d): %s", pid, exc)


def _ensure_browser():
    """Launch the single persistent Chromium (headed, offscreen) on first use.
    If the stored browser object is dead (process killed by the watchdog or a
    crash), tear it down and start a fresh one - a stale non-None object must
    never short-circuit the relaunch."""
    global _browser, _playwright, _context, _page
    if _browser is not None:
        try:
            if _browser.is_connected():
                return
        except Exception:
            pass
        logger.warning("Persistent Chromium is dead; relaunching")
        _teardown_browser()
    from playwright.sync_api import sync_playwright  # lazy: heavy dep
    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(
        headless=False,
        channel="chromium",
        args=["--window-position=-32000,-32000"],
    )
    _context = _browser.new_context(
        user_agent=_FF_UA, viewport={"width": 1400, "height": 900})
    _page = _context.new_page()
    logger.info("Persistent Chromium launched (offscreen window)")


def _teardown_browser():
    """Close browser/context/page AND stop the Playwright driver. The sync
    driver cannot be restarted in the same thread without .stop() first, so a
    crash-relaunch that skipped stop() poisoned every later fetch with
    'Playwright Sync API inside the asyncio loop'."""
    global _browser, _playwright, _context, _page
    for obj in (_page, _context, _browser):
        try:
            if obj is not None:
                obj.close()
        except Exception:
            pass
    _page = _context = _browser = None
    if _playwright is not None:
        try:
            _playwright.stop()
        except Exception:
            pass
        _playwright = None


def _fetch_calendar_once() -> list:
    # Short navigation timeout: page.goto raises on its own (never hangs the
    # worker forever), and the watchdog below is the hard backstop.
    _page.goto(_FF_WWW_URL, timeout=_GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
    prev = -1
    stable = 0
    for _ in range(15):
        time.sleep(2)
        n = _page.locator("tr[data-event-id]").count()
        if n == prev:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        prev = n
    html = _page.content()

    soup = BeautifulSoup(html, "html.parser")
    current_day: str | None = None
    now = datetime.now(UTC)
    events: list = []
    for tr in soup.find_all("tr"):
        cls = " ".join(tr.get("class") or [])
        if "day-breaker" in cls:
            txt = " ".join(tr.get_text(" ", strip=True).split())
            if txt:
                current_day = txt
            continue
        if "data-event-id" not in tr.attrs:
            continue
        time_td = tr.find("td", class_="calendar__time")
        cur_td = tr.find("td", class_="calendar__currency")
        imp_span = tr.find("span", class_=lambda c: c and "ff-impact" in c)
        ev_span = tr.find("span", class_="calendar__event-title")
        date_td = tr.find("td", class_="calendar__date")
        if date_td and date_td.find("span", class_="date"):
            current_day = " ".join(date_td.get_text(" ", strip=True).split())
        if not (time_td and cur_td and imp_span and ev_span and current_day):
            continue
        imp_cls = " ".join(imp_span.get("class") or [])
        if "red" not in imp_cls:
            continue  # only High Impact Expected
        country = cur_td.get_text(strip=True).upper()
        if country not in ("USD", "ALL"):
            continue
        time_str = time_td.get_text(strip=True)
        if not time_str:
            continue  # TBA events have no usable timestamp
        try:
            day_parts = current_day.split()
            if len(day_parts) != 3:
                continue
            year = now.year
            if now.month == 12 and int(day_parts[2]) <= 15:
                year = now.year + 1  # December calendar shows January of next year
            event_dt = datetime.strptime(
                f"{current_day} {time_str}", "%a %b %d %I:%M%p")
            event_dt = event_dt.replace(year=year, tzinfo=ZoneInfo("America/New_York"))
        except Exception:
            continue
        event_dt = event_dt.astimezone(UTC)
        events.append({
            "title": ev_span.get_text(strip=True),
            "country": country,
            "timestamp_utc": int(event_dt.timestamp()),
            "datetime_str": event_dt.strftime("%Y-%m-%d %H:%M UTC"),
        })
    logger.info("calendar fetched: %d High-impact event(s)", len(events))
    return events


def _fetch_calendar() -> list:
    """Navigate the shared page to the calendar and parse High-impact USD
    events. If the browser is unusable, relaunch it once and retry; a single
    transient failure is retried with a short pause before relaunching."""
    try:
        _ensure_browser()
        return _fetch_calendar_once()
    except Exception:
        logger.warning("calendar fetch failed, retrying once in 5s")
        time.sleep(5)
        try:
            return _fetch_calendar_once()
        except Exception:
            logger.warning("browser unusable, relaunching persistent Chromium")
            _teardown_browser()
            _ensure_browser()
            return _fetch_calendar_once()


def main():
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
        except Exception:
            continue
        out = {
            "events": [],
            "source": "forexfactory_www_browser",
            "fetched_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        }
        if job.get("cmd") == "calendar":
            watchdog = None
            browser_pid = None
            try:
                if _browser is not None and getattr(_browser, "process", None) is not None:
                    browser_pid = int(_browser.process.pid)
                if browser_pid:
                    watchdog = threading.Timer(_WATCHDOG_SECONDS, _kill_chromium, args=(browser_pid,))
                    watchdog.daemon = True
                    watchdog.start()
                out["events"] = _fetch_calendar()
            except Exception as exc:
                logger.warning("calendar fetch failed: %s", exc)
                out["error"] = str(exc)
            finally:
                if watchdog is not None:
                    watchdog.cancel()
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
