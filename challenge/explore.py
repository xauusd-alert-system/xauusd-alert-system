"""python -m challenge.explore — dump the logged-in terminal DOM/screenshots.

Run after challenge.login: saves logs/challenge/dom_app.html, app.png and
prints the visible page structure so the connector selectors can be mapped.
"""

import os
import sys
import time

from config.loader import load_config

from challenge.browser import ensure_logged_in, launch, open_page

OUT_DIR = "logs/challenge"


def main():
    cfg = load_config().get("challenge", {})
    os.makedirs(OUT_DIR, exist_ok=True)
    pw, context = launch(cfg)
    try:
        page = ensure_logged_in(context, cfg)
        page.goto(cfg["platform"]["url"], wait_until="domcontentloaded")
        time.sleep(10)
        html = page.content()
        with open(os.path.join(OUT_DIR, "dom_app.html"), "w", encoding="utf-8") as f:
            f.write(html)
        page.screenshot(path=os.path.join(OUT_DIR, "app.png"))
        text = page.inner_text("body")
        print("URL:", page.url)
        print("DOM len:", len(html))
        print("=== BODY TEXT ===")
        print(text[:4000])
        print("=== BUTTONS ===")
        for b in page.locator("button").all():
            label = (b.inner_text() or "").strip()[:80]
            if label:
                print("button:", repr(label))
        print("=== INPUTS ===")
        for inp in page.locator("input").all():
            print("input:", inp.get_attribute("type"), inp.get_attribute("placeholder"))
    finally:
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())