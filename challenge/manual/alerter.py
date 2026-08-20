# -*- coding: utf-8 -*-
"""Live setup alerter for the manual system (ТЗ §4): polls the UTEX API during
the session and sends a Telegram message the moment a tradable A/B setup forms
(once per symbol per day).

Usage (from repo root):
    $env:PYTHONIOENCODING="utf-8"
    venv\\Scripts\\python.exe challenge\\manual\\alerter.py            # watch loop
    venv\\Scripts\\python.exe challenge\\manual\\alerter.py --once     # single scan
    venv\\Scripts\\python.exe challenge\\manual\\alerter.py --test     # test message

Telegram credentials come from .env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID),
same as the XAUUSD alert system.
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

from config.loader import get_env  # loads .env via dotenv
from challenge.manual import scanner as scanner_mod
from challenge.manual import risk as risk_mod

REFRESH_URL = "https://api.utex.io/rest/grpc/com.unitedtraders.luna.sessionservice.api.sso.SsoService.refreshAuthorization"
GRPC_BASE = "https://demoususdt-api-margin.utex.io/rest/grpc/com.unitedtraders.luna.utex.protocol.mobile."
TOKEN_FILE = os.path.join(ROOT, "data", "challenge_tokens.json")
SENT_FILE = os.path.join(ROOT, "data", "manual", "alerts_sent.json")
REALM = "aurora"
CLIENT_ID = "utexweb"

CFG = yaml.safe_load(open(os.path.join(ROOT, "challenge", "manual", "manual_config.yaml"),
                          encoding="utf-8"))
SYMBOLS = json.load(open(os.path.join(ROOT, "data", "backtest", "symbols.json"),
                         encoding="utf-8"))
POLL_SECONDS = int(CFG.get("alert_poll_seconds", 90))
SESSION_START = dt.time(*map(int, CFG.get("session_start_utc", "13:30").split(":")))
SESSION_END = dt.time(*map(int, CFG.get("session_end_utc", "19:55").split(":")))
STAGE = int(CFG.get("default_stage", 1))
PROFILE = CFG.get("default_profile", "B")
REF_EQUITY = float(CFG.get("reference_equity", 1000.0))


def tg_send(text: str) -> bool:
    token = get_env("TELEGRAM_BOT_TOKEN", required=False)
    chat = get_env("TELEGRAM_CHAT_ID", required=False)
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — cannot send", file=sys.stderr)
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"telegram send failed: {e}", file=sys.stderr)
        return False


def refresh_access():
    with open(TOKEN_FILE, encoding="utf-8") as f:
        rt = json.load(f)["refresh_token"]
    r = requests.post(REFRESH_URL,
                      json={"realm": REALM, "clientId": CLIENT_ID, "refreshToken": rt},
                      headers={"Authorization": "Bearer",
                               "Content-Type": "application/json",
                               "X-UT-GRPC-METADATA": "{}",
                               "Origin": "https://markets-app.hashhedge.com",
                               "Referer": "https://markets-app.hashhedge.com/",
                               "User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    return r.json()["accessToken"]


def fetch_candles(access, symbol_id, candles_count=720):
    r = requests.post(GRPC_BASE + "MobileDataService.getCandlesToDate",
                      json={"to": int(time.time()), "symbolId": symbol_id,
                            "candlesCount": candles_count, "interval": "Min1"},
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
        out.append({"time": int(c["time"]), "open": int(c["open"]) / 1e8,
                    "high": int(c["high"]) / 1e8, "low": int(c["low"]) / 1e8,
                    "close": int(c["close"]) / 1e8, "volume": float(c.get("volume", 0))})
    out.sort(key=lambda x: x["time"])
    return out


def load_sent() -> dict:
    if os.path.exists(SENT_FILE):
        try:
            return json.load(open(SENT_FILE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_sent(sent: dict) -> None:
    os.makedirs(os.path.dirname(SENT_FILE), exist_ok=True)
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(sent, f, indent=2, ensure_ascii=False)


def day_line() -> str:
    sm = risk_mod.DailyStateMachine()
    s = sm.state
    return (f"День {s.stage}-й, профиль {s.profile}: сделок {s.trades_today}/"
            f"{s.effective_max_trades}, убытков {s.losses_today}, PnL {s.daily_pnl():+.2f}$, "
            f"статус: {s.status}")


def format_setup(res) -> str:
    risk_usd = risk_mod.PROFILES[PROFILE]["risk_usd"]
    stop_dist = abs(res.entry - res.stop)
    qty = risk_usd / stop_dist if stop_dist > 0 else 0.0
    sb = res.signal_bar or res.impulse_bar or {}
    st = f"{dt.datetime.fromtimestamp(sb['time'], dt.timezone.utc):%H:%M}" if sb else "?"
    r1 = res.entry + (res.target - res.entry) / 2 if res.bias == "long" else res.entry - (res.entry - res.target) / 2
    be = res.entry if res.bias == "long" else res.entry
    end = SESSION_END.strftime("%H:%M")
    return (
        f"СЕТАП {res.bias.upper()} {res.symbol} — класс {res.grade} (сигнал {st} UTC)\n"
        f"Вход {res.entry:.2f} | Стоп {res.stop:.2f} | Тейк {res.target:.2f} (2R)\n"
        f"Размер ~{qty:.2f} шт (риск {risk_usd:.2f}$)\n"
        f"План выхода: 50% на 1R ({r1:.2f}), остаток в BE, закрыть до {end} UTC\n"
        f"{day_line()}\n"
        f"Проверь: тренд 15m={res.trend15}, 30m={res.trend30}; решай по правилам ТЗ."
    )


def scan_watchlist(access, only_sym=None) -> list:
    today = dt.datetime.now(dt.timezone.utc).date()
    out = []
    for sym, sid in SYMBOLS.items():
        if only_sym and sym != only_sym:
            continue
        if sym not in CFG.get("watchlist", []):
            continue
        try:
            candles = fetch_candles(access, sid)
        except Exception as e:
            print(f"{sym}: {e}", file=sys.stderr)
            continue
        res = scanner_mod.scan_setup(sym, today, candles, SESSION_START, CFG)
        if res.tradable:
            out.append(res)
    return out


def main() -> int:
    once = "--once" in sys.argv
    test = "--test" in sys.argv

    if test:
        ok = tg_send(f"Алертер ручной системы: тест (UTC {dt.datetime.now(dt.timezone.utc):%H:%M:%S}). "
                     f"\n{day_line()}")
        print("sent:", ok)
        return 0 if ok else 1

    if once:
        access = refresh_access()
        hits = scan_watchlist(access)
        for res in hits:
            print(format_setup(res))
        print(f"tradable: {len(hits)}")
        return 0

    print(f"Алертер запущен: poll {POLL_SECONDS}s, сессия {SESSION_START}-{SESSION_END} UTC, "
          f"watchlist {len(CFG.get('watchlist', []))}", file=sys.stderr)
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        if now.time() < SESSION_START or now.time() > SESSION_END:
            print(f"{now:%H:%M:%S} UTC: вне сессии, жду", file=sys.stderr)
            time.sleep(POLL_SECONDS)
            continue
        try:
            access = refresh_access()
            sent = load_sent()
            today = now.date().isoformat()
            hits = scan_watchlist(access)
            for res in hits:
                key = f"{today}:{res.symbol}"
                if sent.get(key):
                    continue
                ok = tg_send(format_setup(res))
                if ok:
                    sent[key] = {"sent_at": now.isoformat(), "grade": res.grade,
                                 "entry": res.entry, "stop": res.stop,
                                 "target": res.target}
                    save_sent(sent)
                    print(f"{now:%H:%M:%S} UTC: alert sent for {res.symbol}", file=sys.stderr)
        except Exception as e:
            print(f"{now:%H:%M:%S} UTC: error: {e}", file=sys.stderr)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())