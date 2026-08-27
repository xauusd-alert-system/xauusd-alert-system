import sqlite3, datetime
cut = int(datetime.datetime(2026, 8, 8, tzinfo=datetime.timezone.utc).timestamp())
con = sqlite3.connect("data/research_prelock.sqlite")
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name like 'ohlcv_%'")]
for t in tables:
    con.execute(f"delete from {t} where timestamp_utc >= ?", (cut,))
con.commit()
print("truncated:", tables)
