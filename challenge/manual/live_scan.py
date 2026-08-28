# -*- coding: utf-8 -*-
"""Live watchlist scan for the manual system (ТЗ §4): fetch fresh 1-min candles
from the UTEX API for the session that is starting/has started and run the setup
scanner. Prints any tradable A/B setups for the day.

Usage (run from repo root):
    $env:PYTHONIOENCODING="utf-8"
    venv\\Scripts\\python.exe challenge\\manual\\live_scan.py [--symbol AAPL]
"""
import datetime as dt
import json
import os
import sys
import time
import uuid

import requests
import yaml

ROOT = r"C:\Users\botbo\Desktop\xauusd-alert-system"
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "challenge", "manual"))

from challenge.manual import scanner as scanner_mod

REFRESH_URL = "https://api.utex.io/rest/grpc/com.unitedtraders.luna.sessionservice.api.sso.SsoService.refreshAuthorization"
GRPC_BASE = "https://demoususdt-api-margin.utex.io/rest/grpc/com.unitedtraders.luna.utex.protocol.mobile."
TOKEN_FILE = os.path.join(ROOT, "data", "challenge_tokens.json")
REALM = "aurora"
CLIENT_ID = "utexweb"


def load_refresh_token():
    with open(TOKEN_FILE, encoding="utf-8") as f:
        d = json.load(f)
    return d["refresh_token"]


def refresh_access(refresh_token):
    r = requests.post(REFRESH_URL,
                      json={"realm": REALM, "clientId": CLIENT_ID,
                            "refreshToken": refresh_token},
                      headers={"Authorization": "Bearer",
                               "Content-Type": "application/json",
                               "X-UT-GRPC-METADATA": "{}",
                               "Origin": "https://markets-app.hashhedge.com",
                               "Referer": "https://markets-app.hashhedge.com/",
                               "User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return r.json()["accessToken"]


def get_candles(access, symbol_id, interval="Min1", candles_count=720, to=None):
    to = int(time.time()) if to is None else int(to)
    r = requests.post(GRPC_BASE + "MobileDataService.getCandlesToDate",
                      json={"to": to, "symbolId": symbol_id, "candlesCount": candles_count,
                            "interval": interval},
                      headers={"Authorization": "Bearer " + access,
                               "Content-Type": "application/json",
                               "X-UT-GRPC-METADATA": "{}",
                               "X-B3-SpanId": uuid.uuid4().hex[:16],
                               "X-B3-TraceId": uuid.uuid4().hex[:16],
                               "Origin": "https://markets-app.hashhedge.com",
                               "Referer": "https://markets-app.hashhedge.com/",
                               "User-Agent": "Mozilla/5.0"}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"getCandlesToDate {symbol_id}: {r.status_code} {r.text[:200]}")
    out = []
    for c in r.json().get("candles", []):
        out.append({"time": int(c["time"]),
                    "open": int(c["open"]) / 1e8, "high": int(c["high"]) / 1e8,
                    "low": int(c["low"]) / 1e8, "close": int(c["close"]) / 1e8,
                    "volume": float(c.get("volume", 0))})
    out.sort(key=lambda x: x["time"])
    return out


def main():
    cfg = yaml.safe_load(open(os.path.join(ROOT, "challenge", "manual", "manual_config.yaml"),
                              encoding="utf-8"))
    watch = cfg.get("watchlist") or []
    symbols = json.load(open(os.path.join(ROOT, "data", "backtest", "symbols.json"),
                             encoding="utf-8"))

    only = None
    if "--symbol" in sys.argv:
        only = sys.argv[sys.argv.index("--symbol") + 1].upper()

    today = dt.datetime.now(dt.UTC).date()
    sess = dt.time(*map(int, cfg.get("session_start_utc", "13:30").split(":")))

    rt = load_refresh_token()
    access = refresh_access(rt)
    print(f"UTC now: {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M:%S}  "
          f"session date: {today}", file=sys.stderr)

    hits = 0
    for sym in watch:
        if only and sym != only:
            continue
        sid = symbols.get(sym)
        if not sid:
            print(f"{sym}: no symbol id", file=sys.stderr)
            continue
        try:
            candles = get_candles(access, sid)
        except Exception as e:
            print(f"{sym}: fetch error: {e}", file=sys.stderr)
            continue
        res = scanner_mod.scan_setup(sym, today, candles, sess, cfg)
        line = f"{sym}: trend15={res.trend15} trend30={res.trend30} grade={res.grade} bias={res.bias} rr={res.rr}"
        if res.impulse_bar:
            ib = res.impulse_bar
            line += f" | impulse {dt.datetime.fromtimestamp(ib['time'], dt.UTC):%H:%M} "
            line += f"H{ib['high']:.2f} L{ib['low']:.2f}"
        if res.signal_bar:
            sb = res.signal_bar
            line += f" | signal {dt.datetime.fromtimestamp(sb['time'], dt.UTC):%H:%M} C{sb['close']:.2f}"
        if res.tradable:
            hits += 1
            print(f"TRADE {res.bias.upper()} {sym} @ {res.entry:.2f} stop {res.stop:.2f} "
                  f"target {res.target:.2f} (R:R {res.rr}, {res.grade})")
        else:
            print(line + (" | NO-GO: " + ", ".join(res.no_go) if res.no_go else ""))
    print(f"\nTradable setups today: {hits}", file=sys.stderr)
    if hits == 0:
        print("No A/B setups right now. Re-run during/after the impulse window "
              "(first 60-90 min of the session).")


if __name__ == "__main__":
    main()
