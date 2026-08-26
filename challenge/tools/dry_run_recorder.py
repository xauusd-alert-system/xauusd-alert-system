"""Dry-Run Recorder — records BrowserHumanizer actions on the live terminal.

Usage:
    python -m challenge.tools.dry_run_recorder --record-video

Opens the terminal, runs through pre-trade activity, order placement
(w/ confirmation suppressed), and post-trade activity while logging
every BrowserHumanizer decision.  Records 1920×1080 video to
logs/utex_sessions/.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

OUT_DIR = "logs/utex_sessions"
ET = timezone(timedelta(hours=-4))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("dry_run")


def main():
    parser = argparse.ArgumentParser(description="Dry-run recorder for UTEx stealth layer")
    parser.add_argument("--record-video", action="store_true",
                        help="Record 1920x1080 video of the session")
    parser.add_argument("--duration", type=int, default=300,
                        help="Duration in seconds (default: 300)")
    parser.add_argument("--ticker", default="TSLA",
                        help="Ticker to test with (default: TSLA)")
    args = parser.parse_args()

    try:
        from config.loader import load_config
        from challenge.browser import launch, ensure_logged_in
        from challenge.connector import terminal_url
        from challenge.stealth.humanized_timer import HumanizedTimer
        from challenge.stealth.browser_humanizer import BrowserHumanizer
        from challenge.stealth.session_simulator import SessionSimulator
    except ImportError as e:
        print(f"Import error: {e}")
        return 1

    cfg = load_config().get("challenge", {})
    session_id = cfg.get("platform", {}).get("session_id", "")
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Video recording config
    launch_args = BrowserHumanizer.get_stealth_launch_options()
    fp_config = BrowserHumanizer.get_fingerprint_config()

    pw, context = launch(cfg)
    try:
        page = ensure_logged_in(context, cfg)

        # Init stealth modules
        timer = HumanizedTimer(seed=42)
        humanizer = BrowserHumanizer(page, seed=42)
        session_sim = SessionSimulator(seed=42)

        log_entries = []

        def log_event(event_type: str, details: dict):
            entry = {
                "timestamp": datetime.now(ET).isoformat(),
                "type": event_type,
                **details,
            }
            log_entries.append(entry)
            logger.info("EVENT %s: %s", event_type, json.dumps(details, ensure_ascii=False))

        # Navigate to ticker
        url = terminal_url(args.ticker, session_id)
        logger.info("Navigating to %s", url)
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)
        log_event("navigation", {"url": url})

        # Take initial screenshot
        page.screenshot(path=os.path.join(OUT_DIR, f"initial_{ts}.png"))

        # Simulate pre-trade activity
        logger.info("=== Pre-trade activity ===")
        pre_actions = humanizer.pre_trade_activity()
        log_event("pre_trade", {"actions": pre_actions})
        time.sleep(2)

        # Simulate hover on price levels
        for _ in range(3):
            x = int(500 + timer.jitter() * 100)
            y = int(300 + timer.jitter() * 50)
            path = humanizer.move_mouse_to(x, y)
            log_event("bezier_move", {"start": path[0], "end": path[-1], "steps": len(path)})
            time.sleep(timer.jitter())

        # Simulate delay computation
        delay = timer.compute_delay(datetime.now(ET))
        log_event("delay", {"seconds": round(delay, 2)})

        # Simulate order placement (log only, don't execute)
        logger.info("=== Simulated order placement ===")
        log_event("order_sim", {
            "ticker": args.ticker,
            "side": "long",
            "shares": 10,
            "method": "dom_click" if not humanizer.use_hotkey() else "hotkey",
        })

        # Simulate post-trade activity
        logger.info("=== Post-trade activity ===")
        post_actions = humanizer.post_trade_activity()
        log_event("post_trade", {"actions": post_actions})

        # Simulate visibility change
        bg_time = humanizer.simulate_visibility_change()
        log_event("visibility_change", {"away_seconds": round(bg_time, 1)})

        # Simulate idle break
        idle = humanizer.maybe_idle_break()
        log_event("idle_break", {"duration": round(idle, 1)})

        # Keep open for specified duration
        remaining = args.duration
        logger.info("Keeping browser open for %ds...", remaining)
        try:
            time.sleep(remaining)
        except KeyboardInterrupt:
            logger.info("Interrupted by user")

        # Save log
        log_path = os.path.join(OUT_DIR, f"dry_run_{ts}.json")
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(log_entries, f, ensure_ascii=False, indent=2)
        logger.info("Dry run log saved: %s", log_path)

        # Final screenshot
        page.screenshot(path=os.path.join(OUT_DIR, f"final_{ts}.png"))

    finally:
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
