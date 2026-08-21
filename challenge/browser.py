"""Playwright session manager with a persistent profile.

The user logs in manually ONCE (no credentials ever enter the codebase); the
session lives in the persistent profile dir and is reused by every later run.
"""

import os
import time

from playwright.sync_api import sync_playwright


def launch(cfg):
    browser_cfg = cfg.get("browser", {})
    headless = bool(browser_cfg.get("headless", False))
    profile_dir = os.path.abspath(
        cfg.get("platform", {}).get("profile_dir", "data/challenge_browser_profile"))
    os.makedirs(profile_dir, exist_ok=True)
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        profile_dir,
        headless=headless,
        viewport={"width": 1440, "height": 900},
        locale="ru-RU",
        args=["--disable-blink-features=AutomationControlled"],
    )
    return pw, context


def open_page(context):
    return context.pages[0] if context.pages else context.new_page()


LOGIN_MARKERS = ("Вход в аккаунт", "Почта")
DASHBOARD_MARKERS = ("Заработать", "Начать торговлю")


def is_logged_in(page) -> bool:
    """The SPA renders the login form even on the dashboard route when
    unauthenticated, so the check looks at the visible content: the dashboard
    is only shown when the session is valid."""
    try:
        text = page.inner_text("body")
        has_login = any(m in text for m in LOGIN_MARKERS)
        has_dash = any(m in text for m in DASHBOARD_MARKERS)
        return has_dash and not has_login
    except Exception:
        return False


def ensure_logged_in(context, cfg):
    platform = cfg.get("platform", {})
    url = platform.get("url", "")
    wait_seconds = int(platform.get("login_wait_seconds", 900))
    if not url:
        raise RuntimeError("challenge.platform.url is not configured")
    deadline = time.time() + wait_seconds
    printed = False
    while time.time() < deadline:
        page = open_page(context)
        try:
            page.goto(url, wait_until="domcontentloaded")
            time.sleep(4)
            if is_logged_in(page):
                print("=== LOGGED IN:", page.url, "===", flush=True)
                return page
            if not printed:
                print("=== MANUAL LOGIN ===", flush=True)
                print("Log in in the opened browser window (up to %d s)." % wait_seconds,
                      flush=True)
                printed = True
            time.sleep(5)
        except Exception as e:
            print("login poll error (retrying): %s" % e)
            time.sleep(5)
    raise TimeoutError("Manual login was not completed in time")