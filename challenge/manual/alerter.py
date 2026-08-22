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
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import yaml

ROOT = r"C:\Users\botbo\Desktop\xauusd-alert-system"
sys.path.insert(0, ROOT)

# Audit G 2026-08-23: under pythonw (scheduled task) stdout/stderr are None
# and every print() would silently vanish. Route them to a log file instead.
os.makedirs(os.path.join(ROOT, "logs", "challenge"), exist_ok=True)
if sys.stderr is None:
    sys.stderr = open(os.path.join(ROOT, "logs", "challenge", "alerter_stderr.log"),
                      "a", encoding="utf-8")
if sys.stdout is None:
    sys.stdout = open(os.path.join(ROOT, "logs", "challenge", "alerter_stdout.log"),
                      "a", encoding="utf-8")

# Audit G: single-instance guard — a second copy would double-scan and race
# the sent-file dedupe. Same pattern as watchdog.lock.
_LOCK_FILE = os.path.join(ROOT, "logs", "alerter.lock")


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return '"python' in out
    except Exception:
        return False


def acquire_single_instance() -> bool:
    if os.path.exists(_LOCK_FILE):
        try:
            old_pid = int(open(_LOCK_FILE, encoding="utf-8").read().strip())
            if old_pid != os.getpid() and _pid_alive(old_pid):
                print(f"another alerter alive (pid={old_pid}) — exiting",
                      file=sys.stderr)
                return False
        except (ValueError, OSError):
            pass
    with open(_LOCK_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


import subprocess  # noqa: E402  (used by the instance guard above)

from config.loader import get_env  # loads .env via dotenv
from challenge.manual import scanner as scanner_mod
from challenge.manual import risk as risk_mod
from challenge.manual import outcomes as outcomes_mod

REFRESH_URL = "https://api.utex.io/rest/grpc/com.unitedtraders.luna.sessionservice.api.sso.SsoService.refreshAuthorization"
GRPC_BASE = "https://demoususdt-api-margin.utex.io/rest/grpc/com.unitedtraders.luna.utex.protocol.mobile."
TOKEN_FILE = os.path.join(ROOT, "data", "challenge_tokens.json")
SENT_FILE = os.path.join(ROOT, "data", "manual", "alerts_sent.json")
OUTCOMES_CSV = os.path.join(ROOT, "data", "manual", "setup_outcomes.csv")
RESOLVED_FILE = os.path.join(ROOT, "data", "manual", "outcomes_resolved.json")
STATS_FILE = os.path.join(ROOT, "data", "manual", "setup_stats.json")
# Outcome evaluation fetches enough candles to also cover a previous day's
# session (e.g. EOD finalisation of yesterday after an overnight restart).
EVAL_CANDLES = 1500
# Scan window: ~3000 x 1-min = 50h, so at least one full prior session is
# present for the daily-activity (dead-day) ATR filter to work live.
SCAN_CANDLES = 3000
# After the session end the loop stays up a few more minutes so EOD outcomes
# ("закрыть до {end} UTC") are finalised the same evening, not next day.
FINALIZE_MINUTES = 20
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
    end = SESSION_END.strftime("%H:%M")
    return (
        f"СЕТАП {res.bias.upper()} {res.symbol} — класс {res.grade} (сигнал {st} UTC)\n"
        f"Вход {res.entry:.2f} | Стоп {res.stop:.2f} | Тейк {res.target:.2f} ({res.rr:.1f}R)\n"
        f"Размер ~{qty:.2f} шт (риск {risk_usd:.2f}$)\n"
        f"План выхода: вся позиция, стоп −1R, тейк +{res.rr:.1f}R, иначе закрыть до {end} UTC\n"
        f"{day_line()}\n"
        f"Проверь: тренд 15m={res.trend15}, 30m={res.trend30}; решай по правилам ТЗ."
    )


def resolve_open_setups(access) -> int:
    """Evaluate every alerted setup that is not resolved yet (from
    alerts_sent.json) against live candles and write decided outcomes to the
    journal + cumulative stats. Returns the number newly resolved."""
    sent = load_sent()
    resolved = outcomes_mod.load_resolved(RESOLVED_FILE)
    now = dt.datetime.now(dt.timezone.utc)
    changed = 0
    for key, rec in sorted(sent.items()):
        if key in resolved or not isinstance(rec, dict):
            continue
        try:
            date_str, sym = key.split(":", 1)
        except ValueError:
            continue
        bias = rec.get("bias")
        signal_ts = rec.get("signal_time")
        if not bias or not signal_ts:
            print(f"{now:%H:%M:%S} UTC: outcome: {key} без bias/signal_time "
                  f"(legacy-запись), пропускаю", file=sys.stderr)
            continue
        sid = SYMBOLS.get(sym)
        if not sid:
            continue
        try:
            candles = fetch_candles(access, sid, candles_count=EVAL_CANDLES)
        except Exception as e:
            print(f"{now:%H:%M:%S} UTC: outcome fetch {sym}: {e}", file=sys.stderr)
            continue
        outcome, r, mins = outcomes_mod.simulate_outcome(
            int(signal_ts), float(rec["entry"]), float(rec["stop"]),
            float(rec["target"]), bias, candles, now_ts=int(now.timestamp()))
        if outcome is None:
            continue  # сессия ещё идёт — сетап не разрешился
        row = {"date": date_str, "symbol": sym, "grade": rec.get("grade", ""),
               "bias": bias, "signal_utc": signal_ts,
               "entry": rec["entry"], "stop": rec["stop"],
               "target": rec["target"], "rr": rec.get("rr", ""),
               "outcome": outcome, "r": r, "minutes": mins,
               "resolved_utc": now.isoformat(timespec="seconds")}
        outcomes_mod.append_journal(OUTCOMES_CSV, row)
        resolved[key] = {"outcome": outcome, "r": r,
                         "resolved_utc": row["resolved_utc"]}
        outcomes_mod.save_resolved(RESOLVED_FILE, resolved)
        stats = outcomes_mod.compute_stats(outcomes_mod.read_journal(OUTCOMES_CSV))
        outcomes_mod.save_stats(STATS_FILE, stats)
        changed += 1
        print(f"{now:%H:%M:%S} UTC: исход {key}: {outcome} R{r:+.2f}", file=sys.stderr)
        try:
            tg_send(outcomes_mod.format_resolution(row, stats))
        except Exception as e:
            print(f"{now:%H:%M:%S} UTC: tg outcome msg failed: {e}", file=sys.stderr)
    return changed


def scan_watchlist(access, only_sym=None) -> list:
    today = dt.datetime.now(dt.timezone.utc).date()
    tasks = []
    for sym, sid in SYMBOLS.items():
        if only_sym and sym != only_sym:
            continue
        if sym not in CFG.get("watchlist", []):
            continue
        tasks.append((sym, sid))

    def _one(item):
        sym, sid = item
        try:
            candles = fetch_candles(access, sid, candles_count=SCAN_CANDLES)
        except Exception as e:
            print(f"{sym}: {e}", file=sys.stderr)
            return None
        return scanner_mod.scan_setup(sym, today, candles, SESSION_START, CFG)

    results = []
    with ThreadPoolExecutor(max_workers=min(12, len(tasks))) as ex:
        futures = [ex.submit(_one, t) for t in tasks]
        for fut in as_completed(futures):
            res = fut.result()
            if res is not None and res.tradable:
                results.append(res)
    return results


def main() -> int:
    once = "--once" in sys.argv
    test = "--test" in sys.argv

    if test:
        ok = tg_send(f"Алертер ручной системы: тест (UTC {dt.datetime.now(dt.timezone.utc):%H:%M:%S}). "
                     f"\n{day_line()}")
        print("sent:", ok)
        return 0 if ok else 1

    if once:
        #现货 setups (UTEX) — опционально, может упасть при протухшем токене
        try:
            access = refresh_access()
            hits = scan_watchlist(access)
            for res in hits:
                print(format_setup(res))
            print(f"tradable: {len(hits)}")
        except Exception as e:
            print(f"UTEX unavailable: {e}", file=sys.stderr)
        return 0

    print(f"Алертер запущен: poll {POLL_SECONDS}s, сессия {SESSION_START}-{SESSION_END} UTC, "
          f"watchlist {len(CFG.get('watchlist', []))}", file=sys.stderr)
    last_summary_date = ""
    # Audit A: token-death monitor — if the UTEX refresh token rots, the loop
    # used to fail silently every cycle forever. Now: 5 failures in a row ->
    # one Telegram scream, then a reminder every 10 min until it recovers.
    refresh_failures = 0
    last_dead_alert = 0.0
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        t = now.time()
        end_plus = (dt.datetime.combine(now.date(), SESSION_END)
                    + dt.timedelta(minutes=FINALIZE_MINUTES)).time()
        in_session = SESSION_START <= t <= SESSION_END
        finalizing = SESSION_END < t <= end_plus
        if not (in_session or finalizing):
            print(f"{now:%H:%M:%S} UTC: вне сессии, жду", file=sys.stderr)
            time.sleep(POLL_SECONDS)
            continue
        access = None
        try:
            access = refresh_access()
            refresh_failures = 0
        except Exception as e:
            refresh_failures += 1
            print(f"{now:%H:%M:%S} UTC: refresh failed ({refresh_failures}): {e}",
                  file=sys.stderr)
            if refresh_failures >= 5 and now.timestamp() - last_dead_alert > 600:
                tg_send("🚨 Алерты челленджа МЕРТВЫ: UTEX-токен не обновляется "
                        f"({refresh_failures} неудач подряд). Нужен релогин в браузере.")
                last_dead_alert = now.timestamp()
            time.sleep(POLL_SECONDS)
            continue
            if finalizing:
                # Сессия закончилась: финализируем EOD-исходы и шлём сводку дня.
                try:
                    resolve_open_setups(access)
                except Exception as e:
                    print(f"{now:%H:%M:%S} UTC: resolve error: {e}", file=sys.stderr)
                if now.date().isoformat() != last_summary_date:
                    last_summary_date = now.date().isoformat()
                    stats = outcomes_mod.load_stats(STATS_FILE)
                    if stats:
                        try:
                            tg_send(outcomes_mod.format_stats_summary(stats))
                        except Exception as e:
                            print(f"{now:%H:%M:%S} UTC: tg stats msg failed: {e}", file=sys.stderr)
                time.sleep(POLL_SECONDS)
                continue
            sent = load_sent()
            today = now.date().isoformat()
            hits = scan_watchlist(access)
            for res in hits:
                key = f"{today}:{res.symbol}"
                if sent.get(key):
                    continue
                ok = tg_send(format_setup(res))
                if ok:
                    sb = res.signal_bar or res.impulse_bar or {}
                    sent[key] = {"sent_at": now.isoformat(), "grade": res.grade,
                                 "entry": res.entry, "stop": res.stop,
                                 "target": res.target, "bias": res.bias,
                                 "signal_time": sb.get("time") if sb else None,
                                 "rr": res.rr}
                    save_sent(sent)
                    print(f"{now:%H:%M:%S} UTC: alert sent for {res.symbol}", file=sys.stderr)
            # После скана — разрешение открытых сетапов.
            try:
                resolve_open_setups(access)
            except Exception as e2:
                print(f"{now:%H:%M:%S} UTC: resolve error: {e2}", file=sys.stderr)
        except Exception as e:
            print(f"{now:%H:%M:%S} UTC: error: {e}", file=sys.stderr)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if not acquire_single_instance():
        sys.exit(1)
    sys.exit(main())