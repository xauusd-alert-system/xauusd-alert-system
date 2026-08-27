//+------------------------------------------------------------------+
//| AlertDispatcher.mqh - push / e-mail / WebRequest alert channels   |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-07; MQL5 book 7.5, pages    |
//| 1795-1838).                                                      |
//|                                                                  |
//| Three delivery paths, all built into the terminal:               |
//|  * SendNotification  - MetaQuotes ID push to the mobile app      |
//|    (works only for the account owner's terminal; rate-limited);  |
//|  * SendMail          - e-mail via the terminal SMTP settings;    |
//|  * WebRequest POST   - JSON hook into a Telegram bot / own       |
//|    backend. IMPORTANT (book p. 1795-1796): the URL must be       |
//|    allow-listed in Tools -> Options -> Expert Advisors ->        |
//|    "Allow WebRequest for listed URL" - it CANNOT be changed      |
//|    programmatically; the class fails closed (logs, no crash)     |
//|    when the URL is not whitelisted.                              |
//|                                                                  |
//| Channel failures never break trading: alerts are best-effort and |
//| every dispatch result is journaled so a silent channel is        |
//| detectable from the logs (full trace, task T-22).                |
//+------------------------------------------------------------------+
#ifndef NEUROTRADER_ALERT_DISPATCHER_MQH
#define NEUROTRADER_ALERT_DISPATCHER_MQH

//--- minimal ASCII-safe JSON escaping for the hook payload
string NeuroAlertJsonEscape(const string value)
{
   string out = "";
   int len = StringLen(value);
   for(int i = 0; i < len; i++)
   {
      ushort ch = StringGetCharacter(value, i);
      switch(ch)
      {
         case '"':  out += "\\\""; break;
         case '\\': out += "\\\\"; break;
         case '\n': out += "\\n";  break;
         case '\r': out += "\\r";  break;
         case '\t': out += "\\t";  break;
         default:
            if(ch < 0x20 || ch > 0x7E)
               out += StringFormat("\\u%04x", ch);
            else
               out += ShortToString(ch);
            break;
      }
   }
   return out;
}

class CAlertDispatcher
{
private:
   bool   m_usePush;
   bool   m_useMail;
   string m_hookUrl;       // empty = WebRequest channel disabled
   int    m_timeoutMs;
   int    m_pushSent;
   int    m_pushFailed;
   int    m_hookSent;
   int    m_hookFailed;

public:
   CAlertDispatcher(const bool usePush = true, const bool useMail = false,
                    const string hookUrl = "", const int timeoutMs = 5000)
   {
      m_usePush   = usePush;
      m_useMail   = useMail;
      m_hookUrl   = hookUrl;
      m_timeoutMs = (timeoutMs > 500) ? timeoutMs : 5000;
      m_pushSent  = 0;
      m_pushFailed= 0;
      m_hookSent  = 0;
      m_hookFailed= 0;
   }

   //+--------------------------------------------------------------+
   //| Push via the MetaQuotes cloud (SendNotification)             |
   //+--------------------------------------------------------------+
   bool SendPush(const string text)
   {
      if(!m_usePush)
         return false;
      bool ok = SendNotification(text);
      if(ok)
         m_pushSent++;
      else
      {
         m_pushFailed++;
         PrintFormat("[AlertDispatcher] SendNotification failed (err %d): %.120s",
                     GetLastError(), text);
      }
      return ok;
   }

   //+--------------------------------------------------------------+
   //| E-mail via the terminal SMTP settings (SendMail)              |
   //+--------------------------------------------------------------+
   bool SendEmail(const string subject, const string body)
   {
      if(!m_useMail)
         return false;
      bool ok = SendMail(subject, body);
      if(!ok)
         PrintFormat("[AlertDispatcher] SendMail failed (err %d): %.80s",
                     GetLastError(), subject);
      return ok;
   }

   //+--------------------------------------------------------------+
   //| JSON webhook (Telegram bot / own backend).                    |
   //| URL must be whitelisted in the terminal options (book 7.5).   |
   //+--------------------------------------------------------------+
   bool SendHook(const string jsonBody)
   {
      if(StringLen(m_hookUrl) == 0)
         return false;
      char data[];
      char result[];
      string resultHeaders;
      StringToCharArray(jsonBody, data, 0, StringLen(jsonBody), CP_UTF8);
      ArrayResize(data, ArraySize(data) - 1);   // strip the trailing NUL
      string headers = "Content-Type: application/json\r\n";
      ResetLastError();
      int status = WebRequest("POST", m_hookUrl, headers, m_timeoutMs,
                              data, result, resultHeaders);
      if(status == -1)
      {
         m_hookFailed++;
         PrintFormat("[AlertDispatcher] WebRequest failed (err %d) - is '%s' "
                     "whitelisted in Tools->Options->Expert Advisors?",
                     GetLastError(), m_hookUrl);
         return false;
      }
      if(status < 200 || status >= 300)
      {
         m_hookFailed++;
         PrintFormat("[AlertDispatcher] hook returned HTTP %d", status);
         return false;
      }
      m_hookSent++;
      return true;
   }

   //+--------------------------------------------------------------+
   //| Dispatch a trading signal through every enabled channel.     |
   //| `extraJson` carries fields for the hook payload.             |
   //+--------------------------------------------------------------+
   void DispatchSignal(const string asset, const int direction,
                       const double probability, const double lots,
                       const string extraJson = "")
   {
      string side = (direction > 0) ? "BUY" : "SELL";
      string text = StringFormat("%s %s signal p=%.2f lots=%.2f",
                                 asset, side, probability, lots);
      SendPush(text);
      if(m_useMail)
         SendEmail("NeuroTrader signal: " + asset,
                   text + "\r\n" + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS));
      if(StringLen(m_hookUrl) > 0)
      {
         string payload = "{";
         payload += "\"asset\":\"" + NeuroAlertJsonEscape(asset) + "\",";
         payload += "\"direction\":" + IntegerToString(direction) + ",";
         payload += "\"probability\":" + DoubleToString(probability, 4) + ",";
         payload += "\"lots\":" + DoubleToString(lots, 2) + ",";
         payload += "\"time_utc\":\"" + TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS) + "\"";
         if(StringLen(extraJson) > 0)
            payload += "," + extraJson;
         payload += "}";
         SendHook(payload);
      }
   }

   //+--------------------------------------------------------------+
   //| "Whitelist reminder" printed once at init (book p. 1795-96)  |
   //+--------------------------------------------------------------+
   string StartupHint() const
   {
      if(StringLen(m_hookUrl) == 0)
         return "alert hook disabled";
      return StringFormat("ensure the terminal options whitelist %s "
                          "for WebRequest, otherwise the hook fails closed",
                          m_hookUrl);
   }

   void Stats(int &pushSent, int &pushFailed, int &hookSent, int &hookFailed) const
   {
      pushSent = m_pushSent;
      pushFailed = m_pushFailed;
      hookSent = m_hookSent;
      hookFailed = m_hookFailed;
   }
};

#endif // NEUROTRADER_ALERT_DISPATCHER_MQH
