import MetaTrader5 as mt5
mt5.initialize()
for s in ['GOLD','SILVER','BITCOIN','EURUSD','GBPUSD']:
    rates = mt5.copy_rates_from_pos(s, mt5.TIMEFRAME_M5, 1, 1)
    tick = mt5.symbol_info_tick(s)
    bar_time = rates[0]['time'] if rates is not None else None
    print(f'{s}: bar_time={bar_time}, tick_time={tick.time if tick else None}, bid={tick.bid if tick else None}')
