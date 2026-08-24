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
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import yaml

ROOT = r"C:\Users\botbo\Desktop\xauusd-alert-system"
sys.path.insert(0, ROOT)

# Audit G 2026-08-23: under pythonw (scheduled task) stdout/stderr are None
# and every print() would silently vanish. Also when launched via Popen with
# DEVNULL, stderr exists but all output is discarded. Always redirect to file.
os.makedirs(os.path.join(ROOT, "logs", "challenge"), exist_ok=True)
sys.stderr = open(os.path.join(ROOT, "logs", "challenge", "alerter_stderr.log"),
                  "a", encoding="utf-8")
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


from config.loader import get_env  # loads .env via dotenv
from challenge.manual import scanner as scanner_mod
from challenge.manual import risk as risk_mod
from challenge.manual import outcomes as outcomes_mod
from challenge.manual import quality_gate as quality_mod

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
    payload = {"realm": REALM, "clientId": CLIENT_ID, "refreshToken": rt}
    headers = {"Authorization": "Bearer",
               "Content-Type": "application/json",
               "X-UT-GRPC-METADATA": "{}",
               "Origin": "https://markets-app.hashhedge.com",
               "Referer": "https://markets-app.hashhedge.com/",
               "User-Agent": "Mozilla/5.0"}
    # 1) обычный путь через requests
    try:
        r = requests.post(REFRESH_URL, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()["accessToken"]
    except Exception as e:
        msg = str(e)
        is_network = (
            isinstance(e, (requests.exceptions.SSLError,
                           requests.exceptions.ConnectionError,
                           requests.exceptions.ReadTimeout,
                           requests.exceptions.Timeout))
            or "SSLEOF" in msg or "Read timed out" in msg
            or "handshake" in msg.lower() or "Max retries" in msg
            or "Timeout" in type(e).__name__
        )
        if not is_network:
            raise
        print(f"refresh via requests failed ({type(e).__name__}), "
              f"пробую через Playwright…", file=sys.stderr)
        try:
            import json as _json
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True,
                    args=["--disable-blink-features=AutomationControlled"])
                ctx = browser.new_context()
                resp = ctx.request.post(REFRESH_URL,
                    data=_json.dumps(payload),
                    headers=headers)
                if not resp.ok:
                    raise RuntimeError(f"playwright refresh {resp.status}: {resp.text()[:200]}")
                data = resp.json()
                browser.close()
                return data["accessToken"]
        except Exception as e2:
            print(f"playwright fallback also failed: {e2}", file=sys.stderr)
            raise e


def _decode_candles(data, symbol_id):
    """Normalize UTEX candle payload into scanner candles."""
    out = []
    for c in (data or {}).get("candles", []):
        def number(key):
            value = c[key]
            return float(value) / 1e8 if isinstance(value, int) else float(value)
        out.append({"time": int(c["time"]), "open": number("open"),
                    "high": number("high"), "low": number("low"),
                    "close": number("close"), "volume": float(c.get("volume", 0))})
    out.sort(key=lambda x: x["time"])
    if not out:
        raise RuntimeError(f"getCandles {symbol_id}: empty candle response")
    return out


def fetch_candles(access, symbol_id, candles_count=720):
    payload = {"to": int(time.time()), "symbolId": symbol_id,
               "candlesCount": candles_count, "interval": "Min1"}
    headers = {"Authorization": "Bearer " + access,
               "Content-Type": "application/json",
               "X-UT-GRPC-METADATA": "{}",
               "X-B3-SpanId": uuid.uuid4().hex[:16],
               "X-B3-TraceId": uuid.uuid4().hex[:16],
               "Origin": "https://markets-app.hashhedge.com",
               "Referer": "https://markets-app.hashhedge.com/",
               "User-Agent": "Mozilla/5.0"}
    # 1) requests
    try:
        r = requests.post(GRPC_BASE + "MobileDataService.getCandlesToDate",
                          json=payload, headers=headers, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"getCandlesToDate {symbol_id}: {r.status_code} {r.text[:200]}")
        return _decode_candles(r.json(), symbol_id)
    except Exception as e:
        msg = str(e)
        is_network = (
            isinstance(e, (requests.exceptions.SSLError,
                           requests.exceptions.ConnectionError,
                           requests.exceptions.ReadTimeout,
                           requests.exceptions.Timeout))
            or "SSLEOF" in msg or "Read timed out" in msg
            or "handshake" in msg.lower() or "Max retries" in msg
            or "Timeout" in type(e).__name__
        )
        if not is_network:
            raise
        print(f"getCandles {symbol_id} via requests failed ({type(e).__name__}), "
              f"пробую Playwright…", file=sys.stderr)
        import json as _json
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True,
                args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context()
            resp = ctx.request.post(GRPC_BASE + "MobileDataService.getCandlesToDate",
                data=_json.dumps(payload), headers=headers)
            if not resp.ok:
                raise RuntimeError(f"playwright getCandles {symbol_id}: {resp.status}: {resp.text()[:200]}")
            data = resp.json()
            browser.close()
            return _decode_candles(data, symbol_id)


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


def format_setup(res, setup_type: str = "impulse") -> str:
    risk_usd = risk_mod.PROFILES[PROFILE]["risk_usd"]
    stop_dist = abs(res.entry - res.stop)
    qty = risk_usd / stop_dist if stop_dist > 0 else 0.0
    sb = res.signal_bar or res.impulse_bar or {}
    st = f"{dt.datetime.fromtimestamp(sb['time'], dt.timezone.utc):%H:%M}" if sb else "?"
    end = SESSION_END.strftime("%H:%M")
    type_labels = {
        "impulse": "ИМПУЛЬС+ОТКАТ",
        "gap_fade": "ГЭП-ФЕЙД",
        "opening_drive": "OPENING DRIVE",
    }
    label = type_labels.get(setup_type, setup_type.upper())
    qscore = getattr(res, 'quality_score', 0)
    # Quality-tier emoji: fire (>=80), lightning (>=65), bell (<65)
    if qscore >= 80:
        emoji = "🔥"
    elif qscore >= 65:
        emoji = "⚡"
    else:
        emoji = "🔔"
    header = f"{emoji} {label} {res.bias.upper()} {res.symbol} — класс {res.grade} Q={qscore} (сигнал {st} UTC)"
    footer = ""
    if setup_type == "gap_fade":
        footer = f"\nГэп-фейд: цель = закрытие предыдущего дня, стоп за экстремумом гэпа."
    elif setup_type == "opening_drive":
        footer = f"\nOpening drive: первые {CFG.get('opening_drive_minutes', 5)} мин. " \
                 f"Стоп за минимумом/максимумом драйва, тейк {res.rr:.1f}R."
    return (
        f"{header}\n"
        f"Вход {res.entry:.2f} | Стоп {res.stop:.2f} | Тейк {res.target:.2f} ({res.rr:.1f}R)\n"
        f"Размер ~{qty:.2f} шт (риск {risk_usd:.2f}$)\n"
        f"План выхода: вся позиция, стоп −1R, тейк +{res.rr:.1f}R, иначе закрыть до {end} UTC\n"
        f"{day_line()}"
        f"{footer}"
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
            parts = key.split(":", 2)
            if len(parts) == 3:
                date_str, setup_type, sym = parts
            else:
                date_str, sym = parts[0], parts[1]
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


def scan_watchlist(access, only_sym=None) -> list[dict]:
    """Returns list of {setup, setup_type} dicts."""
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
            print(f"{sym}: fetched {len(candles)} candles", file=sys.stderr)
        except Exception as e:
            print(f"{sym}: candle fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
            return None
        # Run all 3 setup scanners on the same candles in parallel
        results = []
        for stype, fn in (("impulse", scanner_mod.scan_setup),
                          ("gap_fade", scanner_mod.scan_gap_fade),
                          ("opening_drive", scanner_mod.scan_opening_drive)):
            result = fn(sym, today, candles, SESSION_START, CFG)
            if result is not None and result.tradable:
                # Quality filter (2026-08-24 calibration): reject low-quality setups
                if not quality_mod.passes_quality_filter(stype, result.quality_score):
                    print(f"{sym}: {stype} REJECTED quality={result.quality_score}",
                          file=sys.stderr)
                    continue
                results.append({"setup": result, "setup_type": stype})
                print(f"{sym}: {stype} TRADABLE grade={result.grade} bias={result.bias} Q={result.quality_score}",
                      file=sys.stderr)
            elif result is not None:
                reasons = '; '.join(result.no_go) or 'filtered'
                print(f"{sym}: {stype} no ({reasons})", file=sys.stderr)
        return results

    results = []
    if not tasks:
        print(f"{today} UTC: watchlist has no matching symbols", file=sys.stderr)
        return results
    with ThreadPoolExecutor(max_workers=min(12, len(tasks))) as ex:
        futures = [ex.submit(_one, t) for t in tasks]
        for fut in as_completed(futures):
            per_symbol = fut.result()
            if per_symbol:
                results.extend(per_symbol)
    return results


def symbol_cluster(sym: str) -> str:
    """Cluster name from manual_config (anti-correlation cap), '' if none."""
    for name, members in (CFG.get("clusters") or {}).items():
        if sym in members:
            return name
    return ""


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
    last_autocal_week = ""  # Sunday recalibration
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
        # --- refresh with error classification ---
        # Сетевые сбои (SSLEOF/ReadTimeout из РФ) — не считаем «смертью» токена,
        # иначе ложная тревога каждые 10 мин при блоировке сети.
        access = None
        try:
            access = refresh_access()
            refresh_failures = 0
        except Exception as e:
            msg = str(e)
            is_network = (
                isinstance(e, (requests.exceptions.SSLError,
                               requests.exceptions.ConnectionError,
                               requests.exceptions.ReadTimeout,
                               requests.exceptions.Timeout))
                or "SSLEOF" in msg or "Read timed out" in msg
                or "handshake" in msg.lower() or "Max retries" in msg
            )
            if is_network:
                print(f"{now:%H:%M:%S} UTC: refresh network fail: {e}",
                      file=sys.stderr)
                time.sleep(POLL_SECONDS)
                continue
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

                # Weekly auto-calibration: каждый понедельник 00:00-01:00 UTC
                # (после закрытия воскресной сессии). Пересчитываем пороги качества.
                if now.weekday() == 0:  # Monday UTC = after Sunday session
                    week_id = now.strftime("%Y-W%W")
                    if week_id != last_autocal_week:
                        last_autocal_week = week_id
                        try:
                            print(f"{now:%H:%M:%S} UTC: running weekly quality autocal...",
                                  file=sys.stderr)
                            from challenge.manual import quality_autocal
                            new_thresh = quality_autocal.recalibrate()
                            quality_autocal.save_and_reload(new_thresh)
                            # Report to Telegram
                            lines = ["Weekly Quality Auto-Calibration:"]
                            for stype in ("impulse", "gap_fade", "opening_drive"):
                                lines.append(f"  {stype}: threshold={new_thresh.get(stype, '?')}")
                            tg_send("\n".join(lines))
                        except Exception as ae:
                            print(f"{now:%H:%M:%S} UTC: autocal failed: {ae}",
                                  file=sys.stderr)
            time.sleep(POLL_SECONDS)
            continue

        # --- normal in-session scan ---
        try:
            sent = load_sent()
            today = now.date().isoformat()
            hits = scan_watchlist(access)
            for hit in hits:
                res = hit["setup"]
                setup_type = hit.get("setup_type", "impulse")
                key = f"{today}:{setup_type}:{res.symbol}"
                if sent.get(key):
                    continue
                # Anti-correlation cap: block same-day same-cluster signals.
                # Different setup types on the same symbol are allowed
                # (gap fade is an independent edge, not a duplicate).
                cluster = symbol_cluster(res.symbol)
                same_cluster = []
                if cluster:
                    for k in sent:
                        try:
                            parts = k.split(":", 2)
                            k_date = parts[0]
                            k_sym = parts[-1] if len(parts) >= 2 else ""
                        except ValueError:
                            continue
                        if k_date == today and k_sym != res.symbol \
                                and symbol_cluster(k_sym) == cluster:
                            same_cluster.append(k_sym)
                msg = format_setup(res, setup_type)
                if cluster and same_cluster:
                    msg += (f"\n⚠️ Кластер «{cluster}»: сегодня уже алертились "
                            f"{', '.join(sorted(set(same_cluster)))}. Кап: "
                            f"макс 1 позиция на кластер в день — входи только "
                            f"если уверен, что та сделка не открыта.")
                ok = tg_send(msg)
                if ok:
                    sb = res.signal_bar or res.impulse_bar or {}
                    sent[key] = {"sent_at": now.isoformat(), "grade": res.grade,
                                 "entry": res.entry, "stop": res.stop,
                                 "target": res.target, "bias": res.bias,
                                 "signal_time": sb.get("time") if sb else None,
                                 "rr": res.rr, "cluster": cluster,
                                 "setup_type": setup_type,
                                 "quality_score": getattr(res, 'quality_score', 0)}
                    save_sent(sent)
                    print(f"{now:%H:%M:%S} UTC: alert [{setup_type}] sent for {res.symbol}",
                          file=sys.stderr)
            # После скана — разрешение открытых сетапов.
            try:
                resolve_open_setups(access)
            except Exception as e2:
                print(f"{now:%H:%M:%S} UTC: resolve error: {e2}", file=sys.stderr)
        except Exception as e:
            print(f"{now:%H:%M:%S} UTC: scan error: {e}", file=sys.stderr)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if not acquire_single_instance():
        sys.exit(1)
    sys.exit(main())