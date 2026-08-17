//+------------------------------------------------------------------+
//| HistoryReconciler.mqh - restart reconciliation of terminal facts  |
//|                                                                  |
//| Part of SignalDeskObserver (read-only MT5 telemetry agent).      |
//|                                                                  |
//| OnTradeTransaction callbacks can be missed (terminal restart,     |
//| EA re-attach, network hiccup). On startup and periodically the    |
//| reconciler scans HistorySelect(now - N days, now) and emits any   |
//| deal/order whose deterministic event_id is NOT already in the     |
//| outbox, tagged precision=history_reconciled.                      |
//|                                                                  |
//| It NEVER fabricates a fill: facts come only from terminal history |
//| (HistoryDealGet*/HistoryOrderGet*) and the idempotent event_id    |
//| makes duplicate emission safe.                                    |
//+------------------------------------------------------------------+
#ifndef SIGNALDESK_HISTORY_RECONCILER_MQH
#define SIGNALDESK_HISTORY_RECONCILER_MQH

#include "DiskOutbox.mqh"
#include "EventSerializer.mqh"
#include "SymbolResolver.mqh"

class CHistoryReconciler
{
private:
   string m_known[];      // sorted event ids already present in the outbox
   int    m_knownCount;

   bool BinarySearch(const string needle)
   {
      int lo = 0;
      int hi = m_knownCount - 1;
      while(lo <= hi)
      {
         int mid = (lo + hi) / 2;
         int cmp = StringCompare(m_known[mid], needle);
         if(cmp == 0)
            return true;
         if(cmp < 0)
            lo = mid + 1;
         else
            hi = mid - 1;
      }
      return false;
   }

public:
   //--- load known ids from the outbox and sort them for binary search
   bool LoadKnown(const CDiskOutbox &outbox)
   {
      ArrayResize(m_known, 0);
      outbox.ReadAllIds(m_known);
      m_knownCount = ArraySize(m_known);
      if(m_knownCount > 1)
         ArraySort(m_known);
      return true;
   }

   //--- scan history and append missing facts; returns events added
   int Reconcile(CDiskOutbox &outbox, const CSymbolResolver &resolver,
                 const SObserverContext &ctx, const long magicFilter,
                 const int days, const int maxEvents)
   {
      if(days < 1)
         return 0;
      LoadKnown(outbox);

      datetime from = TimeCurrent() - (datetime)days * 86400;
      datetime to = TimeCurrent();
      if(!HistorySelect(from, to))
         return 0;

      int added = 0;
      int skippedUnmapped = 0;
      int scannedDeals = HistoryDealsTotal();
      int scannedOrders = HistoryOrdersTotal();

      // Tickets are unique within one scan, so new ids need no lookups until
      // the scan ends; collect them and sort once instead of re-sorting the
      // whole known set on every insert.
      string newIds[];
      string line;
      string error;
      for(int i = 0; i < scannedDeals && added < maxEvents; i++)
      {
         ulong ticket = HistoryDealGetTicket(i);
         if(ticket == 0 || !HistoryDealSelect(ticket))
            continue;
         if(magicFilter != 0 && HistoryDealGetInteger(ticket, DEAL_MAGIC) != magicFilter)
            continue;
         string expected = CanonicalEventId(ctx, "deal", IntegerToString(ticket));
         if(BinarySearch(expected))
            continue;
         if(!SerializeDealEvent(ctx, ticket, resolver, true, line, error))
         {
            skippedUnmapped++;          // unmapped symbol or history race
            continue;
         }
         if(outbox.Append(line))
         {
            added++;
            int n = ArraySize(newIds);
            ArrayResize(newIds, n + 1);
            newIds[n] = expected;
         }
      }

      for(int i = 0; i < scannedOrders && added < maxEvents; i++)
      {
         ulong ticket = HistoryOrderGetTicket(i);
         if(ticket == 0 || !HistoryOrderSelect(ticket))
            continue;
         if(magicFilter != 0 && HistoryOrderGetInteger(ticket, ORDER_MAGIC) != magicFilter)
            continue;
         string expected = CanonicalEventId(ctx, "order", IntegerToString(ticket));
         if(BinarySearch(expected))
            continue;
         if(!SerializeOrderEvent(ctx, ticket, resolver, true, line, error))
         {
            skippedUnmapped++;
            continue;
         }
         if(outbox.Append(line))
         {
            added++;
            int n = ArraySize(newIds);
            ArrayResize(newIds, n + 1);
            newIds[n] = expected;
         }
      }

      //--- merge new ids into the sorted known set (single sort)
      if(ArraySize(newIds) > 0)
      {
         int total = m_knownCount + ArraySize(newIds);
         ArrayResize(m_known, total);
         for(int i = 0; i < ArraySize(newIds); i++)
            m_known[m_knownCount + i] = newIds[i];
         m_knownCount = total;
         ArraySort(m_known);
      }

      //--- one summary fact per reconciliation run
      if(SerializeReconcileEvent(ctx, scannedDeals, scannedOrders, added,
                                 skippedUnmapped, error, line, error))
         outbox.Append(line);
      return added;
   }
};

#endif // SIGNALDESK_HISTORY_RECONCILER_MQH
