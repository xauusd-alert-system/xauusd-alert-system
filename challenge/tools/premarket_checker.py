"""Premarket Checker — verify where UTEx shows premarket volume.

Usage:
    python -m challenge.tools.premarket_checker

Opens the terminal for each ticker and checks if premarket volume
is available, where it's displayed, and logs the data.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

OUT_DIR = "logs/utex_dom_dump"

TICKERS = ["TSLA", "AAPL", "NVDA", "AMZN", "META"]


def main():
    try:
        from config.loader import load_config
        from challenge.browser import launch, ensure_logged_in
        from challenge.connector import HashHedgeConnector, terminal_url
    except ImportError as e:
        print(f"Import error: {e}")
        return 1

    cfg = load_config().get("challenge", {})
    session_id = cfg.get("platform", {}).get("session_id", "")
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    pw, context = launch(cfg)
    try:
        page = ensure_logged_in(context, cfg)
        results = {}

        for ticker in TICKERS:
            print(f"\n=== Checking {ticker} ===")
            try:
                url = terminal_url(ticker, session_id)
                page.goto(url, wait_until="domcontentloaded")
                time.sleep(3)

                # Look for volume data
                body_text = page.inner_text("body")
                has_volume = "объём" in body_text.lower() or "volume" in body_text.lower()
                has_premarket = "premarket" in body_text.lower() or "pre-market" in body_text.lower()

                # Dump visible text snippets that might contain volume
                volume_snippets = []
                for line in body_text.split("\n"):
                    line_lower = line.lower().strip()
                    if any(kw in line_lower for kw in ["объём", "volume", "premarket", "pre-market", "pre market"]):
                        volume_snippets.append(line.strip()[:100])

                # Take screenshot
                png_path = os.path.join(OUT_DIR, f"premarket_{ticker}_{ts}.png")
                page.screenshot(path=png_path)

                results[ticker] = {
                    "has_volume_text": has_volume,
                    "has_premarket_text": has_premarket,
                    "volume_snippets": volume_snippets,
                    "screenshot": png_path,
                }
                print(f"  Volume text: {has_volume}, Premarket text: {has_premarket}")
                if volume_snippets:
                    for s in volume_snippets:
                        print(f"  > {s}")

            except Exception as e:
                print(f"  ERROR: {e}")
                results[ticker] = {"error": str(e)}

        # Save results
        json_path = os.path.join(OUT_DIR, f"premarket_check_{ts}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n=== Results saved: {json_path} ===")

    finally:
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
