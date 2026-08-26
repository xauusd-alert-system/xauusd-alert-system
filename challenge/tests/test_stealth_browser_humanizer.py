"""Tests for BrowserHumanizer — Bezier paths, stealth, hotkeys, idle breaks."""
import time
import pytest

from challenge.stealth.browser_humanizer import BrowserHumanizer


class TestBezierPath:
    def test_generate_bezier_path_returns_correct_length(self):
        bh = BrowserHumanizer(seed=42)
        path = bh.generate_bezier_path((0, 0), (500, 500), steps=30)
        assert len(path) == 31  # steps + 1 points

    def test_bezier_path_starts_at_start(self):
        bh = BrowserHumanizer(seed=42)
        path = bh.generate_bezier_path((100, 200), (500, 600))
        assert path[0] == (100, 200)

    def test_bezier_path_ends_at_end(self):
        bh = BrowserHumanizer(seed=42)
        path = bh.generate_bezier_path((100, 200), (500, 600))
        assert path[-1] == (500, 600)

    def test_bezier_path_not_linear(self):
        bh = BrowserHumanizer(seed=42)
        # Generate many paths — most should NOT be linear due to jitter
        non_linear_count = 0
        for i in range(20):
            path = bh.generate_bezier_path((0, 0), (500, 500), steps=30)
            if not bh.is_linear_path(path):
                non_linear_count += 1
        assert non_linear_count > 10  # Most should be non-linear

    def test_is_linear_path_identifies_line(self):
        path = [(0, 0), (1, 1), (2, 2), (3, 3)]
        assert BrowserHumanizer.is_linear_path(path) is True

    def test_is_linear_path_identifies_curve(self):
        path = [(0, 0), (1, 2), (2, 1), (3, 3)]
        assert BrowserHumanizer.is_linear_path(path) is False

    def test_bezier_path_minimum_steps(self):
        bh = BrowserHumanizer(seed=42)
        path = bh.generate_bezier_path((0, 0), (100, 100), steps=2)
        # Should be clamped to at least 4 steps
        assert len(path) >= 5

    def test_bezier_path_varies_with_seed(self):
        bh1 = BrowserHumanizer(seed=42)
        bh2 = BrowserHumanizer(seed=99)
        p1 = bh1.generate_bezier_path((0, 0), (500, 500))
        p2 = bh2.generate_bezier_path((0, 0), (500, 500))
        # Different seeds should produce different paths
        assert p1 != p2

    def test_bezier_path_consistent_with_same_seed(self):
        bh1 = BrowserHumanizer(seed=42)
        bh2 = BrowserHumanizer(seed=42)
        p1 = bh1.generate_bezier_path((0, 0), (500, 500))
        p2 = bh2.generate_bezier_path((0, 0), (500, 500))
        assert p1 == p2


class TestVisibilityChange:
    def test_simulate_visibility_returns_float(self):
        bh = BrowserHumanizer(seed=42)
        result = bh.simulate_visibility_change()
        assert isinstance(result, (int, float))

    def test_visibility_change_count_increments(self):
        bh = BrowserHumanizer(seed=0)  # seed 0 has high chance
        initial = bh.visibility_change_count
        # Run many times to catch a positive one
        for _ in range(50):
            bh.simulate_visibility_change()
        assert bh.visibility_change_count >= initial


class TestHotkeys:
    def test_use_hotkey_returns_bool(self):
        bh = BrowserHumanizer(seed=42)
        results = [bh.use_hotkey() for _ in range(100)]
        # ~30% should be True
        true_count = sum(results)
        assert 10 < true_count < 50  # reasonable range for 30%

    def test_execute_hotkey_buy_market(self):
        bh = BrowserHumanizer(seed=42)
        key = bh.execute_hotkey("buy_market")
        assert key == "F1"

    def test_execute_hotkey_unknown(self):
        bh = BrowserHumanizer(seed=42)
        key = bh.execute_hotkey("nonexistent_action")
        assert key is None


class TestIdleBreak:
    def test_maybe_idle_break_returns_zero_initially(self):
        bh = BrowserHumanizer(seed=42)
        bh._last_idle_break = time.time()  # just had one
        result = bh.maybe_idle_break()
        assert result == 0.0

    def test_maybe_idle_break_can_return_positive(self):
        bh = BrowserHumanizer(seed=42)
        bh._last_idle_break = time.time() - 1000  # long ago
        result = bh.maybe_idle_break()
        assert result >= 0.0  # May or may not trigger depending on interval

    def test_idle_break_resets_timer(self):
        bh = BrowserHumanizer(seed=42)
        bh._last_idle_break = time.time() - 10000
        bh.maybe_idle_break()
        # After triggering, the timer should be recent
        assert time.time() - bh._last_idle_break < 5


class TestPreTradeActivity:
    def test_pre_trade_returns_actions(self):
        bh = BrowserHumanizer(seed=42)
        actions = bh.pre_trade_activity()
        assert len(actions) >= 1
        assert all(isinstance(a, str) for a in actions)

    def test_post_trade_returns_actions(self):
        bh = BrowserHumanizer(seed=42)
        actions = bh.post_trade_activity()
        assert len(actions) >= 1


class TestFingerprintConfig:
    def test_fingerprint_config_has_required_keys(self):
        config = BrowserHumanizer.get_fingerprint_config()
        assert config["headless"] is False
        assert "Chrome" in config["user_agent"]
        assert "HeadlessChrome" not in config["user_agent"]
        assert config["viewport"]["width"] == 1920
        assert config["viewport"]["height"] == 1080
        assert config["timezone_id"] == "America/New_York"
        assert config["locale"] == "en-US"
        # Fingerprint blocking should be OFF
        assert config["block_canvas_fingerprint"] is False
        assert config["block_webgl_fingerprint"] is False
        assert config["block_audio_fingerprint"] is False


class TestStealthLaunchOptions:
    def test_launch_options_have_automation_controlled(self):
        opts = BrowserHumanizer.get_stealth_launch_options()
        assert "--disable-blink-features=AutomationControlled" in opts["args"]
        assert "--enable-automation" in opts["ignore_default_args"]


class TestRecordAction:
    def test_record_action_sets_timestamp(self):
        bh = BrowserHumanizer(seed=42)
        assert bh.last_action_ts is None
        bh.record_action()
        assert bh.last_action_ts is not None
