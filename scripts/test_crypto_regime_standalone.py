"""Standalone test of crypto regime detector on Aug 24 data."""
import sys, os, json, datetime as dt, time, requests

ROOT = r"C:\Users\botbo\Desktop\xauusd-alert-system"
sys.path.insert(0, ROOT)

# Load token and symbols manually (no alerter import)
TOKEN_FILE = os.path.join(ROOT, "data", "challenge_tokens.json")
SYMBOLS_FILE = os.path.join(ROOT, "data", "backtest", "symbols.json")
CFG_FILE = os.path.join(ROOT, "challenge", "manual", "manual_config.yaml")

import yaml
CFG = yaml.safe_load(open(CFG_FILE, encoding="utf-8"))
SYMBOLS = json.load(open(SYMBOLS_FILE, encoding="utf-8"))

REALM = "aurora"
CLIENT_ID = "utexweb"
REFRESH_URL = "https://api.utex.io/rest/grpc/com.unitedtraders.luna.sessionservice.api.sso.SsoService.refreshAuthorization"
GRPC_BASE = "https://demoususdt-api-margin.utex.io/rest/grpc/com.unitedtraders.luna.utex.protocol.mobile."

def refresh_access():
    with open(TOKEN_FILE) as f:
        rt = json.load(f)["refresh_token"]
    r = requests.post(REFRESH_URL, json={"realm": REALM, "clientId": CLIENT_ID, "refreshToken": rt},
                       headers={"Authorization": "Bearer", "Content-Type": "application/json",
                                "X-UT-GRPC-METADATA": "{}",
                                "Origin": "https://markets-app.hashhedge.com",
                                "Referer": "https://markets-app.hashhedge.com/",
                                "User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return r.json()["accessToken"]

def fetch_candles(access, symbol_id, count=1500):
    import uuid
    r = requests.post(GRPC_BASE + "MobileDataService.getCandlesToDate",
                       json={"to": int(time.time()), "symbolId": symbol_id,
                             "candlesCount": count, "interval": "Min1"},
                       headers={"Authorization": "Bearer " + access,
                                "Content-Type": "application/json",
                                "X-UT-GRPC-METADATA": "{}",
                                "Origin": "https://markets-app.hashhedge.com",
                                "Referer": "https://markets-app.hashhedge.com/",
                                "User-Agent": "Mozilla/5.0"}, timeout=60)
    r.raise_for_status()
    data = r.json()
    out = []
    for c in (data or {}).get("candles", []):
        def num(k):
            v = c[k]
            return float(v) / 1e8 if isinstance(v, int) else float(v)
        out.append({"time": int(c["time"]), "open": num("open"), "high": num("high"),
                     "low": num("low"), "close": num("close"), "volume": float(c.get("volume", 0))})
    out.sort(key=lambda x: x["time"])
    return out

def symbol_cluster(sym):
    for name, members in CFG.get("clusters", {}).items():
        if sym in members:
            return name
    return ""

def main():
    print("Refreshing token...")
    access = refresh_access()
    print("Token OK")

    # Fetch BTC data (using COIN as proxy)
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
        prices = [c["close"] for c in day]
        print(f"  Open: {prices[0]:.2f}")
        print(f"  Close: {prices[-1]:.2f}")
        print(f"  High: {max(c['high'] for c in day):.2f}")
        print(f"  Low: {min(c['low'] for c in day):.2f}")
        print(f"  Return: {(prices[-1] - prices[0]) / prices[0] * 100:+.2f}%")

        # Classify regime
        from challenge.manual.crypto_regime import classify_crypto_regime, should_block_trade
        regime = classify_crypto_regime(day)
        print(f"\nRegime: {regime.label} ({regime.score}/100)")
        print(f"BTC intraday: -{regime.btc_intraday_dd_pct:.1f}% | multiday: -{regime.btc_multiday_dd_pct:.1f}% | EMA: {regime.btc_ema_slope_pct:+.2f}%")
        print(f"Longs: {'BLOCKED' if regime.block_longs else 'ok'} | Shorts: {'BLOCKED' if regime.block_shorts else 'ok'}")

        # Test what would have been blocked
        print(f"\n=== TRADE BLOCKING ANALYSIS ===")
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

if __name__ == "__main__":
    main()
