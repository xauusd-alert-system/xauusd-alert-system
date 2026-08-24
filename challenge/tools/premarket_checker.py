"""Premarket volume feed checker — verifies where UTEx shows premarket volume.

Run: python -m challenge.tools.premarket_checker --symbol TSLA

Checks:
- Dashboard for volume
- Ticker modal for volume
- Market data API if available
"""

import os
import sys
import time
import json
import re
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from challenge.browser import launch, ensure_logged_in
from challenge.connector import terminal_url
from pathlib import Path

DUMP_DIR = Path("logs/utex_dom_dump")
DUMP_DIR.mkdir(parents=True, exist_ok=True)


def check_premarket_volume(page, symbol: str, session_id: str):
    print(f"\n=== Checking premarket volume for {symbol} ===")

    # Try dashboard
    print("\n--- Dashboard snapshot ---")
    try:
        page.goto(f"https://markets-app.hashhedge.com/stocks-usdt/dashboard?lng=ru&session={session_id}", wait_until="domcontentloaded")
        time.sleep(3)
        body_text = page.inner_text("body")[:2000]
        print(f"Dashboard body snippet: {body_text[:500]}")

        # Look for volume in page
        volume_data = page.evaluate("""
        () => {
            const result = [];
            // Search for volume-related text
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while (node = walker.nextNode()) {
                const txt = node.textContent.trim();
                if (txt.toLowerCase().includes('volume') || txt.toLowerCase().includes('объем') || /\\d+\\s*[KMB]\\s*vol/i.test(txt)) {
                    result.push({
                        text: txt.slice(0, 200),
                        parentHTML: node.parentElement ? node.parentElement.outerHTML.slice(0, 400) : ''
                    });
                }
            }
            // Also check for any element with volume in data-testid
            document.querySelectorAll('[data-testid*=\"vol\" i]').forEach(el => {
                result.push({
                    type: 'data-testid-vol',
                    testId: el.getAttribute('data-testid'),
                    text: (el.innerText || '').slice(0, 200),
                    outerHTML: el.outerHTML.slice(0, 400)
                });
            });
            return result;
        }
        """)
        print(f"Found {len(volume_data)} volume-related elements on dashboard")
        for item in volume_data[:10]:
            print(f"  {item}")

    except Exception as e:
        print(f"Dashboard check failed: {e}")

    # Try ticker terminal
    print(f"\n--- Terminal for {symbol} ---")
    try:
        url = terminal_url(symbol, session_id)
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)
        page.wait_for_selector('input', timeout=10000)
        time.sleep(2)

        # Check for volume in terminal
        terminal_vol = page.evaluate("""
        () => {
            const result = {
                inputs: [],
                volumeTexts: [],
                chartInfo: []
            };
            // Look for volume in chart info, stats panel
            document.querySelectorAll('*').forEach(el => {
                const txt = (el.innerText || '').trim();
                if (txt.length > 0 && txt.length < 100) {
                    if (txt.toLowerCase().includes('volume') || txt.toLowerCase().includes('объем')) {
                        result.volumeTexts.push({
                            text: txt,
                            tag: el.tagName,
                            className: el.className,
                            outerHTML: el.outerHTML.slice(0, 300)
                        });
                    }
                }
            });
            // Check for any stats panel
            const stats = document.querySelectorAll('[class*=\"stat\" i], [class*=\"info\" i], [data-testid*=\"stat\" i]');
            stats.forEach(el => {
                result.chartInfo.push({
                    text: (el.innerText || '').slice(0, 300),
                    outerHTML: el.outerHTML.slice(0, 500)
                });
            });
            return result;
        }
        """)
        print(f"Terminal volume texts: {len(terminal_vol.get('volumeTexts', []))}")
        for item in terminal_vol.get('volumeTexts', [])[:10]:
            print(f"  {item}")

        # Check network requests for volume data
        print("\n--- Network logs (check DevTools manually) ---")
        print("Open DevTools → Network → filter 'volume' or 'market' or 'quote'")
        print("Look for API endpoints that return premarket volume")

        # Screenshot
        screenshot_path = DUMP_DIR / f"{symbol}_premarket_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"Screenshot saved: {screenshot_path}")

    except Exception as e:
        print(f"Terminal check failed: {e}")

    print("\n=== Premarket Volume Checklist ===")
    print("1. Does UTEx dashboard show premarket volume? If yes, what selector?")
    print("2. Does ticker modal show volume? Check stats panel")
    print("3. Check Network tab for API calls: does /api/market or similar return volume?")
    print("4. If no premarket volume in UTEx, consider external feed: Yahoo Finance, Finnhub, Alpaca")
    print("5. For rotation, you need volume for TSLA, AAPL, NVDA, AMZN, META each morning 9:00-9:20 ET")
    print("6. Fallback: if no premarket data, rotate randomly or by previous day volume")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Premarket volume checker")
    parser.add_argument("--symbol", type=str, default="TSLA")
    args = parser.parse_args()

    full_cfg = load_config()
    cfg = full_cfg.get("challenge", {})

    from execution.stealth.config import StealthConfig
    stealth_cfg_dict = full_cfg.get("stealth", {}) or {}
    stealth_config = StealthConfig.from_dict(stealth_cfg_dict)

    pw, context = launch(cfg, stealth_config=stealth_config)
    try:
        page = ensure_logged_in(context, cfg)
        session_id = str(cfg.get("platform", {}).get("session_id") or "")
        check_premarket_volume(page, args.symbol.upper(), session_id)
        print("\nKeeping browser open for 60s for manual inspection...")
        time.sleep(60)
    finally:
        pw.stop()


if __name__ == "__main__":
    import sys
    sys.exit(main())
