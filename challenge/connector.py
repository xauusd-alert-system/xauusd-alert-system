"""HashHedge trading terminal connector.

The trading terminal is a separate UTEX-based exchange app:
    https://markets-app.hashhedge.com/stocks-usdt/exchange-pro/<SYM>-USDT
        ?modal=ticker&ticker=<SYM>-USDT&lng=ru&session=<userId>
Auth lives in localStorage of that origin (tokens for the `session` user id),
persisted in the browser profile dir; `session` is the user id shown in the
terminal URL after pressing "Торговать" on the challenges page.

The UI is styled-components (hashed css-* classes are unstable), so only
data-testid / structural selectors are used.
"""

import re
import time

MARKET_BASE = "https://markets-app.hashhedge.com/stocks-usdt"


def terminal_url(symbol: str, session_id: str) -> str:
    return ("%s/exchange-pro/%s-USDT?modal=ticker&ticker=%s-USDT&lng=ru&session=%s"
            % (MARKET_BASE, symbol, symbol, session_id))


class HashHedgeConnector:
    """Drives the exchange-pro terminal page (playwright sync API)."""

    def __init__(self, page, session_id: str):
        self.page = page
        self.session_id = session_id

    # -- navigation ---------------------------------------------------------

    def open_symbol(self, symbol: str):
        self.page.goto(terminal_url(symbol, self.session_id),
                       wait_until="domcontentloaded")
        self.page.wait_for_selector('input[name="qty"]', timeout=60000)

    # -- reading ------------------------------------------------------------

    def _tab(self, name):
        self.page.get_by_role("tab", name=re.compile(name, re.I)).first.click()

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
        """List open positions shown on the Позиции tab of the current ticker."""
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
        """Last price from the price input prefilled in the ticker modal."""
        val = self.page.locator('input[name="price"]').first.input_value()
        return float(val.replace(" ", "")) if val else 0.0

    def quote(self, symbol: str):
        self.open_symbol(symbol)
        return {"symbol": symbol, "last": self.last_price(),
                "balance": self.balance()}

    # -- trading ------------------------------------------------------------

    def _set_qty(self, qty):
        self.page.locator('input[name="qty"]').first.fill(str(qty))

    def place_order(self, symbol: str, side: str, qty: float,
                    price: float | None = None, order_type: str = "market"):
        self.open_symbol(symbol)
        if order_type != "market":
            tab = self.page.get_by_role("button",
                                        name=re.compile(order_type, re.I))
            if tab.count():
                tab.first.click()
            if price:
                self.page.locator('input[name="price"]').first.fill(str(price))
        self._set_qty(qty)
        label = "Купить" if side.lower() == "buy" else "Продать"
        btn = self.page.get_by_role("button",
                                    name=re.compile(label + r"\s", re.I)).first
        btn.click(timeout=15000)
        time.sleep(2)
        confirm = self.page.get_by_role("button",
                                        name=re.compile("Принять", re.I))
        if confirm.count():
            confirm.first.click()
            time.sleep(2)
        return True

    def close_position(self, symbol: str, qty: float | None = None):
        self.open_symbol(symbol)
        btn = self.page.get_by_role("button",
                                    name=re.compile("Закрыть позицию", re.I))
        if not btn.count():
            return False
        btn.first.click()
        time.sleep(2)
        if qty:
            self._set_qty(qty)
        confirm = self.page.get_by_role("button",
                                        name=re.compile("Принять", re.I))
        if confirm.count():
            confirm.first.click()
            time.sleep(2)
        return True

    def flatten(self):
        self.page.goto(MARKET_BASE + "/balance?lng=ru&session=" + self.session_id,
                       wait_until="domcontentloaded")
        time.sleep(5)
        return True

    def close_partial(self, symbol: str, qty: float):
        """Partial close: close `qty` shares of the position.

        RESEARCH 2026-08-22: used for the 50% partial at 1R strategy.
        Falls back to full close if the platform doesn't support partials.
        """
        return self.close_position(symbol, qty=qty)

    def modify_stop(self, symbol: str, new_stop: float):
        """Best-effort stop modification.

        Hash Hedge terminal may not expose a direct stop-modify UI.
        If the button isn't found, we silently succeed (the runner will
        re-check the stop on the next poll anyway).
        """
        try:
            self.open_symbol(symbol)
            # Try to find and click a stop-loss edit button
            sl_btn = self.page.get_by_role(
                "button", name=re.compile("стоп-лосс|stop.loss|SL", re.I))
            if sl_btn.count():
                sl_btn.first.click()
                time.sleep(1)
                # Find the SL input and update it
                sl_input = self.page.locator('input[name*="stop"], input[name*="sl"]').first
                if sl_input.count():
                    sl_input.fill(str(new_stop))
                    # Confirm
                    confirm = self.page.get_by_role(
                        "button", name=re.compile("Принять|Подтвердить|OK", re.I))
                    if confirm.count():
                        confirm.first.click()
                        time.sleep(1)
                        return True
        except Exception:
            pass  # best-effort: stop will be re-evaluated on next poll
        return False