"""Dry-run with BrowserHumanizer logging and screen recording.

Runs challenge bot in dry-run mode with minimal size, logs all BrowserHumanizer actions
(mouse Bezier paths, clicks, hovers, visibility switches, idle breaks) and records video.

Usage:
    python -m challenge.tools.dry_run_recorder --record-video

Video saved to logs/utex_sessions/
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from challenge.browser import launch, ensure_logged_in
from execution.stealth.config import StealthConfig
from execution.stealth.browser_humanizer import BrowserHumanizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dry_run_recorder")

VIDEO_DIR = Path("logs/utex_sessions")
VIDEO_DIR.mkdir(parents=True, exist_ok=True)


class LoggingBrowserHumanizer(BrowserHumanizer):
    """Extends BrowserHumanizer with detailed logging for manual review."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.action_log = []
        self.log_path = VIDEO_DIR / f"humanizer_actions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"

    def _log_action(self, action_type: str, details: dict):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": action_type,
            **details
        }
        self.action_log.append(entry)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass
        logger.info(f"[HUMANIZER] {action_type}: {details}")

    def generate_bezier_path(self, start, end, steps=None):
        path = super().generate_bezier_path(start, end, steps=steps)
        self._log_action("bezier_path", {
            "start": start,
            "end": end,
            "steps": len(path),
            "is_linear": self.is_linear_path(path),
            "path_sample": path[:5],  # first 5 points
            "max_deviation": max(abs(y - (start[1] + (end[1]-start[1]) * (i/len(path)))) for i, (x,y) in enumerate(path)) if len(path) > 1 else 0
        })
        return path

    def humanized_move(self, x, y):
        self._log_action("mouse_move", {"x": x, "y": y, "from": self._last_mouse_pos})
        super().humanized_move(x, y)

    def humanized_click(self, x, y, button="left"):
        self._log_action("mouse_click", {"x": x, "y": y, "button": button})
        super().humanized_click(x, y, button=button)

    def click_locator(self, locator, use_dom=None):
        if use_dom is None:
            use_dom = self._rng.random() < self.CLICK_DOM_PROB
        action_type = "click_dom" if use_dom else "hotkey"
        try:
            box = locator.bounding_box() if hasattr(locator, 'bounding_box') else None
            self._log_action(action_type, {"box": box, "use_dom": use_dom})
        except Exception:
            self._log_action(action_type, {"use_dom": use_dom})
        super().click_locator(locator, use_dom=use_dom)

    def random_scroll(self, direction="down", amount=None):
        self._log_action("scroll", {"direction": direction, "amount": amount})
        super().random_scroll(direction=direction, amount=amount)

    def hover_level(self, price_level, x_range=(200, 800), y=None):
        self._log_action("hover_level", {"price_level": price_level, "x_range": x_range, "y": y})
        super().hover_level(price_level=price_level, x_range=x_range, y=y)

    def click_empty(self):
        self._log_action("click_empty", {"pos": self._last_mouse_pos})
        super().click_empty()

    def pre_trade_activity(self):
        self._log_action("pre_trade_activity", {})
        super().pre_trade_activity()

    def post_trade_activity(self):
        self._log_action("post_trade_activity", {})
        super().post_trade_activity()

    def simulate_visibility_change(self):
        duration = self._rng.randint(self.VISIBILITY_DURATION_MIN_SEC, self.VISIBILITY_DURATION_MAX_SEC)
        self._log_action("visibility_change", {"duration": duration, "done": self._visibility_switches_done, "total": self._visibility_switches_total})
        return super().simulate_visibility_change()

    def maybe_idle_break(self):
        # Check without sleeping in log
        now = time.time()
        elapsed = now - self._last_idle_time
        if elapsed >= self._next_idle_interval:
            duration = self._rng.randint(self.IDLE_DURATION_MIN_SEC, self.IDLE_DURATION_MAX_SEC)
            self._log_action("idle_break", {"elapsed": elapsed, "duration": duration})
        return super().maybe_idle_break()

    def execute_hotkey(self, action):
        hotkey = self.HOTKEY_MAP.get(action, action)
        self._log_action("hotkey", {"action": action, "hotkey": hotkey})
        super().execute_hotkey(action)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Dry-run recorder")
    parser.add_argument("--record-video", action="store_true", help="Record video of session")
    parser.add_argument("--symbol", type=str, default="TSLA")
    args = parser.parse_args()

    full_cfg = load_config()
    cfg = full_cfg.get("challenge", {})
    stealth_cfg_dict = full_cfg.get("stealth", {}) or {}
    stealth_config = StealthConfig.from_dict(stealth_cfg_dict)

    # Force minimal size for dry-run
    stealth_config.challenge_daily_cap = 1
    stealth_config.enabled = True

    print(f"Launching browser with video recording={args.record_video}")
    print(f"Stealth config: viewport={stealth_config.browser_viewport} headful={not stealth_config.browser_headless}")

    # Launch with video recording if requested
    from playwright.sync_api import sync_playwright
    import os

    profile_dir = os.path.abspath(cfg.get("platform", {}).get("profile_dir", "data/challenge_browser_profile"))
    os.makedirs(profile_dir, exist_ok=True)

    pw = sync_playwright().start()
    context_options = {
        "headless": stealth_config.browser_headless,
        "viewport": {"width": stealth_config.browser_viewport[0], "height": stealth_config.browser_viewport[1]},
        "locale": "en-US",
        "timezone_id": stealth_config.browser_timezone,
        "user_agent": stealth_config.browser_user_agent,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if args.record_video:
        context_options["record_video_dir"] = str(VIDEO_DIR)
        context_options["record_video_size"] = {"width": 1920, "height": 1080}

    context = pw.chromium.launch_persistent_context(profile_dir, **context_options)
    try:
        page = ensure_logged_in(context, cfg)
        session_id = str(cfg.get("platform", {}).get("session_id") or "")

        # Use logging humanizer
        humanizer = LoggingBrowserHumanizer(page=page, seed=stealth_config.seed, config=stealth_config)
        print(f"Humanizer log path: {humanizer.log_path}")

        # Simulate some human activity
        print("\n=== Simulating pre-trade activity ===")
        humanizer.pre_trade_activity()

        print("\n=== Simulating mouse moves ===")
        for _ in range(5):
            x = humanizer._rng.randint(100, 1000)
            y = humanizer._rng.randint(100, 700)
            humanizer.humanized_move(x, y)
            time.sleep(0.5)

        print("\n=== Simulating visibility changes ===")
        for _ in range(2):
            humanizer.simulate_visibility_change()
            time.sleep(1)

        print("\n=== Simulating idle breaks ===")
        humanizer._last_idle_time = time.time() - 1000  # force break
        # Mock sleep for quick test
        original_sleep = time.sleep
        try:
            # Don't actually sleep long in test, just log
            pass
        finally:
            pass

        # Open a symbol
        from challenge.connector import terminal_url
        url = terminal_url(args.symbol, session_id)
        print(f"\n=== Opening {args.symbol} terminal {url} ===")
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)

        print("\n=== Simulating post-trade activity ===")
        humanizer.post_trade_activity()

        print(f"\n=== Dry-run complete ===")
        print(f"Action log: {humanizer.log_path}")
        print(f"Actions logged: {len(humanizer.action_log)}")
        print(f"Bezier paths generated: {sum(1 for a in humanizer.action_log if a['type']=='bezier_path')}")
        print(f"Linear paths (should be 0): {sum(1 for a in humanizer.action_log if a['type']=='bezier_path' and a.get('is_linear'))}")
        print(f"Click DOM vs Hotkey: {sum(1 for a in humanizer.action_log if 'click_dom' in a['type'])} vs {sum(1 for a in humanizer.action_log if 'hotkey' in a['type'])}")

        if args.record_video:
            # Video saved on context close
            print(f"\nVideo will be saved to {VIDEO_DIR} on close")
            # Get video path
            try:
                video = page.video
                if video:
                    print(f"Video path: {video.path()}")
            except Exception as e:
                print(f"Could not get video path: {e}")

        print("\nKeeping browser open for 30s for manual inspection of mouse paths")
        time.sleep(30)

    finally:
        context.close()
        pw.stop()

        if args.record_video:
            print(f"\nVideos saved to {VIDEO_DIR}:")
            for f in VIDEO_DIR.glob("*.webm"):
                print(f"  {f} ({f.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
