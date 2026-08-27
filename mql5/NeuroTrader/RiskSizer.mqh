//+------------------------------------------------------------------+
//| RiskSizer.mqh - risk-based lot calculator with margin precheck   |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-06; MQL5 book 6.1 + 6.4.7-  |
//| 6.4.8, pages 1049-1134, 1178-1196).                              |
//|                                                                  |
//| Lot size so that hitting the full stop-loss costs exactly        |
//| `riskPercent` of the account equity:                             |
//|                                                                  |
//|   stopMoney   = equity * riskPercent / 100                       |
//|   slTicks     = |entry - sl| / tickSize                          |
//|   tickValue   = SYMBOL_TRADE_TICK_VALUE (account currency/tick)  |
//|   lots        = stopMoney / (slTicks * tickValue)                |
//|                                                                  |
//| Then broker normalization (book 6.1):                            |
//|   * round DOWN to SYMBOL_VOLUME_STEP (never round up into a      |
//|     bigger risk than budgeted);                                  |
//|   * clamp to [SYMBOL_VOLUME_MIN, SYMBOL_VOLUME_MAX];             |
//|   * if the minimum lot already exceeds the risk budget -> SKIP  |
//|     the trade (mirrors the repo's audit rule "never round up to  |
//|     0.01");                                                      |
//|   * OrderCalcMargin precheck: free margin must cover the margin  |
//|     of the computed lot, otherwise the trade is declined BEFORE  |
//|     any request is built (book 6.4.7).                           |
//+------------------------------------------------------------------+
#ifndef NEUROTRADER_RISK_SIZER_MQH
#define NEUROTRADER_RISK_SIZER_MQH

struct SRiskSizeResult
{
   bool   ok;          // trade may proceed with `lots`
   double lots;        // normalized volume (0.0 when skipped)
   double riskMoney;   // projected loss at the full SL
   double margin;      // projected margin for the position
   string reason;      // "" or the skip cause
};

class CRiskSizer
{
private:
   double m_riskPercent;      // risk per trade, % of equity
   double m_maxLots;          // absolute position cap

public:
   CRiskSizer(const double riskPercent = 0.25, const double maxLots = 1.0)
   {
      m_riskPercent = (riskPercent > 0.0) ? riskPercent : 0.25;
      m_maxLots     = (maxLots > 0.0) ? maxLots : 1.0;
   }

   double RiskPercent() const { return m_riskPercent; }

   //+--------------------------------------------------------------+
   //| Compute the tradable volume for one entry                    |
   //+--------------------------------------------------------------+
   bool SizePosition(const string symbol, const int direction,
                     const double entry, const double sl,
                     SRiskSizeResult &out) const
   {
      out.ok = false;
      out.lots = 0.0;
      out.riskMoney = 0.0;
      out.margin = 0.0;
      out.reason = "";

      if(direction == 0 || sl == 0.0 || entry == 0.0)
      {
         out.reason = "missing direction/entry/sl";
         return false;
      }

      double equity    = AccountInfoDouble(ACCOUNT_EQUITY);
      double freeMargin= AccountInfoDouble(ACCOUNT_MARGIN_FREE);
      double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
      double tickSize  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
      double volMin    = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      double volMax    = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
      double volStep   = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

      if(tickValue <= 0.0 || tickSize <= 0.0)
      {
         out.reason = "symbol has no tick value/size (not subscribed?)";
         return false;
      }
      if(volMin <= 0.0 || volStep <= 0.0)
      {
         out.reason = "symbol has no volume limits";
         return false;
      }

      //--- risk budget in account currency
      double stopMoney = equity * m_riskPercent / 100.0;
      if(stopMoney <= 0.0)
      {
         out.reason = "non-positive equity";
         return false;
      }

      //--- stop distance expressed in ticks (book 6.1 tick arithmetic)
      double slDistance = MathAbs(entry - sl);
      double slTicks = slDistance / tickSize;
      if(slTicks <= 0.0)
      {
         out.reason = StringFormat("SL distance %.5f is below one tick %.5f",
                                   slDistance, tickSize);
         return false;
      }

      //--- raw lot size (risk formula), then broker normalization
      double rawLots = stopMoney / (slTicks * tickValue);
      double lots = MathFloor(rawLots / volStep) * volStep;   // round DOWN

      if(lots < volMin)
      {
         // minimum lot would exceed the risk budget -> SKIP, never round up
         out.reason = StringFormat("computed %.4f lots < minimum %.2f; skip "
                                   "(budget %.2f cannot be respected)",
                                   rawLots, volMin, stopMoney);
         return false;
      }
      lots = MathMin(lots, volMax);
      lots = MathMin(lots, m_maxLots);

      //--- margin precheck via OrderCalcMargin (book 6.4.7, p. 1178-1190)
      ENUM_ORDER_TYPE orderType = (direction > 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      double margin = 0.0;
      if(!OrderCalcMargin(orderType, symbol, lots, entry, margin))
      {
         out.reason = StringFormat("OrderCalcMargin failed (err %d)", GetLastError());
         return false;
      }
      if(margin > freeMargin)
      {
         out.reason = StringFormat("margin %.2f exceeds free margin %.2f",
                                   margin, freeMargin);
         return false;
      }

      out.ok = true;
      out.lots = lots;
      out.riskMoney = slTicks * tickValue * lots;
      out.margin = margin;
      return true;
   }

   //+--------------------------------------------------------------+
   //| Point value of one lot (book: TickValue * Point / TickSize)  |
   //+--------------------------------------------------------------+
   static double PointValuePerLot(const string symbol)
   {
      double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
      double tickSize  = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
      double point     = SymbolInfoDouble(symbol, SYMBOL_POINT);
      if(tickSize <= 0.0)
         return 0.0;
      return tickValue * point / tickSize;
   }

   //+--------------------------------------------------------------+
   //| Projected P/L of a closed move (OrderCalcProfit wrapper,     |
   //| book 6.4.8, p. 1190-1196)                                    |
   //+--------------------------------------------------------------+
   static bool ProjectProfit(const string symbol, const int direction,
                             const double volume, const double openPrice,
                             const double closePrice, double &profit)
   {
      ENUM_ORDER_TYPE orderType = (direction > 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      return OrderCalcProfit(orderType, symbol, volume, openPrice, closePrice, profit);
   }
};

#endif // NEUROTRADER_RISK_SIZER_MQH
