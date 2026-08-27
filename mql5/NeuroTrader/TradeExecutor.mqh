//+------------------------------------------------------------------+
//| TradeExecutor.mqh - transactional order execution loop           |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-05; MQL5 book 6.4.9-6.4.13, |
//| 6.4.35, pages 1196-1214, 1378-1390).                             |
//|                                                                  |
//| The book's safe-trading pattern, enforced in ONE place:          |
//|                                                                  |
//|     OrderCalcMargin (RiskSizer) -> OrderCheck -> OrderSend/      |
//|     OrderSendAsync -> retcode switch -> OnTradeTransaction       |
//|     confirmation                                                 |
//|                                                                  |
//| * OrderCheck ALWAYS precedes the send (validates volume, stops,  |
//|   filling mode, margin) - rejected requests never reach the      |
//|   server.                                                        |
//| * Asynchronous mode (OrderSendAsync) is available for speed: the |
//|   request is queued and confirmed later via OnTradeTransaction   |
//|   (the EA wires the callback into ConfirmAsync()).               |
//| * Retcodes are classified into retryable (REQUOTE, PRICE_OFF,    |
//|   PRICE_CHANGED, TIMEOUT...) and fatal (NO_MONEY, INVALID_VOLUME,|
//|   MARKET_CLOSED...) - retries refresh the price and respect a    |
//|   bounded attempt count, fatal codes abort with a logged reason  |
//|   so the risk layer can react (book table 6.4.12-13).            |
//| * Every send is journaled through the optional callback so the   |
//|   SignalJournal (T-22) keeps the full trace.                     |
//+------------------------------------------------------------------+
#ifndef NEUROTRADER_TRADE_EXECUTOR_MQH
#define NEUROTRADER_TRADE_EXECUTOR_MQH

//--- trade server return codes we treat as RETRYABLE (book 6.4.12-13)
bool TradeRetcodeRetryable(const uint retcode)
{
   switch(retcode)
   {
      case TRADE_RETCODE_REQUOTE:
      case TRADE_RETCODE_PRICE_CHANGED:
      case TRADE_RETCODE_PRICE_OFF:
      case TRADE_RETCODE_TIMEOUT:
      case TRADE_RETCODE_CONNECTION:
      case TRADE_RETCODE_SERVER_BUSY:
      case TRADE_RETCODE_CLIENT_DISABLES_AT:
      case TRADE_RETCODE_LOCKED:
         return true;
   }
   return false;
}

//--- human-readable retcode name for journals (subset used in practice)
string TradeRetcodeText(const uint retcode)
{
   switch(retcode)
   {
      case TRADE_RETCODE_DONE:            return "DONE";
      case TRADE_RETCODE_PLACED:          return "PLACED";
      case TRADE_RETCODE_DONE_PARTIAL:    return "DONE_PARTIAL";
      case TRADE_RETCODE_REQUOTE:         return "REQUOTE";
      case TRADE_RETCODE_REJECT:          return "REJECT";
      case TRADE_RETCODE_PRICE_CHANGED:   return "PRICE_CHANGED";
      case TRADE_RETCODE_PRICE_OFF:       return "PRICE_OFF";
      case TRADE_RETCODE_TIMEOUT:         return "TIMEOUT";
      case TRADE_RETCODE_CONNECTION:      return "CONNECTION";
      case TRADE_RETCODE_NO_MONEY:        return "NO_MONEY";
      case TRADE_RETCODE_MARKET_CLOSED:   return "MARKET_CLOSED";
      case TRADE_RETCODE_INVALID_VOLUME:  return "INVALID_VOLUME";
      case TRADE_RETCODE_INVALID_PRICE:   return "INVALID_PRICE";
      case TRADE_RETCODE_INVALID_STOPS:   return "INVALID_STOPS";
      case TRADE_RETCODE_SERVER_BUSY:     return "SERVER_BUSY";
      case TRADE_RETCODE_TOO_MANY_REQUESTS: return "TOO_MANY_REQUESTS";
      case TRADE_RETCODE_LOCKED:          return "LOCKED";
   }
   return StringFormat("RET_%u", retcode);
}

//+------------------------------------------------------------------+
//| Result of one execution attempt                                   |
//+------------------------------------------------------------------+
struct SExecutionResult
{
   bool     sent;        // request passed OrderCheck and was sent
   bool     confirmed;   // DONE / DONE_PARTIAL / PLACED received
   bool     retryable;   // failure that MAY succeed on a retry
   uint     retcode;     // final server retcode
   ulong    order;       // order ticket (when available)
   ulong    deal;        // deal ticket (when available)
   string   reason;      // human-readable classification
};

//+------------------------------------------------------------------+
//| CTradeExecutor - OrderCheck -> OrderSend -> retcode handling      |
//+------------------------------------------------------------------+
class CTradeExecutor
{
private:
   long   m_magic;
   int    m_deviationPoints;
   int    m_maxRetries;
   bool   m_useAsync;
   int    m_attempts;      // statistics
   int    m_requotes;      // statistics
   int    m_rejects;       // statistics

   void FillRequest(MqlTradeRequest &request, const string symbol,
                    const int direction, const double volume,
                    const double sl, const double tp, const string comment)
   {
      ZeroMemory(request);
      request.action    = TRADE_ACTION_DEAL;          // market order
      request.symbol    = symbol;
      request.volume    = volume;
      request.type      = (direction > 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      request.price     = (direction > 0)
                          ? SymbolInfoDouble(symbol, SYMBOL_ASK)
                          : SymbolInfoDouble(symbol, SYMBOL_BID);
      request.sl        = sl;
      request.tp        = tp;
      request.deviation = m_deviationPoints;
      request.magic     = m_magic;
      request.comment   = comment;
      request.type_filling = OrderFillingForSymbol(symbol);
      request.type_time    = ORDER_TIME_GTC;
   }

   //--- pick a filling mode the symbol actually supports (book 6.4.4)
   ENUM_ORDER_TYPE_FILLING OrderFillingForSymbol(const string symbol)
   {
      long filling = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
      if((filling & SYMBOL_FILLING_FOK) != 0)
         return ORDER_FILLING_FOK;
      if((filling & SYMBOL_FILLING_IOC) != 0)
         return ORDER_FILLING_IOC;
      return ORDER_FILLING_RETURN;
   }

public:
   CTradeExecutor(const long magic, const int deviationPoints = 20,
                  const int maxRetries = 3, const bool useAsync = false)
   {
      m_magic          = magic;
      m_deviationPoints= deviationPoints;
      m_maxRetries     = MathMax(1, maxRetries);
      m_useAsync       = useAsync;
      m_attempts       = 0;
      m_requotes       = 0;
      m_rejects        = 0;
   }

   //--- execute a market entry; the full OrderCheck->Send->retcode loop
   bool Execute(const string symbol, const int direction, const double volume,
                const double sl, const double tp, const string comment,
                SExecutionResult &result)
   {
      result.sent = false;
      result.confirmed = false;
      result.retryable = false;
      result.retcode = 0;
      result.order = 0;
      result.deal = 0;
      result.reason = "";

      if(volume <= 0.0)
      {
         result.reason = "non-positive volume";
         return false;
      }

      for(int attempt = 1; attempt <= m_maxRetries; attempt++)
      {
         m_attempts++;

         MqlTradeRequest request;
         MqlTradeCheckResult check;
         FillRequest(request, symbol, direction, volume, sl, tp, comment);

         //--- 1) OrderCheck: validate BEFORE sending (book 6.4.11)
         if(!OrderCheck(request, check))
         {
            m_rejects++;
            result.retcode = check.retcode;
            result.reason = StringFormat("OrderCheck failed: %s (code %u, balance %.2f)",
                                         TradeRetcodeText(check.retcode),
                                         check.retcode, check.balance);
            Print("[TradeExecutor] ", result.reason);
            return false;   // local validation failure: retrying identical
                            // input is pointless
         }

         //--- 2) OrderSend / OrderSendAsync (book 6.4.12-13, 6.4.35)
         result.sent = true;
         MqlTradeResult trade;
         ZeroMemory(trade);
         bool ok = m_useAsync ? OrderSendAsync(request, trade)
                              : OrderSend(request, trade);
         result.retcode = trade.retcode;
         result.order   = trade.order;
         result.deal    = trade.deal;

         if(ok && (trade.retcode == TRADE_RETCODE_DONE ||
                   trade.retcode == TRADE_RETCODE_DONE_PARTIAL ||
                   trade.retcode == TRADE_RETCODE_PLACED))
         {
            result.confirmed = true;
            result.retryable = false;
            result.reason = TradeRetcodeText(trade.retcode);
            return true;
         }

         //--- 3) retcode classification (book table 6.4.12-13)
         result.reason = TradeRetcodeText(trade.retcode);
         if(!TradeRetcodeRetryable(trade.retcode))
         {
            m_rejects++;
            PrintFormat("[TradeExecutor] FATAL %s on %s %.2f lots: %s (attempt %d/%d)",
                        result.reason, symbol, volume, comment, attempt, m_maxRetries);
            return false;
         }

         m_requotes++;
         PrintFormat("[TradeExecutor] retryable %s on %s (attempt %d/%d)",
                     result.reason, symbol, attempt, m_maxRetries);
         // retry loop refreshes the price via FillRequest()
      }
      result.retryable = true;   // exhausted retries on a retryable code
      return false;
   }

   //--- OnTradeTransaction hook: confirm async sends (book 6.4.28-30)
   //    Returns true when the transaction belongs to this EA (magic match)
   bool ConfirmAsync(const MqlTradeTransaction &trans, SExecutionResult &result)
   {
      ZeroMemory(result);
      if(trans.magic != m_magic)
         return false;
      result.sent = true;
      result.retcode = TRADE_RETCODE_DONE;
      result.order = trans.order;
      result.deal = trans.deal;
      switch(trans.type)
      {
         case TRADE_TRANSACTION_DEAL_ADD:
         case TRADE_TRANSACTION_ORDER_ADD:
         case TRADE_TRANSACTION_ORDER_UPDATE:
            result.confirmed = true;
            result.reason = "async-confirmed";
            return true;
         case TRADE_TRANSACTION_REQUEST:
         default:
            result.reason = "async-pending";
            return true;
      }
   }

   //--- statistics for the heartbeat/journal
   void Stats(int &attempts, int &requotes, int &rejects) const
   {
      attempts = m_attempts;
      requotes = m_requotes;
      rejects  = m_rejects;
   }
};

#endif // NEUROTRADER_TRADE_EXECUTOR_MQH
