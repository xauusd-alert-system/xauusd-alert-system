"""BrowserHumanizer — Playwright stealth layer for UTEx.

Generates organic mouse movements, manages page visibility, injects
idle micro-breaks, and provides a stealth launch configuration that
passes fingerprint checks (Clause 6.5c: canvas, WebGL, fonts, audio).
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("stealth.browser")


class BrowserHumanizer:
    """Organic browser interaction layer on top of Playwright.

    Parameters
    ----------
    page : object | None
        Playwright Page object (or mock for tests).
    seed : int | None
        Optional RNG seed.
    cfg : dict | None
        Override config for hotkeys, viewport, etc.
    """

    # --- Defaults ---
    BEZIER_MIN_STEPS: int = 20
    BEZIER_MAX_STEPS: int = 40
    MICRO_JITTER_PX: float = 2.0       # 1-3 px shake
    VISIT_BG_MIN_S: float = 30.0
    VISIT_BG_MAX_S: float = 120.0
    VISIT_BG_CHANCE: float = 0.33      # per-check 33 % (2-3x per session)
    CLICK_LOCATOR_CHANCE: float = 0.30  # 30 % hotkey / locator, 70 % DOM click
    IDLE_BREAK_MIN_INTERVAL_S: float = 8 * 60   # 8 min
    IDLE_BREAK_MAX_INTERVAL_S: float = 15 * 60  # 15 min
    IDLE_BREAK_MIN_S: float = 20.0
    IDLE_BREAK_MAX_S: float = 60.0
    PRE_TRADE_MOVES: int = 3
    POST_TRADE_MOVES: int = 2

    # Default hotkey map for UTEx Nova terminal
    DEFAULT_HOTKEY_MAP: Dict[str, str] = {
        "buy_market":  "F1",
        "sell_market": "F2",
        "buy_limit":   "F3",
        "sell_limit":  "F4",
        "cancel_all":  "F9",
        "close_all":   "F10",
        "qty_up":      "Shift+F1",
        "qty_down":    "Shift+F2",
        "price_up":    "Shift+F3",
        "price_down":  "Shift+F4",
    }

    def __init__(
        self,
        page=None,
        *,
        seed: int | None = None,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        c = cfg or {}
        self._page = page
        self._rng = random.Random(seed)
        self._last_action_ts: Optional[datetime] = None
        self._visibility_changes: int = 0
        self._last_idle_break: float = time.time()
        self._hotkey_map = c.get("browser_hotkey_map", self.DEFAULT_HOTKEY_MAP)

    # ------------------------------------------------------------------
    # Bezier mouse path generation
    # ------------------------------------------------------------------

    def generate_bezier_path(
        self,
        start: Tuple[int, int],
        end: Tuple[int, int],
        steps: Optional[int] = None,
    ) -> List[Tuple[int, int]]:
        """Generate a cubic Bezier path with micro-jitter.

        Returns a list of (x, y) pixel coordinates from *start* to *end*.
        """
        if steps is None:
            steps = self._rng.randint(self.BEZIER_MIN_STEPS, self.BEZIER_MAX_STEPS)
        steps = max(4, steps)

        sx, sy = start
        ex, ey = end

        # Random control points with moderate offset
        dx = ex - sx
        dy = ey - sy
        dist = max(1, (dx ** 2 + dy ** 2) ** 0.5)

        cp1x = sx + dx * self._rng.uniform(0.2, 0.4) + self._rng.uniform(-dist * 0.1, dist * 0.1)
        cp1y = sy + dy * self._rng.uniform(0.2, 0.4) + self._rng.uniform(-dist * 0.1, dist * 0.1)
        cp2x = sx + dx * self._rng.uniform(0.6, 0.8) + self._rng.uniform(-dist * 0.1, dist * 0.1)
        cp2y = sy + dy * self._rng.uniform(0.6, 0.8) + self._rng.uniform(-dist * 0.1, dist * 0.1)

        path: List[Tuple[int, int]] = []
        for i in range(steps + 1):
            t = i / steps
            u = 1.0 - t
            x = (u ** 3 * sx + 3 * u ** 2 * t * cp1x
                  + 3 * u * t ** 2 * cp2x + t ** 3 * ex)
            y = (u ** 3 * sy + 3 * u ** 2 * t * cp1y
                  + 3 * u * t ** 2 * cp2y + t ** 3 * ey)
            # Micro-jitter (except endpoints)
            if 0 < i < steps:
                jx = self._rng.uniform(-self.MICRO_JITTER_PX, self.MICRO_JITTER_PX)
                jy = self._rng.uniform(-self.MICRO_JITTER_PX, self.MICRO_JITTER_PX)
                x += jx
                y += jy
            path.append((int(round(x)), int(round(y))))

        return path

    @staticmethod
    def is_linear_path(path: List[Tuple[int, int]]) -> bool:
        """Test helper: True if all points lie on a straight line."""
        if len(path) < 3:
            return True
        x0, y0 = path[0]
        x1, y1 = path[1]
        dx = x1 - x0
        dy = y1 - y0
        for px, py in path[2:]:
            cross = dx * (py - y0) - dy * (px - x0)
            if abs(cross) > 1e-6:
                return False
        return True

    def _execute_bezier_move(
        self,
        start: Tuple[int, int],
        end: Tuple[int, int],
    ) -> None:
        """Move mouse along a Bezier path on the page (if page available)."""
        path = self.generate_bezier_path(start, end)
        if self._page is None:
            return
        for x, y in path:
            self._page.mouse.move(x, y)
            time.sleep(self._rng.uniform(0.003, 0.015))  # ~3-15 ms per step

    def move_mouse_to(
        self,
        x: int,
        y: int,
        current: Optional[Tuple[int, int]] = None,
    ) -> List[Tuple[int, int]]:
        """Move to (x, y) from *current* (or last known position).

        Returns the Bezier path for inspection.
        """
        if current is None:
            current = (self._rng.randint(100, 800), self._rng.randint(100, 600))
        self._execute_bezier_move(current, (x, y))
        return self.generate_bezier_path(current, (x, y))

    # ------------------------------------------------------------------
    # Page Visibility API simulation
    # ------------------------------------------------------------------

    def simulate_visibility_change(self) -> float:
        """Simulate switching to another tab and back.

        Returns the time spent "away" in seconds.  Designed to fire
        2-3 times per session (caller decides when).
        """
        if self._rng.random() > self.VISIT_BG_CHANCE:
            return 0.0
        away_s = self._rng.uniform(self.VISIT_BG_MIN_S, self.VISIT_BG_MAX_S)
        self._visibility_changes += 1
        if self._page:
            try:
                self._page.evaluate(
                    "() => { document.dispatchEvent(new Event('visibilitychange')); }"
                )
            except Exception:
                pass
        logger.debug("visibility_change: away %.1fs (#%d)", away_s, self._visibility_changes)
        # In production, the caller sleeps; here we just record the event.
        return away_s

    @property
    def visibility_change_count(self) -> int:
        return self._visibility_changes

    # ------------------------------------------------------------------
    # Action variance (click vs hotkey)
    # ------------------------------------------------------------------

    def use_hotkey(self) -> bool:
        """True 30 % of the time (hotkey path), False 70 % (DOM click)."""
        return self._rng.random() < self.CLICK_LOCATOR_CHANCE

    def execute_hotkey(self, action: str) -> Optional[str]:
        """Return the keyboard shortcut for *action* (from hotkey_map).

        Does NOT press the key — caller uses page.keyboard.press(result).
        Returns None if action not mapped.
        """
        key = self._hotkey_map.get(action)
        if key is None:
            logger.warning("hotkey action '%s' not mapped", action)
        return key

    # ------------------------------------------------------------------
    # Idle micro-breaks
    # ------------------------------------------------------------------

    def maybe_idle_break(self) -> float:
        """Maybe inject an idle micro-break (20-60 s).

        Called in the main loop; only fires every 8-15 min.
        Returns the break duration (0 if no break triggered).
        """
        elapsed = time.time() - self._last_idle_break
        interval = self._rng.uniform(
            self.IDLE_BREAK_MIN_INTERVAL_S, self.IDLE_BREAK_MAX_INTERVAL_S,
        )
        if elapsed < interval:
            return 0.0
        duration = self._rng.uniform(self.IDLE_BREAK_MIN_S, self.IDLE_BREAK_MAX_S)
        self._last_idle_break = time.time()
        logger.debug("idle_break: %.1fs", duration)
        return duration

    # ------------------------------------------------------------------
    # Pre-trade / post-trade activity
    # ------------------------------------------------------------------

    def pre_trade_activity(self) -> List[str]:
        """Return a list of humanized actions before placing a trade.

        Actions: scroll chart, hover price levels, click empty area.
        """
        actions: List[str] = []
        for _ in range(self._rng.randint(1, self.PRE_TRADE_MOVES)):
            kind = self._rng.choice(["scroll", "hover_level", "click_empty"])
            actions.append(kind)
            if self._page and kind == "scroll":
                try:
                    self._page.mouse.wheel(0, self._rng.randint(-3, 3) * 50)
                except Exception:
                    pass
            elif self._page and kind == "hover_level":
                try:
                    cx = self._rng.randint(200, 1200)
                    cy = self._rng.randint(100, 700)
                    self._execute_bezier_move(
                        (cx + self._rng.randint(-50, 50), cy + self._rng.randint(-50, 50)),
                        (cx, cy),
                    )
                except Exception:
                    pass
            elif self._page and kind == "click_empty":
                try:
                    self._page.mouse.click(
                        self._rng.randint(50, 100), self._rng.randint(50, 100),
                    )
                except Exception:
                    pass
        return actions

    def post_trade_activity(self) -> List[str]:
        """Micro-movements after a trade is placed."""
        actions: List[str] = []
        for _ in range(self._rng.randint(1, self.POST_TRADE_MOVES)):
            kind = self._rng.choice(["hover_position", "micro_move"])
            actions.append(kind)
            if self._page:
                try:
                    x = self._rng.randint(200, 1200)
                    y = self._rng.randint(100, 700)
                    self._page.mouse.move(x, y)
                except Exception:
                    pass
        return actions

    # ------------------------------------------------------------------
    # Browser fingerprint config
    # ------------------------------------------------------------------

    @staticmethod
    def get_fingerprint_config() -> Dict[str, Any]:
        """Return a stealth fingerprint configuration dict.

        These values ensure the browser does NOT look headless and
        passes canvas/WebGL/audio fingerprint checks (Clause 6.5c).
        """
        return {
            "headless": False,
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1920, "height": 1080},
            "device_scale_factor": 1.0,
            "timezone_id": "America/New_York",
            "locale": "en-US",
            "color_scheme": "light",
            # Do NOT block these — they must be real for fingerprint checks
            "block_canvas_fingerprint": False,
            "block_webgl_fingerprint": False,
            "block_audio_fingerprint": False,
            "block_font_enumeration": False,
        }

    @staticmethod
    def get_stealth_launch_options() -> Dict[str, Any]:
        """Return Playwright launch args that reduce automation detection.

        Compatible with playwright-extra stealth plugin.
        """
        return {
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--window-size=1920,1080",
            ],
            "ignore_default_args": ["--enable-automation"],
            "env": {
                "DISPLAY": ":99",
            },
        }

    # ------------------------------------------------------------------
    # Timing helpers
    # ------------------------------------------------------------------

    def record_action(self) -> None:
        """Mark that an action just occurred."""
        self._last_action_ts = datetime.now()

    @property
    def last_action_ts(self) -> Optional[datetime]:
        return self._last_action_ts

    def sleep(self, seconds: float) -> None:
        """Humanized sleep wrapper — uses time.sleep."""
        if seconds > 0:
            time.sleep(seconds)

    def page(self):
        """Return the underlying Playwright page."""
        return self._page

    def set_page(self, page) -> None:
        """Set/update the Playwright page reference."""
        self._page = page
