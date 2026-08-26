"""Autonomous correlation analysis — no imports from alerter.py."""
import sys, os, json, time, datetime as dt

ROOT = r"C:\Users\botbo\Desktop\xauusd-alert-system"
sys.path.insert(0, ROOT)

# Load token and symbols manually (no alerter import)
TOKEN_FILE = os.path.join(ROOT, "data", "challenge_tokens.json")
SYMBOLS_FILE = os.path.join(ROOT, "data", "backtest", "symbols.json")
CFG_FILE = os.path.join(ROOT, "challenge", "manual", "manual_config.yaml")

import requests, yaml

CFG = yaml.safe_load(open(CFG_FILE, encoding="utf-8"))
SYMBOLS = json.load(open(SYMBOLS_FILE, encoding="utf-8"))
CLUSTER_MEMBERS = CFG.get("clusters", {}).get("crypto_beta", [])

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

def pearson(a, b):
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[:n], b[:n]
    ma = sum(a) / n
    mb = sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / n
    sa = (sum((x - ma) ** 2 for x in a) / n) ** 0.5
    sb = (sum((x - mb) ** 2 for x in b) / n) ** 0.5
    if sa == 0 or sb == 0:
        return 0.0
    return cov / (sa * sb)

def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-08-24"
    date = dt.date.fromisoformat(date_str)
    sess_start = int(dt.datetime.combine(date, dt.time(13, 30), tzinfo=dt.timezone.utc).timestamp())
    sess_end = int(dt.datetime.combine(date, dt.time(19, 55), tzinfo=dt.timezone.utc).timestamp())

    print(f"Refreshing token...")
    access = refresh_access()
    print(f"Token OK\n")

    candle_data = {}
    for sym, sid in SYMBOLS.items():
        try:
            candles = fetch_candles(access, sid, 1500)
            day = [c for c in candles if sess_start <= c["time"] <= sess_end]
            if day:
                candle_data[sym] = day
                print(f"  {sym:8s}: {len(day)} candles")
        except Exception as e:
            print(f"  {sym:8s}: FAILED ({e})")

    print(f"\nTotal: {len(candle_data)} symbols with data\n")

    # Opening drive analysis
    drive_n = int(CFG.get("opening_drive_minutes", 5))
    print("=" * 80)
    print(f"OPENING DRIVE ANALYSIS: {date_str} (first {drive_n} min)")
    print("=" * 80)
    print(f"\n{'Symbol':8s} {'Cluster':8s} {'Bias':6s} {'Body%':6s} {'SessRet%':9s} {'MaxAdv%':8s}")
    print("-" * 60)

    results = []
    for sym, candles in sorted(candle_data.items()):
        if len(candles) < drive_n + 5:
            continue
        drive = candles[:drive_n]
        dopen = drive[0]["open"]
        dclose = drive[-1]["close"]
        dhigh = max(b["high"] for b in drive)
        dlow = min(b["low"] for b in drive)
        drange = dhigh - dlow
        dbody = abs(dclose - dopen)
        if drange <= 0:
            continue
        bias = "short" if dclose < dopen else "long"
        sess_ret = (candles[-1]["close"] - dopen) / dopen * 100
        if bias == "short":
            max_adv = max(b["high"] for b in candles[drive_n:]) - dclose
        else:
            max_adv = dclose - min(b["low"] for b in candles[drive_n:])
        max_adv_pct = max_adv / dclose * 100

        is_cl = sym in CLUSTER_MEMBERS
        results.append({"sym": sym, "cluster": is_cl, "bias": bias,
                         "body_ratio": dbody / drange, "sess_ret": sess_ret,
                         "max_adv": max_adv_pct})
        cl = "CRYPTO" if is_cl else ""
        print(f"{sym:8s} {cl:8s} {bias:6s} {dbody/drange:5.0%} {sess_ret:+8.2f}% {max_adv_pct:7.2f}%")

    # Direction alignment
    cl_results = [r for r in results if r["cluster"]]
    if cl_results:
        shorts = sum(1 for r in cl_results if r["bias"] == "short")
        print(f"\nCRYPTO_BETA DIRECTION: {shorts}/{len(cl_results)} short "
              f"({100*shorts/len(cl_results):.0f}%)")

    # Pairwise correlation
    print(f"\n{'='*80}")
    print(f"PAIRWISE CLOSE-PRICE CORRELATION (normalized to open)")
    print("=" * 80)

    norm = {}
    for sym, candles in candle_data.items():
        op = candles[0]["open"]
        if op > 0:
            norm[sym] = [(c["close"] - op) / op * 100 for c in candles]

    cl_syms = [s for s in CLUSTER_MEMBERS if s in norm]
    nc_syms = [s for s in norm if s not in CLUSTER_MEMBERS]

    if len(cl_syms) >= 2:
        pairs = []
        for i in range(len(cl_syms)):
            for j in range(i + 1, len(cl_syms)):
                c = pearson(norm[cl_syms[i]], norm[cl_syms[j]])
                pairs.append((cl_syms[i], cl_syms[j], c))
        pairs.sort(key=lambda x: -x[2])
        avg_c = sum(p[2] for p in pairs) / len(pairs)
        print(f"\nCluster intra-correlation ({len(cl_syms)} syms, {len(pairs)} pairs):")
        print(f"  Average: {avg_c:.3f}")
        print(f"\n  Top 10 pairs:")
        for s1, s2, c in pairs[:10]:
            print(f"    {s1:8s} - {s2:8s}: {c:.3f}")
        print(f"\n  Bottom 5 pairs:")
        for s1, s2, c in pairs[-5:]:
            print(f"    {s1:8s} - {s2:8s}: {c:.3f}")

    if cl_syms and nc_syms:
        cross = []
        for cs in cl_syms:
            for nc in nc_syms:
                c = pearson(norm[cs], norm[nc])
                cross.append((cs, nc, c))
        avg_cross = sum(c[2] for c in cross) / len(cross)
        print(f"\nCluster vs Non-cluster ({len(cross)} pairs):")
        print(f"  Average cross-correlation: {avg_cross:.3f}")

    # Stop-out clustering
    print(f"\n{'='*80}")
    print(f"STOP-OUT CLUSTERING (from setup_outcomes.csv)")
    print("=" * 80)
    outcomes_path = os.path.join(ROOT, "data", "manual", "setup_outcomes.csv")
    if os.path.exists(outcomes_path):
        import csv
        with open(outcomes_path) as f:
            rows = list(csv.DictReader(f))
        day_rows = [r for r in rows if r.get("date") == date_str]
        stops = [r for r in day_rows if r.get("outcome") == "stop"]
        wins = [r for r in day_rows if r.get("outcome") == "target"]
        cl_stops = [r for r in stops if r["symbol"] in CLUSTER_MEMBERS]
        nc_stops = [r for r in stops if r["symbol"] not in CLUSTER_MEMBERS]

        print(f"\n  Total: {len(day_rows)} trades | Stops: {len(stops)} | Targets: {len(wins)}")
        print(f"  Cluster stops: {len(cl_stops)}/{len(stops)} ({100*len(cl_stops)/max(1,len(stops)):.0f}%)")
        print(f"  Non-cluster stops: {len(nc_stops)}/{len(stops)}")

        # Resolution time
        print(f"\n  All trades:")
        for r in sorted(day_rows, key=lambda x: x["symbol"]):
            m = "WIN " if float(r["r"]) > 0 else "LOSE"
            cl = " [CRYPTO]" if r["symbol"] in CLUSTER_MEMBERS else ""
            print(f"    {m} {r['symbol']:8s} bias={r['bias']:5s} min={float(r['minutes']):5.0f}{cl}")


if __name__ == "__main__":
    main()
