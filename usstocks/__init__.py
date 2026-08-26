"""usstocks — signal-only US Stocks Headliners challenge subsystem.

Profile `us_stocks_challenge`: scanner -> strategy (VWAP Pullback) ->
risk engine -> Telegram signals -> journal. Auto-trading is technically
impossible here (see docs/MIGRATION_PLAN.md, Stage B):

- every executable entry point refuses to start (usstocks.guards);
- the only Executor in the graph is execution.DisabledExecutor, which
  raises on any submit attempt;
- manual confirmation via Telegram inline buttons is the sole trigger
  for recording outcomes (never orders).
"""
