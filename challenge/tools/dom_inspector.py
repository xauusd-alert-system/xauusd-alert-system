"""DOM Inspector — opens UTEx terminal and dumps DOM structure.

Usage:
    python -m challenge.tools.dom_inspector

Opens a persistent browser profile, navigates to the terminal, dumps
relevant DOM elements (inputs, buttons, data-testid, ticket-form candidates),
takes screenshots, saves HTML and JSON to logs/utex_dom_dump/, and keeps
the browser open for 60s for manual DevTools inspection.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

OUT_DIR = "logs/utex_dom_dump"
KEEP_OPEN_SECONDS = 60


def main():
    try:
        from config.loader import load_config
        from challenge.browser import launch, ensure_logged_in, open_page
    except ImportError as e:
        print(f"Import error: {e}")
        print("Run from the project root with: python -m challenge.tools.dom_inspector")
        return 1

    cfg = load_config().get("challenge", {})
    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    pw, context = launch(cfg)
    try:
        page = ensure_logged_in(context, cfg)

        # Navigate to a terminal page
        url = cfg.get("platform", {}).get("url", "")
        if url:
            page.goto(url, wait_until="domcontentloaded")
        time.sleep(5)

        # 1. Dump all inputs
        inputs = []
        for inp in page.locator("input").all():
            inputs.append({
                "type": inp.get_attribute("type"),
                "name": inp.get_attribute("name"),
                "placeholder": inp.get_attribute("placeholder"),
                "data_testid": inp.get_attribute("data-testid"),
                "id": inp.get_attribute("id"),
            })
        print(f"=== INPUTS ({len(inputs)}) ===")
        for i in inputs:
            print(f"  {json.dumps(i, ensure_ascii=False)}")

        # 2. Dump all buttons
        buttons = []
        for btn in page.locator("button").all():
            text = (btn.inner_text() or "").strip()[:80]
            buttons.append({
                "text": text,
                "role": btn.get_attribute("role"),
                "data_testid": btn.get_attribute("data-testid"),
                "aria_label": btn.get_attribute("aria-label"),
            })
        print(f"\n=== BUTTONS ({len(buttons)}) ===")
        for b in buttons:
            print(f"  {json.dumps(b, ensure_ascii=False)}")

        # 3. Dump data-testid elements
        testid_els = []
        for el in page.locator("[data-testid]").all():
            testid_els.append({
                "data_testid": el.get_attribute("data-testid"),
                "tag": el.evaluate("el => el.tagName.toLowerCase()"),
            })
        print(f"\n=== DATA-TESTID ({len(testid_els)}) ===")
        for e in testid_els:
            print(f"  {json.dumps(e, ensure_ascii=False)}")

        # 4. Dump role elements
        role_els = []
        for el in page.locator("[role]").all():
            role_els.append({
                "role": el.get_attribute("role"),
                "text": (el.inner_text() or "").strip()[:60],
                "tag": el.evaluate("el => el.tagName.toLowerCase()"),
            })
        print(f"\n=== ROLE ELEMENTS ({len(role_els)}) ===")
        for e in role_els:
            print(f"  {json.dumps(e, ensure_ascii=False)}")

        # 5. Save full HTML
        html = page.content()
        html_path = os.path.join(OUT_DIR, f"dom_{ts}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\n=== HTML saved: {html_path} ({len(html)} bytes) ===")

        # 6. Save screenshot
        png_path = os.path.join(OUT_DIR, f"terminal_{ts}.png")
        page.screenshot(path=png_path)
        print(f"=== Screenshot saved: {png_path} ===")

        # 7. Save JSON summary
        json_path = os.path.join(OUT_DIR, f"dom_summary_{ts}.json")
        summary = {
            "timestamp": ts,
            "url": page.url,
            "inputs": inputs,
            "buttons": buttons,
            "data_testid_elements": testid_els,
            "role_elements": role_els,
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"=== JSON saved: {json_path} ===")

        # 8. Keep open for manual inspection
        print(f"\n=== Browser stays open for {KEEP_OPEN_SECONDS}s for DevTools ===")
        print("Press Ctrl+C to close early.")
        try:
            time.sleep(KEEP_OPEN_SECONDS)
        except KeyboardInterrupt:
            print("\nClosing...")

    finally:
        pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
