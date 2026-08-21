# US Stocks Headliners: manual analytical workflow

This package implements the user-approved **analysis-only** workflow for the Hash Hedge US Stocks Headliners challenge. It is a decision-support and recordkeeping tool, not an execution system. The package has no terminal URL, browser profile, trading credential, order-routing client or code path that can submit, amend or close an order.

> A candidate returned by the scanner is **not** a trading instruction. The operator alone reviews the chart, the terminal countdown, the news calendar, buying power, displayed fees, position size, stop and target before placing any order manually in the Hash Hedge terminal.

## What the package does

| Component | Permitted role | Key safeguards |
|---|---|---|
| `scanner.py` | Causal evaluation of supplied local candles for the trend–impulse–pullback hypothesis | `as_of_ts` excludes future candles; local session is explicit; unknown calendar is NO-GO. |
| `risk.py` | Advisory whole-share sizing, fee estimate and local day-state checks | Includes $1 minimum estimated fee per order; one-open-position and 45-minute-to-close gates; never sends an order. |
| `outcomes.py` | Theoretical plan outcome accounting | Models 50% at 1R, breakeven on remainder and 2R final target; it is separate from actual fills. |
| `journal.py` | Append-only manual trade journal and weekly metrics | Records actual human-entered facts, discipline and violations. |
| `run.py` | Local command-line interface | Reads only local JSON candles and local manual records. |

## Daily operating protocol

The operator selects one to three stock symbols, verifies the terminal's live market countdown, verifies the economic-calendar red zone and enters the observed start-of-day **Balance**. The system defaults to profile **B**. Profile A requires an explicit confirmation and a stage-profit buffer; C/B/A de-risking and pause gates are evaluated locally only.

The scanner accepts only local data from `data/manual/candles/<SYMBOL>.json` or a manually supplied JSON path. A candle item must contain `time` (Unix seconds), `open`, `high`, `low`, `close` and optional `volume`. No fetcher, browser or account token is included in this branch.

```bash
python3 -m challenge.manual.run risk calc --price 100 --stop 99 --profile B --stage 1 --balance 1000 --buying-power 5000
python3 -m challenge.manual.run day start --stage 1 --profile B --balance 1000 --stage-start-balance 1000
python3 -m challenge.manual.run scan --symbol EXAMPLE --date 2026-08-21 --candles /absolute/path/local_candles.json
```

The expected output is an advisory or a **NO-GO**. It does not and cannot interact with Hash Hedge's order form.

## Schedule reconciliation

The user specification configures an NYSE-oriented local window of **18:30–00:55 UTC+4** and manual flattening by **00:50 UTC+4**. The general published Hash Hedge US Stocks guide currently states a different general terminal deadline: new positions block at 23:55 UTC+4 and all open positions close at 23:59 UTC+4. The live terminal countdown is therefore the controlling operational check on each day; do not rely on a static time setting alone. [Official US Stocks guide](https://hashhedge.gitbook.io/hashhedge-user-guide/trading-terminal/stocks-terminal.md)

## Verification

Run the regression suite before changing the workflow:

```bash
python3 -m unittest challenge.manual.test_manual -v
```

The suite covers the no-execution boundary, whole-share fee-aware risk sizing, profile gates, cross-midnight session conversion, causal `as_of` behavior and the partial exit model.
