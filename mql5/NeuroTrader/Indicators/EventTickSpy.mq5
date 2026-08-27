//+------------------------------------------------------------------+
//| EventTickSpy.mq5 - event-model data collection spy (T-13)        |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-13; MQL5 book 6.4.1 +       |
//| 6.4.38).                                                         |
//|                                                                  |
//| WHY A SPY (book 6.4.1): an EA running on an M5 chart receives    |
//| OnTick only while the M5 chart itself receives ticks - in the    |
//| tester and in quiet sessions this starves the event loop. The    |
//| canonical fix is a tiny "spy" indicator dropped on a FASTER      |
//| chart: indicators on open charts are always pumped by the        |
//| terminal, so the spy becomes the reliable event source and the  |
//| data collector reacts to indicator events instead of polling.    |
//|                                                                  |
//| This spy demonstrates the three event channels and records their|
//| arrivals to a CSV in MQL5\Files so the event model can be       |
//| audited:                                                         |
//|   OnCalculate  - every tick delivered to the chart it runs on    |
//|   OnTimer      - fixed cadence independent of tick flow          |
//|   OnBookEvent  - DOM updates (book 6.4.38, depth-of-market)      |
//|                                                                  |
//| Usage: attach to an M1 chart of the traded symbol, run the EA on |
//| M5; the CSV columns are:                                         |
//|   ts_msc, event, symbol, bid, ask, reason                        |
//+------------------------------------------------------------------+
#property copyright "xauusd-alert-system / books integration"
#property version   "1.00"
#property indicator_chart_window
#property indicator_plots 0

input int    InpTimerSeconds   = 1;      // OnTimer cadence
input bool   InpLogBookEvent   = true;   // record OnBookEvent rows too
input string InpCsvFile        = "event_tick_spy.csv";
input int    InpMaxRows        = 100000; // safety cap per attach

int    g_rows = 0;
int    g_file = INVALID_HANDLE;
string g_symbol;

//+------------------------------------------------------------------+
int OnInit()
{
   g_symbol = _Symbol;
   EventSetTimer(MathMax(1, InpTimerSeconds));
   if(InpLogBookEvent)
      MarketBookAdd(g_symbol);            // subscribe to DOM updates
   g_file = FileOpen(InpCsvFile, FILE_WRITE | FILE_READ | FILE_CSV | FILE_ANSI,
                     ';');
   if(g_file != INVALID_HANDLE)
   {
      FileSeek(g_file, 0, SEEK_END);
      if(FileTell(g_file) == 0)
         FileWrite(g_file, "ts_msc", "event", "symbol", "bid", "ask", "reason");
   }
   PrintFormat("[EventTickSpy] attached to %s %s: timer=%ds, book=%s",
               g_symbol, EnumToString(_Period), InpTimerSeconds,
               InpLogBookEvent ? "on" : "off");
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   if(InpLogBookEvent)
      MarketBookRelease(g_symbol);
   if(g_file != INVALID_HANDLE)
   {
      FileFlush(g_file);
      FileClose(g_file);
   }
   PrintFormat("[EventTickSpy] detached (reason %d), rows written: %d",
               reason, g_rows);
}

//+------------------------------------------------------------------+
//| Write one event row (shared by all three channels).              |
//+------------------------------------------------------------------+
void LogEvent(const string eventName, const string reason)
{
   if(g_file == INVALID_HANDLE || g_rows >= InpMaxRows)
      return;
   MqlTick tick;
   if(!SymbolInfoTick(g_symbol, tick))
   {
      tick.bid = 0.0;
      tick.ask = 0.0;
   }
   g_rows++;
   FileWrite(g_file,
             (long)(tick.time_msc),
             eventName,
             g_symbol,
             DoubleToString(tick.bid, _Digits),
             DoubleToString(tick.ask, _Digits),
             reason);
   if(g_rows % 500 == 0)
      FileFlush(g_file);
}

//+------------------------------------------------------------------+
//| Channel 1: ticks of the chart the spy runs on (book 6.4.1).      |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[],
                const double &high[], const double &low[],
                const double &close[], const long &tick_volume[],
                const long &volume[], const int &spread[])
{
   //--- first call only builds buffers; every later call = one event
   if(prev_calculated > 0)
      LogEvent("OnCalculate", StringFormat("bar %d", rates_total - 1));
   return rates_total;
}

//+------------------------------------------------------------------+
//| Channel 2: timer - the heartbeat that survives tick starvation.  |
//+------------------------------------------------------------------+
void OnTimer()
{
   LogEvent("OnTimer", "heartbeat");
}

//+------------------------------------------------------------------+
//| Channel 3: depth-of-market updates (book 6.4.38).                |
//| Fired only while the DOM window could change; used to detect    |
//| quiet-vs-aggressive book regimes for XAUUSD.                    |
//+------------------------------------------------------------------+
void OnBookEvent(const string &symbol)
{
   if(!InpLogBookEvent || symbol != g_symbol)
      return;
   LogEvent("OnBookEvent", "dom-update");
}
//+------------------------------------------------------------------+
