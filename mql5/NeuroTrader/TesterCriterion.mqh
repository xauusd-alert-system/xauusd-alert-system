//+------------------------------------------------------------------+
//| TesterCriterion.mqh - custom OnTester criterion + equity frames  |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-08; MQL5 book 6.5.1,        |
//| 6.5.6-6.5.11, pages 1449-1506).                                  |
//|                                                                  |
//| Custom optimization criterion (mirrored bit-for-bit by the Python |
//| twin backtest/tester_criterion.py, so optimizer rankings agree): |
//|                                                                  |
//|   score = PF * sqrt(trades) - ddWeight * maxRelativeDD[%]        |
//|                                                                  |
//| * PF is capped (pfCap): a run with zero losing trades must not   |
//|   win an optimization on an infinite PF;                         |
//| * sqrt(trades) rewards statistical significance;                 |
//| * the drawdown penalty keeps high-PF/one-crash curves from       |
//|   winning (book 6.5.5-6.5.7: the built-in max complex criterion  |
//|   ignores both trade count stability and DD shape).              |
//|                                                                  |
//| Equity frames: after every forward pass the EA adds an equity    |
//| curve frame (FrameAdd) so the terminal collects per-pass curves   |
//| from ALL optimization agents into one report (book 6.5.10-11).   |
//|                                                                  |
//| Tick mode mandate (task T-03/T-08): the final validation pass     |
//| must use "Every tick based on real ticks" - asserted in the       |
//| init log line TickModeReminder().                                |
//+------------------------------------------------------------------+
#ifndef NEUROTRADER_TESTER_CRITERION_MQH
#define NEUROTRADER_TESTER_CRITERION_MQH

class CTesterCriterion
{
private:
   double m_ddWeight;
   double m_pfCap;
   int    m_minTrades;

public:
   CTesterCriterion(const double ddWeight = 1.0, const double pfCap = 10.0,
                    const int minTrades = 1)
   {
      m_ddWeight  = (ddWeight >= 0.0) ? ddWeight : 1.0;
      m_pfCap     = (pfCap > 1.0) ? pfCap : 10.0;
      m_minTrades = (minTrades > 0) ? minTrades : 1;
   }

   //+--------------------------------------------------------------+
   //| The criterion itself. STAT_EQUITY_DDRELATIVE is already in    |
   //| percent in TesterStatistics (book 6.5.6 table).               |
   //+--------------------------------------------------------------+
   double Score() const
   {
      int    trades = (int)TesterStatistics(STAT_TRADES);
      if(trades < m_minTrades)
         return -DBL_MAX;

      double pf = TesterStatistics(STAT_PROFIT_FACTOR);
      if(pf <= 0.0 || !MathIsValidNumber(pf))
         return -DBL_MAX;
      if(pf > m_pfCap)                    // includes the "no losses" inf case
         pf = m_pfCap;

      double dd = TesterStatistics(STAT_EQUITY_DDRELATIVE);   // percent
      if(dd < 0.0 || !MathIsValidNumber(dd))
         dd = 0.0;

      return pf * MathSqrt((double)trades) - m_ddWeight * dd;
   }

   //+--------------------------------------------------------------+
   //| Equity-curve frame: sampled deal-based equity of the finished |
   //| pass, uploaded to the terminal via FrameAdd so optimization  |
   //| agents report curves, not just scalars (book 6.5.10-11).      |
   //+--------------------------------------------------------------+
   bool AddEquityFrame(const int points = 128)
   {
      if(!HistorySelect(0, TimeCurrent()))
         return false;
      int deals = HistoryDealsTotal();
      if(deals <= 0)
         return false;
      int take = MathMin(deals, points);
      int stride = MathMax(1, deals / take);
      double sample[];
      ArrayResize(sample, 0);
      double running = 0.0;
      for(int i = 0; i < deals; i++)
      {
         ulong ticket = HistoryDealGetTicket(i);
         if(ticket == 0)
            continue;
         running += HistoryDealGetDouble(ticket, DEAL_PROFIT)
                  + HistoryDealGetDouble(ticket, DEAL_SWAP)
                  + HistoryDealGetDouble(ticket, DEAL_COMMISSION);
         if(i % stride == 0 || i == deals - 1)
         {
            int k = ArraySize(sample);
            ArrayResize(sample, k + 1);
            sample[k] = running;
         }
      }
      if(ArraySize(sample) <= 0)
         return false;
      return FrameAdd("equity", (int)TesterStatistics(STAT_TRADES), sample);
   }

   //+--------------------------------------------------------------+
   //| One-line reminder for the log: the tick-mode mandate          |
   //+--------------------------------------------------------------+
   string TickModeReminder() const
   {
      return "final validation pass must run with 'Every tick based on real "
             "ticks' (MQL5 book 6.5.1) - any other mode is exploratory only";
   }
};

#endif // NEUROTRADER_TESTER_CRITERION_MQH
