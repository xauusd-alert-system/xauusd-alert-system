//+------------------------------------------------------------------+
//| DiskOutbox.mqh - append-only, disk-backed event outbox           |
//|                                                                  |
//| Part of SignalDeskObserver (read-only MT5 telemetry agent).      |
//|                                                                  |
//| Storage: two flat files under MQL5\Files                         |
//|   SignalDeskObserver_outbox.jsonl - one event per line, format   |
//|       "<event_id>\t<json>"   (tab is the only separator;         |
//|       event ids are built from broker tickets and contain no     |
//|       tabs). The file is strictly append-only.                   |
//|   SignalDeskObserver_outbox.ack  - single integer: the number of |
//|       LEADING lines already acknowledged by the server (2xx).    |
//|                                                                  |
//| Guarantees:                                                      |
//|   * events are never deleted while unacknowledged;               |
//|   * rotation happens only when EVERY line is acked;              |
//|   * a crash between append and ack simply re-delivers the same   |
//|     event, which the server dedupes by deterministic event_id.   |
//+------------------------------------------------------------------+
#ifndef SIGNALDESK_DISK_OUTBOX_MQH
#define SIGNALDESK_DISK_OUTBOX_MQH

#define OUTBOX_FILE  "SignalDeskObserver_outbox.jsonl"
#define OUTBOX_STATE "SignalDeskObserver_outbox.ack"

class CDiskOutbox
{
private:
   string m_file;         // data file name (relative to MQL5\Files)
   string m_state;        // ack watermark file name
   long   m_acked;        // number of leading acked lines
   long   m_maxBytes;     // rotation threshold
   long   m_errors;       // persistent append/read error counter

public:
   //--- init: load watermark, validate filenames
   bool Init(const string file, const string state, const long maxBytes)
   {
      m_file = file;
      m_state = state;
      m_maxBytes = (maxBytes > 1024) ? maxBytes : 1048576;
      m_errors = 0;
      m_acked = 0;
      if(StringLen(m_file) == 0 || StringLen(m_state) == 0)
         return false;
      if(!LoadWatermark())
         return false;
      //--- self-heal: a watermark larger than the file means the file was
      //--- rotated/truncated AFTER those lines were acked, so clamp to total.
      long total = TotalLines();
      if(m_acked > total)
         m_acked = total;
      return true;
   }

   long Errors() const { return m_errors; }
   long AckedCount() const { return m_acked; }

   //--- total lines currently in the file
   long TotalLines() const
   {
      int handle = FileOpen(m_file, FILE_READ | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE)
         return 0;
      long count = 0;
      while(!FileIsEnding(handle))
      {
         FileReadString(handle);
         count++;
      }
      FileClose(handle);
      return count;
   }

   //--- append one "<event_id>\t<json>" line; returns false on I/O failure
   bool Append(const string line)
   {
      if(StringLen(line) == 0)
         return false;
      int handle = FileOpen(m_file, FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE)
      {
         m_errors++;
         return false;
      }
      FileSeek(handle, 0, SEEK_END);
      bool ok = (FileWrite(handle, line) > 0);
      if(!ok)
         m_errors++;
      FileClose(handle);
      return ok;
   }

   //--- read pending lines (after the watermark); caller resizes the array
   int ReadPending(string &lines[]) const
   {
      ArrayResize(lines, 0);
      int handle = FileOpen(m_file, FILE_READ | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE)
         return 0;
      long index = 0;
      while(!FileIsEnding(handle))
      {
         string text = FileReadString(handle);
         if(index >= m_acked)
         {
            int n = ArraySize(lines);
            ArrayResize(lines, n + 1);
            lines[n] = text;
         }
         index++;
      }
      FileClose(handle);
      return ArraySize(lines);
   }

   //--- read the event_id of every line (for the reconciler)
   int ReadAllIds(string &ids[]) const
   {
      ArrayResize(ids, 0);
      int handle = FileOpen(m_file, FILE_READ | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE)
         return 0;
      while(!FileIsEnding(handle))
      {
         string text = FileReadString(handle);
         int tab = StringFind(text, "\t");
         if(tab > 0)
         {
            int n = ArraySize(ids);
            ArrayResize(ids, n + 1);
            ids[n] = StringSubstr(text, 0, tab);
         }
      }
      FileClose(handle);
      return ArraySize(ids);
   }

   //--- advance the watermark by `count` lines (only after HTTP 2xx)
   bool Ack(const long count)
   {
      if(count <= 0)
         return true;
      long total = TotalLines();
      if(m_acked + count > total)
         return false;                 // never ack more than exists
      m_acked += count;
      bool saved = SaveWatermark();
      if(!saved)
         return false;
      //--- rotate only when EVERYTHING is acked (never delete unacked rows)
      if(m_acked > 0 && m_acked == total)
      {
         int handle = FileOpen(m_file, FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI);
         if(handle != INVALID_HANDLE)
         {
            long size = FileSize(handle);
            FileClose(handle);
            if(size > m_maxBytes)
            {
               FileDelete(m_file);
               m_acked = 0;
               SaveWatermark();
            }
         }
      }
      return true;
   }

private:
   bool LoadWatermark()
   {
      int handle = FileOpen(m_state, FILE_READ | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE)
         return true;                 // first run: watermark is zero
      string text = FileReadString(handle);
      FileClose(handle);
      m_acked = (long)StringToInteger(StringTrim(text));
      if(m_acked < 0)
         m_acked = 0;
      return true;
   }

   bool SaveWatermark()
   {
      int handle = FileOpen(m_state, FILE_WRITE | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE)
      {
         m_errors++;
         return false;
      }
      bool ok = (FileWrite(handle, IntegerToString(m_acked)) > 0);
      FileClose(handle);
      if(!ok)
         m_errors++;
      return ok;
   }
};

#endif // SIGNALDESK_DISK_OUTBOX_MQH
