//+------------------------------------------------------------------+
//| DayFilter.mqh - intraweek day filter (MQL5 mirror of             |
//| model/day_of_week_filter.py, TZ_BOOKS task T-10)                 |
//|                                                                  |
//| Same contract as the Python side: a day is blocked only when     |
//| BOTH statistics are negative on a sufficient sample:             |
//|   win rate < minWinRate  AND  net PnL < 0,  trades >= minTrades  |
//| Fail-open by default (filter disabled lists no days) so a        |
//| missing stats file never blocks trading.                         |
//|                                                                  |
//| Time base: SERVER time (TimeTradeServer), matching the Python    |
//| side's UTC-by-bar convention only when the backtest feeds the    |
//| same clock - documented in the report.                           |
//+------------------------------------------------------------------+
#ifndef NEUROTRADER_DAY_FILTER_MQH
#define NEUROTRADER_DAY_FILTER_MQH

//--- MQL5 has no in-place-returning StringTrim(): Left/Right pair wrapper
string NeuroTrim(const string value)
{
   string text = value;
   StringTrimLeft(text);
   StringTrimRight(text);
   return text;
}

class CDayFilter
{
private:
   bool   m_enabled;
   bool   m_blocked[7];      // 0=Sunday .. 6=Saturday
   double m_minWinRate;
   int    m_minTrades;
   string m_source;

public:
   CDayFilter(const double minWinRate = 0.45, const int minTrades = 30)
   {
      m_enabled   = false;             // fail-open default
      m_minWinRate= minWinRate;
      m_minTrades = minTrades;
      m_source    = "";
      ArrayInitialize(m_blocked, false);
   }

   //+--------------------------------------------------------------+
   //| Load blocked days from a JSON file in MQL5\Files produced by   |
   //| model/day_of_week_filter.py::blocked_days_from_stats:          |
   //|   {"enabled": true, "min_trades": 30, "min_win_rate": 0.45,    |
   //|    "days_blocked": [1, 3]}                                     |
   //| Missing file or parse error leaves the filter disabled.        |
   //+--------------------------------------------------------------+
   bool LoadFromFile(const string fileName)
   {
      m_enabled = false;
      ArrayInitialize(m_blocked, false);
      m_source = fileName;

      if(!FileIsExist(fileName))
      {
         PrintFormat("[DayFilter] %s not found - filter disabled (fail-open)",
                     fileName);
         return false;
      }
      int handle = FileOpen(fileName, FILE_READ | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE)
      {
         PrintFormat("[DayFilter] cannot open %s (err %d)", fileName,
                     GetLastError());
         return false;
      }
      string json = FileReadString(handle);
      FileClose(handle);

      if(StringFind(json, "\"enabled\"") < 0)
      {
         Print("[DayFilter] malformed config - filter disabled");
         return false;
      }
      //--- minimal JSON scan: "enabled": true|false
      int pos = StringFind(json, "\"enabled\"");
      string tail = StringSubstr(json, pos);
      m_enabled = (StringFind(tail, "true") <
                   StringFind(tail, "false") + 10);
      //--- day list: collect integers inside "days_blocked": [...]
      ArrayInitialize(m_blocked, false);
      pos = StringFind(json, "\"days_blocked\"");
      if(pos >= 0)
      {
         int open  = StringFind(json, "[", pos);
         int close = StringFind(json, "]", pos);
         if(open > 0 && close > open)
         {
            string body = StringSubstr(json, open + 1, close - open - 1);
            string parts[];
            int n = StringSplit(body, ',', parts);
            for(int i = 0; i < n; i++)
            {
               int day = (int)StringToInteger(NeuroTrim(parts[i]));
               if(day >= 0 && day <= 6)
                  m_blocked[day] = true;
            }
         }
      }
      if(m_enabled)
      {
         int cnt = 0;
         for(int d = 0; d < 7; d++)
            if(m_blocked[d])
               cnt++;
         PrintFormat("[DayFilter] loaded %s: %d day(s) blocked", fileName, cnt);
      }
      else
         Print("[DayFilter] loaded but disabled");
      return true;
   }

   bool Enabled() const { return m_enabled; }

   //+--------------------------------------------------------------+
   //| Trade gate for the CURRENT server day.                        |
   //+--------------------------------------------------------------+
   bool TradingAllowedNow() const
   {
      if(!m_enabled)
         return true;                    // fail-open
      MqlDateTime dt;
      TimeToStruct(TimeTradeServer(), dt);
      return !m_blocked[dt.day_of_week];
   }

   string BlockedDaysText() const
   {
      static const string names[7] = {"Sun", "Mon", "Tue", "Wed",
                                      "Thu", "Fri", "Sat"};
      string text = "";
      for(int d = 0; d < 7; d++)
      {
         if(m_blocked[d])
            text += (text == "" ? "" : ",") + names[d];
      }
      return text;
   }
};

#endif // NEUROTRADER_DAY_FILTER_MQH
