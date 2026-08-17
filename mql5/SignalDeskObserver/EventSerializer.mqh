//+------------------------------------------------------------------+
//| EventSerializer.mqh - ExecutionEvent v1 facts for the ledger      |
//|                                                                  |
//| Part of SignalDeskObserver (read-only MT5 telemetry agent).      |
//|                                                                  |
//| Wire contract (mirror of contracts/execution_contracts.py):      |
//|   line = "<event_id>\t" + JSON object with the ExecutionEvent v1 |
//|   fields. event_id is the DETERMINISTIC canonical id string      |
//|       mt5_observer|<account_fingerprint>|<kind>|<ticket>         |
//|   (Python hashes the same string; the server treats event_id as  |
//|   an opaque primary key, so both forms dedupe identically).      |
//|                                                                  |
//| Facts: deal_added, order_history_added, position_modified,       |
//| request_result (optional), execution_reconciled, health_heartbeat|
//+------------------------------------------------------------------+
#ifndef SIGNALDESK_EVENT_SERIALIZER_MQH
#define SIGNALDESK_EVENT_SERIALIZER_MQH

#include "JsonWriter.mqh"
#include "SymbolResolver.mqh"

#define SERIALIZER_SOURCE "mt5_observer"

struct SObserverContext
{
   string accountFingerprint;   // "demo:12345" / "contest:12345" / "real:12345"
   string accountMode;          // "demo" | "contest" | "real"
   long   login;
};

//--- deterministic canonical id string (mirror of Python canonical_event_id_string)
string CanonicalEventId(const SObserverContext &ctx, const string kind, const string id)
{
   return SERIALIZER_SOURCE + "|" + ctx.accountFingerprint + "|" + kind + "|" + id;
}

//--- extract the first 8-hex-char intent token from an order comment
bool ExtractIntentShort(const string comment, string &shortIdOut)
{
   string parts[];
   int n = StringSplit(comment, ' ', parts);
   for(int i = 0; i < n; i++)
   {
      string token = StringTrim(parts[i]);
      if(StringLen(token) == 8)
      {
         bool hex = true;
         for(int j = 0; j < 8; j++)
         {
            ushort ch = StringGetCharacter(token, j);
            if(!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f') || (ch >= 'A' && ch <= 'F')))
            {
               hex = false;
               break;
            }
         }
         if(hex)
         {
            shortIdOut = token;
            return true;
         }
      }
   }
   return false;
}

//--- build the common object prefix of every ExecutionEvent
void SerializerBegin(string &obj, const SObserverContext &ctx, const string eventId,
                     const string eventType, const string brokerSymbol,
                     const string assetKey, const long magicNumber,
                     const string precision)
{
   obj = "{";
   JsonFieldInt(obj, "schema_version", 1);
   JsonFieldString(obj, "event_id", eventId);
   JsonFieldString(obj, "event_type", eventType);
   JsonField(obj, "intent_id", "null");
   JsonFieldString(obj, "source", SERIALIZER_SOURCE);
   JsonFieldString(obj, "account_mode", ctx.accountMode);
   JsonFieldString(obj, "broker_symbol", brokerSymbol);
   JsonFieldString(obj, "asset_key", assetKey);
   JsonFieldInt(obj, "magic_number", magicNumber);
}

//--- deal fact (TRADE_TRANSACTION_DEAL_ADD / history reconciliation)
bool SerializeDealEvent(const SObserverContext &ctx, const ulong dealTicket,
                        const CSymbolResolver &resolver, const bool reconciled,
                        string &lineOut, string &errorOut)
{
   if(!HistoryDealSelect(dealTicket))
   {
      errorOut = "HistoryDealSelect failed for " + IntegerToString(dealTicket);
      return false;
   }
   string brokerSymbol = HistoryDealGetString(dealTicket, DEAL_SYMBOL);
   string canonical;
   if(!resolver.BrokerToCanonical(brokerSymbol, canonical))
   {
      errorOut = "unmapped broker symbol " + brokerSymbol;   // fail-closed, never guess
      return false;
   }
   long magic = HistoryDealGetInteger(dealTicket, DEAL_MAGIC);
   long orderTicket = HistoryDealGetInteger(dealTicket, DEAL_ORDER);
   long positionTicket = HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);
   long timeMsc = HistoryDealGetInteger(dealTicket, DEAL_TIME_MSC);
   double price = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
   double volume = HistoryDealGetDouble(dealTicket, DEAL_VOLUME);
   double commission = HistoryDealGetDouble(dealTicket, DEAL_COMMISSION);
   double swap = HistoryDealGetDouble(dealTicket, DEAL_SWAP);
   double profit = HistoryDealGetDouble(dealTicket, DEAL_PROFIT);
   int entry = (int)HistoryDealGetInteger(dealTicket, DEAL_ENTRY);
   int dealType = (int)HistoryDealGetInteger(dealTicket, DEAL_TYPE);

   string eventId = CanonicalEventId(ctx, "deal", IntegerToString(dealTicket));
   string obj;
   SerializerBegin(obj, ctx, eventId, "deal_added", brokerSymbol, canonical, magic,
                   reconciled ? "history_reconciled" : "passive");
   JsonFieldInt(obj, "order_ticket", orderTicket);
   JsonFieldInt(obj, "deal_ticket", (long)dealTicket);
   JsonFieldInt(obj, "position_ticket", positionTicket);
   JsonFieldInt(obj, "deal_time_msc", timeMsc);
   JsonField(obj, "retcode", "null");
   JsonField(obj, "requested_price", "null");
   JsonFieldDouble(obj, "fill_price", price);
   JsonFieldDouble(obj, "filled_volume", volume);
   JsonField(obj, "volume_requested", "null");

   //--- passive spread observation at event time (approximate, never request-to-fill)
   double spreadPoints = 0.0;
   MqlTick tick;
   if(SymbolInfoTick(brokerSymbol, tick) && tick.ask > 0.0 && tick.bid > 0.0)
      spreadPoints = (tick.ask - tick.bid) / SymbolInfoDouble(brokerSymbol, SYMBOL_POINT);
   if(spreadPoints > 0.0)
      JsonFieldDouble(obj, "spread_points", spreadPoints, 2);
   else
      JsonField(obj, "spread_points", "null");

   JsonFieldDouble(obj, "commission", commission);
   JsonFieldDouble(obj, "swap", swap);
   JsonField(obj, "latency_ms", "null");
   JsonFieldString(obj, "precision", reconciled ? "history_reconciled" : "passive");
   JsonFieldInt(obj, "received_at_utc_ms", (long)TimeCurrent() * 1000);
   JsonField(obj, "reason", "null");

   string payload = "{";
   JsonFieldInt(payload, "entry", entry);
   JsonFieldInt(payload, "deal_type", dealType);
   JsonFieldDouble(payload, "profit", profit);
   JsonFieldBool(payload, "reconciled", reconciled);
   string intentShort = "";
   if(orderTicket > 0 && HistoryOrderSelect(orderTicket))
      ExtractIntentShort(HistoryOrderGetString(orderTicket, ORDER_COMMENT), intentShort);
   if(StringLen(intentShort) > 0)
      JsonFieldString(payload, "intent_id_short", intentShort);
   payload += "}";
   JsonField(obj, "payload", payload);
   obj += "}";

   lineOut = eventId + "\t" + obj;
   errorOut = "";
   return true;
}

//--- order fact (TRADE_TRANSACTION_HISTORY_ADD / history reconciliation)
bool SerializeOrderEvent(const SObserverContext &ctx, const ulong orderTicket,
                         const CSymbolResolver &resolver, const bool reconciled,
                         string &lineOut, string &errorOut)
{
   if(!HistoryOrderSelect(orderTicket))
   {
      errorOut = "HistoryOrderSelect failed for " + IntegerToString(orderTicket);
      return false;
   }
   string brokerSymbol = HistoryOrderGetString(orderTicket, ORDER_SYMBOL);
   string canonical;
   if(!resolver.BrokerToCanonical(brokerSymbol, canonical))
   {
      errorOut = "unmapped broker symbol " + brokerSymbol;
      return false;
   }
   long magic = HistoryOrderGetInteger(orderTicket, ORDER_MAGIC);
   long setupMsc = HistoryOrderGetInteger(orderTicket, ORDER_TIME_SETUP_MSC);
   double priceOpen = HistoryOrderGetDouble(orderTicket, ORDER_PRICE_OPEN);
   double volumeInitial = HistoryOrderGetDouble(orderTicket, ORDER_VOLUME_INITIAL);
   double volumeCurrent = HistoryOrderGetDouble(orderTicket, ORDER_VOLUME_CURRENT);
   int orderType = (int)HistoryOrderGetInteger(orderTicket, ORDER_TYPE);
   int orderState = (int)HistoryOrderGetInteger(orderTicket, ORDER_STATE);
   string comment = HistoryOrderGetString(orderTicket, ORDER_COMMENT);

   string eventId = CanonicalEventId(ctx, "order", IntegerToString(orderTicket));
   string obj;
   SerializerBegin(obj, ctx, eventId, "order_history_added", brokerSymbol, canonical, magic,
                   reconciled ? "history_reconciled" : "passive");
   JsonFieldInt(obj, "order_ticket", (long)orderTicket);
   JsonField(obj, "deal_ticket", "null");
   JsonField(obj, "position_ticket", "null");
   JsonFieldInt(obj, "deal_time_msc", setupMsc);
   JsonField(obj, "retcode", "null");
   JsonFieldDouble(obj, "requested_price", priceOpen);
   JsonField(obj, "fill_price", "null");
   JsonField(obj, "filled_volume", "null");
   JsonFieldDouble(obj, "volume_requested", volumeInitial);
   JsonField(obj, "spread_points", "null");
   JsonField(obj, "commission", "null");
   JsonField(obj, "swap", "null");
   JsonField(obj, "latency_ms", "null");
   JsonFieldString(obj, "precision", reconciled ? "history_reconciled" : "passive");
   JsonFieldInt(obj, "received_at_utc_ms", (long)TimeCurrent() * 1000);
   JsonField(obj, "reason", "null");

   string payload = "{";
   JsonFieldInt(payload, "order_type", orderType);
   JsonFieldInt(payload, "order_state", orderState);
   JsonFieldDouble(payload, "volume_current", volumeCurrent);
   JsonFieldBool(payload, "reconciled", reconciled);
   string intentShort;
   if(ExtractIntentShort(comment, intentShort))
      JsonFieldString(payload, "intent_id_short", intentShort);
   payload += "}";
   JsonField(obj, "payload", payload);
   obj += "}";

   lineOut = eventId + "\t" + obj;
   errorOut = "";
   return true;
}

//--- position fact (TRADE_TRANSACTION_POSITION)
bool SerializePositionEvent(const SObserverContext &ctx, const ulong positionTicket,
                            const CSymbolResolver &resolver,
                            string &lineOut, string &errorOut)
{
   if(!PositionSelectByTicket(positionTicket))
   {
      errorOut = "PositionSelectByTicket failed for " + IntegerToString(positionTicket);
      return false;
   }
   string brokerSymbol = PositionGetString(POSITION_SYMBOL);
   string canonical;
   if(!resolver.BrokerToCanonical(brokerSymbol, canonical))
   {
      errorOut = "unmapped broker symbol " + brokerSymbol;
      return false;
   }
   long magic = PositionGetInteger(POSITION_MAGIC);
   long timeMsc = PositionGetInteger(POSITION_TIME_MSC);
   double priceOpen = PositionGetDouble(POSITION_PRICE_OPEN);
   double sl = PositionGetDouble(POSITION_SL);
   double tp = PositionGetDouble(POSITION_TP);
   double volume = PositionGetDouble(POSITION_VOLUME);

   //--- position events can repeat on every modification; time_msc disambiguates
   string eventId = CanonicalEventId(ctx, "position",
                                     IntegerToString(positionTicket) + ":" + IntegerToString(timeMsc));
   string obj;
   SerializerBegin(obj, ctx, eventId, "position_modified", brokerSymbol, canonical, magic, "passive");
   JsonField(obj, "order_ticket", "null");
   JsonField(obj, "deal_ticket", "null");
   JsonFieldInt(obj, "position_ticket", (long)positionTicket);
   JsonFieldInt(obj, "deal_time_msc", timeMsc);
   JsonField(obj, "retcode", "null");
   JsonFieldDouble(obj, "requested_price", priceOpen);
   JsonField(obj, "fill_price", "null");
   JsonFieldDouble(obj, "filled_volume", volume);
   JsonField(obj, "volume_requested", "null");
   JsonField(obj, "spread_points", "null");
   JsonField(obj, "commission", "null");
   JsonField(obj, "swap", "null");
   JsonField(obj, "latency_ms", "null");
   JsonFieldString(obj, "precision", "passive");
   JsonFieldInt(obj, "received_at_utc_ms", (long)TimeCurrent() * 1000);
   JsonField(obj, "reason", "null");

   string payload = "{";
   JsonFieldDouble(payload, "sl_price", sl);
   JsonFieldDouble(payload, "tp_price", tp);
   payload += "}";
   JsonField(obj, "payload", payload);
   obj += "}";

   lineOut = eventId + "\t" + obj;
   errorOut = "";
   return true;
}

//--- request fact (TRADE_TRANSACTION_REQUEST, optional input ObserveRequests)
bool SerializeRequestEvent(const SObserverContext &ctx, const MqlTradeRequest &request,
                           const MqlTradeResult &result, const long transTime,
                           string &lineOut, string &errorOut)
{
   string brokerSymbol = request.symbol;
   long magic = request.magic;
   //--- best-effort deterministic id: order/deal ticket when assigned
   string id;
   if(result.order > 0)
      id = "order:" + IntegerToString(result.order);
   else if(result.deal > 0)
      id = "deal:" + IntegerToString(result.deal);
   else
      id = "t:" + IntegerToString(transTime) + ":" + IntegerToString(result.retcode);
   string eventId = CanonicalEventId(ctx, "request", id);

   string obj;
   SerializerBegin(obj, ctx, eventId, "request_result", brokerSymbol, "null", magic, "request");
   JsonFieldInt(obj, "order_ticket", result.order);
   JsonFieldInt(obj, "deal_ticket", result.deal);
   JsonField(obj, "position_ticket", "null");
   JsonField(obj, "deal_time_msc", "null");
   JsonFieldInt(obj, "retcode", result.retcode);
   JsonFieldDouble(obj, "requested_price", request.price);
   JsonFieldDouble(obj, "fill_price", result.price);
   JsonFieldDouble(obj, "filled_volume", result.volume);
   JsonFieldDouble(obj, "volume_requested", request.volume);
   JsonField(obj, "spread_points", "null");
   JsonField(obj, "commission", "null");
   JsonField(obj, "swap", "null");
   JsonField(obj, "latency_ms", "null");
   JsonFieldString(obj, "precision", "request");
   JsonFieldInt(obj, "received_at_utc_ms", (long)TimeCurrent() * 1000);
   JsonFieldString(obj, "reason", result.comment);

   string payload = "{";
   JsonFieldInt(payload, "request_action", (long)request.action);
   JsonFieldInt(payload, "request_type", (long)request.type);
   payload += "}";
   JsonField(obj, "payload", payload);
   obj += "}";

   lineOut = eventId + "\t" + obj;
   errorOut = "";
   return true;
}

//--- reconcile summary fact (one per reconciliation run)
bool SerializeReconcileEvent(const SObserverContext &ctx, const int scannedDeals,
                             const int scannedOrders, const int addedEvents,
                             const int skippedUnmapped, const string errorSummary,
                             string &lineOut, string &errorOut)
{
   string eventId = CanonicalEventId(ctx, "reconcile", IntegerToString(TimeCurrent()));
   string obj = "{";
   JsonFieldInt(obj, "schema_version", 1);
   JsonFieldString(obj, "event_id", eventId);
   JsonFieldString(obj, "event_type", "execution_reconciled");
   JsonField(obj, "intent_id", "null");
   JsonFieldString(obj, "source", SERIALIZER_SOURCE);
   JsonFieldString(obj, "account_mode", ctx.accountMode);
   JsonFieldString(obj, "broker_symbol", "ALL");
   JsonField(obj, "asset_key", "null");
   JsonField(obj, "magic_number", "null");
   JsonField(obj, "order_ticket", "null");
   JsonField(obj, "deal_ticket", "null");
   JsonField(obj, "position_ticket", "null");
   JsonField(obj, "deal_time_msc", "null");
   JsonField(obj, "retcode", "null");
   JsonField(obj, "requested_price", "null");
   JsonField(obj, "fill_price", "null");
   JsonField(obj, "filled_volume", "null");
   JsonField(obj, "volume_requested", "null");
   JsonField(obj, "spread_points", "null");
   JsonField(obj, "commission", "null");
   JsonField(obj, "swap", "null");
   JsonField(obj, "latency_ms", "null");
   JsonFieldString(obj, "precision", "history_reconciled");
   JsonFieldInt(obj, "received_at_utc_ms", (long)TimeCurrent() * 1000);
   JsonField(obj, "reason", "null");

   string payload = "{";
   JsonFieldInt(payload, "scanned_deals", scannedDeals);
   JsonFieldInt(payload, "scanned_orders", scannedOrders);
   JsonFieldInt(payload, "added_events", addedEvents);
   JsonFieldInt(payload, "skipped_unmapped", skippedUnmapped);
   JsonFieldString(payload, "error_summary", errorSummary);
   payload += "}";
   JsonField(obj, "payload", payload);
   obj += "}";

   lineOut = eventId + "\t" + obj;
   errorOut = "";
   return true;
}

//--- heartbeat fact (liveness / staleness signal for the server UI)
bool SerializeHeartbeatEvent(const SObserverContext &ctx, const long uptimeSeconds,
                             const long pendingCount, const long outboxErrors,
                             string &lineOut, string &errorOut)
{
   string eventId = CanonicalEventId(ctx, "heartbeat", IntegerToString(TimeCurrent()));
   string obj = "{";
   JsonFieldInt(obj, "schema_version", 1);
   JsonFieldString(obj, "event_id", eventId);
   JsonFieldString(obj, "event_type", "health_heartbeat");
   JsonField(obj, "intent_id", "null");
   JsonFieldString(obj, "source", SERIALIZER_SOURCE);
   JsonFieldString(obj, "account_mode", ctx.accountMode);
   JsonFieldString(obj, "broker_symbol", "ALL");
   JsonField(obj, "asset_key", "null");
   JsonField(obj, "magic_number", "null");
   JsonField(obj, "order_ticket", "null");
   JsonField(obj, "deal_ticket", "null");
   JsonField(obj, "position_ticket", "null");
   JsonField(obj, "deal_time_msc", "null");
   JsonField(obj, "retcode", "null");
   JsonField(obj, "requested_price", "null");
   JsonField(obj, "fill_price", "null");
   JsonField(obj, "filled_volume", "null");
   JsonField(obj, "volume_requested", "null");
   JsonField(obj, "spread_points", "null");
   JsonField(obj, "commission", "null");
   JsonField(obj, "swap", "null");
   JsonField(obj, "latency_ms", "null");
   JsonFieldString(obj, "precision", "passive");
   JsonFieldInt(obj, "received_at_utc_ms", (long)TimeCurrent() * 1000);
   JsonField(obj, "reason", "null");

   string payload = "{";
   JsonFieldInt(payload, "uptime_seconds", uptimeSeconds);
   JsonFieldInt(payload, "pending_outbox", pendingCount);
   JsonFieldInt(payload, "outbox_errors", outboxErrors);
   payload += "}";
   JsonField(obj, "payload", payload);
   obj += "}";

   lineOut = eventId + "\t" + obj;
   errorOut = "";
   return true;
}

#endif // SIGNALDESK_EVENT_SERIALIZER_MQH
