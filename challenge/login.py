"""python -m challenge.login — open the persistent browser for manual login.

Run this once, log in by hand (the window stays open up to
platform.login_wait_seconds), the session persists in the profile dir. Right
after a successful login the dashboard DOM + screenshot are dumped to
logs/challenge/ for the connector mapping.
"""

import os
import sys
import time

from config.loader import load_config

from challenge.browser import ensure_logged_in, launch

OUT_DIR = "logs/challenge"


def main():
    cfg = load_config().get("challenge", {})
    os.makedirs(OUT_DIR, exist_ok=True)
    pw, context = launch(cfg)
    try:
        page = ensure_logged_in(context, cfg)
        time.sleep(8)
        html = page.content()
        with open(os.path.join(OUT_DIR, "dom_app.html"), "w", encoding="utf-8") as f:
            f.write(html)
        page.screenshot(path=os.path.join(OUT_DIR, "app.png"))
        print("Dumped logs/challenge/dom_app.html (%d chars) + app.png" % len(html))
    finally:
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())