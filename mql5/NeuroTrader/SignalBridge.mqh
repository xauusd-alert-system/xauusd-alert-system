//+------------------------------------------------------------------+
//| SignalBridge.mqh - SQLite bridge to the Python ML pipeline       |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-16; MQL5 book 7.6 + 7.9).   |
//|                                                                  |
//| The book's division of responsibility for ML systems: Python     |
//| owns data + inference (the MetaTrader5 package has no events and |
//| no indicator buffers, book p. 1998-2000), MQL5 owns execution.   |
//| The hand-off is a SQLite table in the terminal's MQL5\Files:     |
//|                                                                  |
//|   Python (execution/signal_bridge.py) WRITES rows:               |
//|     intent_id, created_at_utc, asset, direction, probability,    |
//|     entry/sl/tp, expires_at_utc, status='new', features_hash     |
//|                                                                  |
//|   This reader POLLs pending rows from OnTimer (never OnTick: no  |
//|   SQLite I/O inside the tick path) and flips status:             |
//|     new -> consumed -> executed | skipped | failed               |
//|                                                                  |
//| Safety:                                                          |
//|  * schema_version is checked on open - mismatch = refuse to run  |
//|    (fail-closed, like the observer wire contract);               |
//|  * expired rows are marked 'expired', never executed;            |
//|  * autotrading from Python stays disabled in the terminal (the   |
//|    10027 error is a FEATURE here: only this EA sends orders).    |
//|                                                                  |
//| The database is opened READWRITE from MQL5\Files; DatabaseOpen   |
//| with common-folder flag is NOT used so multiple terminals on one |
//| machine keep separate bridges (book 7.6 file scoping).           |
//+------------------------------------------------------------------+
#ifndef NEUROTRADER_SIGNAL_BRIDGE_MQH
#define NEUROTRADER_SIGNAL_BRIDGE_MQH

#define NEURO_BRIDGE_SCHEMA_VERSION 1

struct SBridgeSignal
{
   string intentId;
   long   createdAtUtc;
   string asset;
   int    direction;        // +1 / -1
   double probability;
   double entryPrice;
   double slPrice;
   double tpPrice;
   long   expiresAtUtc;
   string featuresHash;
   string comment;
};

class CSignalBridge
{
private:
   int    m_db;             // database handle (-1 = closed)
   string m_fileName;       // inside MQL5\Files
   int    m_consumed;
   int    m_markedExpired;

   bool OpenDb()
   {
      if(m_db >= 0)
         return true;
      ResetLastError();
      m_db = DatabaseOpen(m_fileName, DATABASE_OPEN_READWRITE);
      if(m_db < 0)
      {
         PrintFormat("[SignalBridge] cannot open %s (err %d) - the Python "
                     "writer must create it first", m_fileName, GetLastError());
         return false;
      }
      return true;
   }

   bool SchemaVersionOk()
   {
      //--- read schema_version from bridge_meta when present; the Python
      //--- writer keeps the version in table bridge_meta(key,value).
      int request = DatabasePrepare(m_db,
         "SELECT value FROM bridge_meta WHERE key='schema_version'");
      if(request == INVALID_HANDLE)
         return true;   // no meta table: accept (writer created by older tool)
      int version = -1;
      if(DatabaseRead(request))
      {
         string value = "";
         DatabaseColumnText(request, 0, value);
         version = (int)StringToInteger(value);
      }
      DatabaseFinalize(request);
      if(version < 0)
         return true;
      if(version != NEURO_BRIDGE_SCHEMA_VERSION)
      {
         PrintFormat("[SignalBridge] schema version mismatch: db=%d, EA=%d - "
                     "refusing to read signals (fail-closed)",
                     version, NEURO_BRIDGE_SCHEMA_VERSION);
         return false;
      }
      return true;
   }

   //+--------------------------------------------------------------+
   //| Rows affected by the most recent write on this connection.    |
   //| SQLite's changes() function - portable across every SQLite    |
   //| version MT5 can bundle (write statements themselves produce   |
   //| no result rows, so this is the only reliable rowcount).       |
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
   //| Execute a bound write statement (UPDATE/INSERT). Returns the   |
   //| affected-row count: DatabaseRead on a write returns false     |
   //| because writes have no result rows - that is EXPECTED, so     |
   //| success is measured through the rowcount instead.             |
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
   CSignalBridge(const string fileName = "ml_signal_bridge.sqlite")
   {
      m_db = -1;
      m_fileName = fileName;
      m_consumed = 0;
      m_markedExpired = 0;
   }

   ~CSignalBridge()
   {
      if(m_db >= 0)
         DatabaseClose(m_db);
   }

   //+--------------------------------------------------------------+
   //| Initialization: open + verify schema. Called once from OnInit |
   //+--------------------------------------------------------------+
   bool Init()
   {
      if(!OpenDb())
         return false;
      if(!SchemaVersionOk())
      {
         DatabaseClose(m_db);
         m_db = -1;
         return false;
      }
      if(!DatabaseTableExists(m_db, "ml_signals"))
      {
         Print("[SignalBridge] table ml_signals missing - run the Python "
               "writer once to create the schema");
         return false;
      }
      return true;
   }

   bool IsOpen() const { return (m_db >= 0); }

   //+--------------------------------------------------------------+
   //| Fetch one pending (new, unexpired) signal, oldest first.      |
   //| The row is immediately marked 'consumed' so a crash between   |
   //| fetch and execution cannot double-trade it (idempotent loop). |
   //+--------------------------------------------------------------+
   bool NextPending(SBridgeSignal &signal)
   {
      if(!OpenDb())
         return false;
      long nowUtc = (long)TimeGMT();

      int request = DatabasePrepare(m_db,
         "SELECT intent_id, created_at_utc, asset, direction, probability, "
         "       entry_price, sl_price, tp_price, expires_at_utc, "
         "       features_hash, comment "
         "FROM ml_signals WHERE status = 'new' "
         "AND (expires_at_utc IS NULL OR expires_at_utc > ?) "
         "ORDER BY created_at_utc ASC LIMIT 1");
      if(request == INVALID_HANDLE)
         return false;
      DatabaseBind(request, 1, nowUtc);

      bool found = false;
      if(DatabaseRead(request))
      {
         DatabaseColumnText(request, 0, signal.intentId);
         DatabaseColumnLong(request, 1, signal.createdAtUtc);
         DatabaseColumnText(request, 2, signal.asset);
         DatabaseColumnLong(request, 3, signal.direction);
         DatabaseColumnDouble(request, 4, signal.probability);
         DatabaseColumnDouble(request, 5, signal.entryPrice);
         DatabaseColumnDouble(request, 6, signal.slPrice);
         DatabaseColumnDouble(request, 7, signal.tpPrice);
         DatabaseColumnLong(request, 8, signal.expiresAtUtc);
         DatabaseColumnText(request, 9, signal.featuresHash);
         DatabaseColumnText(request, 10, signal.comment);
         found = true;
      }
      DatabaseFinalize(request);
      if(!found)
         return false;

      MarkStatus(signal.intentId, "consumed", "picked by EA");
      m_consumed++;
      return true;
   }

   //+--------------------------------------------------------------+
   //| Flip the status of a consumed row (executed/skipped/failed).  |
   //+--------------------------------------------------------------+
   bool MarkStatus(const string intentId, const string status,
                   const string comment)
   {
      if(!OpenDb() || intentId == "")
         return false;
      int request = DatabasePrepare(m_db,
         "UPDATE ml_signals SET status = ?, updated_at_utc = ?, "
         "comment = ? WHERE intent_id = ?");
      if(request == INVALID_HANDLE)
         return false;
      DatabaseBind(request, 1, status);
      DatabaseBind(request, 2, (long)TimeGMT());
      DatabaseBind(request, 3, comment);
      DatabaseBind(request, 4, intentId);
      return ExecuteWrite(request) > 0;   // the row actually transitioned
   }

   //+--------------------------------------------------------------+
   //| Housekeeping: expire stale 'new' rows (housekeeping timer).   |
   //| Returns the number of rows flipped to 'expired'.              |
   //+--------------------------------------------------------------+
   int ExpireStale()
   {
      if(!OpenDb())
         return 0;
      int request = DatabasePrepare(m_db,
         "UPDATE ml_signals SET status = 'expired', updated_at_utc = ? "
         "WHERE status = 'new' AND expires_at_utc IS NOT NULL "
         "AND expires_at_utc <= ?");
      if(request == INVALID_HANDLE)
         return 0;
      DatabaseBind(request, 1, (long)TimeGMT());
      DatabaseBind(request, 2, (long)TimeGMT());
      int changed = ExecuteWrite(request);
      if(changed > 0)
         m_markedExpired += changed;
      return (changed > 0) ? changed : 0;
   }

   void Stats(int &consumed, int &markedExpired) const
   {
      consumed = m_consumed;
      markedExpired = m_markedExpired;
   }
};

#endif // NEUROTRADER_SIGNAL_BRIDGE_MQH
