import sqlite3
import pandas as pd

db_path = "data/market_data_mt5.sqlite"
conn = sqlite3.connect(db_path)

query = """
SELECT
    ticket,
    datetime(entry_time, 'unixepoch') AS entry_utc,
    datetime(close_time, 'unixepoch') AS close_utc,
    bias,
    entry_price,
    close_price,
    pnl,
    outcome
FROM executed_trades
WHERE symbol = 'BTCUSD' AND outcome IS NOT NULL
ORDER BY close_time DESC;
"""

df = pd.read_sql_query(query, conn)
conn.close()

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 20)
pd.set_option("display.max_rows", 100)

print(df.to_string(index=False))