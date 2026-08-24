"""Reset window edge case simulation — tests daily reset 00:00-00:13 UTC+4 with open position.

Simulates: position open at 20:00 UTC (00:00 UTC+4) with floating -$25, after reset balance_at_day_start recalculates.

Run: python -m challenge.tools.reset_window_sim
"""

import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from execution.stealth.humanized_risk_manager import HumanizedRiskManager
from execution.stealth.config import StealthConfig


def simulate_reset_edge_case():
    print("=== Reset Window Edge Case Simulation ===")
    print("Scenario: position open at 20:00 UTC (00:00 UTC+4) with floating -$25")
    print("After reset, balance_at_day_start should recalculate, risk manager should continue correctly")

    config = StealthConfig(seed=42, challenge_daily_hard_stop=30.0, challenge_overall_buffer=10.0, challenge_daily_reset_window_utc4=("00:00", "00:13"), challenge_daily_reset_offset_hours=4)
    rm = HumanizedRiskManager(seed=42, config=config)

    # Day 1: start balance $1000, open position, floating -$25 at 19:55 UTC (before reset)
    day1_before_reset = datetime(2026, 8, 20, 19, 55, tzinfo=timezone.utc)
    print(f"\n--- Day1 before reset {day1_before_reset} ---")
    rm._balance_at_start = 1000.0
    rm._balance_at_day_start = 1000.0
    rm._overall_pnl = -2.90  # starting loss
    rm._closed_pnl_since_reset = 0.0
    rm.update_floating_pnl(-25, equity=1000 - 25 - 2.90, now=day1_before_reset)
    print(f"Floating: {rm.get_floating_pnl()}, Daily: {rm.get_daily_pnl()}, Overall: {rm.get_overall_pnl()}")
    print(f"Balance at day start: {rm._balance_at_day_start}")
    can, reason = rm.can_trade()
    print(f"Can trade? {can} ({reason}) - should be True (daily -25 > -30)")

    # At 20:00 UTC reset window starts
    reset_time = datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc)
    print(f"\n--- Reset window {reset_time} (00:00 UTC+4) ---")
    print(f"Is in reset window? {rm._is_in_reset_window(reset_time)}")
    rm._ensure_day(reset_time)
    print(f"After reset: Balance at day start: {rm._balance_at_day_start}")
    print(f"Floating: {rm.get_floating_pnl()}, Daily: {rm.get_daily_pnl()}, Overall: {rm.get_overall_pnl()}")
    print(f"Last reset date: {rm._last_reset_date}")
    can, reason = rm.can_trade()
    print(f"Can trade after reset? {can} ({reason})")

    # After reset, if floating still -25, daily should be -25 (floating only, closed reset to 0)
    # But if position was open before reset, should it count as new day's PnL?
    # According to spec: daily_loss = (balance_at_day_start + floating + closed_since_reset) - balance_at_day_start
    # If balance_at_day_start recalculated to equity at reset (which includes floating -25), then daily after reset should be 0 + floating_new?
    # Let's think: at reset, equity = 972.10 (1000 -2.90 -25), balance_at_day_start becomes 972.10, closed_since_reset=0, daily = floating (-25)?? Actually floating still -25, but balance_at_day_start now 972.10, so daily would be floating + closed = -25, but should be 0 right after reset?
    # The spec says reset recalculates Balance at start of day. So after reset, daily loss should be reset to floating (which is -25) or 0?
    # Common prop firm logic: at reset, floating PnL is still counted as part of new day if position held overnight.
    # But we want to avoid closing position erroneously.

    # Simulate: after reset, position still open, floating -25, but new balance_at_day_start = 972.10
    # If we keep daily = floating + closed_since_reset = -25 + 0 = -25, it would be close to hard stop -30
    # That's correct: if you hold overnight with -25 floating, you start new day with -25 daily.

    # Now simulate price moves to -35 daily after reset
    after_reset = datetime(2026, 8, 20, 20, 5, tzinfo=timezone.utc)
    rm.update_floating_pnl(-35, equity=rm._balance_at_day_start - 35, now=after_reset)
    print(f"\n--- After reset, floating goes to -35 {after_reset} ---")
    print(f"Floating: {rm.get_floating_pnl()}, Daily: {rm.get_daily_pnl()}, Overall: {rm.get_overall_pnl()}")
    can, reason = rm.can_trade()
    print(f"Can trade? {can} ({reason}) - should be False (daily -35 <= -30) -> force close")

    should_close, reason = rm.should_force_close(daily_pnl=rm.get_daily_pnl(), overall_pnl=rm.get_overall_pnl())
    print(f"Should force close? {should_close} ({reason})")

    # Edge case: if position closed before reset, closed_pnl_today should be counted
    print(f"\n--- Edge: close position before reset ---")
    rm2 = HumanizedRiskManager(seed=42, config=config)
    rm2._balance_at_start = 1000.0
    rm2._balance_at_day_start = 1000.0
    rm2._overall_pnl = -2.90
    # Closed -20 during day
    rm2.update_pnl(-20, now=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc))
    print(f"After closed -20: Daily {rm2.get_daily_pnl()}, Overall {rm2.get_overall_pnl()}")
    # Floating 0
    rm2.update_floating_pnl(0, equity=1000 - 2.90 - 20, now=datetime(2026, 8, 20, 15, 5, tzinfo=timezone.utc))
    print(f"After floating 0: Daily {rm2.get_daily_pnl()}")
    # Reset
    rm2._ensure_day(datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc))
    print(f"After reset: Balance at day start {rm2._balance_at_day_start}, Daily {rm2.get_daily_pnl()}, Closed since reset {rm2._closed_pnl_since_reset}")
    # Daily should be reset to floating (0) after reset, not -20
    assert rm2._closed_pnl_since_reset == 0.0
    print("PASS: closed PnL reset after daily reset window")

    print("\n=== Simulation complete ===")
    print("Check: risk manager correctly handles reset window with open position, doesn't close erroneously at reset, but triggers force close when daily goes to -35")


if __name__ == "__main__":
    simulate_reset_edge_case()
