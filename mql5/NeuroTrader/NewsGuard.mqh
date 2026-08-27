//+------------------------------------------------------------------+
//| NewsGuard.mqh - economic calendar blackout filter (live)         |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-15; MQL5 book 7.3, pages    |
//| 1689-1773).                                                      |
//|                                                                  |
//| Blocks trading +-N minutes around high-importance calendar       |
//| events of the configured currencies (default: USD - the          |
//| XAUUSD-critical set NFP / FOMC / CPI).                           |
//|                                                                  |
//| Book-critical platform facts encoded here:                       |
//|  * Calendar functions are FORBIDDEN in the Strategy Tester       |
//|    (error 4014 FUNCTION_NOT_ALLOWED) - in tester mode the guard  |
//    | fails OPEN for trading but logs the limitation, and the      |
//|    backtest must instead use the Python-side SQLite news table   |
//|    (data/news_sqlite.py) for the same windows.                   |
//|  * Calendar times are in the SERVER time zone (TimeTradeServer), |
//|    which drifts with DST (book p. 1690): we convert the blackout  |
//|    window into server time before comparing with TimeCurrent().  |
//|                                                                  |
//| The live cache refreshes on a timer (default 15 min) so the      |
//| OnTick path never calls the calendar API directly.               |
//+------------------------------------------------------------------+
#ifndef NEUROTRADER_NEWS_GUARD_MQH
#define NEUROTRADER_NEWS_GUARD_MQH

//--- MQL5 has no in-place-returning StringTrim(): Left/Right pair wrapper
string NeuroTrim(const string value)
{
   string text = value;
   StringTrimLeft(text);
   StringTrimRight(text);
   return text;
}

class CNewsGuard
{
private:
   string m_currencies[];   // e.g. {"USD"}
   int    m_bufferBeforeMin;
   int    m_bufferAfterMin;
   int    m_refreshMinutes;
   datetime m_lastRefresh;
   datetime m_blackouts[];  // flat array: [start1, end1, start2, end2, ...]
   int    m_blackoutCount;  // number of (start,end) pairs
   bool   m_inTester;
   int    m_calendarErrors;

   bool IsHighImportance(const MqlCalendarEvent &event) const
   {
      return (event.importance == CALENDAR_IMPORTANCE_HIGH);
   }

   void AppendBlackout(const datetime start, const datetime end)
   {
      if(end <= start)
         return;
      int idx = m_blackoutCount * 2;
      ArrayResize(m_blackouts, idx + 2);
      m_blackouts[idx]     = start;
      m_blackouts[idx + 1] = end;
      m_blackoutCount++;
   }

public:
   CNewsGuard(const int bufferBeforeMin = 30, const int bufferAfterMin = 30,
              const int refreshMinutes = 15)
   {
      m_bufferBeforeMin = bufferBeforeMin;
      m_bufferAfterMin  = bufferAfterMin;
      m_refreshMinutes  = MathMax(1, refreshMinutes);
      m_lastRefresh     = 0;
      m_blackoutCount   = 0;
      m_calendarErrors  = 0;
      m_inTester        = (bool)MQLInfoInteger(MQL_TESTER);
      ArrayResize(m_currencies, 1);
      m_currencies[0] = "USD";    // XAUUSD default (TZ task T-15)
   }

   void SetCurrencies(const string csv)
   {
      //--- "USD" or "USD,EUR"
      string parts[];
      int n = StringSplit(csv, ',', parts);
      if(n <= 0)
         return;
      ArrayResize(m_currencies, 0);
      for(int i = 0; i < n; i++)
      {
         string cur = NeuroTrim(parts[i]);
         if(StringLen(cur) == 3)
         {
            int k = ArraySize(m_currencies);
            ArrayResize(m_currencies, k + 1);
            m_currencies[k] = cur;
         }
      }
   }

   //+--------------------------------------------------------------+
   //| Refresh the blackout cache from the economic calendar.        |
   //| Call from OnTimer; safe in the tester (fails open).           |
   //+--------------------------------------------------------------+
   bool Refresh()
   {
      if(m_inTester)
      {
         //--- book 7.3: calendar functions are not allowed in the tester
         //    (4014). Backtests use the Python-side SQLite news table.
         if(m_calendarErrors == 0)
            Print("[NewsGuard] Strategy Tester detected: calendar API disabled "
                  "(4014); news windows must come from the SQLite table "
                  "(data/news_sqlite.py). Failing OPEN for trading.");
         m_calendarErrors++;
         return false;
      }

      datetime now = TimeTradeServer();          // server time (book p. 1690)
      if(m_lastRefresh != 0 &&
         (now - m_lastRefresh) < m_refreshMinutes * 60)
         return true;

      m_blackoutCount = 0;
      ArrayResize(m_blackouts, 0);
      m_lastRefresh = now;

      datetime from = now - m_bufferAfterMin * 60 - 3600;   // 1h lookback
      datetime to   = now + 48 * 3600;                      // 48h lookahead
      bool any = false;

      for(int c = 0; c < ArraySize(m_currencies); c++)
      {
         MqlCalendarValue values[];
         ResetLastError();
         int n = CalendarValueHistory(values, from, to, NULL, m_currencies[c]);
         if(n < 0)
         {
            m_calendarErrors++;
            PrintFormat("[NewsGuard] CalendarValueHistory(%s) failed (err %d)",
                        m_currencies[c], GetLastError());
            continue;
         }
         any = true;
         for(int i = 0; i < n; i++)
         {
            MqlCalendarEvent event;
            if(!CalendarEventById(values[i].event_id, event))
               continue;
            if(!IsHighImportance(event))
               continue;
            AppendBlackout(values[i].time - m_bufferBeforeMin * 60,
                           values[i].time + m_bufferAfterMin * 60);
         }
      }
      //--- merge overlapping windows (flat array of pairs)
      if(m_blackoutCount > 1)
      {
         for(int i = 0; i < m_blackoutCount - 1; i++)
         {
            int a = i * 2;
            for(int j = i + 1; j < m_blackoutCount; j++)
            {
               int b = j * 2;
               if(m_blackouts[b] <= m_blackouts[a + 1] &&
                  m_blackouts[b + 1] >= m_blackouts[a])
               {
                  m_blackouts[a]     = MathMin(m_blackouts[a], m_blackouts[b]);
                  m_blackouts[a + 1] = MathMax(m_blackouts[a + 1], m_blackouts[b + 1]);
                  for(int k = b; k + 2 < ArraySize(m_blackouts); k++)
                     m_blackouts[k] = m_blackouts[k + 2];
                  ArrayResize(m_blackouts, ArraySize(m_blackouts) - 2);
                  m_blackoutCount--;
                  j--;
               }
            }
         }
      }
      return any;
   }

   //+--------------------------------------------------------------+
   //| Blackout check for the CURRENT server time (OnTick path).     |
   //+--------------------------------------------------------------+
   bool IsBlackout() const
   {
      datetime now = TimeTradeServer();
      for(int i = 0; i < m_blackoutCount; i++)
      {
         int a = i * 2;
         if(now >= m_blackouts[a] && now <= m_blackouts[a + 1])
            return true;
      }
      return false;
   }

   //--- name of the active blocking window ("" when trading is allowed)
   string ActiveWindowText() const
   {
      datetime now = TimeTradeServer();
      for(int i = 0; i < m_blackoutCount; i++)
      {
         int a = i * 2;
         if(now >= m_blackouts[a] && now <= m_blackouts[a + 1])
            return StringFormat("%s..%s", TimeToString(m_blackouts[a]),
                                TimeToString(m_blackouts[a + 1]));
      }
      return "";
   }

   int BlackoutWindowCount() const { return m_blackoutCount; }
   int CalendarErrors() const { return m_calendarErrors; }
   int BufferBeforeMin() const { return m_bufferBeforeMin; }
   int BufferAfterMin() const { return m_bufferAfterMin; }
};

#endif // NEUROTRADER_NEWS_GUARD_MQH
