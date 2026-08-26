# Pre-Launch Verification Checklist

Complete EVERY item before running the bot live. Check off each item
after manual verification on the live terminal.

---

## 1. DOM Selector Verification

Run `python -m challenge.tools.dom_inspector` and verify:

- [ ] `input[name="qty"]` exists and is editable
- [ ] `input[name="price"]` exists and is editable
- [ ] "Купить" (Buy) button found and clickable
- [ ] "Продать" (Sell) button found and clickable
- [ ] "Принять" (Confirm) button found and clickable
- [ ] "Закрыть позицию" (Close position) button found and clickable
- [ ] Balance chip found (contains "прибыль")
- [ ] "Мои позиции" tab navigable
- [ ] All `data-testid` elements logged — update `challenge/dom_config.yaml` if any selectors differ

## 2. Hotkey Verification

Open **Tools → Hotkey settings** in the terminal and verify:

- [ ] F1 → Buy Market (or map to correct key)
- [ ] F2 → Sell Market (or map to correct key)
- [ ] F3 → Buy Limit
- [ ] F4 → Sell Limit
- [ ] F9 → Cancel All
- [ ] F10 → Close All
- [ ] Shift+F1 → Quantity Up
- [ ] Shift+F2 → Quantity Down
- [ ] Shift+F3 → Price Up
- [ ] Shift+F4 → Price Down
- [ ] Update `challenge/dom_config.yaml` hotkey section if different

## 3. Premarket Volume Feed

Run `python -m challenge.tools.premarket_checker` and verify:

- [ ] TSLA: premarket volume visible (where?)
- [ ] AAPL: premarket volume visible (where?)
- [ ] NVDA: premarket volume visible (where?)
- [ ] AMZN: premarket volume visible (where?)
- [ ] META: premarket volume visible (where?)
- [ ] Document the DOM location of premarket volume in `dom_config.yaml`

## 4. Market vs Limit Order — Slippage Check

Place ONE small test order (1 share) as:

- [ ] **Market order**: Note fill price vs displayed price → slippage: ___
- [ ] **Limit order at B/A**: Note fill behavior → immediate fill? partial?
- [ ] **Stop order**: Note trigger behavior → market or limit?
- [ ] Document: which order type minimizes slippage for each ticker

## 5. Reset Window Behavior

Run `python -m challenge.tools.reset_window_sim` and verify:

- [ ] balance_at_day_start recalculated at 00:00 UTC+4
- [ ] daily_pnl resets to 0 (or floating from new start)
- [ ] No orders placed during 00:00-00:13 UTC+4 window
- [ ] Open position survives the reset window
- [ ] Verify with real terminal: does UTEx auto-close positions at reset? (should NOT)

## 6. BrowserHumanizer Visual Check

Run `python -m challenge.tools.dry_run_recorder --record-video --duration 60` and verify:

- [ ] Mouse movements are smooth (Bezier curves, no teleporting)
- [ ] Movements look organic on video playback
- [ ] No HeadlessChrome in user agent
- [ ] Canvas/WebGL fingerprint passes (check console for errors)
- [ ] Idle breaks trigger after 8-15 minutes (shorten for test)
- [ ] Pre-trade activity (scroll, hover) looks natural
- [ ] Screenshots saved to `logs/utex_sessions/`

## 7. Position Sizing Verification

Manually calculate and verify:

- [ ] $10 risk at 0.5% stop on TSLA ($250) = 8 shares → bot says: ___
- [ ] $10 risk at 1% stop on NVDA ($120) = 8 shares → bot says: ___
- [ ] Buying power check: $1000 × 5 = $5000 max notional
- [ ] Notional 8 × $250 = $2000 < $5000 ✓

## 8. Emergency Scenarios

Test these edge cases:

- [ ] Simulate -$30 daily floating → bot should close all and stop
- [ ] Simulate -$90 overall floating → bot should halt completely
- [ ] Simulate daily reset at 00:00-00:13 UTC+4 with open position
- [ ] Simulate session end at 15:30 ET → all positions closed

## 9. Final Pre-Launch

- [ ] All DOM selectors verified against live terminal
- [ ] Hotkeys confirmed in terminal settings
- [ ] Premarket volume location documented
- [ ] Dry-run video reviewed — looks human
- [ ] Emergency scenarios pass
- [ ] Config overrides applied in `config/config.yaml` under `challenge.stealth`
- [ ] Earnings calendar populated (or TODO noted)
- [ ] News calendar feed connected (or TODO noted)
- [ ] `logs/` directory exists and is writable

---

**Date verified:** _______________
**Verified by:** _______________
**Notes:** _______________
