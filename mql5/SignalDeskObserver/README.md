# SignalDeskObserver — read-only MT5 observer EA

**Status: Wave 1 of the MQL5 observer plan (`docs/MQL5_OBSERVER_PLAN.md`).**

This EA is a **narrow observer/telemetry agent**. It does **not** open, close or
modify trades: there is no `OrderSend`, no `CTrade`, no `OrderSend`-equivalent
anywhere in this directory (verified by the acceptance checklist below). The
Python sender `execution/mt5_trader.py` remains the only order sender; this EA
only records what the trade server actually did and delivers those facts to the
server-side ledger (`POST /api/ledger/ingest`).

## Files

| File | Responsibility | Forbidden |
|---|---|---|
| `ObserverEA.mq5` | `OnInit` / `OnTradeTransaction` / `OnTimer` / `OnDeinit`, heartbeat, outbox flush | Calculating alpha, opening/modifying positions |
| `EventSerializer.mqh` | Builds deterministic `ExecutionEvent` v1 JSON facts from terminal history APIs | Reading secrets from chart inputs/logs |
| `DiskOutbox.mqh` | Append-only JSONL outbox + ack watermark + bounded rotation | Deleting unacknowledged events |
| `SymbolResolver.mqh` | Canonical `XAUUSD` ↔ broker symbol map (fail-closed) | Guessing a mapping on ambiguity |
| `HistoryReconciler.mqh` | Restart scan of `HistorySelect` for missing deal/order facts | Fabricating a fill |
| `JsonWriter.mqh` | ASCII-safe JSON writer (MQL5 has no built-in JSON) | — |

## Install

1. Copy the whole `mql5/SignalDeskObserver/` folder to
   `%APPDATA%\MetaQuotes\Terminal\<INSTANCE>\MQL5\Experts\SignalDeskObserver\`.
2. In MetaEditor, right-click `ObserverEA.mq5` → **Compile** (F7). Zero warnings
   expected.
3. In the MT5 terminal: **Tools → Options → Expert Advisors → WebRequest** —
   add `http://127.0.0.1` to the allow-list (loopback signing proxy only; direct
   remote URLs are rejected by the EA).
4. Attach the EA to **any chart on a DEMO account**. On a real account
   `OnInit` returns `INIT_FAILED` and the EA does not run.

### Inputs

| Input | Default | Meaning |
|---|---|---|
| `InpBrokerSymbolMap` | `XAUUSD=GOLD,XAGUSD=SILVER,BTCUSD=BITCOIN,EURUSD=EURUSD,GBPUSD=GBPUSD` | canonical=broker pairs; unknown symbols are skipped, never guessed |
| `InpMagicFilter` | `777111` | only observe orders/deals/positions with this magic; `0` = all |
| `InpProxyUrl` | `http://127.0.0.1:8787/v1/observer/ingest` | must be exactly the loopback proxy URL (`http://127.0.0.1:<port>/v1/observer/ingest`); direct remote URLs are rejected |
| `InpProxyToken` | *(empty)* | local proxy bearer token (`OBSERVER_PROXY_TOKEN`); the observer NEVER knows the remote `LEDGER_INGEST_TOKEN` or `LEDGER_INGEST_SECRET` |
| `InpFlushSeconds` | `15` | outbox flush interval (`WebRequest` only from `OnTimer`) |
| `InpHeartbeatSeconds` | `600` | `health_heartbeat` fact interval |
| `InpReconcileDays` | `30` | history scan depth for restart reconciliation |
| `InpReconcileHours` | `24` | reconciliation interval |
| `InpOutboxMaxBytes` | `1048576` | rotate outbox only when fully acked |
| `InpObserveRequests` | `false` | also record `request_result` facts (Python sender already does) |

## Wire contract

Each outbox line is `<event_id>\t<json>` where the JSON is an
`ExecutionEvent` v1 (see `contracts/execution_contracts.py`). The
`event_id` is the deterministic canonical id string

```
mt5_observer|<account_fingerprint>|<kind>|<ticket>
```

e.g. `mt5_observer|demo:12345678|deal|1701234567`. The Python side emits the
sha256 hex of the same canonical string; the server treats `event_id` as an
opaque primary key, so both forms dedupe identically and retries are safe.

Facts: `deal_added` (passive or `history_reconciled`), `order_history_added`,
`position_modified`, `request_result` (opt-in), `execution_reconciled`
(summary), `health_heartbeat`.

Correlation: if the Python sender puts an 8-hex intent short id at the end of
the order comment (`f"{asset_key} ML Scalp {intent_id[:8]}"`), the observer
extracts it into `payload.intent_id_short`, which the server joins to the full
`ledger_intents` row for the Lifecycle Trace view.

## Acceptance checklist (demo account only)

- [ ] Compiled source contains no `OrderSend` / `OrderModify` / `OrderDelete` /
      `PositionClose` / `CTrade` symbol
      (`grep -nE "OrderSend|OrderModify|OrderDelete|PositionClose|CTrade" mql5/*.mq5 mql5/*.mqh`
      → empty).
- [ ] EA refuses to start on a real account (`OnInit` → `INIT_FAILED`, log line
      `REFUSING to run on a REAL account`).
- [ ] Every selected demo trade (manual or Python-sent) produces a
      `deal_added` outbox line within seconds.
- [ ] Restarting the terminal re-attaches the EA; reconciliation emits exactly
      the missing facts, no duplicates (event ids are deterministic).
- [ ] No network call happens inside `OnTradeTransaction` (outbox is file-only;
      `WebRequest` appears only in `OnTimer`/`OnDeinit`).
- [ ] With the network down, events queue in the outbox; after recovery they are
      delivered and the ack watermark advances; nothing is deleted.
- [ ] Events with an unmapped broker symbol are skipped and counted
      (`skipped_unmapped` in the deinit log), never emitted with a fake asset.
- [ ] All timestamps are UTC epoch seconds/ms; `received_at_utc_ms` is UTC ms.
- [ ] After the server returns 2xx, the acked leading lines are eventually
      rotated when the file exceeds `InpOutboxMaxBytes`; unacked lines persist.

## Security notes

- HTTPS + bearer token only; no secrets in chart comments or logs (the token is
  an EA input and appears in the profile file — protect the `.set`/profile files
  on the Windows host).
- Strict signed ingress: the server requires BOTH the remote bearer token and a
  valid HMAC-SHA256 `X-Ledger-Signature`; bearer-only POSTs are rejected (401/503).
- This directory is reference code reviewed against the contract tests in
  `mql5/tests/test_wire_contract.py`; compiling and running it on a live Windows
  host requires the operator acceptance checklist above.
