"""Playwright session manager with a persistent profile and stealth fingerprint.

The user logs in manually ONCE (no credentials ever enter the codebase);
the session lives in the persistent profile dir and is reused by every later run.

Stealth fingerprint: headful, real UA, 1920x1080, timezone ET (America/New_York).
All timing constants live inside BrowserHumanizer, not here.
"""

import os
import time
import random
from datetime import datetime

from playwright.sync_api import sync_playwright

from execution.stealth.config import StealthConfig


def launch(cfg, stealth_config: StealthConfig = None):
    """Launch browser with stealth fingerprint."""
    browser_cfg = cfg.get("browser", {})
    # Stealth overrides from config
    if stealth_config is None:
        try:
            from config.loader import load_config
            full_cfg = load_config()
            stealth_cfg_dict = full_cfg.get("stealth", {}) or {}
            stealth_config = StealthConfig.from_dict(stealth_cfg_dict)
        except Exception:
            stealth_config = StealthConfig()

    # Fingerprint from stealth config (no hardcoded outside stealth modules except defaults here that mirror stealth)
    headless = stealth_config.browser_headless if hasattr(stealth_config, "browser_headless") else bool(browser_cfg.get("headless", False))
    viewport_w, viewport_h = stealth_config.browser_viewport if hasattr(stealth_config, "browser_viewport") else (1920, 1080)
    user_agent = stealth_config.browser_user_agent if hasattr(stealth_config, "browser_user_agent") else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    timezone_id = stealth_config.browser_timezone if hasattr(stealth_config, "browser_timezone") else "America/New_York"

    profile_dir = os.path.abspath(
        cfg.get("platform", {}).get("profile_dir", "data/challenge_browser_profile"))
    os.makedirs(profile_dir, exist_ok=True)

    pw = sync_playwright().start()

    # Use stealth fingerprint
    context = pw.chromium.launch_persistent_context(
        profile_dir,
        headless=headless,
        viewport={"width": viewport_w, "height": viewport_h},
        locale="en-US",
        timezone_id=timezone_id,
        user_agent=user_agent,
        args=["--disable-blink-features=AutomationControlled"],
    )

    # Optional: set extra headers to look human
    # Randomize slightly the viewport? Keep 1920x1080 as per spec
    return pw, context


def open_page(context):
    return context.pages[0] if context.pages else context.new_page()


LOGIN_MARKERS = ("Вход в аккаунт", "Почта")
DASHBOARD_MARKERS = ("Заработать", "Начать торговлю")


def is_logged_in(page) -> bool:
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
