# Paper-Observation Log — US Stocks Headliners (ТЗ §13/§14)

Template for daily manual entries during the 10–20 session paper window.
Never edit past rows — append new rows at the bottom.

Columns:
- **Date (NY)** — YYYY-MM-DD
- **Trades** — number of signals taken (after `/us_win/loss`)
- **Win %** — wins / trades
- **Avg R** — mean R-multiple of taken trades
- **Sum R** — cumulative R for the day
- **PnL $** — sum of realised PnL
- **Max Adverse R** — worst intra-trade drawdown in R (estimate)
- **False Setups** — signals generated but not taken (rejected/pending)
- **Signals Off?** — Y if `/us_signals off` used
- **Notes** — any manual overrides, abnormal conditions, operator fatigue

---

| Date (NY) | Trades | Win % | Avg R | Sum R | PnL $ | Max Adv R | False Setups | Signals Off | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 2026-08-26 | 0 | — | — | 0.00 | 0.00 | 0.00 | 0 | N | First paper session — no signals; universe filtered to 3 |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |
| | | | | | | | | | |

---

## Export / Analysis

After each session run `usstocks.paper` batch (or the single-session replay):
```powershell
$env:PROFILE="replay"
venv\Scripts\python.exe -m usstocks.replay --symbol-csv AMD=data/replay/AMD_2026-08-26.csv --benchmark-csv QQQ=data/replay/QQQ_2026-08-26.csv --watchlist AMD --is-tech AMD
# or batch for multiple days:
venv\Scripts\python.exe -m usstocks.paper --csv-root data/replay --dates 2026-08-26,2026-08-27 --universe AMD,NVDA,TSLA
```

Export produces `data/usstocks_export/us_signals_YYYY-MM-DD.csv` (per day) and `data/usstocks_export/paper_summary.csv` (aggregate across all paper days).

**Acceptance criteria for live launch (ТЗ §14):**
- ≥ 10 paper sessions logged
- Win rate ≥ 40% (minimum threshold, not a guarantee)
- Avg R ≥ 0.5
- Max consecutive losses ≤ 2
- Max adverse R per trade ≤ 1.5 (no single trade kills daily budget)
- No parameter changes without documented reason

---

## Auto-computed Summary (from paper_summary.csv)

After the batch, run:
```python
import pandas as pd
df = pd.read_csv("data/usstocks_export/paper_summary.csv")
print(f"Total trades: {df.trades.sum()}")
print(f"Win rate: {df[df.trades>0].win_rate.mean():.1f}%")
print(f"Avg R: {df[df.trades>0].avg_r.mean():.3f}")
print(f"Max 1-day PnL: {df.total_pnl.max():.2f}")
print(f"Max adverse R: {df.max_adverse_r.max():.2f}")
```

---

**Reminder:** If any parameter is tweaked, document the reason in Notes and the date of change. The goal is to avoid curve-fitting; the baseline strategy must survive unmodified for 10+ sessions.