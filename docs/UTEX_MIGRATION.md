# UTEX Migration & Provider Architecture

## Overview

The US stocks signal subsystem relies on UTEX mobile gRPC REST gateway endpoints for market data (1m and 5m closed candles).

Previously, data fetching code was duplicated across `challenge/manual/alerter.py` and `usstocks/data/utex_provider.py` with hardcoded Windows paths (`C:\Users\botbo\...`).

As part of P0-1 and architectural consolidation:
1. All UTEX data fetching has been centralized into `usstocks.data.utex_provider`.
2. `challenge/manual/alerter.py` now delegates all candle fetching and auth token refreshes to `usstocks.data.utex_provider`.
3. Hardcoded paths have been eliminated in favor of repository-relative path resolution.

## Key Components

### 1. `UtexClient` (`usstocks/data/utex_provider.py`)
- **`refresh_access(token_file)`**: Refreshes JWT access tokens using the refresh token stored in `data/challenge_tokens.json`.
- **`fetch_candle_dicts(access, symbol_id, candles_count, to_ts)`**: Retrieves raw candle dictionaries normalized to standard units.
- **`fetch_bars(access, symbol_id, candles_count, to_ts)`**: Returns timezone-aware domain `Bar` objects with UTC/NY timestamps.
- **`decode_candles(data)`**: Normalizes UTEX integer 1e8 scaled prices to standard floats.

### 2. Dual-Layer Transport (Resilience)
- **Primary**: `requests.post` with short timeouts (30-60s) for low overhead and fast execution.
- **Fallback**: Headless Chromium via Playwright when requests encounters TLS handshakes or SSLEOF network errors (common in restricted regional networks).

### 3. Integration & Compatibility
- `challenge/manual/alerter.py` exports the same top-level signatures (`refresh_access`, `fetch_candles`, `_decode_candles`) ensuring 100% backward compatibility with legacy tooling and tests.
- Single source of truth prevents drift in candle decoding formulas or header metadata.
