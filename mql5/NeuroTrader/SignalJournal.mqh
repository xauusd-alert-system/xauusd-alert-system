//+------------------------------------------------------------------+
//| SignalJournal.mqh - SQLite full-trace audit journal              |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-22; MQL5 book 7.6).         |
//|                                                                  |
//| The complete signal lifecycle is persisted so any alert can be   |
//| reconstructed months later (schema mirrors the Python ledgers):  |
//|                                                                  |
//|   features_hash -> model decision -> alert -> execution ->       |
//|   retcode -> position result                                     |
//|                                                                  |
//| Table `signal_trace` (append-only, one row per signal):          |
//|   signal_id (PK), ts_utc, asset, direction, probability,         |
//|   features_hash, features_json, decision, trade_level,          |
//|   alert_push, alert_hook, lots, entry, sl, tp, retcode,          |
//|   order_ticket, deal_ticket, pnl, status, updated_utc           |
//|                                                                  |
//| Idempotency: INSERT OR IGNORE on signal_id - a restart re-runs   |
//| the UPDATE path, never duplicates (dedup by primary key, book    |
//| 7.6 prepared-statement pattern).                                |
//+------------------------------------------------------------------+
#ifndef NEUROTRADER_SIGNAL_JOURNAL_MQH
#define NEUROTRADER_SIGNAL_JOURNAL_MQH

class CSignalJournal
{
private:
   int    m_db;
   string m_fileName;
   int    m_rows;

   bool EnsureSchema()
   {
      if(!DatabaseTableExists(m_db, "signal_trace"))
      {
         string ddl =
            "CREATE TABLE signal_trace ("
            "  signal_id     TEXT PRIMARY KEY,"
            "  ts_utc        INTEGER NOT NULL,"
            "  asset         TEXT NOT NULL,"
            "  direction     INTEGER NOT NULL,"
            "  probability   REAL,"
            "  features_hash TEXT,"
            "  features_json TEXT,"
            "  decision      TEXT,"
            "  trade_level   REAL,"
            "  alert_push    INTEGER DEFAULT 0,"
            "  alert_hook    INTEGER DEFAULT 0,"
            "  lots          REAL,"
            "  entry         REAL,"
            "  sl            REAL,"
            "  tp            REAL,"
            "  retcode       INTEGER,"
            "  order_ticket  INTEGER,"
            "  deal_ticket   INTEGER,"
            "  pnl           REAL,"
            "  status        TEXT,"
            "  updated_utc   INTEGER"
            ");";
         if(!DatabaseExecute(m_db, ddl))
         {
            PrintFormat("[SignalJournal] CREATE TABLE failed (err %d)",
                        GetLastError());
            return false;
         }
      }
      return true;
   }

   //+--------------------------------------------------------------+
   //| Rows affected by the most recent write (SQLite changes()).    |
   //| Write statements produce no result rows, so DatabaseRead's    |
   //| false is EXPECTED - the rowcount is the real success signal.  |
   //+--------------------------------------------------------------+
   int AffectedRows()
   {
      int request = DatabasePrepare(m_db, "SELECT changes()");
      if(request == INVALID_HANDLE)
         return -1;
      long n = 0;
      if(DatabaseRead(request))
         DatabaseColumnLong(request, 0, n);
      DatabaseFinalize(request);
      return (int)n;
   }

   //+--------------------------------------------------------------+
   //| Execute a bound write statement; returns affected rows.       |
   //+--------------------------------------------------------------+
   int ExecuteWrite(int request)
   {
      if(request == INVALID_HANDLE)
         return -1;
      DatabaseRead(request);            // executes the statement
      DatabaseFinalize(request);
      return AffectedRows();
   }

public:
   CSignalJournal(const string fileName = "neurotrader_trace.sqlite")
   {
      m_db = -1;
      m_fileName = fileName;
      m_rows = 0;
   }

   ~CSignalJournal()
   {
      if(m_db >= 0)
         DatabaseClose(m_db);
   }

   bool Init()
   {
      ResetLastError();
      m_db = DatabaseOpen(m_fileName,
                          DATABASE_OPEN_READWRITE | DATABASE_OPEN_CREATE);
      if(m_db < 0)
      {
         PrintFormat("[SignalJournal] cannot open %s (err %d)",
                     m_fileName, GetLastError());
         return false;
      }
      return EnsureSchema();
   }

   bool IsOpen() const { return (m_db >= 0); }

   //+--------------------------------------------------------------+
   //| Stage 1: the decision row (features -> model outcome).         |
   //| INSERT OR IGNORE keeps the first sighting (idempotent).        |
   //+--------------------------------------------------------------+
   bool LogDecision(const string signalId, const string asset,
                    const int direction, const double probability,
                    const string featuresHash, const string featuresJson,
                    const string decision, const double tradeLevel)
   {
      if(m_db < 0)
         return false;
      int request = DatabasePrepare(m_db,
         "INSERT OR IGNORE INTO signal_trace "
         "(signal_id, ts_utc, asset, direction, probability, features_hash, "
         " features_json, decision, trade_level, status, updated_utc) "
         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'decided', ?)");
      if(request == INVALID_HANDLE)
         return false;
      DatabaseBind(request, 1, signalId);
      DatabaseBind(request, 2, (long)TimeGMT());
      DatabaseBind(request, 3, asset);
      DatabaseBind(request, 4, direction);
      DatabaseBind(request, 5, probability);
      DatabaseBind(request, 6, featuresHash);
      DatabaseBind(request, 7, featuresJson);
      DatabaseBind(request, 8, decision);
      DatabaseBind(request, 9, tradeLevel);
      DatabaseBind(request, 10, (long)TimeGMT());
      int affected = ExecuteWrite(request);
      if(affected > 0)          // 0 = ignored duplicate (idempotent retry)
         m_rows++;
      return affected >= 0;     // statement ran
   }

   //+--------------------------------------------------------------+
   //| Stage 2: alert delivery flags (push / hook results).          |
   //+--------------------------------------------------------------+
   bool LogAlerts(const string signalId, const bool pushOk, const bool hookOk)
   {
      if(m_db < 0)
         return false;
      int request = DatabasePrepare(m_db,
         "UPDATE signal_trace SET alert_push = ?, alert_hook = ?, "
         "updated_utc = ? WHERE signal_id = ?");
      if(request == INVALID_HANDLE)
         return false;
      DatabaseBind(request, 1, pushOk ? 1 : 0);
      DatabaseBind(request, 2, hookOk ? 1 : 0);
      DatabaseBind(request, 3, (long)TimeGMT());
      DatabaseBind(request, 4, signalId);
      return ExecuteWrite(request) > 0;
   }

   //+--------------------------------------------------------------+
   //| Stage 3: the execution outcome (order/retcode/position).      |
   //+--------------------------------------------------------------+
   bool LogExecution(const string signalId, const double lots,
                     const double entry, const double sl, const double tp,
                     const uint retcode, const ulong orderTicket,
                     const ulong dealTicket, const string status)
   {
      if(m_db < 0)
         return false;
      int request = DatabasePrepare(m_db,
         "UPDATE signal_trace SET lots = ?, entry = ?, sl = ?, tp = ?, "
         "retcode = ?, order_ticket = ?, deal_ticket = ?, status = ?, "
         "updated_utc = ? WHERE signal_id = ?");
      if(request == INVALID_HANDLE)
         return false;
      DatabaseBind(request, 1, lots);
      DatabaseBind(request, 2, entry);
      DatabaseBind(request, 3, sl);
      DatabaseBind(request, 4, tp);
      DatabaseBind(request, 5, (long)retcode);
      DatabaseBind(request, 6, (long)orderTicket);
      DatabaseBind(request, 7, (long)dealTicket);
      DatabaseBind(request, 8, status);
      DatabaseBind(request, 9, (long)TimeGMT());
      DatabaseBind(request, 10, signalId);
      return ExecuteWrite(request) > 0;
   }

   //+--------------------------------------------------------------+
   //| Stage 4: the final position result (closed PnL).               |
   //+--------------------------------------------------------------+
   bool LogResult(const string signalId, const double pnl,
                  const string status)
   {
      if(m_db < 0)
         return false;
      int request = DatabasePrepare(m_db,
         "UPDATE signal_trace SET pnl = ?, status = ?, updated_utc = ? "
         "WHERE signal_id = ?");
      if(request == INVALID_HANDLE)
         return false;
      DatabaseBind(request, 1, pnl);
      DatabaseBind(request, 2, status);
      DatabaseBind(request, 3, (long)TimeGMT());
      DatabaseBind(request, 4, signalId);
      return ExecuteWrite(request) > 0;
   }

   int RowCount() const { return m_rows; }
};

#endif // NEUROTRADER_SIGNAL_JOURNAL_MQH
