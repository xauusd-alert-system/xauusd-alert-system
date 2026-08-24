# UTEx Manual Verification Checklist — Before First Real Run

## 1. DOM Selectors UTEx (Critical)

**Tool:** `python -m challenge.tools.dom_inspector --symbol TSLA`  
**Output:** `logs/utex_dom_dump/` — JSON, HTML, PNG

### What to check live:
1. Open UTEx via Hash Hedge dashboard → press "Торговать" → verify URL contains `session_id`
2. In DevTools (F12) → Elements tab:
   - Search `input[name="qty"]` — does it exist? What is real qty selector?
   - Check `data-testid` attributes — UTEx uses styled-components but data-testid should be stable
   - Buy/Sell buttons: text "Купить"/"Продать" or data-testid buy/sell?
   - Confirm button: "Принять" or "Confirm"?
   - Positions tab: `[data-testid="terminalTabPositions"]` exists?
   - Balance chip: `button:has(span:has-text("прибыль"))`?
   - Chart: canvas or div with data-testid chart?

3. Update `challenge/dom_config.yaml` with real selectors:
   ```yaml
   ticket_form:
     qty_input:
       selectors: ['real_selector_1', 'real_selector_2']
     buy_button:
       selectors: ['real_buy_selector']
   ```

4. Test again with updated config.

### Current guesses in connector.py:
- `input[name="qty"]` for qty
- `input[name="price"]` for price
- `button:has-text("Купить")` / `Продать` for buy/sell
- `button:has-text("Принять")` for confirm
- `button:has-text("Закрыть позицию")` for close
- `[data-testid="terminalTabPositions"]` for positions

These need verification.

## 2. Hotkey Map (UTEx Nova)

**Tool:** Check manually in UTEx UI

1. In UTEx terminal → Tools → Hotkey settings
2. Screenshot current bindings
3. Compare with `config.yaml` `stealth.browser_hotkey_map`:
   ```yaml
   browser_hotkey_map:
     buy_market_best_ask: "F1"
     sell_market_best_bid: "F2"
     buy_limit_best_bid: "F3"
     sell_limit_best_ask: "F4"
     buy_stop_mark: "F9"
     sell_stop_mark: "F10"
     buy_market_mark: "Shift+F1"
     sell_market_mark: "Shift+F2"
     close_position: "Shift+F3"
     cancel_all: "Shift+F4"
   ```
4. UTEx supports:
   - F1-F4, F9-F10 and Shift+F1-F4 customizable
   - Actions: Buy/Sell limit/stop/market at Best bid/Best ask/Mark price
   - Params: Volume (or manual), Price offset %, Confirmation mode (with/without)

5. Ensure your account's hotkeys match config, or update config to match account.

6. Test hotkey execution: `python -m challenge.tools.dry_run_recorder` — check logs for hotkey vs DOM clicks (should be 70% DOM, 30% hotkey).

## 3. Premarket Volume Feed

**Tool:** `python -m challenge.tools.premarket_checker --symbol TSLA`

### Current implementation:
- `ORBStrategy.select_tickers_by_premarket_volume(premarket_volumes, top_n=3)` — takes dict of volumes, sorts descending, selects top N.
- Rotation: not one ticker each day, but top by premarket volume.

### Questions:
- Where does UTEx show premarket volume?
  - Dashboard? Ticker modal? Stats panel?
  - Check Network tab for API: does `/api/market` or similar return volume?
- If UTEx doesn't provide premarket volume conveniently:
  - External feed: Yahoo Finance, Finnhub, Alpaca, Polygon
  - Fallback: random rotation or previous day volume

### Verification:
1. Run premarket_checker at 9:00-9:20 ET
2. Check logs/utex_dom_dump for volume-related elements
3. If no volume found, implement external feed
4. Test rotation: provide mock premarket dict, verify selected tickers are top 3

## 4. Slippage on Market Orders

**Risk:** ORB entries via market order on fast market = slippage. 1% risk ($10) with SL $0.50 on TSLA can become 1.5% after slippage.

### Checks:
1. On demo account, place market order for TSLA at 9:45 ET during high volume, check execution vs expected price
2. Measure slippage: (executed_price - expected_price) / expected_price
3. If slippage >0.3%, consider limit orders:
   - Option A: Buy limit at ORB_high + small offset (e.g., +$0.05) for long, sell limit at ORB_low - offset for short
   - Option B: Use Best bid/ask + offset % via hotkeys
   - Update `challenge/connector.py` to support limit orders with price

4. Position sizing already checks notional ≤ buying power ($5000 with leverage 1:5), but slippage can increase risk:
   - If entry slips $0.20 against you with SL $0.50, risk becomes $10 * (0.70/0.50) = $14 (1.4%)
   - Monitor actual risk vs intended

### Config option:
```yaml
challenge:
  strategy:
    orb:
      use_limit_orders: false  # set true to use limit instead of market
      limit_offset_cents: 5  # $0.05 offset for limit
```

## 5. Reset Window Edge Case (00:00-00:13 UTC+4)

**Tool:** `python -m challenge.tools.reset_window_sim`

### Scenario:
- Position open at 20:00 UTC (00:00 UTC+4) with floating -$25
- After reset, balance_at_day_start recalculates to equity at reset (includes floating)
- Risk manager should continue tracking correctly, not close erroneously

### Expected behavior:
- Before reset: floating -$25, closed 0, daily -$25, overall -$27.90 (including starting loss -$2.90) → can trade (daily -25 > -30)
- At reset 20:00 UTC: balance_at_day_start becomes equity (e.g., $972.10), closed_since_reset reset to 0, daily = floating (-$25) (if holding overnight, floating counts as new day's PnL)
- After reset, floating goes to -$35 → daily -$35 ≤ -$30 → force close triggered

### Verification:
1. Run reset_window_sim, check logs
2. Simulate holding position overnight with -$25 floating
3. Ensure after reset, daily is not reset to 0 erroneously if floating still -$25 (should be -$25 if holding)
4. Ensure force close triggers at -$35 daily after reset, not before

### Implementation details:
- `HumanizedRiskManager._is_in_reset_window(now_utc)` checks if now_utc in 00:00-00:13 UTC+4
- `_ensure_day()` handles reset: if in reset window and not yet reset for this UTC+4 date, update balance_at_day_start and reset closed_pnl_since_reset
- `update_floating_pnl()` tracks floating + closed for daily

## 6. BrowserHumanizer Visual Check

**Tool:** `python -m challenge.tools.dry_run_recorder --record-video`

### What to check:
1. Run dry-run with minimal size, watch mouse movements:
   - Bezier curves, not linear interpolation
   - Micro jitter 1-3px
   - No teleport (mouse moves through path)

2. Check logs `logs/utex_sessions/humanizer_actions_*.jsonl`:
   - Bezier paths: steps 20-40, is_linear should be False
   - Click DOM vs Hotkey: 70% vs 30%
   - Visibility changes: 2-3 per session, 30-120s duration
   - Idle breaks: every 8-15min pause 20-60s
   - Pre-trade: scroll, hover, click empty
   - Post-trade: micro movements

3. If video recording enabled (`--record-video`), videos saved to `logs/utex_sessions/*.webm`:
   - Watch video, does mouse look organic?
   - Does it look like human drawing on chart (click empty + drag)?
   - Does it scroll chart and hover levels before trade?

4. If robotic, adjust `BrowserHumanizer` params:
   - Increase bezier steps variance
   - Increase jitter
   - Add more pre-trade actions

## 7. First Real Run Recommendations

- Use minimal size: qty=1 share
- Enable detailed logging: `LOG_LEVEL=DEBUG`
- Record screen: `python -m challenge.tools.dry_run_recorder --record-video`
- Monitor `logs/utex_dom_dump/` and `logs/utex_sessions/` for organic behavior
- Check floating PnL tracking: does daily loss correctly show floating + closed?
- Verify daily reset: does balance_at_day_start update at 00:00-00:13 UTC+4?
- Watch for platform Daily Loss Protection: bot should close before -$50, at -$30

## 8. Config Overrides

All params overridable via `config.yaml` `stealth:` section, no hardcoded timings outside stealth modules:

```yaml
stealth:
  enabled: true
  seed: 42
  use_et: true  # ET mode for UTEx
  timer_base_reaction_range: [2.5, 8.0]
  risk_jitter_range: [0.007, 0.013]  # 0.7-1.3%
  challenge_daily_hard_stop: 30.0  # -$30 floating
  challenge_overall_buffer: 10.0  # -$90 floating (buffer $10 before -$100)
  challenge_tickers: ["TSLA", "AAPL", "NVDA", "AMZN", "META"]
  challenge_daily_reset_window_utc4: ["00:00", "00:13"]
  et_range_window: ["09:30", "09:45"]
  et_entry_window: ["09:45", "10:30"]
  browser_viewport: [1920, 1080]
  browser_hotkey_map:
    buy_market_best_ask: "F1"
    # ... etc
```

## 9. Next Steps

After manual verification:
1. Update `challenge/dom_config.yaml` with real selectors
2. Update hotkey map to match account
3. Implement premarket volume feed if needed
4. Decide market vs limit orders for ORB entry
5. Test reset window edge case simulation
6. Dry-run with video recording, review mouse paths
7. First real run with qty=1, minimal risk

If screen recording looks robotic, I can tune BrowserHumanizer further.
