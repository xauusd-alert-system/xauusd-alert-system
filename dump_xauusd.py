import sqlite3
import pandas as pd

DB_PATH = "data/market_data_mt5.sqlite"
SYMBOL = "XAUUSD"

conn = sqlite3.connect(DB_PATH)
df = pd.read_sql_query(
    """
    SELECT
        ticket,
        entry_time,
        close_time,
        bias,
        entry_price,
        close_price,
        pnl,
        outcome
    FROM executed_trades
    WHERE symbol = ? AND outcome IS NOT NULL
    ORDER BY close_time DESC
    """,
    conn,
    params=(SYMBOL,),
)
conn.close()

df["duration_min"] = (pd.to_numeric(df["close_time"]) - pd.to_numeric(df["entry_time"])) / 60.0
bad = df[df["duration_min"] < 0]
valid = df[df["duration_min"] >= 0].copy()

print(f"Всего закрытых записей: {len(df)}")
print(f"Из них дефектных (close_time < entry_time): {len(bad)}")
if len(bad) > 0:
    print("\nДефектные записи:")
    print(bad[["ticket", "entry_time", "close_time", "pnl"]].to_string(index=False))

print(f"\nВалидные сделки ({len(valid)}):")
print(valid[["ticket", "entry_time", "close_time", "bias", "entry_price", "close_price", "pnl", "duration_min"]].to_string(index=False))

if len(valid) > 0:
    wins = valid[valid["pnl"] > 0]
    losses = valid[valid["pnl"] <= 0]
    gp = float(wins["pnl"].sum()) if len(wins) else 0.0
    gl = float(-losses["pnl"].sum()) if len(losses) else 0.0
    pf = (gp / gl) if gl > 0 else 999.0
    wr = 100.0 * len(wins) / len(valid)
    print(f"\nИтог по валидным сделкам:")
    print(f"  n = {len(valid)}")
    print(f"  Total PnL = {valid['pnl'].sum():.2f}")
    print(f"  WR = {wr:.1f}%")
    print(f"  PF = {pf:.2f}")
    print(f"  Средняя длительность = {valid['duration_min'].mean():.1f} мин")
    print(f"  Long/Short = {(valid['bias']=='long').sum()} / {(valid['bias']=='short').sum()}")
else:
    print("\nВалидных закрытых сделок нет.")