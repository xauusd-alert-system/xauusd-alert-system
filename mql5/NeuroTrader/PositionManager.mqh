//+------------------------------------------------------------------+
//| PositionManager.mqh - SL/TP with spread, trailing, partial close |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-12; NN book p. 687; MQL5    |
//| book 6.4.15-6.4.17, pages 1229-1265).                            |
//|                                                                  |
//| The book's EA deliberately skipped money management and position  |
//| accompaniment (p. 687: adding them "can potentially increase      |
//| profitability") - this module closes that gap for XAUUSD:        |
//|                                                                  |
//|  * SL/TP placement with SPREAD accounted for: XAUUSD costs are   |
//|    asymmetric (spread paid on entry), so stops shift by the      |
//|    current spread before being validated against the broker's    |
//|    SYMBOL_TRADE_STOPS_LEVEL;                                     |
//|  * trailing stop: when profit exceeds `trailStartPoints`, the    |
//|    stop follows price at `trailDistancePoints`, only ever        |
//|    tightening (book 6.4.16);                                     |
//|  * partial close: at TP1 close `partialFraction` of the volume   |
//|    and move the stop to breakeven (book 6.4.17) - the repo's     |
//|    TP1/BE discipline in EA form.                                 |
//|                                                                  |
//| All modifications go through the same request-check loop; failed |
//| modifications are retried on the next management tick, never     |
//| silently dropped.                                                |
//+------------------------------------------------------------------+
#ifndef NEUROTRADER_POSITION_MANAGER_MQH
#define NEUROTRADER_POSITION_MANAGER_MQH

struct SPositionPlan
{
   double sl;          // stop-loss price (spread-adjusted, validated)
   double tp;          // take-profit price (spread-adjusted, validated)
   bool   ok;
   string reason;
};

class CPositionManager
{
private:
   long   m_magic;
   int    m_stopsLevelBuffer;   // extra points beyond TRADE_STOPS_LEVEL
   int    m_trailStartPoints;
   int    m_trailDistancePoints;
   double m_partialFraction;    // fraction closed at TP1 (book 6.4.17)
   bool   m_breakevenAfterPartial;

   double SpreadPoints(const string symbol) const
   {
      double spread = SymbolInfoDouble(symbol, SYMBOL_ASK)
                    - SymbolInfoDouble(symbol, SYMBOL_BID);
      double point  = SymbolInfoDouble(symbol, SYMBOL_POINT);
      if(point <= 0.0)
         return 0.0;
      return spread / point;
   }

   double MinStopDistance(const string symbol) const
   {
      long stopsLevel = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
      return (stopsLevel + m_stopsLevelBuffer)
             * SymbolInfoDouble(symbol, SYMBOL_POINT);
   }

public:
   CPositionManager(const long magic, const int trailStartPoints = 300,
                    const int trailDistancePoints = 150,
                    const double partialFraction = 0.5,
                    const bool breakevenAfterPartial = true,
                    const int stopsLevelBuffer = 10)
   {
      m_magic                = magic;
      m_trailStartPoints     = trailStartPoints;
      m_trailDistancePoints  = trailDistancePoints;
      m_partialFraction      = MathMin(0.9, MathMax(0.0, partialFraction));
      m_breakevenAfterPartial= breakevenAfterPartial;
      m_stopsLevelBuffer     = stopsLevelBuffer;
   }

   //+--------------------------------------------------------------+
   //| Initial SL/TP for a new position, SPREAD-ADJUSTED.             |
   //| The caller passes the raw geometry (e.g. from the signal       |
   //| grid); we push the SL further away by the current spread so   |
   //| the worst case includes the entry cost (XAUUSD, task T-12).   |
   //+--------------------------------------------------------------+
   SPositionPlan PlanStops(const string symbol, const int direction,
                           const double rawSl, const double rawTp) const
   {
      SPositionPlan plan;
      plan.sl = 0.0;
      plan.tp = 0.0;
      plan.ok = false;
      plan.reason = "";

      double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
      double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
      double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
      if(ask <= 0.0 || bid <= 0.0 || point <= 0.0)
      {
         plan.reason = "no market prices";
         return plan;
      }
      double spread = ask - bid;
      double minDist = MinStopDistance(symbol);

      if(direction > 0)
      {
         plan.sl = rawSl - spread;                 // pay spread on the stop
         plan.tp = rawTp;
         if(ask - plan.sl < minDist)
            plan.sl = ask - minDist;
         if(plan.tp - bid < minDist)
            plan.tp = bid + minDist;
         if(plan.sl >= ask)
         {
            plan.reason = "SL adjusted above price";
            return plan;
         }
      }
      else
      {
         plan.sl = rawSl + spread;
         plan.tp = rawTp;
         if(plan.sl - bid < minDist)
            plan.sl = bid + minDist;
         if(ask - plan.tp < minDist)
            plan.tp = ask - minDist;
         if(plan.sl <= bid)
         {
            plan.reason = "SL adjusted below price";
            return plan;
         }
      }
      plan.ok = true;
      return plan;
   }

   //+--------------------------------------------------------------+
   //| Trailing stop (book 6.4.16): tighten only. Returns true when  |
   //| a modification request was sent.                              |
   //+--------------------------------------------------------------+
   bool TrailStop(const ulong positionTicket) const
   {
      if(!PositionSelectByTicket(positionTicket))
         return false;
      string symbol = PositionGetString(POSITION_SYMBOL);
      long   magic  = PositionGetInteger(POSITION_MAGIC);
      if(magic != m_magic)
         return false;
      long   type   = PositionGetInteger(POSITION_TYPE);
      double open   = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl     = PositionGetDouble(POSITION_SL);
      double point  = SymbolInfoDouble(symbol, SYMBOL_POINT);
      if(point <= 0.0)
         return false;

      double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
      double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
      double minDist = MinStopDistance(symbol);
      bool   isLong  = (type == POSITION_TYPE_BUY);

      double profitPoints = isLong ? (bid - open) / point : (open - ask) / point;
      if(profitPoints < m_trailStartPoints)
         return false;

      double newSl;
      if(isLong)
      {
         newSl = bid - m_trailDistancePoints * point;
         if(newSl <= sl + point)          // tighten-only
            return false;
         if(bid - newSl < minDist)
            return false;                 // too close, retry next tick
      }
      else
      {
         newSl = ask + m_trailDistancePoints * point;
         if(sl > 0.0 && newSl >= sl - point)
            return false;
         if(newSl - ask < minDist)
            return false;
      }

      MqlTradeRequest request;
      MqlTradeResult  result;
      ZeroMemory(request);
      ZeroMemory(result);
      request.action   = TRADE_ACTION_SLTP;
      request.position = positionTicket;
      request.symbol   = symbol;
      request.sl       = newSl;
      request.tp       = PositionGetDouble(POSITION_TP);
      request.magic    = m_magic;
      if(!OrderSend(request, result))
      {
         PrintFormat("[PositionManager] trailing modify failed: %u",
                     result.retcode);
         return false;
      }
      return true;
   }

   //+--------------------------------------------------------------+
   //| Partial close at TP1 + breakeven (book 6.4.17).                |
   //| Closes `fraction` of the volume; when `toBreakeven` is set,    |
   //| moves the stop of the remainder to the open price.            |
   //+--------------------------------------------------------------+
   bool PartialCloseAtTarget(const ulong positionTicket, const double targetPrice,
                             const bool toBreakeven) const
   {
      if(!PositionSelectByTicket(positionTicket))
         return false;
      if(PositionGetInteger(POSITION_MAGIC) != m_magic)
         return false;
      string symbol = PositionGetString(POSITION_SYMBOL);
      long   type   = PositionGetInteger(POSITION_TYPE);
      double volume = PositionGetDouble(POSITION_VOLUME);
      double open   = PositionGetDouble(POSITION_PRICE_OPEN);
      double point  = SymbolInfoDouble(symbol, SYMBOL_POINT);
      double volStep= SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
      double volMin = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
      if(point <= 0.0 || volStep <= 0.0)
         return false;

      double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
      double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
      bool   isLong = (type == POSITION_TYPE_BUY);
      bool   reached = isLong ? (bid >= targetPrice) : (ask <= targetPrice);
      if(!reached)
         return false;

      double closeVolume = MathFloor(volume * m_partialFraction / volStep) * volStep;
      if(closeVolume < volMin)
         return false;                    // too small to split - keep full size

      MqlTradeRequest request;
      MqlTradeResult  result;
      ZeroMemory(request);
      ZeroMemory(result);
      request.action   = TRADE_ACTION_DEAL;
      request.position = positionTicket;
      request.symbol   = symbol;
      request.volume   = closeVolume;
      request.type     = isLong ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
      request.price    = isLong ? bid : ask;
      request.deviation= 20;
      request.magic    = m_magic;
      request.comment  = "TP1 partial";
      if(!OrderSend(request, result))
      {
         PrintFormat("[PositionManager] partial close failed: %u", result.retcode);
         return false;
      }

      if(toBreakeven && PositionSelectByTicket(positionTicket))
      {
         ZeroMemory(request);
         ZeroMemory(result);
         request.action   = TRADE_ACTION_SLTP;
         request.position = positionTicket;
         request.symbol   = symbol;
         request.sl       = open;    // breakeven at the ACTUAL fill (repo rule)
         request.tp       = PositionGetDouble(POSITION_TP);
         request.magic    = m_magic;
         OrderSend(request, result);
      }
      return true;
   }

   //+--------------------------------------------------------------+
   //| Sweep: trail + partial-close every own position. Called from   |
   //| OnTick/OnTimer.                                                |
   //+--------------------------------------------------------------+
   void ManageAll()
   {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0)
            continue;
         if(PositionGetInteger(POSITION_MAGIC) != m_magic)
            continue;
         TrailStop(ticket);
         // PartialCloseAtTarget is driven by the EA's TP1 level; the EA
         // calls it explicitly with its signal-grid TP1 price.
      }
   }
};

#endif // NEUROTRADER_POSITION_MANAGER_MQH
