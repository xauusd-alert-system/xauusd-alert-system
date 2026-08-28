import sqlite3

import pandas as pd

db_path = "data/market_data_mt5.sqlite"
conn = sqlite3.connect(db_path)

df = pd.read_sql_query("""
SELECT
    ticket,
    entry_time,
    close_time,
    bias,
    pnl
FROM executed_trades
WHERE symbol = 'BTCUSD' AND outcome IS NOT NULL
ORDER BY entry_time ASC
""", conn)
conn.close()

df["duration_min"] = (pd.to_numeric(df["close_time"]) - pd.to_numeric(df["entry_time"])) / 60.0

print("Все сделки:")
print(df.to_string(index=False))

bad = df[df["duration_min"] < 0]
print(f"\nОтрицательная длительность: {len(bad)} шт.")
if len(bad) > 0:
    print(bad.to_string(index=False))
