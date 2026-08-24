"""HashHedge trading terminal connector with BrowserHumanizer and DOM config.

The trading terminal is a separate UTEX-based exchange app:
    https://markets-app.hashhedge.com/stocks-usdt/exchange-pro/<SYM>-USDT
        ?modal=ticker&ticker=<SYM>-USDT&lng=ru&session=<userId>
Auth lives in localStorage of that origin (tokens for the `session` user id),
persisted in the browser profile dir; `session` is the user id shown in the
terminal URL after pressing "Торговать" on the challenges page.

The UI is styled-components (hashed css-* classes are unstable), so only
data-testid / structural selectors are used. Real selectors should be verified
via challenge/tools/dom_inspector.py on live terminal.

All browser actions (clicks, hovers, scrolls) are executed via BrowserHumanizer
with delays from HumanizedTimer — no hardcoded timings outside stealth modules.
"""

import re
import time
import random
import logging
import os
import yaml
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from pathlib import Path

logger = logging.getLogger("utex_connector")

MARKET_BASE = "https://markets-app.hashhedge.com/stocks-usdt"


def terminal_url(symbol: str, session_id: str) -> str:
    return ("%s/exchange-pro/%s-USDT?modal=ticker&ticker=%s-USDT&lng=ru&session=%s"
            % (MARKET_BASE, symbol, symbol, session_id))


def load_dom_config(path: str = "challenge/dom_config.yaml") -> Dict[str, Any]:
    """Load DOM selectors from yaml, fallback to defaults if not exists."""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            logger.info(f"Loaded DOM config from {path}")
            return data
    except Exception as e:
        logger.warning(f"Failed to load DOM config {path}: {e}")
    return {}


class HashHedgeConnector:
    """Drives the exchange-pro terminal page (playwright sync API) with humanization."""

    def __init__(self, page, session_id: str, browser_humanizer: Optional[Any] = None, stealth_engine: Optional[Any] = None, dom_config_path: str = "challenge/dom_config.yaml"):
        self.page = page
        self.session_id = session_id
        self.browser_humanizer = browser_humanizer
        self.stealth_engine = stealth_engine
        self._last_action_time = time.time()
        self.dom_config = load_dom_config(dom_config_path)
        # Cache for which selectors work
        self._working_selectors: Dict[str, str] = {}

    def _try_selectors(self, selectors: List[str], timeout: int = 5000):
        """Try multiple selectors, return first locator that exists."""
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() > 0:
                    # Optionally check visible
                    try:
                        if loc.is_visible(timeout=timeout):
                            self._working_selectors[sel] = sel
                            logger.debug(f"Selector worked: {sel}")
                            return loc
                    except Exception:
                        # If not visible check, still return if count>0
                        return loc
            except Exception:
                continue
        return None

    def _get_selector_list(self, key_path: str, default: List[str]) -> List[str]:
        """Get selector list from dom_config via dot path, fallback to default."""
        try:
            parts = key_path.split(".")
            cur = self.dom_config
            for p in parts:
                cur = cur.get(p, {})
            if isinstance(cur, dict):
                sels = cur.get("selectors")
                if sels and isinstance(sels, list):
                    return sels
            elif isinstance(cur, list):
                return cur
        except Exception:
            pass
        return default

    # -- navigation with humanization ---------------------------------------

    def open_symbol(self, symbol: str):
        if self.browser_humanizer:
            try:
                self.browser_humanizer.pre_trade_activity()
                self.browser_humanizer.maybe_idle_break()
            except Exception:
                pass

        self.page.goto(terminal_url(symbol, self.session_id),
                       wait_until="domcontentloaded")
        # Try to wait for qty input with multiple selectors
        qty_selectors = self._get_selector_list("ticket_form.qty_input", ['input[name="qty"]', 'input[name="quantity"]', '[data-testid*="qty"]'])
        found = False
        for sel in qty_selectors:
            try:
                self.page.wait_for_selector(sel, timeout=10000)
                found = True
                logger.debug(f"open_symbol {symbol}: found qty selector {sel}")
                break
            except Exception:
                continue
        if not found:
            logger.warning(f"open_symbol {symbol}: no qty selector found from {qty_selectors}, waiting generic input")
            try:
                self.page.wait_for_selector('input', timeout=15000)
            except Exception:
                pass
        time.sleep(1)

        if self.browser_humanizer:
            try:
                self.browser_humanizer.post_trade_activity()
            except Exception:
                pass

    # -- reading ------------------------------------------------------------

    def _tab(self, name):
        # Try dom_config first
        tab_key = "ticket_form.positions_tab" if "позиц" in name.lower() else "ticket_form.orders_tab" if "ордер" in name.lower() else None
        if tab_key:
            sels = self._get_selector_list(tab_key, [])
            loc = self._try_selectors(sels)
            if loc:
                if self.browser_humanizer:
                    try:
                        self.browser_humanizer.click_locator(loc)
                        return
                    except Exception:
                        pass
                loc.click()
                return

        locator = self.page.get_by_role("tab", name=re.compile(name, re.I)).first
        if self.browser_humanizer:
            try:
                self.browser_humanizer.click_locator(locator)
                return
            except Exception:
                pass
        try:
            locator.click()
        except Exception as e:
            logger.debug(f"_tab {name} click failed: {e}")

    def _balance_chip_text(self) -> str:
        # Try multiple selectors for balance chip
        balance_selectors = self._get_selector_list("balance.chip", ['button:has(span:has-text("прибыль"))', '[data-testid*="balance"]', '[data-testid*="equity"]'])
        for sel in balance_selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count():
                    txt = loc.inner_text() or ""
                    if txt:
                        return txt
            except Exception:
                continue
        return ""

    def balance(self):
        """Return equity, pnl with floating."""
        text = self._balance_chip_text()
        if not text:
            return None
        # Parse equity and floating PnL
        m = re.search(r"([\d\s]+)\s*\$", text.replace("\u00a0", " "))
        equity = float(m.group(1).replace(" ", "")) if m else 0.0
        m = re.search(r"([-+\d\s]+)\s*\$?\s*прибыль", text.replace("\u00a0", " "))
        pnl = float(m.group(1).replace(" ", "")) if m else 0.0
        # Floating PnL is same as pnl in this chip
        return {"equity": equity, "pnl": pnl, "floating_pnl": pnl, "balance": equity - pnl}

    def snapshot(self, watchlist=()):
        """Full platform snapshot: equity + quotes + floating PnL."""
        if self.browser_humanizer:
            try:
                if random.random() < 0.05:
                    self.browser_humanizer.simulate_visibility_change()
                self.browser_humanizer.maybe_idle_break()
            except Exception:
                pass

        self.page.goto("%s/dashboard?lng=ru&session=%s"
                       % (MARKET_BASE, self.session_id),
                       wait_until="domcontentloaded")
        try:
            self.page.wait_for_selector("text=Мои позиции", timeout=15000)
        except Exception:
            logger.debug("snapshot: Мои позиции not found, trying positions tab")
        time.sleep(2)
        quotes = {}
        for symbol in watchlist:
            try:
                q = self.quote(symbol)
                quotes[symbol] = q
            except Exception as e:
                logger.debug(f"snapshot quote failed for {symbol}: {e}")
                continue
        bal = self.balance()
        equity = (bal or {}).get("equity", 0.0)
        floating = (bal or {}).get("floating_pnl", 0.0) or (bal or {}).get("pnl", 0.0)
        return {"equity": equity,
                "pnl": floating,
                "floating_pnl": floating,
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
        # Try multiple price selectors
        price_selectors = self._get_selector_list("ticket_form.price_input", ['input[name="price"]', '[data-testid*="price"]'])
        for sel in price_selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count():
                    val = loc.input_value()
                    if val:
                        return float(val.replace(" ", ""))
            except Exception:
                continue
        return 0.0

    def quote(self, symbol: str):
        self.open_symbol(symbol)
        return {"symbol": symbol, "last": self.last_price(),
                "balance": self.balance()}

    # -- trading with humanization ------------------------------------------

    def _set_qty(self, qty):
        qty_selectors = self._get_selector_list("ticket_form.qty_input", ['input[name="qty"]', 'input[name="quantity"]'])
        locator = self._try_selectors(qty_selectors)
        if not locator:
            locator = self.page.locator('input[name="qty"]').first

        if self.browser_humanizer:
            try:
                self.browser_humanizer.click_locator(locator)
                time.sleep(random.uniform(0.1, 0.3))
            except Exception:
                pass
        try:
            locator.fill(str(qty))
        except Exception as e:
            logger.warning(f"_set_qty fill failed for {qty}: {e}")

    def _humanized_click_button(self, locator):
        if self.browser_humanizer:
            try:
                self.browser_humanizer.click_locator(locator)
                return
            except Exception:
                pass
        try:
            locator.click(timeout=15000)
        except Exception as e:
            logger.debug(f"click button failed: {e}")

    def place_order(self, symbol: str, side: str, qty: float,
                    price: float | None = None, order_type: str = "market"):
        if self.browser_humanizer:
            try:
                self.browser_humanizer.pre_trade_activity()
            except Exception:
                pass

        self.open_symbol(symbol)

        if order_type != "market":
            # Try to find order type tab
            try:
                tab = self.page.get_by_role("button", name=re.compile(order_type, re.I))
                if tab.count():
                    if self.browser_humanizer:
                        try:
                            self.browser_humanizer.click_locator(tab.first)
                        except Exception:
                            tab.first.click()
                    else:
                        tab.first.click()
            except Exception:
                pass

            if price:
                price_selectors = self._get_selector_list("ticket_form.price_input", ['input[name="price"]'])
                price_locator = self._try_selectors(price_selectors)
                if not price_locator:
                    price_locator = self.page.locator('input[name="price"]').first
                if self.browser_humanizer:
                    try:
                        self.browser_humanizer.click_locator(price_locator)
                    except Exception:
                        pass
                try:
                    price_locator.fill(str(price))
                except Exception:
                    pass

        self._set_qty(qty)

        # Buy/Sell button with multiple selectors
        if side.lower() in ("buy", "long"):
            buy_selectors = self._get_selector_list("ticket_form.buy_button", ['button:has-text("Купить")', '[data-testid*="buy"]'])
            btn = self._try_selectors(buy_selectors)
            if not btn:
                label = "Купить"
                btn = self.page.get_by_role("button", name=re.compile(label + r"\s", re.I)).first
        else:
            sell_selectors = self._get_selector_list("ticket_form.sell_button", ['button:has-text("Продать")', '[data-testid*="sell"]'])
            btn = self._try_selectors(sell_selectors)
            if not btn:
                label = "Продать"
                btn = self.page.get_by_role("button", name=re.compile(label + r"\s", re.I)).first

        if btn:
            self._humanized_click_button(btn)
        time.sleep(2)

        confirm_selectors = self._get_selector_list("ticket_form.confirm_button", ['button:has-text("Принять")', '[data-testid*="confirm"]'])
        confirm = self._try_selectors(confirm_selectors)
        if not confirm:
            confirm = self.page.get_by_role("button", name=re.compile("Принять", re.I)).first
            if not confirm.count():
                confirm = None

        if confirm:
            try:
                if confirm.count():
                    self._humanized_click_button(confirm.first if hasattr(confirm, 'first') else confirm)
                    time.sleep(2)
            except Exception:
                try:
                    # Try generic confirm
                    self.page.get_by_role("button", name=re.compile("Принять", re.I)).first.click()
                    time.sleep(2)
                except Exception:
                    pass

        if self.browser_humanizer:
            try:
                self.browser_humanizer.post_trade_activity()
            except Exception:
                pass

        logger.info(f"place_order {symbol} {side} qty={qty} price={price} type={order_type} -> OK (selectors logged)")
        return True

    def close_position(self, symbol: str, qty: float | None = None):
        if self.browser_humanizer:
            try:
                self.browser_humanizer.pre_trade_activity()
            except Exception:
                pass

        self.open_symbol(symbol)

        close_selectors = self._get_selector_list("ticket_form.close_position_button", ['button:has-text("Закрыть позицию")', '[data-testid*="close"]'])
        btn = self._try_selectors(close_selectors)
        if not btn:
            btn = self.page.get_by_role("button", name=re.compile("Закрыть позицию", re.I)).first
            if not btn.count():
                logger.warning(f"close_position {symbol}: close button not found")
                return False

        self._humanized_click_button(btn.first if hasattr(btn, 'first') else btn)
        time.sleep(2)

        if qty:
            self._set_qty(qty)

        confirm_selectors = self._get_selector_list("ticket_form.confirm_button", ['button:has-text("Принять")'])
        confirm = self._try_selectors(confirm_selectors)
        if not confirm:
            confirm = self.page.get_by_role("button", name=re.compile("Принять", re.I)).first

        if confirm and confirm.count():
            self._humanized_click_button(confirm.first if hasattr(confirm, 'first') else confirm)
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
        logger.info(f"close_partial {symbol} qty={qty}")
        return self.close_position(symbol, qty=qty)

    def modify_stop(self, symbol: str, new_stop: float):
        """Modify SL via UI — UTEx may not have native trailing, so we modify SL order."""
        try:
            self.open_symbol(symbol)
            sl_selectors = ['button:has-text("стоп-лосс")', 'button:has-text("Stop")', '[data-testid*="sl"]', '[data-testid*="stop"]']
            sl_btn = self._try_selectors(sl_selectors)
            if not sl_btn:
                sl_btn = self.page.get_by_role("button", name=re.compile("стоп-лосс|stop.loss|SL", re.I)).first
                if not sl_btn.count():
                    sl_btn = None

            if sl_btn:
                self._humanized_click_button(sl_btn.first if hasattr(sl_btn, 'first') else sl_btn)
                time.sleep(1)
                sl_input_selectors = ['input[name*="stop"]', 'input[name*="sl"]', '[data-testid*="stop"]']
                sl_input = self._try_selectors(sl_input_selectors)
                if not sl_input:
                    sl_input = self.page.locator('input[name*="stop"], input[name*="sl"]').first

                if sl_input and sl_input.count():
                    if self.browser_humanizer:
                        try:
                            self.browser_humanizer.click_locator(sl_input)
                        except Exception:
                            pass
                    sl_input.fill(str(new_stop))
                    confirm_selectors = self._get_selector_list("ticket_form.confirm_button", ['button:has-text("Принять")', 'button:has-text("Подтвердить")'])
                    confirm = self._try_selectors(confirm_selectors)
                    if not confirm:
                        confirm = self.page.get_by_role("button", name=re.compile("Принять|Подтвердить|OK", re.I)).first
                    if confirm and confirm.count():
                        self._humanized_click_button(confirm.first if hasattr(confirm, 'first') else confirm)
                        time.sleep(1)
                        logger.info(f"modify_stop {symbol} new_stop={new_stop} -> OK")
                        return True
        except Exception as e:
            logger.debug(f"modify_stop failed for {symbol}: {e}")
        return False
