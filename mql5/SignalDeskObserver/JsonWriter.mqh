//+------------------------------------------------------------------+
//| JsonWriter.mqh - minimal ASCII-safe JSON writer for MQL5         |
//|                                                                  |
//| Part of SignalDeskObserver (read-only MT5 telemetry agent).      |
//| No external dependencies. Output is pure ASCII: every character  |
//| above 0x7E is escaped as \uXXXX so the file/HTTP body survives   |
//| any codepage (FILE_ANSI on Windows terminals).                   |
//+------------------------------------------------------------------+
#ifndef SIGNALDESK_JSON_WRITER_MQH
#define SIGNALDESK_JSON_WRITER_MQH

//--- escape one string for JSON (returns content WITHOUT surrounding quotes)
string JsonEscape(const string value)
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
         case '\b': out += "\\b";  break;
         case '\f': out += "\\f";  break;
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

//--- quoted JSON string (null when the input is empty)
string JsonString(const string value)
{
   if(StringLen(value) == 0)
      return "null";
   return "\"" + JsonEscape(value) + "\"";
}

//--- double as JSON number; non-finite values become null
string JsonNumber(const double value, const int digits = 8)
{
   if(!MathIsValidNumber(value))
      return "null";
   return DoubleToString(value, digits);
}

//--- long as JSON integer
string JsonInt(const long value)
{
   return IntegerToString(value);
}

//--- bool as JSON literal
string JsonBool(const bool value)
{
   return value ? "true" : "false";
}

//--- object construction: field separator handling
void JsonField(string &obj, const string name, const string formattedValue)
{
   if(StringLen(obj) > 1)         // "{" already present
      obj += ",";
   obj += JsonString(name) + ":" + formattedValue;
}

void JsonFieldString(string &obj, const string name, const string value)
{
   JsonField(obj, name, JsonString(value));
}

void JsonFieldInt(string &obj, const string name, const long value)
{
   JsonField(obj, name, JsonInt(value));
}

void JsonFieldDouble(string &obj, const string name, const double value, const int digits = 8)
{
   JsonField(obj, name, JsonNumber(value, digits));
}

void JsonFieldBool(string &obj, const string name, const bool value)
{
   JsonField(obj, name, JsonBool(value));
}

//--- array construction (events array inside the envelope)
void JsonArrayBegin(string &arr)
{
   arr = "[";
}

void JsonArrayEnd(string &arr)
{
   arr += "]";
}

void JsonArrayElement(string &arr, const string formattedValue)
{
   if(StringLen(arr) > 1)
      arr += ",";
   arr += formattedValue;
}

#endif // SIGNALDESK_JSON_WRITER_MQH
