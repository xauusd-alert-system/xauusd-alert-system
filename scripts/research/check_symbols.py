import sqlite3

con = sqlite3.connect("data/market_data_mt5.sqlite")

tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("TABLES:", tables)

for t in tables:
    try:
        cols = [c[1] for c in con.execute(f'PRAGMA table_info("{t}")')]
        if "symbol" in cols:
            syms = [r[0] for r in con.execute(f'SELECT DISTINCT symbol FROM "{t}"')]
            print(f"{t}: {syms}")
        else:
            print(f"{t}: (no symbol column) cols={cols}")
    except Exception as e:
        print(t, "ERROR", e)

con.close()
