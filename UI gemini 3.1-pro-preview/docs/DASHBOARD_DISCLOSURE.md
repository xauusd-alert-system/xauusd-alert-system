# Dashboard data-source contract

Every API value rendered as current/live must include `available`, `source`,
`mode`, and `as_of_utc` (or equivalent response headers for SVG).

Rules:

- Missing MT5/account/news/trade data is `available: false`; it is never replaced
  with a demo balance, static correlation, sample headline, random candle chart,
  hypothetical PnL sequence, or hard-coded institutional metric.
- `/api/correlation` uses aligned real closed-candle returns from at least two assets.
- `/api/monte-carlo` uses persisted `executed_trades.pnl` and requires at least two
  closed trades.
- `/api/chart/{asset}` and `/api/institutional-metrics` require real closed candles.
- `/api/sentiment` remains unavailable until a real news adapter is configured.
- `/api/paper-status` exposes liveness/counts only and never outcome metrics.
- Web `closeall` is deliberately not wired to broker mutation. Emergency close is
  available only through the authenticated Telegram control bot.

The dashboard JavaScript must handle every unavailable response without calling
methods such as `.join()` or `.toFixed()` on missing values.
