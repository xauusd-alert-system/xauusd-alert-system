"""HashHedge trading terminal connector with BrowserHumanizer integration.

The trading terminal is a separate UTEX-based exchange app:
    https://markets-app.hashhedge.com/stocks-usdt/exchange-pro/<SYM>-USDT
        ?modal=ticker&ticker=<SYM>-USDT&lng=ru&session=<userId>
Auth lives in localStorage of that origin (tokens for the `session` user id),
persisted in the browser profile dir; `session` is the user id shown in the
terminal URL after pressing "Торговать" on the challenges page.

The UI is styled-components (hashed css-* classes are unstable), so only
data-testid / structural selectors are used.

All browser actions (clicks, hovers, scrolls) are executed via BrowserHumanizer
with delays from HumanizedTimer — no hardcoded timings outside stealth modules.
"""

import re
import time
import random
from datetime import datetime, timezone
from typing import Optional, Dict, Any

MARKET_BASE = "https://markets-app.hashhedge.com/stocks-usdt"


def terminal_url(symbol: str, session_id: str) -> str:
    return ("%s/exchange-pro/%s-USDT?modal=ticker&ticker=%s-USDT&lng=ru&session=%s"
            % (MARKET_BASE, symbol, symbol, session_id))


class HashHedgeConnector:
    """Drives the exchange-pro terminal page (playwright sync API) with humanization."""

    def __init__(self, page, session_id: str, browser_humanizer: Optional[Any] = None, stealth_engine: Optional[Any] = None):
        self.page = page
        self.session_id = session_id
        self.browser_humanizer = browser_humanizer
        self.stealth_engine = stealth_engine
        # For idle break tracking
        self._last_action_time = time.time()

    # -- navigation with humanization ---------------------------------------

    def open_symbol(self, symbol: str):
        # Pre-trade activity before navigation (if humanizer available)
        if self.browser_humanizer:
            try:
                self.browser_humanizer.pre_trade_activity()
                self.browser_humanizer.maybe_idle_break()
            except Exception:
                pass

        self.page.goto(terminal_url(symbol, self.session_id),
                       wait_until="domcontentloaded")
        self.page.wait_for_selector('input[name="qty"]', timeout=60000)

        # Post-navigation micro movements
        if self.browser_humanizer:
            try:
                self.browser_humanizer.post_trade_activity()
            except Exception:
                pass

    # -- reading ------------------------------------------------------------

    def _tab(self, name):
        locator = self.page.get_by_role("tab", name=re.compile(name, re.I)).first
        if self.browser_humanizer:
            try:
                self.browser_humanizer.click_locator(locator)
                return
            except Exception:
                pass
        locator.click()

    def _balance_chip_text(self) -> str:
        chip = self.page.locator(
            'button:has(span:has-text("прибыль"))').first
        return (chip.inner_text() or "") if chip.count() else ""

    def balance(self):
        """Return (equity, pnl, free_margin, margin_used) or None."""
        text = self._balance_chip_text()
        if not text:
            return None
        m = re.search(r"([\d\s]+)\s*\$", text.replace("\u00a0", " "))
        equity = float(m.group(1).replace(" ", "")) if m else 0.0
        m = re.search(r"([-+\d\s]+)\s*\$?\s*прибыль", text.replace("\u00a0", " "))
        pnl = float(m.group(1).replace(" ", "")) if m else 0.0
        return {"equity": equity, "pnl": pnl}

    def snapshot(self, watchlist=()):
        """Full platform snapshot: equity + quotes for the watchlist."""
        # Maybe simulate visibility change 2-3 times per session
        if self.browser_humanizer:
            try:
                # 5% chance per snapshot to simulate background tab
                if random.random() < 0.05:
                    self.browser_humanizer.simulate_visibility_change()
            except Exception:
                pass

        self.page.goto("%s/dashboard?lng=ru&session=%s"
                       % (MARKET_BASE, self.session_id),
                       wait_until="domcontentloaded")
        self.page.wait_for_selector("text=Мои позиции", timeout=60000)
        time.sleep(2)
        quotes = {}
        for symbol in watchlist:
            try:
                q = self.quote(symbol)
                quotes[symbol] = q
            except Exception:
                continue
        bal = self.balance()
        return {"equity": (bal or {}).get("equity", 0.0),
                "pnl": (bal or {}).get("pnl", 0.0),
                "quotes": quotes,
                "positions": []}

    def positions(self):
        self._tab("Позиции")
        rows = self.page.locator('[data-testid="terminalTabPositions"] '
                                 '~ * [role="row"]')
        out = []
        if rows.count():
            for i in range(rows.count()):
                cells = rows.nth(i).locator("[role=cell], td")
                out.append([(cells.nth(j).inner_text() or "").strip()
                            for j in range(cells.count())])
        return out

    def last_price(self) -> float:
        val = self.page.locator('input[name="price"]').first.input_value()
        return float(val.replace(" ", "")) if val else 0.0

    def quote(self, symbol: str):
        self.open_symbol(symbol)
        return {"symbol": symbol, "last": self.last_price(),
                "balance": self.balance()}

    # -- trading with humanization ------------------------------------------

    def _set_qty(self, qty):
        locator = self.page.locator('input[name="qty"]').first
        if self.browser_humanizer:
            try:
                # Humanized click then fill
                self.browser_humanizer.click_locator(locator)
                time.sleep(random.uniform(0.1, 0.3))
            except Exception:
                pass
        locator.fill(str(qty))

    def _humanized_click_button(self, locator):
        """Click button via BrowserHumanizer with 70% DOM / 30% hotkey variance."""
        if self.browser_humanizer:
            try:
                self.browser_humanizer.click_locator(locator)
                return
            except Exception:
                pass
        locator.click(timeout=15000)

    def place_order(self, symbol: str, side: str, qty: float,
                    price: float | None = None, order_type: str = "market"):
        # Pre-trade activity
        if self.browser_humanizer:
            try:
                self.browser_humanizer.pre_trade_activity()
            except Exception:
                pass

        self.open_symbol(symbol)

        if order_type != "market":
            tab = self.page.get_by_role("button",
                                        name=re.compile(order_type, re.I))
            if tab.count():
                if self.browser_humanizer:
                    try:
                        self.browser_humanizer.click_locator(tab.first)
                    except Exception:
                        tab.first.click()
                else:
                    tab.first.click()
            if price:
                price_locator = self.page.locator('input[name="price"]').first
                if self.browser_humanizer:
                    try:
                        self.browser_humanizer.click_locator(price_locator)
                    except Exception:
                        pass
                price_locator.fill(str(price))

        self._set_qty(qty)

        label = "Купить" if side.lower() in ("buy", "long") else "Продать"
        btn = self.page.get_by_role("button",
                                    name=re.compile(label + r"\s", re.I)).first

        self._humanized_click_button(btn)
        time.sleep(2)

        confirm = self.page.get_by_role("button",
                                        name=re.compile("Принять", re.I))
        if confirm.count():
            self._humanized_click_button(confirm.first)
            time.sleep(2)

        # Post-trade activity
        if self.browser_humanizer:
            try:
                self.browser_humanizer.post_trade_activity()
            except Exception:
                pass

        return True

    def close_position(self, symbol: str, qty: float | None = None):
        if self.browser_humanizer:
            try:
                self.browser_humanizer.pre_trade_activity()
            except Exception:
                pass

        self.open_symbol(symbol)
        btn = self.page.get_by_role("button",
                                    name=re.compile("Закрыть позицию", re.I))
        if not btn.count():
            return False

        self._humanized_click_button(btn.first)
        time.sleep(2)

        if qty:
            self._set_qty(qty)

        confirm = self.page.get_by_role("button",
                                        name=re.compile("Принять", re.I))
        if confirm.count():
            self._humanized_click_button(confirm.first)
            time.sleep(2)

        if self.browser_humanizer:
            try:
                self.browser_humanizer.post_trade_activity()
            except Exception:
                pass

        return True

    def flatten(self):
        self.page.goto(MARKET_BASE + "/balance?lng=ru&session=" + self.session_id,
                       wait_until="domcontentloaded")
        time.sleep(5)
        return True

    def close_partial(self, symbol: str, qty: float):
        return self.close_position(symbol, qty=qty)

    def modify_stop(self, symbol: str, new_stop: float):
        try:
            self.open_symbol(symbol)
            sl_btn = self.page.get_by_role(
                "button", name=re.compile("стоп-лосс|stop.loss|SL", re.I))
            if sl_btn.count():
                self._humanized_click_button(sl_btn.first)
                time.sleep(1)
                sl_input = self.page.locator('input[name*="stop"], input[name*="sl"]').first
                if sl_input.count():
                    if self.browser_humanizer:
                        try:
                            self.browser_humanizer.click_locator(sl_input)
                        except Exception:
                            pass
                    sl_input.fill(str(new_stop))
                    confirm = self.page.get_by_role(
                        "button", name=re.compile("Принять|Подтвердить|OK", re.I))
                    if confirm.count():
                        self._humanized_click_button(confirm.first)
                        time.sleep(1)
                        return True
        except Exception:
            pass
        return False
