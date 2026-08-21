# -*- coding: utf-8 -*-
import io, sys, time, json, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\botbo\Desktop\xauusd-alert-system")

import challenge.backtest.utext_data as ud

WATCHLIST = ["AAPL", "NVDA", "TSLA", "SPY", "GLD", "COIN", "AMD", "MU", "MRVL", "PLTR",
             "SHOP", "SMCI", "RDDT", "RKLB", "ABNB", "BA", "CAT", "KO", "MRK", "CSCO"]

BASE = r"C:\Users\botbo\Desktop\xauusd-alert-system\data\backtest"

def main():
    # symbol map from previous browser dump
    with open(r"C:\Users\botbo\AppData\Local\Temp\opencode\symbol_map4.json", encoding="utf-8") as f:
        by_icon = json.load(f)
    rev = {}
    for k, v in by_icon.items():
        rev[v] = int(k)
    ids = {t: rev.get(t) for t in WATCHLIST}
    missing = [t for t, i in ids.items() if i is None]
    if missing:
        print("NO ID:", missing)
    os.makedirs(os.path.join(BASE, "candles"), exist_ok=True)
    with open(os.path.join(BASE, "symbols.json"), "w", encoding="utf-8") as f:
        json.dump(ids, f, indent=1)

    rt = ud.load_refresh_token()
    access = ud.refresh_access(rt)
    now = int(time.time())
    log = open(os.path.join(BASE, "download.log"), "w", encoding="utf-8")
    for t in WATCHLIST:
        sid = ids[t]
        if sid is None:
            continue
        # paginate back in chunks of 3000 (API cap) until we cover ~21 calendar days
        all_candles = []
        to = now
        seen = set()
        for _ in range(20):
            chunk = ud.get_candles(access, sid, "Min1", 3000, to=to)
            new = [c for c in chunk if c["time"] not in seen]
            all_candles = new + all_candles
            for c in new:
                seen.add(c["time"])
            if not new or (now - new[0]["time"]) / 86400 > 21:
                break
            to = new[0]["time"] - 1
            time.sleep(0.3)
        with open(os.path.join(BASE, "candles", t + ".json"), "w", encoding="utf-8") as f:
            json.dump(all_candles, f)
        span_days = (all_candles[-1]["time"] - all_candles[0]["time"]) / 86400 if all_candles else 0
        log.write(f"{t} (id {sid}): {len(all_candles)} candles, span {span_days:.1f} days\n")
        log.flush()
    log.close()
    print("done")

if __name__ == "__main__":
    main()