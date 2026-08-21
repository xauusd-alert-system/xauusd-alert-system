# -*- coding: utf-8 -*-
"""UTEX demo margin gRPC API client: candles history download for HashHedge challenge backtest."""
import io, sys, time, json, uuid, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\botbo\Desktop\xauusd-alert-system")

import requests

REFRESH_URL = "https://api.utex.io/rest/grpc/com.unitedtraders.luna.sessionservice.api.sso.SsoService.refreshAuthorization"
GRPC_BASE = "https://demoususdt-api-margin.utex.io/rest/grpc/com.unitedtraders.luna.utex.protocol.mobile."
TOKEN_FILE = r"C:\Users\botbo\Desktop\xauusd-alert-system\data\challenge_tokens.json"
REALM = "aurora"
CLIENT_ID = "utexweb"

def _headers(access):
    return {
        "Authorization": "Bearer " + access,
        "Content-Type": "application/json",
        "X-UT-GRPC-METADATA": "{}",
        "X-B3-SpanId": uuid.uuid4().hex[:16],
        "X-B3-TraceId": uuid.uuid4().hex[:16],
        "Origin": "https://markets-app.hashhedge.com",
        "Referer": "https://markets-app.hashhedge.com/",
        "User-Agent": "Mozilla/5.0",
    }

def load_refresh_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("refresh_token"):
            return d["refresh_token"]
    from config.loader import load_config
    from challenge.browser import launch, open_page
    cfg = load_config().get("challenge", {})
    pw, context = launch(cfg)
    try:
        page = open_page(context)
        page.goto(cfg["platform"]["markets_url"] + "/dashboard?lng=ru&session=" + cfg["platform"]["session_id"],
                  wait_until="domcontentloaded")
        time.sleep(6)
        rt = page.evaluate("localStorage.getItem('utex_u.770216147_refreshToken')")
    finally:
        pw.stop()
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"refresh_token": rt}, f)
    return rt

def refresh_access(refresh_token):
    r = requests.post(REFRESH_URL,
                      json={"realm": REALM, "clientId": CLIENT_ID, "refreshToken": refresh_token},
                      headers={"Authorization": "Bearer", "Content-Type": "application/json",
                               "X-UT-GRPC-METADATA": "{}",
                               "Origin": "https://markets-app.hashhedge.com",
                               "Referer": "https://markets-app.hashhedge.com/",
                               "User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return r.json()["accessToken"]

def get_candles(access, symbol_id, interval="Min1", candles_count=10000, to=None):
    to = int(time.time()) if to is None else int(to)
    r = requests.post(GRPC_BASE + "MobileDataService.getCandlesToDate",
                      json={"to": to, "symbolId": symbol_id, "candlesCount": candles_count, "interval": interval},
                      headers=_headers(access), timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"getCandlesToDate {symbol_id}: {r.status_code} {r.text[:200]}")
    data = r.json()
    candles = data.get("candles", [])
    out = []
    for c in candles:
        out.append({
            "time": int(c["time"]),
            "open": int(c["open"]) / 1e8,
            "high": int(c["high"]) / 1e8,
            "low": int(c["low"]) / 1e8,
            "close": int(c["close"]) / 1e8,
            "volume": float(c.get("volume", 0)),
        })
    out.sort(key=lambda x: x["time"])
    return out

def get_historical_prices(access, symbol_ids, days_ago=(30, 365)):
    r = requests.post(GRPC_BASE + "MobileDataService.getHistoricalPrices",
                      json={"symbolIds": list(symbol_ids), "daysAgo": list(days_ago)},
                      headers=_headers(access), timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"getHistoricalPrices: {r.status_code} {r.text[:200]}")
    return r.json()

if __name__ == "__main__":
    rt = load_refresh_token()
    print("refresh token:", rt[:20], "...")
    access = refresh_access(rt)
    print("access ok:", len(access))

    # demo: fetch AAPL (6803) 1-min candles count
    c = get_candles(access, 6803, "Min1", 600)
    print("AAPL candles:", len(c))
    if c:
        print("first:", c[0])
        print("last:", c[-1])