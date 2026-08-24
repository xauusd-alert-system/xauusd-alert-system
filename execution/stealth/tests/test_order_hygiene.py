"""Tests for OrderHygiene."""

import pytest

from execution.stealth.order_hygiene import OrderHygiene


def test_magic_pool_outside_banned_ranges():
    hygiene = OrderHygiene(seed=42)
    pool = hygiene.get_magic_pool()
    assert len(pool) == 20
    # Banned ranges: 0-100 and 70000000-89000000
    for magic in pool:
        assert not (0 <= magic <= 100), f"Magic {magic} in banned 0-100"
        assert not (70000000 <= magic <= 89000000), f"Magic {magic} in banned 70000000-89000000"
        assert hygiene.validate_magic(magic) is True


def test_magic_pool_unique():
    hygiene = OrderHygiene(seed=42)
    pool = hygiene.get_magic_pool()
    assert len(pool) == len(set(pool))


def test_magic_rotation_no_consecutive_repeat():
    hygiene = OrderHygiene(seed=123)
    magics = [hygiene.get_next_magic() for _ in range(100)]
    for i in range(1, len(magics)):
        assert magics[i] != magics[i - 1], f"Consecutive repeat at {i}: {magics[i]}"


def test_magic_rotation_uses_pool():
    hygiene = OrderHygiene(seed=42)
    pool = set(hygiene.get_magic_pool())
    magics = [hygiene.get_next_magic() for _ in range(50)]
    for m in magics:
        assert m in pool


def test_comment_distribution():
    hygiene = OrderHygiene(seed=42)
    comments = [hygiene.get_comment() for _ in range(1000)]
    empty = sum(1 for c in comments if c == "")
    # 70% empty => allow 600-800
    assert 600 <= empty <= 800
    # Non-empty should be from pool
    pool = set(hygiene.COMMENT_POOL)
    for c in comments:
        if c != "":
            assert c in pool


def test_api_jitter_range():
    hygiene = OrderHygiene(seed=42)
    jitters = [hygiene.get_api_jitter_ms() for _ in range(200)]
    assert all(50 <= j <= 350 for j in jitters)
    # Should have variance
    assert max(jitters) - min(jitters) > 100
    # Sec version
    jitter_sec = hygiene.get_api_jitter_sec()
    assert 0.05 <= jitter_sec <= 0.35


def test_seed_reproducibility():
    h1 = OrderHygiene(seed=999)
    h2 = OrderHygiene(seed=999)
    assert h1.get_magic_pool() == h2.get_magic_pool()
    m1 = [h1.get_next_magic() for _ in range(10)]
    # Reset h2 and get again
    h2 = OrderHygiene(seed=999)
    m2 = [h2.get_next_magic() for _ in range(10)]
    assert m1 == m2


def test_custom_magic_pool():
    custom = [1001, 1002, 1003]
    hygiene = OrderHygiene(seed=42, magic_pool=custom)
    assert hygiene.get_magic_pool() == custom
    magics = [hygiene.get_next_magic() for _ in range(10)]
    for m in magics:
        assert m in custom
