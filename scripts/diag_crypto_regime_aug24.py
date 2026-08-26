"""Test crypto regime detector on Aug 24 data."""
import sys, os, json, datetime as dt

ROOT = r"C:\Users\botbo\Desktop\xauusd-alert-system"
sys.path.insert(0, ROOT)
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8")

from challenge.manual.alerter import refresh_access, fetch_candles, SYMBOLS
from challenge.manual.crypto_regime import classify_crypto_regime, format_regime, should_block_trade
from challenge.manual.alerter import symbol_cluster

# Fetch BTC data (using COIN as proxy)
print("Refreshing token...")
access = refresh_access()
btc_sid = SYMBOLS.get("COIN")
print(f"Fetching BTC candles...")
candles = fetch_candles(access, btc_sid, 1500)
print(f"Got {len(candles)} candles")

# Test Aug 24
target = dt.date(2026, 8, 24)
sess_start = int(dt.datetime.combine(target, dt.time(13, 30), tzinfo=dt.timezone.utc).timestamp())
day = [c for c in candles if c["time"] >= sess_start and 
       dt.datetime.fromtimestamp(c["time"], dt.timezone.utc).date() == target]
print(f"\nBTC candles on Aug 24 session: {len(day)}")

if day:
    # Show price action
    prices = [c["close"] for c in day]
    print(f"  Open: {prices[0]:.2f}")
    print(f"  Close: {prices[-1]:.2f}")
    print(f"  High: {max(c['high'] for c in day):.2f}")
    print(f"  Low: {min(c['low'] for c in day):.2f}")
    print(f"  Return: {(prices[-1] - prices[0]) / prices[0] * 100:+.2f}%")
    
    # Classify regime
    regime = classify_crypto_regime(day)
    print(f"\n{format_regime(regime)}")
    
    # Test what would have been blocked
    print(f"\n=== TRADE BLOCKING ANALYSIS ===")
    # Load the live trades from Aug 24
    outcomes_path = os.path.join(ROOT, "data", "manual", "setup_outcomes.csv")
    import csv
    with open(outcomes_path) as f:
        rows = list(csv.DictReader(f))
    day_rows = [r for r in rows if r.get("date") == str(target)]
    
    blocked_longs = 0
    blocked_shorts = 0
    allowed = 0
    for r in day_rows:
        sym = r["symbol"]
        bias = r["bias"]
        cluster = symbol_cluster(sym)
        blocked, reason = should_block_trade(regime, bias, cluster)
        outcome = r["outcome"]
        r_val = float(r["r"])
        
        status = "BLOCKED" if blocked else "ALLOWED"
        if blocked:
            if bias == "long":
                blocked_longs += 1
            else:
                blocked_shorts += 1
        else:
            allowed += 1
        
        print(f"  {sym:8s} {bias:5s} {cluster:12s} -> {status:8s} | actual: {outcome} R{r_val:+.1f}")
    
    print(f"\nSummary:")
    print(f"  Blocked longs: {blocked_longs}")
    print(f"  Blocked shorts: {blocked_shorts}")
    print(f"  Allowed: {allowed}")
    
    # Calculate what would have been the result if blocked
    blocked_rows = [r for r in day_rows if should_block_trade(regime, r["bias"], symbol_cluster(r["symbol"]))[0]]
    allowed_rows = [r for r in day_rows if not should_block_trade(regime, r["bias"], symbol_cluster(r["symbol"]))[0]]
    
    if blocked_rows:
        blocked_r = sum(float(r["r"]) for r in blocked_rows)
        print(f"\n  Blocked trades would have: {len(blocked_rows)} trades, R{blocked_r:+.2f}")
    if allowed_rows:
        allowed_r = sum(float(r["r"]) for r in allowed_rows)
        print(f"  Allowed trades have: {len(allowed_rows)} trades, R{allowed_r:+.2f}")
else:
    print("No data for Aug 24")
