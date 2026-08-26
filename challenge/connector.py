"""HashHedge trading terminal connector.

REFACTORED: no full page reloads in the hot loop.
"""

import re
import time
import logging

logger = logging.getLogger("challenge_connector")
MARKET_BASE = "https://markets-app.hashhedge.com/stocks-usdt"


def terminal_url(symbol, session_id):
    return ("%s/exchange-pro/%s-USDT?modal=ticker&ticker=%s-USDT&lng=ru&session=%s"
            % (MARKET_BASE, symbol, symbol, session_id))


class HashHedgeConnector:

    def __init__(self, page, session_id):
        self.page = page
        self.session_id = session_id
        self._current_symbol = None
        self._navigated_to_terminal = False

    def open_symbol(self, symbol, force=False):
        if self._current_symbol == symbol and not force:
            return
        url = terminal_url(symbol, self.session_id)
        self.page.goto(url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_selector('input[name="qty"]', timeout=30000)
        except Exception:
            logger.warning("qty input not found for %s", symbol)
        self._current_symbol = symbol
        self._navigated_to_terminal = True

    def navigate_to_terminal(self, symbol=None):
        self.open_symbol(symbol or self._current_symbol or "TSLA", force=True)

    def _tab(self, name):
        self.page.get_by_role("tab", name=re.compile(name, re.I)).first.click()

    def _balance_chip_text(self):
        chip = self.page.locator('button:has(span:has-text("прибыль"))').first
        return (chip.inner_text() or "") if chip.count() else ""

    @staticmethod
    def _to_float(s: str) -> float:
        """Parse a numeric string; empty/whitespace-only yields 0.0 instead of
        raising (the balance chip can briefly render '$' with no digits yet)."""
        s = (s or "").replace(" ", "").replace("\u00a0", "")
        return float(s) if s else 0.0

    def balance(self):
        text = self._balance_chip_text()
        if not text:
            return None
        m = re.search(r"([\d\s]+)\s*\$", text.replace(" ", " "))
        equity = self._to_float(m.group(1)) if m else 0.0
        m = re.search(r"([-+\d\s]+)\s*\s*прибыль", text.replace(" ", " "))
        pnl = self._to_float(m.group(1)) if m else 0.0
        return {"equity": equity, "pnl": pnl}

    def last_price(self):
        val = self.page.locator('input[name="price"]').first.input_value()
        return self._to_float(val)

    def snapshot(self, watchlist=()):
        if not self._navigated_to_terminal:
            self.open_symbol(watchlist[0] if watchlist else "TSLA", force=True)
        bal = self.balance()
        quotes = {}
        if self._current_symbol:
            try:
                last = self.last_price()
                if last > 0:
                    quotes[self._current_symbol] = {"symbol": self._current_symbol, "last": last, "balance": bal}
            except Exception:
                pass
        return {"equity": (bal or {}).get("equity", 0.0), "pnl": (bal or {}).get("pnl", 0.0), "quotes": quotes, "positions": []}

    def positions(self):
        try:
            self._tab("Позиции")
            time.sleep(0.5)
        except Exception:
            pass
        rows = self.page.locator('[data-testid="terminalTabPositions"] ~ * [role="row"]')
        out = []
        if rows.count():
            for i in range(rows.count()):
                cells = rows.nth(i).locator("[role=cell], td")
                out.append([(cells.nth(j).inner_text() or "").strip() for j in range(cells.count())])
        return out

    def quote(self, symbol):
        self.open_symbol(symbol)
        return {"symbol": symbol, "last": self.last_price(), "balance": self.balance()}

    def _set_qty(self, qty):
        self.page.locator('input[name="qty"]').first.fill(str(qty))

    def place_order(self, symbol, side, qty, price=None, order_type="market"):
        self.open_symbol(symbol, force=True)
        if order_type != "market":
            tab = self.page.get_by_role("button", name=re.compile(order_type, re.I))
            if tab.count(): tab.first.click()
            if price:
                self.page.locator('input[name="price"]').first.fill(str(price))
        self._set_qty(qty)
        label = "Купить" if side.lower() == "buy" else "Продать"
        btn = self.page.get_by_role("button", name=re.compile(label + r"\s", re.I)).first
        btn.click(timeout=15000)
        time.sleep(2)
        confirm = self.page.get_by_role("button", name=re.compile("Принять", re.I))
        if confirm.count():
            confirm.first.click()
            time.sleep(2)
        return True

    def close_position(self, symbol, qty=None):
        self.open_symbol(symbol, force=True)
        btn = self.page.get_by_role("button", name=re.compile("Закрыть позицию", re.I))
        if not btn.count(): return False
        btn.first.click()
        time.sleep(2)
        if qty: self._set_qty(qty)
        confirm = self.page.get_by_role("button", name=re.compile("Принять", re.I))
        if confirm.count():
            confirm.first.click()
            time.sleep(2)
        return True

    def flatten(self):
        self.page.goto(MARKET_BASE + "/balance?lng=ru&session=" + self.session_id, wait_until="domcontentloaded")
        time.sleep(5)
        return True

    def close_partial(self, symbol, qty):
        return self.close_position(symbol, qty=qty)

    def modify_stop(self, symbol, new_stop):
        try:
            self.open_symbol(symbol, force=True)
            sl_btn = self.page.get_by_role("button", name=re.compile("стоп-лосс|stop.loss|SL", re.I))
            if sl_btn.count():
                sl_btn.first.click()
                time.sleep(1)
                sl_input = self.page.locator('input[name*="stop"], input[name*="sl"]').first
                if sl_input.count():
                    sl_input.fill(str(new_stop))
                    confirm = self.page.get_by_role("button", name=re.compile("Принять|Подтвердить|OK", re.I))
                    if confirm.count():
                        confirm.first.click()
                        time.sleep(1)
                        return True
        except Exception:
            pass
        return False
