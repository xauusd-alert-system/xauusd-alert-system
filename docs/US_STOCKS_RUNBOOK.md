# US Stocks VWAP Pullback Scanner — Operations Runbook

**Version:** 1.0  
**Profile:** `us_stocks_challenge` (Signal-Only)  
**Target:** HashHedge $1,000 Challenge ($80 Target, -$50 Daily Limit)

---

## 1. Quick Start & Execution Mode

The scanner operates in **strict signal-only mode**. Automated order placement is disabled at the runtime and code level (`execution.mode: disabled`, `execution.DisabledExecutor`). All trades must be executed manually in the browser terminal.

### Launching the Bot
```bash
export PROFILE=us_stocks_challenge
python -m usstocks.bot
```

### Running Offline Replay
```bash
export PROFILE=replay
python -m usstocks.replay --symbol-csv AMD=data/replay/AMD.csv --benchmark-csv QQQ=data/replay/QQQ.csv --watchlist AMD --is-tech AMD
```

---

## 2. Daily Operating Cycle

| Time (America/New_York) | Activity | Action / Command |
|---|---|---|
| **09:00 - 09:25** | Premarket analysis | Bot scores universe, posts Top-3 watchlist to Telegram |
| **09:30** | Regular session open | VWAP resets to 09:30 NY. Premarket bars ignored |
| **09:30 - 09:45** | Opening range formation | First three 5m candles establish OR15 High/Low/Mid |
| **09:45 - 15:35** | Live signal scanning | Evaluates watchlist every 60s against 14 strategy criteria |
| **15:35 (Close - 25m)** | Session close guard | `SESSION_CLOSE_GUARD` blocks new trade setups |
| **16:00 (or 13:00 on Early Close)** | Market close | Daily stats summarized, EOD outcomes finalized |

---

## 3. Telegram Control Commands

All mutations requires **explicit inline confirmation (✅/❌)** with nonce and 300s TTL replay protection.

| Command | Usage | Description |
|---|---|---|
| `/us_status` | `/us_status` | Current session date, realized/unrealized P&L, trades count, active symbol |
| `/us_watchlist` | `/us_watchlist` | Top-3 ranked symbols with RVOL and gap metrics |
| `/us_signals on\|off` | `/us_signals off` | Pause or resume scanner alerts |
| `/us_win <amount>` | `/us_win 12.50` | Record profitable trade, reset consecutive losses |
| `/us_loss <amount>` | `/us_loss 8.00` | Record losing trade, increment consecutive losses |
| `/us_flat` | `/us_flat` | Clear active symbol without P&L change |
| `/us_stop` | `/us_stop` | Emergency operator stop-day (blocks all new signals) |
| `/us_resume` | `/us_resume` | Resume trading after manual stop |
| `/us_export [YYYY-MM-DD]` | `/us_export` | Export session signals and outcomes to atomic CSV |

---

## 4. Health Check & Observability

- **Health Endpoint**: `http://localhost:8000/api/health`
  Returns JSON status, uptime, realized P&L, and scanner metrics.
- **Metrics Endpoint**: `http://localhost:8000/api/metrics`
  Returns Prometheus-compatible metrics for scan duration and total scans.
- **Latency Alerts**: Scans taking >2.0s log warnings automatically.
- **Graceful Shutdown**: On `SIGINT` or `SIGTERM`, active database transactions are flushed, connections closed cleanly, and process exits with code 0.

---

## 5. Troubleshooting

- **UTEX Token Refresh Failure**:
  If refresh token expires, alerts scream after 5 consecutive failures. Update `data/challenge_tokens.json` with fresh tokens from browser localStorage.
- **Replay Attack / Nonce Error**:
  Confirmation buttons can only be clicked once. If clicked again, "Replay guard" prevents double recording of P&L.
- **Early Close Days**:
  Half days (e.g. Black Friday, Christmas Eve) close at 13:00 NY; new entries are blocked after 12:35 NY.
