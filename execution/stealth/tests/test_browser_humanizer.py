"""Tests for BrowserHumanizer — Bezier mouse paths, visibility, idle breaks, fingerprint."""

import pytest
import time

from execution.stealth.browser_humanizer import BrowserHumanizer


def test_bezier_path_not_linear():
    humanizer = BrowserHumanizer(seed=42)
    start = (100, 100)
    end = (500, 500)
    path = humanizer.generate_bezier_path(start, end, steps=30)
    assert len(path) == 31
    # Path should NOT be linear (Bezier with random control points)
    assert humanizer.is_linear_path(path) is False

    # Linear path should be detected as linear
    linear = [(100 + i * (400 / 30), 100 + i * (400 / 30)) for i in range(31)]
    assert humanizer.is_linear_path(linear) is True


def test_bezier_path_variance():
    humanizer = BrowserHumanizer(seed=123)
    start = (0, 0)
    end = (1000, 800)
    paths = [humanizer.generate_bezier_path(start, end) for _ in range(20)]
    # All paths should be different due to random control points
    # Check first path vs second path differ
    assert paths[0] != paths[1]
    # Each path should have steps in 20-40 range
    for p in paths:
        assert 20 <= len(p) - 1 <= 40


def test_bezier_curvature():
    """Ensure Bezier paths have curvature, not just jitter on linear."""
    humanizer = BrowserHumanizer(seed=42)
    # Long horizontal move should still have vertical deviation from Bezier control points
    start = (0, 400)
    end = (1000, 400)
    path = humanizer.generate_bezier_path(start, end, steps=30)
    # Check max vertical deviation from straight line y=400
    max_dev = max(abs(y - 400) for x, y in path)
    # Should have significant deviation (>10px) due to Bezier curvature
    assert max_dev > 10


def test_micro_jitter():
    humanizer = BrowserHumanizer(seed=42)
    start = (500, 500)
    end = (500, 500)  # zero distance, only jitter
    path = humanizer.generate_bezier_path(start, end, steps=20)
    # With zero distance, jitter should still cause small movements (allow 15px due to Bezier curvature)
    for x, y in path:
        assert abs(x - 500) <= 15
        assert abs(y - 500) <= 15


def test_visibility_switches():
    humanizer = BrowserHumanizer(seed=42)
    total = humanizer._visibility_switches_total
    assert 2 <= total <= 3
    # Simulate switches
    for _ in range(total):
        assert humanizer.simulate_visibility_change() is True
    # After total, should return False
    assert humanizer.simulate_visibility_change() is False


def test_idle_break_interval():
    humanizer = BrowserHumanizer(seed=42)
    interval = humanizer._next_idle_interval
    assert 480 <= interval <= 900  # 8-15 min

    # Force last idle time to be long ago, should trigger break
    humanizer._last_idle_time = time.time() - interval - 1
    # Mock sleep to avoid actual wait
    original_sleep = time.sleep
    try:
        time.sleep = lambda x: None
        assert humanizer.maybe_idle_break() is True
    finally:
        time.sleep = original_sleep


def test_fingerprint_config():
    humanizer = BrowserHumanizer(seed=42)
    fp = humanizer.get_fingerprint_config()
    assert fp["headless"] is False
    assert fp["viewport"]["width"] == 1920
    assert fp["viewport"]["height"] == 1080
    assert "HeadlessChrome" not in fp["user_agent"]
    assert fp["timezone_id"] == "America/New_York"
    # Should NOT block canvas/WebGL/audio
    assert fp.get("bypass_csp") is False or "bypass_csp" not in fp or fp.get("bypass_csp") is False


def test_action_variance():
    humanizer = BrowserHumanizer(seed=42)
    assert 0.6 <= humanizer.CLICK_DOM_PROB <= 0.8
    assert humanizer.CLICK_DOM_PROB + humanizer.HOTKEY_PROB == pytest.approx(1.0, abs=0.01)

    # Sample actions
    actions = []
    for _ in range(1000):
        if humanizer._rng.random() < humanizer.CLICK_DOM_PROB:
            actions.append("click_dom")
        else:
            actions.append("hotkey")
    click_count = actions.count("click_dom")
    # 70% DOM => allow 600-800
    assert 600 <= click_count <= 800


def test_hotkey_map():
    humanizer = BrowserHumanizer(seed=42)
    # Check UTEx hotkeys F1-F4, F9-F10, Shift+F1-F4
    assert "F1" in humanizer.HOTKEY_MAP.values()
    assert "F2" in humanizer.HOTKEY_MAP.values()
    assert "Shift+F1" in humanizer.HOTKEY_MAP.values()
    # Test execute_hotkey doesn't crash without page
    humanizer.execute_hotkey("buy_market_best_ask")
    humanizer.execute_hotkey("close_position")


def test_seed_reproducibility():
    h1 = BrowserHumanizer(seed=999)
    h2 = BrowserHumanizer(seed=999)
    p1 = h1.generate_bezier_path((0, 0), (100, 100), steps=20)
    h2 = BrowserHumanizer(seed=999)
    p2 = h2.generate_bezier_path((0, 0), (100, 100), steps=20)
    assert p1 == p2


def test_pre_post_trade_activity():
    humanizer = BrowserHumanizer(seed=42)
    # Should not crash without page
    humanizer.pre_trade_activity()
    humanizer.post_trade_activity()
    humanizer.random_scroll()
    humanizer.hover_level(150.0)
    humanizer.click_empty()
