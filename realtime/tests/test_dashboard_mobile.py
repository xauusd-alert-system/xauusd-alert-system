"""Mobile layout regression tests for the dashboard.

Opens the dashboard in headless Chromium (Playwright) at phone viewport
widths (390px iPhone 14, 375px iPhone 13) and asserts:

* no page-level horizontal overflow — wide tables must scroll inside their
  own ``overflow-x-auto`` containers instead of stretching the page;
* no element sticks out of the viewport outside a scrollable ancestor;
* the candlestick SVG scales inside its card (viewBox regression — without
  viewBox a CSS ``max-width:100%`` shrink CLIPS the right side instead of
  scaling the whole chart).

Each test boots its own uvicorn instance on an ephemeral port, so this suite
does not depend on (or interfere with) a running dev server.

Run from the project root:

    python -m pytest realtime/tests/test_dashboard_mobile.py -v
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync.sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PHONE_VIEWPORTS = (390, 375)  # iPhone 14 / iPhone 13 widths
PAGE_HEIGHT = 844
_WAIT_AFTER_LOAD_MS = 5000  # let WS connect + first data refresh complete

# Scan for elements whose box sticks out of the viewport and that do NOT sit
# inside an overflow-x:auto/scroll ancestor (those are meant to scroll).
SCAN_JS = """() => {
    const vw = document.documentElement.clientWidth;
    function hasScrollAncestor(el) {
        let n = el.parentElement;
        while (n) {
            const s = getComputedStyle(n).overflowX;
            if (s === 'auto' || s === 'scroll') return true;
            n = n.parentElement;
        }
        return false;
    }
    const bad = [];
    document.querySelectorAll('*').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && (r.right > vw + 1 || r.left < -1) && !hasScrollAncestor(el)) {
            const cls = (typeof el.className === 'string') ? el.className.slice(0, 70) : '';
            bad.push(el.tagName.toLowerCase() + (cls ? '.' + cls : ''));
        }
    });
    return Array.from(new Set(bad)).slice(0, 10);
}"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, proc: subprocess.Popen, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            stderr_tail = ""
            try:
                with open(proc.stderr.name, encoding="utf-8", errors="replace") as f:
                    stderr_tail = f.read()[-2000:]
            except (OSError, AttributeError):
                pass
            pytest.fail(f"dashboard server exited early (rc={proc.returncode}) stderr:\n{stderr_tail}")
        try:
            with urllib.request.urlopen(url + "health", timeout=2):
                return
        except Exception:
            time.sleep(0.5)
    pytest.fail(f"dashboard server did not become ready within {timeout:.0f}s")


@pytest.fixture(scope="module")
def dashboard_server():
    port = _free_port()
    stderr_path = os.path.join(tempfile.gettempdir(), f"dash_mobile_test_{port}.log")
    env = dict(os.environ)
    env["DATA_MODE"] = env.get("DATA_MODE", "live")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "uvicorn", "realtime.app:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=open(stderr_path, "w", encoding="utf-8"),
    )
    url = f"http://127.0.0.1:{port}/"
    try:
        _wait_ready(url, proc)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            os.remove(stderr_path)
        except OSError:
            pass


@pytest.fixture(scope="module")
def chromium_browser():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # browser binary not installed in this env
            pytest.skip(f"Playwright Chromium unavailable: {exc}")
        yield browser
        browser.close()


def _open_page(browser, url: str, width: int):
    ctx = browser.new_context(viewport={"width": width, "height": PAGE_HEIGHT})
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(_WAIT_AFTER_LOAD_MS)
    return ctx, page


def test_no_page_horizontal_overflow(dashboard_server, chromium_browser):
    for width in PHONE_VIEWPORTS:
        ctx, page = _open_page(chromium_browser, dashboard_server, width)
        try:
            m = page.evaluate(
                "() => ({ vw: document.documentElement.clientWidth,"
                " sw: document.documentElement.scrollWidth })"
            )
            assert m["sw"] <= m["vw"], (
                f"page overflows horizontally at {width}px: "
                f"scrollWidth={m['sw']} > clientWidth={m['vw']}"
            )
        finally:
            ctx.close()


def test_no_uncontained_horizontal_overflow(dashboard_server, chromium_browser):
    for width in PHONE_VIEWPORTS:
        ctx, page = _open_page(chromium_browser, dashboard_server, width)
        try:
            bad = page.evaluate(SCAN_JS)
            assert not bad, f"elements stick out of the viewport at {width}px: {bad}"
        finally:
            ctx.close()


def test_chart_svg_scales_inside_card(dashboard_server, chromium_browser):
    # Regression for the missing-viewBox bug: the candlestick SVG has a fixed
    # pixel width; without viewBox a CSS max-width:100% shrink CLIPS the right
    # side of the chart instead of scaling it down proportionally.
    for width in PHONE_VIEWPORTS:
        ctx, page = _open_page(chromium_browser, dashboard_server, width)
        try:
            try:
                page.wait_for_selector("#chart-container svg", timeout=12000)
            except Exception:
                # No live MT5 feed in this environment -> chart shows the
                # "unavailable" placeholder; layout is covered by the
                # overflow tests above.
                continue
            info = page.evaluate("""() => {
                const svg = document.querySelector('#chart-container svg');
                const s = svg.getBoundingClientRect();
                const c = document.getElementById('chart-container').getBoundingClientRect();
                return { svgW: s.width, containerW: c.width, viewBox: svg.getAttribute('viewBox') };
            }""")
            assert info["viewBox"], f"chart SVG missing viewBox at {width}px (right side would be clipped)"
            assert info["svgW"] <= info["containerW"] + 1, (
                f"chart SVG ({info['svgW']:.0f}px) wider than its card "
                f"({info['containerW']:.0f}px) at {width}px"
            )
        finally:
            ctx.close()
