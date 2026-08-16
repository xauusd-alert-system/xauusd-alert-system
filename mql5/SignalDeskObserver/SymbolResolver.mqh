//+------------------------------------------------------------------+
//| SymbolResolver.mqh - canonical <-> broker symbol mapping          |
//|                                                                  |
//| Part of SignalDeskObserver (read-only MT5 telemetry agent).      |
//|                                                                  |
//| Mapping is configured via the EA input BrokerSymbolMap, e.g.     |
//|   "XAUUSD=GOLD,XAGUSD=SILVER,BTCUSD=BITCOIN,EURUSD=EURUSD,       |
//|    GBPUSD=GBPUSD"                                                |
//|                                                                  |
//| Fail-closed policy (plan): never GUESS a mapping. An event whose |
//| broker symbol is not in the map is skipped and counted, never    |
//| emitted with a fabricated asset_key. Ambiguous mappings (the     |
//| same broker symbol appearing twice) invalidate the whole map.    |
//+------------------------------------------------------------------+
#ifndef SIGNALDESK_SYMBOL_RESOLVER_MQH
#define SIGNALDESK_SYMBOL_RESOLVER_MQH

class CSymbolResolver
{
private:
   string m_broker[];     // broker symbol
   string m_canonical[];  // canonical asset key (parallel array)
   int    m_count;        // number of valid pairs (-1 = invalid map)

public:
   CSymbolResolver()
   {
      m_count = 0;
   }

   //--- parse "CANON=BROKER,CANON=BROKER,..."; returns false on ambiguity
   bool SetMapping(const string spec)
   {
      ArrayResize(m_broker, 0);
      ArrayResize(m_canonical, 0);
      m_count = 0;
      if(StringLen(spec) == 0)
         return false;

      string pairs[];
      int n = StringSplit(spec, ',', pairs);
      for(int i = 0; i < n; i++)
      {
         string pair = StringTrim(pairs[i]);
         if(StringLen(pair) == 0)
            continue;
         string kv[];
         int k = StringSplit(pair, '=', kv);
         if(k != 2)
            return false;                       // malformed pair
         string canonical = StringTrim(kv[0]);
         string broker = StringTrim(kv[1]);
         if(StringLen(canonical) == 0 || StringLen(broker) == 0)
            return false;
         //--- ambiguity check: same broker symbol twice, or duplicate canonical
         for(int j = 0; j < m_count; j++)
         {
            if(m_broker[j] == broker || m_canonical[j] == canonical)
               return false;
         }
         int idx = m_count;
         ArrayResize(m_broker, idx + 1);
         ArrayResize(m_canonical, idx + 1);
         m_broker[idx] = broker;
         m_canonical[idx] = canonical;
         m_count++;
      }
      return (m_count > 0);
   }

   bool IsValid() const
   {
      return (m_count > 0);
   }

   //--- broker -> canonical; false when unknown (caller must skip the event)
   bool BrokerToCanonical(const string brokerSymbol, string &canonicalOut)
   {
      if(m_count <= 0)
         return false;
      for(int i = 0; i < m_count; i++)
      {
         if(m_broker[i] == brokerSymbol)
         {
            canonicalOut = m_canonical[i];
            return true;
         }
      }
      return false;
   }

   //--- canonical -> broker; false when unknown
   bool CanonicalToBroker(const string canonicalKey, string &brokerOut)
   {
      if(m_count <= 0)
         return false;
      for(int i = 0; i < m_count; i++)
      {
         if(m_canonical[i] == canonicalKey)
         {
            brokerOut = m_broker[i];
            return true;
         }
      }
      return false;
   }

   int Count() const
   {
      return m_count;
   }
};

#endif // SIGNALDESK_SYMBOL_RESOLVER_MQH
