//+------------------------------------------------------------------+
//| FeatureEngine.mqh - indicator-handle feature pipeline            |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-14; MQL5 book 5.5, pages    |
//| 762-835).                                                        |
//|                                                                  |
//| Book-critical pattern: an indicator handle is created ONCE in     |
//| OnInit; values are copied with CopyBuffer only AFTER             |
//| BarsCalculated(handle) reports the indicator is ready -          |
//| indicator calculation is asynchronous, and reading a half-built  |
//| buffer is the classic "silent NaN feature" bug (book p. 762+).   |
//|                                                                  |
//| Feature vector (the book's XAUUSD set, T-02 twin):               |
//|   [ RSI(14), MACD line, MACD signal, MACD histogram,             |
//|     upper wick, body, lower wick ]                               |
//| with the candle geometry normalized by the bar range exactly as  |
//| model/sample_generator.py does it, so the Python-trained model   |
//| and the EA-side features stay in the same units.                 |
//|                                                                  |
//| Normalization: the TRAIN parameters saved by T-02 are supplied   |
//| via SetNormalization() (one center/scale pair per column, JSON   |
//| file format produced by the Python side); ApplyNormalization()   |
//| reproduces the Python z-score/min-max exactly.                   |
//+------------------------------------------------------------------+
#ifndef NEUROTRADER_FEATURE_ENGINE_MQH
#define NEUROTRADER_FEATURE_ENGINE_MQH

#define NEURO_FEATURE_COUNT 7

class CFeatureEngine
{
private:
   string m_symbol;
   ENUM_TIMEFRAMES m_timeframe;
   int    m_rsiPeriod;
   int    m_macdFast;
   int    m_macdSlow;
   int    m_macdSignal;
   int    m_handleRSI;
   int    m_handleMACD;
   double m_center[NEURO_FEATURE_COUNT];
   double m_scale[NEURO_FEATURE_COUNT];
   bool   m_hasNorm;
   int    m_notReadyEvents;

   void ResetNormalization()
   {
      for(int i = 0; i < NEURO_FEATURE_COUNT; i++)
      {
         m_center[i] = 0.0;
         m_scale[i]  = 1.0;
      }
      m_hasNorm = false;
   }

public:
   CFeatureEngine(const string symbol, const ENUM_TIMEFRAMES timeframe,
                  const int rsiPeriod = 14, const int macdFast = 12,
                  const int macdSlow = 26, const int macdSignal = 9)
   {
      m_symbol        = symbol;
      m_timeframe     = timeframe;
      m_rsiPeriod     = rsiPeriod;
      m_macdFast      = macdFast;
      m_macdSlow      = macdSlow;
      m_macdSignal    = macdSignal;
      m_handleRSI     = INVALID_HANDLE;
      m_handleMACD    = INVALID_HANDLE;
      m_notReadyEvents= 0;
      ResetNormalization();
   }

   ~CFeatureEngine()
   {
      if(m_handleRSI != INVALID_HANDLE)
         IndicatorRelease(m_handleRSI);
      if(m_handleMACD != INVALID_HANDLE)
         IndicatorRelease(m_handleMACD);
   }

   //+--------------------------------------------------------------+
   //| Create the indicator handles (OnInit only, book 5.5).         |
   //+--------------------------------------------------------------+
   bool Init()
   {
      m_handleRSI = iRSI(m_symbol, m_timeframe, m_rsiPeriod, PRICE_CLOSE);
      m_handleMACD = iMACD(m_symbol, m_timeframe, m_macdFast, m_macdSlow,
                           m_macdSignal, PRICE_CLOSE);
      if(m_handleRSI == INVALID_HANDLE || m_handleMACD == INVALID_HANDLE)
      {
         PrintFormat("[FeatureEngine] failed to create indicator handles "
                     "(RSI=%d MACD=%d, err %d)", m_handleRSI, m_handleMACD,
                     GetLastError());
         return false;
      }
      return true;
   }

   //+--------------------------------------------------------------+
   //| Train normalization parameters (from the T-02 JSON).         |
   //| Arrays follow the feature order documented in the header.    |
   //+--------------------------------------------------------------+
   void SetNormalization(const double &center[], const double &scale[])
   {
      if(ArraySize(center) < NEURO_FEATURE_COUNT ||
         ArraySize(scale) < NEURO_FEATURE_COUNT)
      {
         Print("[FeatureEngine] normalization parameter count mismatch - "
               "features stay UNNORMALIZED (fail-closed, do not guess)");
         ResetNormalization();
         return;
      }
      for(int i = 0; i < NEURO_FEATURE_COUNT; i++)
      {
         m_center[i] = center[i];
         m_scale[i]  = (MathAbs(scale[i]) > 1e-12) ? scale[i] : 1.0;
      }
      m_hasNorm = true;
   }

   bool HasNormalization() const { return m_hasNorm; }

   //+--------------------------------------------------------------+
   //| Readiness gate: BarsCalculated > 0 for BOTH indicators        |
   //| (book 5.5: indicator data arrives asynchronously).            |
   //+--------------------------------------------------------------+
   bool Ready() const
   {
      if(m_handleRSI == INVALID_HANDLE || m_handleMACD == INVALID_HANDLE)
         return false;
      return (BarsCalculated(m_handleRSI) > 0 && BarsCalculated(m_handleMACD) > 0);
   }

   int NotReadyEvents() const { return m_notReadyEvents; }

   //+--------------------------------------------------------------+
   //| Build the RAW feature vector of the LAST CLOSED bar.          |
   //| Returns false when the indicators are not ready yet - the    |
   //| caller MUST skip the bar (never trade on stale features).     |
   //+--------------------------------------------------------------+
   bool BuildFeatures(double &features[])
   {
      ArrayResize(features, NEURO_FEATURE_COUNT);
      if(!Ready())
      {
         m_notReadyEvents++;
         return false;
      }

      double rsi[];
      double macdMain[], macdSignal[];
      //--- copy the last CLOSED bar: start=1 (skip the forming bar 0)
      if(CopyBuffer(m_handleRSI, 0, 1, 1, rsi) < 1)
         return false;
      if(CopyBuffer(m_handleMACD, MAIN_LINE, 1, 1, macdMain) < 1)
         return false;
      if(CopyBuffer(m_handleMACD, SIGNAL_LINE, 1, 1, macdSignal) < 1)
         return false;
      //--- iMACD has two buffers; the histogram is main - signal
      double histBuf[1];
      histBuf[0] = macdMain[0] - macdSignal[0];

      //--- candle geometry of the last closed bar (MqlRates, book 5.3)
      MqlRates rates[];
      if(CopyRates(m_symbol, m_timeframe, 1, 1, rates) < 1)
         return false;
      double hi = rates[0].high;
      double lo = rates[0].low;
      double op = rates[0].open;
      double cl = rates[0].close;
      double range = hi - lo;
      if(range <= 0.0)
         range = SymbolInfoDouble(m_symbol, SYMBOL_POINT);

      double upperWick = (hi - MathMax(op, cl)) / range;
      double body      = (cl - op) / range;
      double lowerWick = (MathMin(op, cl) - lo) / range;

      features[0] = rsi[0];
      features[1] = macdMain[0];
      features[2] = macdSignal[0];
      features[3] = histBuf[0];
      features[4] = upperWick;
      features[5] = body;
      features[6] = lowerWick;
      return true;
   }

   //+--------------------------------------------------------------+
   //| Apply the saved TRAIN normalization to a raw feature vector.  |
   //+--------------------------------------------------------------+
   bool ApplyNormalization(const double &raw[], double &normalized[])
   {
      ArrayResize(normalized, NEURO_FEATURE_COUNT);
      if(!m_hasNorm)
      {
         ArrayCopy(normalized, raw);
         return false;   // un-normalized features: caller must refuse to
      }                  // feed them to the trained model
      for(int i = 0; i < NEURO_FEATURE_COUNT; i++)
         normalized[i] = (raw[i] - m_center[i]) / m_scale[i];
      return true;
   }

   //+--------------------------------------------------------------+
   //| Convenience: raw -> normalized in one call.                   |
   //+--------------------------------------------------------------+
   bool BuildNormalizedFeatures(double &features[])
   {
      double raw[];
      if(!BuildFeatures(raw))
         return false;
      if(!ApplyNormalization(raw, features))
      {
         Print("[FeatureEngine] no train normalization parameters loaded - "
               "signal skipped (T-02 contract)");
         return false;
      }
      return true;
   }

   //+--------------------------------------------------------------+
   //| Windowed features for the FC model (EDGE mode): the last     |
   //| `window` CLOSED bars, chronological, flattened row-major to   |
   //| match Python's (N, window, features) sample layout.           |
   //| CopyBuffer/CopyRates fill non-series arrays oldest-first, so  |
   //| index 0 is the OLDEST bar of the window - exactly the Python  |
   //| convention of make_windowed_samples.                           |
   //+--------------------------------------------------------------+
   bool BuildNormalizedWindow(const int window, double &flat[])
   {
      if(window < 1)
         return false;
      ArrayResize(flat, window * NEURO_FEATURE_COUNT);
      if(!Ready())
      {
         m_notReadyEvents++;
         return false;
      }

      double rsi[], macdMain[], macdSignal[];
      if(CopyBuffer(m_handleRSI, 0, 1, window, rsi) < window)
         return false;
      if(CopyBuffer(m_handleMACD, MAIN_LINE, 1, window, macdMain) < window)
         return false;
      if(CopyBuffer(m_handleMACD, SIGNAL_LINE, 1, window, macdSignal) < window)
         return false;

      MqlRates rates[];
      if(CopyRates(m_symbol, m_timeframe, 1, window, rates) < window)
         return false;

      if(!m_hasNorm)
      {
         Print("[FeatureEngine] no train normalization parameters loaded - "
               "signal skipped (T-02 contract)");
         return false;
      }

      for(int b = 0; b < window; b++)
      {
         double hi = rates[b].high;
         double lo = rates[b].low;
         double op = rates[b].open;
         double cl = rates[b].close;
         double range = hi - lo;
         if(range <= 0.0)
            range = SymbolInfoDouble(m_symbol, SYMBOL_POINT);

         double raw[NEURO_FEATURE_COUNT];
         raw[0] = rsi[b];
         raw[1] = macdMain[b];
         raw[2] = macdSignal[b];
         raw[3] = macdMain[b] - macdSignal[b];
         raw[4] = (hi - MathMax(op, cl)) / range;
         raw[5] = (cl - op) / range;
         raw[6] = (MathMin(op, cl) - lo) / range;

         int base = b * NEURO_FEATURE_COUNT;
         for(int i = 0; i < NEURO_FEATURE_COUNT; i++)
            flat[base + i] = (raw[i] - m_center[i]) / m_scale[i];
      }
      return true;
   }

   string FeatureOrder() const
   {
      return "rsi,macd_line,macd_signal,macd_hist,upper_wick,body,lower_wick";
   }

   int FeatureCount() const { return NEURO_FEATURE_COUNT; }

   //+--------------------------------------------------------------+
   //| Load normalization from the T-02 JSON written by             |
   //| model/sample_generator.py::save_normalization_params:         |
   //|   {"method":"zscore","columns":[...],                        |
   //|    "center":{"rsi":50.1,...},"scale":{"rsi":14.2,...}}        |
   //| Minimal scanner per known column name (MQL5 has no JSON       |
   //| parser); a missing column leaves normalization UNSET -> the   |
   //| engine stays fail-closed.                                     |
   //+--------------------------------------------------------------+
   bool LoadNormalizationJson(const string fileName)
   {
      if(!FileIsExist(fileName))
      {
         PrintFormat("[FeatureEngine] %s not found - features stay "
                     "unnormalized (fail-closed)", fileName);
         return false;
      }
      int handle = FileOpen(fileName, FILE_READ | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE)
      {
         PrintFormat("[FeatureEngine] cannot open %s (err %d)", fileName,
                     GetLastError());
         return false;
      }
      string json = FileReadString(handle);
      FileClose(handle);

      string names[NEURO_FEATURE_COUNT] = {"rsi", "macd_line", "macd_signal",
                                           "macd_hist", "upper_wick", "body",
                                           "lower_wick"};
      double center[NEURO_FEATURE_COUNT], scale[NEURO_FEATURE_COUNT];
      for(int i = 0; i < NEURO_FEATURE_COUNT; i++)
      {
         center[i] = 0.0;
         scale[i]  = 1.0;
      }

      //--- locate the center and scale objects
      int posCenter = StringFind(json, "\"center\"");
      int posScale  = StringFind(json, "\"scale\"");
      if(posCenter < 0 || posScale < 0)
      {
         Print("[FeatureEngine] normalization JSON missing center/scale");
         return false;
      }

      int found = 0;
      for(int i = 0; i < NEURO_FEATURE_COUNT; i++)
      {
         string key = "\"" + names[i] + "\"";
         double c = ExtractJsonNumber(json, posCenter, posScale, key);
         double s = ExtractJsonNumber(json, posScale, StringLen(json), key);
         if(s != 0.0)   // 0-scale would be rejected by SetNormalization anyway
         {
            center[i] = c;
            scale[i]  = s;
            found++;
         }
      }
      if(found < NEURO_FEATURE_COUNT)
      {
         PrintFormat("[FeatureEngine] only %d/%d normalization columns "
                     "found in %s - fail-closed", found, NEURO_FEATURE_COUNT,
                     fileName);
         return false;
      }
      SetNormalization(center, scale);
      return true;
   }

private:
   //--- scan json[begin..end) for "key": number, return the number
   double ExtractJsonNumber(const string json, const int begin,
                            const int end, const string key) const
   {
      int pos = StringFind(json, key, begin);
      if(pos < 0 || pos >= end)
         return 0.0;
      int colon = StringFind(json, ":", pos);
      if(colon < 0)
         return 0.0;
      string tail = StringSubstr(json, colon + 1, 24);
      string number = "";
      for(int i = 0; i < StringLen(tail); i++)
      {
         string ch = StringSubstr(tail, i, 1);
         if((ch >= "0" && ch <= "9") || ch == "-" || ch == "+" ||
            ch == "." || ch == "e" || ch == "E")
            number += ch;
         else if(StringLen(number) > 0)
            break;
      }
      return StringToDouble(number);
   }

public:
   //+--------------------------------------------------------------+
   //| Serialize the (normalized) feature vector for the journal.    |
   //+--------------------------------------------------------------+
   string FeaturesJson(const double &features[]) const
   {
      string json = "{";
      string names[NEURO_FEATURE_COUNT] = {"rsi", "macd_line", "macd_signal",
                                           "macd_hist", "upper_wick", "body",
                                           "lower_wick"};
      for(int i = 0; i < NEURO_FEATURE_COUNT && i < ArraySize(features); i++)
      {
         if(i > 0)
            json += ",";
         json += "\"" + names[i] + "\":" + DoubleToString(features[i], 6);
      }
      json += "}";
      return json;
   }

   //+--------------------------------------------------------------+
   //| Deterministic feature hash for the T-22 journal join.         |
   //| 64-bit FNV-1a over the 6-decimal text form of the vector;     |
   //| the Python producer's own hash travels in the bridge row and |
   //| is journaled verbatim in bridge mode.                         |
   //+--------------------------------------------------------------+
   string FeaturesHash(const double &features[]) const
   {
      string text = "";
      for(int i = 0; i < ArraySize(features); i++)
         text += (i > 0 ? "|" : "") + DoubleToString(features[i], 6);
      ulong hash = 1469598103934665603;   // FNV-1a 64 offset basis
      for(int i = 0; i < StringLen(text); i++)
      {
         hash ^= (ulong)StringGetCharacter(text, i);
         hash *= 1099511628211;           // FNV-1a 64 prime
      }
      return StringFormat("%016llx", hash);
   }
};

#endif // NEUROTRADER_FEATURE_ENGINE_MQH
