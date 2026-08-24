"""BrowserHumanizer — Playwright human behavior for UTEx challenge.

Simulates human interaction to avoid bot detection.
All constants inside class.
"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional, Dict, Any


class BrowserHumanizer:
    """Human-like browser automation.

    - Mouse path: Bezier curves, micro jitter, no teleport
    - Page Visibility API: 2-3 times per session background 30-120s
    - Action variance: 70% DOM click, 30% hotkeys
    - Idle micro-breaks every 8-15 min pause 20-60s
    - Pre-trade: scroll chart, hover levels, clicks empty (drawing)
    - Post-trade: micro movements, hover position
    - Fingerprint: headful, real UA, 1920x1080, timezone ET
    """

    # Mouse
    BEZIER_STEPS_MIN = 20
    BEZIER_STEPS_MAX = 40
    MOUSE_JITTER_MIN_PX = 1
    MOUSE_JITTER_MAX_PX = 3
    MOVE_DURATION_MIN_MS = 150
    MOVE_DURATION_MAX_MS = 600

    # Page Visibility
    VISIBILITY_SWITCHES_MIN = 2
    VISIBILITY_SWITCHES_MAX = 3
    VISIBILITY_DURATION_MIN_SEC = 30
    VISIBILITY_DURATION_MAX_SEC = 120

    # Action variance
    CLICK_DOM_PROB = 0.70
    HOTKEY_PROB = 0.30

    # Idle breaks
    IDLE_INTERVAL_MIN_SEC = 8 * 60
    IDLE_INTERVAL_MAX_SEC = 15 * 60
    IDLE_DURATION_MIN_SEC = 20
    IDLE_DURATION_MAX_SEC = 60

    # Fingerprint
    VIEWPORT_WIDTH = 1920
    VIEWPORT_HEIGHT = 1080
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    TIMEZONE = "America/New_York"
    LOCALE = "en-US"

    def __init__(
        self,
        page: Optional[Any] = None,
        seed: Optional[int] = None,
        config: Optional[object] = None,
    ):
        self._rng = random.Random(seed)
        self.page = page

        self.HOTKEY_MAP: Dict[str, str] = {
            "buy_market_best_ask": "F1",
            "sell_market_best_bid": "F2",
            "buy_limit_best_bid": "F3",
            "sell_limit_best_ask": "F4",
            "buy_stop_mark": "F9",
            "sell_stop_mark": "F10",
            "buy_market_mark": "Shift+F1",
            "sell_market_mark": "Shift+F2",
            "close_position": "Shift+F3",
            "cancel_all": "Shift+F4",
        }

        if config is not None:
            self.VIEWPORT_WIDTH, self.VIEWPORT_HEIGHT = config.browser_viewport
            self.USER_AGENT = config.browser_user_agent
            self.TIMEZONE = config.browser_timezone
            self.BEZIER_STEPS_MIN, self.BEZIER_STEPS_MAX = config.browser_mouse_bezier_steps
            self.MOUSE_JITTER_MIN_PX, self.MOUSE_JITTER_MAX_PX = config.browser_mouse_jitter_px
            self.VISIBILITY_SWITCHES_MIN, self.VISIBILITY_SWITCHES_MAX = config.browser_visibility_switches_per_session
            self.VISIBILITY_DURATION_MIN_SEC, self.VISIBILITY_DURATION_MAX_SEC = config.browser_visibility_duration_range
            self.CLICK_DOM_PROB = config.browser_action_click_dom_prob
            self.HOTKEY_PROB = config.browser_action_hotkey_prob
            self.IDLE_INTERVAL_MIN_SEC, self.IDLE_INTERVAL_MAX_SEC = config.browser_idle_break_interval_range
            self.IDLE_DURATION_MIN_SEC, self.IDLE_DURATION_MAX_SEC = config.browser_idle_break_duration_range
            if hasattr(config, "browser_hotkey_map") and config.browser_hotkey_map:
                self.HOTKEY_MAP = config.browser_hotkey_map

        self._last_mouse_pos: Tuple[float, float] = (self._rng.uniform(100, 500), self._rng.uniform(100, 500))
        self._last_idle_time: float = time.time()
        self._next_idle_interval: int = self._rng.randint(self.IDLE_INTERVAL_MIN_SEC, self.IDLE_INTERVAL_MAX_SEC)
        self._visibility_switches_done: int = 0
        self._visibility_switches_total: int = self._rng.randint(self.VISIBILITY_SWITCHES_MIN, self.VISIBILITY_SWITCHES_MAX)

    def _bezier_point(self, p0, p1, p2, p3, t):
        """Cubic Bezier point."""
        u = 1 - t
        tt = t * t
        uu = u * u
        uuu = uu * u
        ttt = tt * t
        x = uuu * p0[0] + 3 * uu * t * p1[0] + 3 * u * tt * p2[0] + ttt * p3[0]
        y = uuu * p0[1] + 3 * uu * t * p1[1] + 3 * u * tt * p2[1] + ttt * p3[1]
        return x, y

    def generate_bezier_path(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        steps: Optional[int] = None,
    ) -> List[Tuple[float, float]]:
        """Generate Bezier curve path from start to end."""
        if steps is None:
            steps = self._rng.randint(self.BEZIER_STEPS_MIN, self.BEZIER_STEPS_MAX)

        # Control points with random offset for human-like curvature
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        dist = math.hypot(dx, dy)

        # Random control point offsets proportional to distance
        # For zero/small distance, keep offset small to avoid large jumps from jitter only
        if dist < 10:
            offset_range = max(2, dist * 0.5)
        else:
            offset_range = max(20, dist * 0.2)
        cp1 = (
            start[0] + dx * 0.25 + self._rng.uniform(-offset_range, offset_range),
            start[1] + dy * 0.25 + self._rng.uniform(-offset_range, offset_range),
        )
        cp2 = (
            start[0] + dx * 0.75 + self._rng.uniform(-offset_range, offset_range),
            start[1] + dy * 0.75 + self._rng.uniform(-offset_range, offset_range),
        )

        path = []
        for i in range(steps + 1):
            t = i / steps
            x, y = self._bezier_point(start, cp1, cp2, end, t)
            # Micro jitter
            jitter_x = self._rng.uniform(-self.MOUSE_JITTER_MAX_PX, self.MOUSE_JITTER_MAX_PX)
            jitter_y = self._rng.uniform(-self.MOUSE_JITTER_MAX_PX, self.MOUSE_JITTER_MAX_PX)
            path.append((x + jitter_x, y + jitter_y))

        return path

    def is_linear_path(self, path: List[Tuple[float, float]]) -> bool:
        """Check if path is linear (for testing that we generate Bezier, not linear)."""
        if len(path) < 3:
            return True
        # Check if all points lie on line between start and end within tolerance
        start = path[0]
        end = path[-1]
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        if dx == 0 and dy == 0:
            return True
        for p in path[1:-1]:
            # Cross product should be near 0 for linear
            cross = abs((p[0] - start[0]) * dy - (p[1] - start[1]) * dx)
            # Normalize by distance
            if cross > 50:  # threshold for deviation
                return False
        return True

    def humanized_move(self, x: float, y: float):
        """Move mouse to x,y via Bezier curve."""
        if self.page is None:
            # No page, just update internal pos
            self._last_mouse_pos = (x, y)
            return

        start = self._last_mouse_pos
        end = (x, y)
        path = self.generate_bezier_path(start, end)

        # Move through path
        try:
            for px, py in path:
                self.page.mouse.move(px, py)
                # Small random delay between moves
                time.sleep(self._rng.uniform(0.005, 0.02))
        except Exception:
            # Fallback to direct move
            try:
                self.page.mouse.move(x, y)
            except Exception:
                pass

        self._last_mouse_pos = end

    def humanized_click(self, x: float, y: float, button: str = "left"):
        """Move and click with humanization."""
        self.humanized_move(x, y)
        # Pre-click hesitation
        time.sleep(self._rng.uniform(0.05, 0.25))
        if self.page is not None:
            try:
                self.page.mouse.click(x, y, button=button)
            except Exception:
                pass
        # Post-click pause
        time.sleep(self._rng.uniform(0.05, 0.15))

    def click_locator(self, locator, use_dom: Optional[bool] = None):
        """Click locator with action variance: 70% DOM click, 30% hotkey."""
        if use_dom is None:
            use_dom = self._rng.random() < self.CLICK_DOM_PROB

        if self.page is None:
            return

        try:
            box = locator.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2 + self._rng.uniform(-5, 5)
                y = box["y"] + box["height"] / 2 + self._rng.uniform(-5, 5)
                if use_dom:
                    self.humanized_click(x, y)
                    # Also try locator click as fallback
                    try:
                        locator.click(timeout=5000)
                    except Exception:
                        pass
                else:
                    # Hotkey path: focus and press Enter/Space
                    try:
                        locator.focus()
                        time.sleep(self._rng.uniform(0.1, 0.3))
                        # 50% Enter, 50% Space
                        key = self._rng.choice(["Enter", " "])
                        self.page.keyboard.press(key)
                    except Exception:
                        # Fallback to click
                        locator.click(timeout=5000)
            else:
                # No bounding box, direct locator click
                locator.click(timeout=5000)
        except Exception:
            try:
                locator.click(timeout=5000)
            except Exception:
                pass

    def random_scroll(self, direction: str = "down", amount: Optional[int] = None):
        """Random scroll for pre-trade activity."""
        if self.page is None:
            return
        if amount is None:
            amount = self._rng.randint(100, 500)
        try:
            if direction == "down":
                self.page.mouse.wheel(0, amount)
            else:
                self.page.mouse.wheel(0, -amount)
            time.sleep(self._rng.uniform(0.1, 0.4))
        except Exception:
            pass

    def hover_level(self, price_level: float, x_range: Tuple[int, int] = (200, 800), y: Optional[int] = None):
        """Hover over chart level (simulate checking support/resistance)."""
        if y is None:
            y = self._rng.randint(200, 600)
        x = self._rng.randint(*x_range)
        self.humanized_move(x, y)
        time.sleep(self._rng.uniform(0.3, 0.8))

    def click_empty(self):
        """Click empty space to simulate drawing."""
        x = self._rng.randint(100, 1000)
        y = self._rng.randint(100, 700)
        self.humanized_click(x, y)
        # Sometimes drag a bit (simulate drawing)
        if self._rng.random() < 0.3:
            if self.page is not None:
                try:
                    end_x = x + self._rng.randint(-100, 100)
                    end_y = y + self._rng.randint(-50, 50)
                    path = self.generate_bezier_path((x, y), (end_x, end_y), steps=15)
                    self.page.mouse.down()
                    for px, py in path:
                        self.page.mouse.move(px, py)
                        time.sleep(self._rng.uniform(0.01, 0.03))
                    self.page.mouse.up()
                except Exception:
                    pass

    def pre_trade_activity(self):
        """Pre-trade: scroll chart, hover levels, clicks empty."""
        actions = self._rng.randint(2, 5)
        for _ in range(actions):
            choice = self._rng.choice(["scroll", "hover", "click_empty", "move"])
            if choice == "scroll":
                self.random_scroll(direction=self._rng.choice(["up", "down"]))
            elif choice == "hover":
                self.hover_level(price_level=self._rng.uniform(100, 500))
            elif choice == "click_empty":
                self.click_empty()
            else:
                x = self._rng.randint(100, 1000)
                y = self._rng.randint(100, 700)
                self.humanized_move(x, y)
                time.sleep(self._rng.uniform(0.1, 0.3))

    def post_trade_activity(self):
        """Post-trade: micro movements, hover position."""
        actions = self._rng.randint(1, 3)
        for _ in range(actions):
            x = self._last_mouse_pos[0] + self._rng.randint(-50, 50)
            y = self._last_mouse_pos[1] + self._rng.randint(-30, 30)
            self.humanized_move(x, y)
            time.sleep(self._rng.uniform(0.1, 0.4))

    def simulate_visibility_change(self):
        """Simulate Page Visibility API: go background 30-120s, 2-3 times per session."""
        if self._visibility_switches_done >= self._visibility_switches_total:
            return False

        duration = self._rng.randint(self.VISIBILITY_DURATION_MIN_SEC, self.VISIBILITY_DURATION_MAX_SEC)
        self._visibility_switches_done += 1

        if self.page is None:
            time.sleep(self._rng.uniform(0.1, 0.3))
            return True

        try:
            # Simulate by evaluating document.hidden = true, dispatching visibilitychange
            # Or by switching to about:blank tab and back
            # We'll use JS to simulate
            self.page.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {value: true, writable: true});
                Object.defineProperty(document, 'visibilityState', {value: 'hidden', writable: true});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            time.sleep(duration)
            self.page.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {value: false, writable: true});
                Object.defineProperty(document, 'visibilityState', {value: 'visible', writable: true});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
        except Exception:
            # Fallback: just wait
            time.sleep(duration)

        return True

    def maybe_idle_break(self) -> bool:
        """Check if idle micro-break should happen (every 8-15 min)."""
        now = time.time()
        elapsed = now - self._last_idle_time
        if elapsed >= self._next_idle_interval:
            duration = self._rng.randint(self.IDLE_DURATION_MIN_SEC, self.IDLE_DURATION_MAX_SEC)
            # Idle: no mouse moves, no clicks
            time.sleep(duration)
            self._last_idle_time = time.time()
            self._next_idle_interval = self._rng.randint(self.IDLE_INTERVAL_MIN_SEC, self.IDLE_INTERVAL_MAX_SEC)
            return True
        return False

    def execute_hotkey(self, action: str):
        """Execute UTEx hotkey for given action.

        UTEx Nova hotkeys: F1-F4, F9-F10, Shift+F1-F4 customizable.
        Actions: Buy/Sell limit/stop/market at Best bid/ask/Mark price.
        """
        hotkey = self.HOTKEY_MAP.get(action)
        if not hotkey:
            # Fallback to action name if not in map
            hotkey = action

        if self.page is None:
            time.sleep(self._rng.uniform(0.1, 0.3))
            return

        try:
            # Pre-hotkey hesitation (human looks at keyboard)
            time.sleep(self._rng.uniform(0.2, 0.6))
            self.page.keyboard.press(hotkey)
            time.sleep(self._rng.uniform(0.1, 0.3))
        except Exception:
            pass

    def get_fingerprint_config(self) -> Dict[str, Any]:
        """Return browser fingerprint config.

        Headful, real UA (not HeadlessChrome), 1920x1080, timezone ET.
        Not blocking canvas/WebGL/audio — natural fingerprint.
        Use playwright-extra + stealth plugin if available.
        """
        config = {
            "headless": False,
            "viewport": {"width": self.VIEWPORT_WIDTH, "height": self.VIEWPORT_HEIGHT},
            "user_agent": self.USER_AGENT,
            "timezone_id": self.TIMEZONE,
            "locale": self.LOCALE,
            "args": ["--disable-blink-features=AutomationControlled"],
        }

        # Try to detect playwright-extra stealth plugin
        try:
            import importlib

            # Check if playwright-stealth or similar is available
            # For now, we just ensure we don't block fingerprinting APIs
            # The stealth plugin would be used at launch time, not here
            # We document that we intentionally do NOT block canvas/WebGL/audio
            config["bypass_csp"] = False
            config["ignore_https_errors"] = False
        except Exception:
            pass

        return config

    def get_stealth_launch_options(self) -> Dict[str, Any]:
        """Return launch options with stealth plugin if available.

        To use playwright-extra + stealth:
            from playwright_stealth import stealth_sync
            stealth_sync(page)
        Or use camoufox, etc.

        We intentionally do NOT block canvas/WebGL/audio fingerprinting — natural.
        """
        options = self.get_fingerprint_config()
        # Add note about video recording: mouse movements must look organic visually
        options["_human_organic"] = True
        options["_video_recording_assumed"] = True
        return options

    def reset(self):
        self._last_mouse_pos = (self._rng.uniform(100, 500), self._rng.uniform(100, 500))
        self._last_idle_time = time.time()
        self._next_idle_interval = self._rng.randint(self.IDLE_INTERVAL_MIN_SEC, self.IDLE_INTERVAL_MAX_SEC)
        self._visibility_switches_done = 0
        self._visibility_switches_total = self._rng.randint(self.VISIBILITY_SWITCHES_MIN, self.VISIBILITY_SWITCHES_MAX)

    # For testing
    def sample_bezier_paths(self, n: int) -> List[List[Tuple[float, float]]]:
        paths = []
        for _ in range(n):
            start = (self._rng.uniform(0, 1000), self._rng.uniform(0, 800))
            end = (self._rng.uniform(0, 1000), self._rng.uniform(0, 800))
            path = self.generate_bezier_path(start, end)
            paths.append(path)
        return paths
