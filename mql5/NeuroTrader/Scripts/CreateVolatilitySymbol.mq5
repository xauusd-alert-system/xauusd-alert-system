//+------------------------------------------------------------------+
//| CreateVolatilitySymbol.mq5 - synthetic risk symbol (T-26)        |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-26; MQL5 book 7.2).         |
//|                                                                  |
//| Builds a CUSTOM SYMBOL "XAUUSD_NV" whose bars carry the          |
//| NORMALIZED VOLATILITY of gold instead of price:                 |
//|                                                                  |
//|   nv(t) = 100 * ATR(14, M5) / close(t)     - "volatility points" |
//|                                                                  |
//| Why a custom symbol (book 7.2): a synthetic symbol behaves like  |
//| a real one - indicators, iRSI, tester optimization all work on   |
//| it without any code changes, so regime/volatility logic can be   |
//| backtested exactly like price logic. The open/high/low of each   |
//| synthetic bar preserve the intrabar range of the volatility      |
//| estimate, close is the final value.                             |
//|                                                                  |
//| Non-standard timeframes (the second half of T-26): the same     |
//| builder can aggregate into e.g. 3-hour bars by passing a         |
//| PERIOD_H3-compatible seconds value - the terminal accepts any    |
//| multiple of 60 that is not a standard period for custom symbols  |
//| (book 7.2 custom-period note).                                  |
//|                                                                  |
//| Run as a Script on the SOURCE chart (XAUUSD M5). The custom      |
//| symbol appears in Market Watch after completion.                 |
//+------------------------------------------------------------------+
#property copyright "xauusd-alert-system / books integration"
#property version   "1.00"
#property script_show_inputs
#property strict

input string InpSourceSymbol   = "XAUUSD";
input ENUM_TIMEFRAMES InpSourceTF = PERIOD_M5;
input string InpCustomSymbol   = "XAUUSD_NV";   // synthetic output symbol
input int    InpAtrPeriod      = 14;
input int    InpBars           = 20000;         // how much history to build
input int    InpAggregationMin = 0;             // >0: build non-standard
                                                // higher-TF bars (e.g. 180)

//+------------------------------------------------------------------+
bool EnsureCustomSymbol(const string name, const string source)
{
   if(SymbolSelect(name, true))
   {
      PrintFormat("[CreateVolatilitySymbol] %s already exists - rebuilding",
                  name);
      return true;
   }
   if(!CustomSymbolCreate(name, NULL, source))
   {
      //--- hidden/duplicate-name failures land here (book 7.2 error table)
      PrintFormat("[CreateVolatilitySymbol] CustomSymbolCreate failed (err %d)",
                  GetLastError());
      return false;
   }
   //--- copy the symbol basics so the tester accepts it
   CustomSymbolSetInteger(name, SYMBOL_DIGITS,
                          (long)SymbolInfoInteger(source, SYMBOL_DIGITS));
   CustomSymbolSetDouble(name, SYMBOL_POINT,
                         SymbolInfoDouble(source, SYMBOL_POINT));
   CustomSymbolSetInteger(name, SYMBOL_TRADE_MODE,
                          SYMBOL_TRADE_MODE_FULL);
   return true;
}

//+------------------------------------------------------------------+
//| True-range series of the source (manual ATR: no indicator        |
//| handles needed inside a script, book 4.1.4 pattern).             |
//+------------------------------------------------------------------+
int TrueRangeSeries(const string symbol, const ENUM_TIMEFRAMES tf,
                    const int bars, double &tr[], datetime &times[])
{
   MqlRates rates[];
   int copied = CopyRates(symbol, tf, 0, bars + 1, rates);
   if(copied <= InpAtrPeriod)
      return 0;
   ArraySetAsSeries(rates, false);
   int n = copied - 1;
   ArrayResize(tr, n);
   ArrayResize(times, n);
   for(int i = 1; i <= n; i++)
   {
      double hi = rates[i].high;
      double lo = rates[i].low;
      double prevClose = rates[i - 1].close;
      tr[i - 1] = MathMax(hi - lo,
                   MathMax(MathAbs(hi - prevClose), MathAbs(lo - prevClose)));
      times[i - 1] = rates[i].time;
   }
   return n;
}

//+------------------------------------------------------------------+
void OnStart()
{
   if(!EnsureCustomSymbol(InpCustomSymbol, InpSourceSymbol))
      return;

   double tr[];
   datetime times[];
   int n = TrueRangeSeries(InpSourceSymbol, InpSourceTF, InpBars, tr, times);
   if(n <= InpAtrPeriod)
   {
      PrintFormat("[CreateVolatilitySymbol] not enough source bars (%d)", n);
      return;
   }

   //--- Wilder-smoothed ATR -> normalized volatility in percent
   double atr = 0.0;
   for(int i = 0; i < InpAtrPeriod && i < n; i++)
      atr += tr[i];
   atr /= InpAtrPeriod;

   MqlRates source[];
   int copied = CopyRates(InpSourceSymbol, InpSourceTF, 0, n + 1, source);
   ArraySetAsSeries(source, false);
   if(copied < n)
   {
      PrintFormat("[CreateVolatilitySymbol] source/series length mismatch");
      return;
   }

   MqlRates custom[];
   ArrayResize(custom, 0);
   int written = 0;
   datetime aggBucketStart = 0;
   double bucketHi = -DBL_MAX, bucketLo = DBL_MAX, bucketLast = 0.0;
   double prevClose = 0.0;
   int aggSeconds = (InpAggregationMin > 0) ? InpAggregationMin * 60 : 0;

   for(int i = InpAtrPeriod; i < n; i++)
   {
      atr = ((atr * (InpAtrPeriod - 1)) + tr[i]) / InpAtrPeriod;  // Wilder
      double closePrice = source[i + 1].close;
      if(closePrice <= 0.0)
         continue;
      double nv = 100.0 * atr / closePrice;

      if(aggSeconds == 0)
      {
         int k = ArraySize(custom);
         ArrayResize(custom, k + 1);
         custom[k].time      = times[i];
         custom[k].open      = (k > 0) ? prevClose : nv;
         custom[k].high      = MathMax(nv, (k > 0) ? prevClose : nv);
         custom[k].low       = MathMin(nv, (k > 0) ? prevClose : nv);
         custom[k].close     = nv;
         custom[k].tick_volume = 0;
         custom[k].spread    = 0;
         custom[k].real_volume = 0;
         prevClose = nv;
      }
      else
      {
         //--- non-standard timeframe bucket (T-26, book 7.2)
         datetime bucket = times[i] - (long)times[i] % aggSeconds;
         if(bucket != aggBucketStart)
         {
            if(aggBucketStart != 0)
            {
               int k = ArraySize(custom);
               ArrayResize(custom, k + 1);
               custom[k].time      = aggBucketStart;
               custom[k].open      = (k > 0) ? prevClose : bucketLast;
               custom[k].high      = bucketHi;
               custom[k].low       = bucketLo;
               custom[k].close     = bucketLast;
               custom[k].tick_volume = 0;
               custom[k].spread    = 0;
               custom[k].real_volume = 0;
               prevClose = bucketLast;
            }
            aggBucketStart = bucket;
            bucketHi = -DBL_MAX;
            bucketLo = DBL_MAX;
         }
         bucketHi = MathMax(bucketHi, nv);
         bucketLo = MathMin(bucketLo, nv);
         bucketLast = nv;
      }
      written++;
   }

   int added = CustomRatesReplace(InpCustomSymbol, 0, TimeCurrent(), custom);
   if(added < 0)
   {
      PrintFormat("[CreateVolatilitySymbol] CustomRatesReplace failed (err %d)",
                  GetLastError());
      return;
   }
   SymbolSelect(InpCustomSymbol, true);

   PrintFormat("[CreateVolatilitySymbol] %s built: %d source bars -> %d "
               "custom bars (ATR%d on %s%s)",
               InpCustomSymbol, written, ArraySize(custom), InpAtrPeriod,
               InpSourceSymbol, EnumToString(InpSourceTF),
               aggSeconds > 0 ? StringFormat(", aggregation %d min",
                                             InpAggregationMin) : "");
   Print("[CreateVolatilitySymbol] the symbol now supports indicators and "
         "the strategy tester like any market symbol (book 7.2)");
}
//+------------------------------------------------------------------+
