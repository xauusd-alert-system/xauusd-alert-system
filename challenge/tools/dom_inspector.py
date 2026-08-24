"""UTEx DOM Inspector — dumps real selectors from live Hash Hedge terminal.

Usage:
    python -m challenge.tools.dom_inspector --symbol TSLA
    python -m challenge.tools.dom_inspector --all

This tool opens the UTEx exchange-pro terminal via the persistent profile
and extracts real DOM structure for ticket-form, buy/sell buttons, qty/price inputs.

All findings saved to logs/utex_dom_dump/ for manual review.
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from config.loader import load_config
from challenge.browser import launch, ensure_logged_in, open_page
from challenge.connector import terminal_url
from execution.stealth.config import StealthConfig


DUMP_DIR = Path("logs/utex_dom_dump")


def dump_dom_for_symbol(page, symbol: str, session_id: str, stealth_config: StealthConfig = None):
    """Open symbol terminal and dump DOM."""
    url = terminal_url(symbol, session_id)
    print(f"\n=== Dumping DOM for {symbol} ===")
    print(f"URL: {url}")

    page.goto(url, wait_until="domcontentloaded")
    # Wait for potential ticket form
    try:
        page.wait_for_selector('input', timeout=15000)
    except Exception:
        print("Warning: no input found within 15s, page may not have loaded ticket form")
    time.sleep(3)

    # Take screenshot
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = DUMP_DIR / f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"Screenshot saved: {screenshot_path}")
    except Exception as e:
        print(f"Screenshot failed: {e}")

    # Save HTML
    html_path = DUMP_DIR / f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    try:
        html_content = page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"HTML saved: {html_path} ({len(html_content)} chars)")
    except Exception as e:
        print(f"HTML save failed: {e}")

    # JS evaluation to collect selectors
    js_collect = """
    () => {
        const result = {
            inputs: [],
            buttons: [],
            forms: [],
            dataTestIds: [],
            ticketFormCandidates: [],
            allText: []
        };

        // Inputs
        document.querySelectorAll('input').forEach(el => {
            result.inputs.push({
                name: el.name || '',
                id: el.id || '',
                type: el.type || '',
                placeholder: el.placeholder || '',
                className: el.className || '',
                value: el.value || '',
                outerHTML: el.outerHTML.slice(0, 300)
            });
        });

        // Buttons with text
        document.querySelectorAll('button').forEach(el => {
            const text = (el.innerText || el.textContent || '').trim().slice(0, 100);
            if (text) {
                result.buttons.push({
                    text: text,
                    name: el.name || '',
                    id: el.id || '',
                    className: el.className || '',
                    dataTestId: el.getAttribute('data-testid') || '',
                    outerHTML: el.outerHTML.slice(0, 400)
                });
            }
        });

        // Forms
        document.querySelectorAll('form').forEach(el => {
            result.forms.push({
                id: el.id || '',
                className: el.className || '',
                innerText: (el.innerText || '').slice(0, 200),
                outerHTML: el.outerHTML.slice(0, 500)
            });
        });

        // data-testid
        document.querySelectorAll('[data-testid]').forEach(el => {
            result.dataTestIds.push({
                testId: el.getAttribute('data-testid'),
                tag: el.tagName,
                text: (el.innerText || '').slice(0, 100),
                outerHTML: el.outerHTML.slice(0, 400)
            });
        });

        // Ticket form candidates - look for qty, price, buy/sell
        const qtyInputs = document.querySelectorAll('input[name*=\"qty\"], input[name*=\"quantity\"], input[placeholder*=\"qty\" i], input[placeholder*=\"кол\" i]');
        qtyInputs.forEach(el => {
            result.ticketFormCandidates.push({
                type: 'qty_input',
                name: el.name,
                id: el.id,
                outerHTML: el.outerHTML.slice(0, 300),
                parentText: el.parentElement ? el.parentElement.innerText.slice(0, 200) : ''
            });
        });

        const priceInputs = document.querySelectorAll('input[name*=\"price\"], input[name*=\"цена\" i]');
        priceInputs.forEach(el => {
            result.ticketFormCandidates.push({
                type: 'price_input',
                name: el.name,
                id: el.id,
                outerHTML: el.outerHTML.slice(0, 300)
            });
        });

        // Buy/Sell buttons
        const buySell = document.querySelectorAll('button');
        buySell.forEach(el => {
            const txt = (el.innerText || '').toLowerCase();
            if (txt.includes('купить') || txt.includes('buy') || txt.includes('long') || txt.includes('продать') || txt.includes('sell') || txt.includes('short')) {
                result.ticketFormCandidates.push({
                    type: 'buy_sell_button',
                    text: (el.innerText || '').trim(),
                    className: el.className,
                    dataTestId: el.getAttribute('data-testid') || '',
                    outerHTML: el.outerHTML.slice(0, 400)
                });
            }
        });

        return result;
    }
    """

    try:
        dom_data = page.evaluate(js_collect)
        json_path = DUMP_DIR / f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_dom.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(dom_data, f, ensure_ascii=False, indent=2)
        print(f"DOM JSON saved: {json_path}")
        print(f"  Inputs: {len(dom_data.get('inputs', []))}")
        print(f"  Buttons: {len(dom_data.get('buttons', []))}")
        print(f"  data-testid: {len(dom_data.get('dataTestIds', []))}")
        print(f"  Ticket candidates: {len(dom_data.get('ticketFormCandidates', []))}")

        # Print ticket candidates for quick review
        print("\n--- Ticket Form Candidates ---")
        for cand in dom_data.get('ticketFormCandidates', []):
            print(f"  [{cand.get('type')}] {cand.get('text') or cand.get('name')} -> {cand.get('outerHTML','')[:150]}")

        print("\n--- All Buttons (first 20) ---")
        for btn in dom_data.get('buttons', [])[:20]:
            print(f"  '{btn.get('text')}' id={btn.get('id')} testId={btn.get('dataTestId')}")

        print("\n--- All Inputs ---")
        for inp in dom_data.get('inputs', []):
            print(f"  name={inp.get('name')} id={inp.get('id')} type={inp.get('type')} placeholder={inp.get('placeholder')} value={inp.get('value')}")

        print("\n--- data-testid ---")
        for dt in dom_data.get('dataTestIds', [])[:30]:
            print(f"  [{dt.get('testId')}] <{dt.get('tag')}> text='{dt.get('text')}'")

        return dom_data

    except Exception as e:
        print(f"DOM collection failed: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="UTEx DOM Inspector")
    parser.add_argument("--symbol", type=str, default="TSLA", help="Symbol to inspect (default TSLA)")
    parser.add_argument("--all", action="store_true", help="Inspect all challenge tickers")
    args = parser.parse_args()

    full_cfg = load_config()
    cfg = full_cfg.get("challenge", {})
    stealth_cfg_dict = full_cfg.get("stealth", {}) or {}
    stealth_config = StealthConfig.from_dict(stealth_cfg_dict)

    if not cfg.get("platform", {}).get("url"):
        print("challenge.platform.url not configured in config.yaml")
        return 1

    symbols = []
    if args.all:
        symbols = stealth_config.challenge_tickers or ["TSLA", "AAPL", "NVDA", "AMZN", "META"]
    else:
        symbols = [args.symbol.upper()]

    print(f"Launching browser with stealth fingerprint: {stealth_config.browser_viewport} headful={not stealth_config.browser_headless} UA={stealth_config.browser_user_agent[:50]}...")
    pw, context = launch(cfg, stealth_config=stealth_config)
    try:
        page = ensure_logged_in(context, cfg)
        session_id = str(cfg.get("platform", {}).get("session_id") or "")
        if not session_id:
            print("No session_id configured")
            return 1

        print(f"Logged in, session_id={session_id}")
        print(f"Inspecting symbols: {symbols}")

        for sym in symbols:
            try:
                dump_dom_for_symbol(page, sym, session_id, stealth_config=stealth_config)
                time.sleep(2)
            except Exception as e:
                print(f"Failed to dump {sym}: {e}")

        print(f"\n=== All dumps saved to {DUMP_DIR} ===")
        print("Next steps:")
        print("1. Open the JSON files and check real selectors for qty, price, buy/sell")
        print("2. Compare with challenge/connector.py current selectors")
        print("3. Update challenge/dom_config.yaml with real selectors")
        print("4. Test with python -m challenge.tools.dom_inspector --symbol TSLA")

        # Keep browser open for manual inspection
        print("\nBrowser kept open for 60s for manual DevTools inspection. Press Ctrl+C to close earlier.")
        try:
            time.sleep(60)
        except KeyboardInterrupt:
            pass

    finally:
        pw.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
