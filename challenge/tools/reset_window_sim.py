"""Reset Window Simulator — test edge cases around the 00:00-00:13 UTC+4 window.

Usage:
    python -m challenge.tools.reset_window_sim

Simulates an open position entering the daily reset window (20:00-20:13 UTC).
Verifies that:
    1. balance_at_day_start is recalculated
    2. daily PnL resets
    3. No trades fire during the window
    4. Position survives through the window
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

from challenge.stealth.humanized_risk_manager import HumanizedRiskManager


def _make_utc4(y, mo, d, h, mi):
    """Helper to make a naive UTC+4 datetime."""
    return datetime(y, mo, d, h, mi)


def main():
    print("=" * 60)
    print("Reset Window Edge-Case Simulation")
    print("=" * 60)

    rm = HumanizedRiskManager(start_balance=1000.0, risk_base_pct=0.01, seed=42)

    # --- Scenario 1: Normal trading before reset ---
    print("\n--- Scenario 1: Trading before reset ---")
    t1 = _make_utc4(2026, 8, 25, 21, 50)  # 17:50 UTC+4 = 13:50 UTC
    rm.update_floating_pnl(floating_pnl=-5.0, equity=995.0, now_utc4=t1)
    print(f"  daily_pnl={rm.daily_pnl():.2f}, overall={rm.overall_pnl():.2f}")
    can, reason = rm.can_trade(now_utc4=t1)
    print(f"  can_trade={can} ({reason})")

    # --- Scenario 2: Entering reset window with loss ---
    print("\n--- Scenario 2: Enter reset window at -20 daily ---")
    t2 = _make_utc4(2026, 8, 26, 0, 0)  # 00:00 UTC+4 = 20:00 UTC (reset window!)
    rm.update_floating_pnl(floating_pnl=-20.0, equity=980.0, now_utc4=t2)
    print(f"  daily_pnl={rm.daily_pnl():.2f} (should be -20 or reset)")
    print(f"  balance_at_day_start={rm.balance_at_day_start:.2f}")
    # Should have reset: new day_start = 980, daily_pnl should be 0 or small
    can, reason = rm.can_trade(now_utc4=t2)
    print(f"  can_trade={can} ({reason})")

    # --- Scenario 3: Middle of reset window ---
    print("\n--- Scenario 3: Middle of reset window (00:05 UTC+4) ---")
    t3 = _make_utc4(2026, 8, 26, 0, 5)
    rm.update_floating_pnl(floating_pnl=-5.0, equity=975.0, now_utc4=t3)
    print(f"  daily_pnl={rm.daily_pnl():.2f}")
    print(f"  balance_at_day_start={rm.balance_at_day_start:.2f}")
    can, reason = rm.can_trade(now_utc4=t3)
    print(f"  can_trade={can} ({reason})")

    # --- Scenario 4: After reset window ---
    print("\n--- Scenario 4: After reset window (00:15 UTC+4) ---")
    t4 = _make_utc4(2026, 8, 26, 0, 15)
    rm.update_floating_pnl(floating_pnl=-3.0, equity=977.0, now_utc4=t4)
    print(f"  daily_pnl={rm.daily_pnl():.2f}")
    print(f"  overall={rm.overall_pnl():.2f}")
    can, reason = rm.can_trade(now_utc4=t4)
    print(f"  can_trade={can} ({reason})")

    # --- Scenario 5: Hit daily limit in new day ---
    print("\n--- Scenario 5: Hit daily limit in new day ---")
    t5 = _make_utc4(2026, 8, 26, 5, 0)
    rm.update_floating_pnl(floating_pnl=-35.0, equity=945.0, now_utc4=t5)
    print(f"  daily_pnl={rm.daily_pnl():.2f}")
    print(f"  should_force_close={rm.should_force_close()}")
    can, reason = rm.can_trade(now_utc4=t5)
    print(f"  can_trade={can} ({reason})")

    # --- Scenario 6: Edge case — position open at boundary ---
    print("\n--- Scenario 6: Open position at exact boundary ---")
    rm2 = HumanizedRiskManager(start_balance=1000.0, risk_base_pct=0.01, seed=42)
    t6 = _make_utc4(2026, 8, 25, 23, 59)
    rm2.update_floating_pnl(floating_pnl=-29.0, equity=971.0, now_utc4=t6)
    print(f"  daily_pnl={rm2.daily_pnl():.2f} (just under -30 limit)")
    can, reason = rm2.can_trade(now_utc4=t6)
    print(f"  can_trade={can} ({reason})")

    t6b = _make_utc4(2026, 8, 26, 0, 0)  # reset!
    rm2.update_floating_pnl(floating_pnl=-29.0, equity=971.0, now_utc4=t6b)
    print(f"  After reset: daily_pnl={rm2.daily_pnl():.2f}")
    print(f"  balance_at_day_start={rm2.balance_at_day_start:.2f}")
    can, reason = rm2.can_trade(now_utc4=t6b)
    print(f"  can_trade={can} ({reason})")

    print("\n" + "=" * 60)
    print("Simulation complete. Verify all outputs match expected behavior.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
