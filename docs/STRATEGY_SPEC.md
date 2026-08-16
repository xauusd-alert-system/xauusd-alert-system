# Strategy specification — xauusd-system-v3-signalbar-2026-08-16

Machine source: `config/strategy_spec.yaml`. Runtime identity includes its SHA-256,
effective config hash, model hash and feature-snapshot hash.

## Lifecycle

`watch → armed → confirmed` or `watch/armed → rejected|expired`; `no_trade` is terminal.
A zone/setup is not an order. Systematic confirmation means all recorded predicates
passed. Human-confirmed mode additionally requires an identified confirmer.

## SignalSpec

`contracts.signal_spec.SignalSpec` stores setup/context timeframes, zone, expiry,
confirmation predicates, arbitrary target legs and close ratios, stop, hashes,
source message and publication time. Telegram is a delivery channel, never the clock
or outcome source. `publish_latency_seconds` is audited separately.

## Geometry and risk

Grid step is causal signal-bar ATR/fixed step with clamps. Targets are arbitrary legs;
current generated strategy uses configured TP1–TP3 ratios. Position size remains
stop-based with account/cluster caps. Broker leverage is not a strategy risk input.
Overnight holding is disabled in the specification.

## News and execution

Live calendar failure is fail-closed. Historical research only applies news filtering
when a dated CSV is configured. Deployment mode is explicit; current mode is
`research`, retraining is frozen, and execution allowlist is deny-all.

## Evidence

Telegram tags and channel reports are not performance evidence. Promotion requires a
commit/config/DB snapshot, purged WF, calibration, DSR/PBO/CSCV, cost stress and a
frozen live-forward read.
