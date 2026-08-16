//+------------------------------------------------------------------+
//| ObserverEA.mq5 - READ-ONLY MT5 observer / telemetry agent         |
//|                                                                  |
//| SignalDeskObserver wave 1 (plan: "первый MQL5 EA — только        |
//| наблюдатель и диагностический агент в demo mode. Он НЕ открывает |
//| сделки.").                                                       |
//|                                                                  |
//| Responsibilities:                                                |
//|   * OnTradeTransaction  - record broker facts (deal/order/        |
//|     position) into a disk-backed append-only outbox. NO network   |
//|     I/O and NO trade calls inside the callback.                   |
//|   * OnTimer             - flush the outbox over HTTPS to the      |
//|     allow-listed ledger URL, send heartbeats, run history         |
//|     reconciliation.                                               |
//|   * HistoryReconciler   - after restart, re-emit missing terminal |
//|     facts with precision=history_reconciled.                      |
//|                                                                  |
//| Hard guarantees (enforced by acceptance checklist):               |
//|   * No trade calls of any kind anywhere (see README grep check).  |
//|   * Demo/contest accounts only; real accounts -> INIT_FAILED.     |
//|   * Unknown broker symbols are SKIPPED, never guessed.            |
//|   * Events are never deleted before the server acks (2xx).        |
//+------------------------------------------------------------------+
#property copyright "xauusd-alert-system"
#property link      "https://github.com/xauusd-alert-system"
#property version   "1.00"
#property description "READ-ONLY MT5 observer: broker execution facts -> ledger. No trading."

#include "SymbolResolver.mqh"
#include "DiskOutbox.mqh"
#include "EventSerializer.mqh"
#include "HistoryReconciler.mqh"

//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
input string InpBrokerSymbolMap  = "XAUUSD=GOLD,XAGUSD=SILVER,BTCUSD=BITCOIN,EURUSD=EURUSD,GBPUSD=GBPUSD"; // canonical=broker pairs
input long   InpMagicFilter      = 777111;      // 0 = observe all magic numbers
input string InpLedgerUrl        = "https://ledger.example.com/api/ledger/ingest"; // allow-listed HTTPS URL
input string InpLedgerToken      = "";          // bearer token for the ingest endpoint
input int    InpFlushSeconds     = 15;          // outbox flush interval
input int    InpHeartbeatSeconds = 600;         // heartbeat interval
input int    InpReconcileDays    = 30;          // history scan depth
input int    InpReconcileHours   = 24;          // reconciliation interval
input int    InpOutboxMaxBytes   = 1048576;     // rotate only when fully acked
input bool   InpObserveRequests  = false;       // observe REQUEST transactions (optional)

//+------------------------------------------------------------------+
//| Globals                                                          |
//+------------------------------------------------------------------+
CSymbolResolver   g_resolver;
CDiskOutbox       g_outbox;
CHistoryReconciler g_reconciler;
SObserverContext  g_ctx;
bool              g_ready = false;
long              g_startTick = 0;
datetime          g_lastFlush = 0;
datetime          g_lastHeartbeat = 0;
datetime          g_lastReconcile = 0;
int               g_skippedUnmapped = 0;
int               g_callbackErrors = 0;

//+------------------------------------------------------------------+
//| Helpers                                                          |
//+------------------------------------------------------------------+
string AccountModeString(const int tradeMode)
{
   if(tradeMode == ACCOUNT_TRADE_MODE_DEMO)
      return "demo";
   if(tradeMode == ACCOUNT_TRADE_MODE_CONTEST)
      return "contest";
   return "real";
}

bool BuildContext(SObserverContext &ctx)
{
   ctx.login = AccountInfoInteger(ACCOUNT_LOGIN);
   int tradeMode = (int)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   ctx.accountMode = AccountModeString(tradeMode);
   ctx.accountFingerprint = ctx.accountMode + ":" + IntegerToString(ctx.login);
   return true;
}

void FlushOutbox()
{
   string lines[];
   int count = g_outbox.ReadPending(lines);
   if(count == 0)
      return;

   //--- build the envelope JSON: {"schema_version":1,"producer":"mt5_observer",
   //---  "account_fingerprint":...,"batch_id":...,"sent_at_utc_ms":...,"events":[...]}
   string events = "[";
   bool first = true;
   for(int i = 0; i < count; i++)
   {
      int tab = StringFind(lines[i], "\t");
      if(tab < 0)
         continue;                       // malformed line: leave it pending
      string json = StringSubstr(lines[i], tab + 1);
      if(!first)
         events += ",";
      first = false;
      events += json;
   }
   events += "]";

   string body = "{";
   JsonFieldInt(body, "schema_version", 1);
   JsonFieldString(body, "producer", SERIALIZER_SOURCE);
   JsonFieldString(body, "account_fingerprint", g_ctx.accountFingerprint);
   JsonFieldString(body, "batch_id",
                   IntegerToString((long)GetTickCount64()) + IntegerToString(rand()));
   JsonFieldInt(body, "sent_at_utc_ms", (long)TimeCurrent() * 1000);
   JsonField(body, "events", events);
   body += "}";

   //--- UTF-8 bytes; WebRequest is the ONLY network call and runs from OnTimer
   uchar data[];
   int written = StringToCharArray(body, data, 0, StringLen(body), CP_UTF8);
   if(written <= 0)
   {
      Print("Observer: failed to encode envelope (", written, ")");
      return;
   }
   ArrayResize(data, written);

   string headers = "Authorization: Bearer " + InpLedgerToken + "\r\n"
                  + "Content-Type: application/json\r\n";
   uchar resultData[];
   string resultHeaders;
   string resultBody = "";

   ResetLastError();
   bool ok = WebRequest("POST", InpLedgerUrl, headers, 5000, data, resultData, resultHeaders);
   if(ok)
   {
      resultBody = CharArrayToString(resultData, 0, WHOLE_ARRAY, CP_UTF8);
      //--- 2xx only: WebRequest true means transport success, check HTTP status
      int status = 0;
      int p = StringFind(resultHeaders, "HTTP/");
      if(p >= 0)
         status = (int)StringToInteger(StringSubstr(resultHeaders, p + 9));
      if(status >= 200 && status < 300)
      {
         if(!g_outbox.Ack(count))
            Print("Observer: ack failed after 2xx; events will be re-delivered (idempotent)");
      }
      else
         PrintFormat("Observer: ingest HTTP %d: %s", status, resultBody);
   }
   else
   {
      PrintFormat("Observer: WebRequest failed err=%d (URL must be allow-listed in terminal options)",
                  GetLastError());
   }
}

void MaybeHeartbeat()
{
   if(TimeCurrent() - g_lastHeartbeat < InpHeartbeatSeconds)
      return;
   g_lastHeartbeat = TimeCurrent();
   long pending = g_outbox.TotalLines() - g_outbox.AckedCount();
   if(pending < 0)
      pending = 0;
   string line, error;
   if(SerializeHeartbeatEvent(g_ctx, (long)(TimeCurrent() - g_startTick),
                              pending, g_outbox.Errors(), line, error))
      g_outbox.Append(line);
}

void MaybeReconcile()
{
   if(TimeCurrent() - g_lastReconcile < (datetime)InpReconcileHours * 3600)
      return;
   g_lastReconcile = TimeCurrent();
   int added = g_reconciler.Reconcile(g_outbox, g_resolver, g_ctx,
                                      InpMagicFilter, InpReconcileDays, 5000);
   PrintFormat("Observer: reconciliation scanned history, added %d missing facts", added);
}

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
{
   if(!BuildContext(g_ctx))
   {
      Print("Observer: cannot read account info");
      return INIT_FAILED;
   }
   if(g_ctx.accountMode == "real")
   {
      Print("Observer: REFUSING to run on a REAL account (demo/contest only)");
      return INIT_FAILED;
   }
   if(StringFind(InpLedgerUrl, "https://") != 0)
   {
      Print("Observer: LedgerUrl must be an https:// URL (WebRequest allowlist)");
      return INIT_FAILED;
   }
   if(StringLen(InpLedgerToken) == 0)
   {
      Print("Observer: LedgerToken is empty; ingest will be rejected by the server");
      return INIT_FAILED;
   }
   if(!g_resolver.SetMapping(InpBrokerSymbolMap))
   {
      Print("Observer: BrokerSymbolMap is empty, malformed or ambiguous");
      return INIT_FAILED;
   }
   if(!g_outbox.Init(OUTBOX_FILE, OUTBOX_STATE, InpOutboxMaxBytes))
   {
      Print("Observer: outbox init failed");
      return INIT_FAILED;
   }
   EventSetTimer(1);
   g_startTick = TimeCurrent();
   g_lastFlush = 0;
   g_lastHeartbeat = 0;
   g_lastReconcile = 0;
   g_ready = true;
   PrintFormat("Observer ready: account=%s fingerprint=%s magic=%I64d symbols=%d",
               g_ctx.accountMode, g_ctx.accountFingerprint, InpMagicFilter, g_resolver.Count());
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| OnTradeTransaction - short, local, NO network, NO trade calls     |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   if(!g_ready)
      return;

   string line, error;
   switch(trans.type)
   {
      case TRADE_TRANSACTION_DEAL_ADD:
         //--- select first so the ticket-form getters are safe; a history race
         //--- here simply defers the fact to the next reconciliation pass.
         if(trans.deal > 0 && HistoryDealSelect(trans.deal) &&
            (InpMagicFilter == 0 ||
             HistoryDealGetInteger(trans.deal, DEAL_MAGIC) == InpMagicFilter))
         {
            if(!SerializeDealEvent(g_ctx, trans.deal, g_resolver, false, line, error))
               g_skippedUnmapped++;
            else if(!g_outbox.Append(line))
               g_callbackErrors++;
         }
         break;

      case TRADE_TRANSACTION_HISTORY_ADD:
         if(trans.order > 0 && HistoryOrderSelect(trans.order) &&
            (InpMagicFilter == 0 ||
             HistoryOrderGetInteger(trans.order, ORDER_MAGIC) == InpMagicFilter))
         {
            if(!SerializeOrderEvent(g_ctx, trans.order, g_resolver, false, line, error))
               g_skippedUnmapped++;
            else if(!g_outbox.Append(line))
               g_callbackErrors++;
         }
         break;

      case TRADE_TRANSACTION_POSITION:
         if(trans.position > 0 && PositionSelectByTicket(trans.position) &&
            (InpMagicFilter == 0 ||
             PositionGetInteger(POSITION_MAGIC) == InpMagicFilter))
         {
            if(!SerializePositionEvent(g_ctx, trans.position, g_resolver, line, error))
               g_skippedUnmapped++;
            else if(!g_outbox.Append(line))
               g_callbackErrors++;
         }
         break;

      case TRADE_TRANSACTION_REQUEST:
         if(InpObserveRequests && (InpMagicFilter == 0 || request.magic == InpMagicFilter))
         {
            if(!SerializeRequestEvent(g_ctx, request, result, (long)trans.time, line, error))
               g_callbackErrors++;
            else if(!g_outbox.Append(line))
               g_callbackErrors++;
         }
         break;

      default:
         break;   // DEAL_DELETE / ORDER_ADD / ORDER_DELETE / others: ignored
   }
}

//+------------------------------------------------------------------+
//| OnTimer                                                          |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!g_ready)
      return;

   if(TimeCurrent() - g_lastFlush >= InpFlushSeconds)
   {
      g_lastFlush = TimeCurrent();
      FlushOutbox();
   }
   MaybeHeartbeat();
   MaybeReconcile();
}

//+------------------------------------------------------------------+
//| OnDeinit                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(g_ready)
   {
      FlushOutbox();                    // best-effort final delivery
      g_ready = false;
   }
   EventKillTimer();
   PrintFormat("Observer stopped: skipped_unmapped=%d callback_errors=%d",
               g_skippedUnmapped, g_callbackErrors);
}
