//+------------------------------------------------------------------+
//| NeuroTraderEA.mq5 - the books-integration expert advisor          |
//|                                                                  |
//| TZ_BOOKS.md final assembly: the MQL5 side of the pipeline that   |
//| the two book analyses describe.                                  |
//|                                                                  |
//| Data flow (book 7.9 architecture):                               |
//|                                                                  |
//|   [A] BRIDGE mode (default): Python trains + infers, the EA      |
//|       executes. OnTimer polls the SQLite signal bridge           |
//|       (SignalBridge.mqh, T-16):                                  |
//|         ml_signals(new) -> gates -> risk -> order -> journal     |
//|                                                                  |
//|   [B] EDGE mode (optional): the EA computes book features        |
//|       (FeatureEngine.mqh, T-14), runs the exported FC model on   |
//|       the GPU (OpenCLInference.mqh, T-18) or CPU, and trades     |
//|       the sigmoid probability against the threshold.             |
//|                                                                  |
//| Gates applied to every signal (both modes):                      |
//|   1. trading allowed at all (terminal + account)                 |
//|   2. DayFilter - blocked intraweek days (T-10, fail-open)        |
//|   3. NewsGuard - high-impact calendar blackout (T-15)            |
//|   4. probability >= InpMinProbability                            |
//|   5. spread <= InpMaxSpreadPoints                                |
//|                                                                  |
//| Then: RiskSizer sizes the order (T-06), TradeExecutor sends it   |
//| with retries (T-05), PositionManager accompanies it (T-12),      |
//| AlertDispatcher reports (T-07), SignalJournal keeps the full     |
//| trace (T-22), TesterCriterion scores the backtest (T-08).        |
//|                                                                  |
//| FAIL-CLOSED defaults: without normalization parameters the       |
//| feature engine refuses to run; without the bridge file the EA    |
//| stays idle and says why. Autotrading from Python is NOT used     |
//| (error 10027 is a feature, book 7.9).                            |
//+------------------------------------------------------------------+
#property copyright "xauusd-alert-system / books integration"
#property version   "1.00"
#property strict

#include "FeatureEngine.mqh"
#include "OpenCLInference.mqh"
#include "RiskSizer.mqh"
#include "TradeExecutor.mqh"
#include "AlertDispatcher.mqh"
#include "PositionManager.mqh"
#include "NewsGuard.mqh"
#include "DayFilter.mqh"
#include "SignalBridge.mqh"
#include "SignalJournal.mqh"
#include "TesterCriterion.mqh"

//--- operation mode
enum ENUM_NT_MODE
{
   NT_MODE_BRIDGE = 0,   // Python infers, EA executes (default)
   NT_MODE_EDGE            // EA infers locally via OpenCL/CPU
};

//--- inputs --------------------------------------------------------
input group "=== General ==="
input long              InpMagic              = 20260828;
input ENUM_NT_MODE      InpMode               = NT_MODE_BRIDGE;
input string            InpAsset              = "XAUUSD";
input double            InpMinProbability     = 0.60;   // ensemble floor

input group "=== Risk (T-06) ==="
input double            InpRiskPercent        = 0.50;   // % equity per trade
input double            InpMaxLots            = 1.0;
input double            InpMaxSpreadPoints    = 350;    // skip when wider

input group "=== Geometry (T-12) ==="
input double            InpStopPoints         = 300;
input double            InpTp2Points          = 600;
input double            InpTrailStartPoints   = 300;
input double            InpTrailDistancePoints= 150;
input double            InpPartialFraction    = 0.5;    // closed at TP1

input group "=== News guard (T-15) ==="
input bool              InpUseNewsGuard       = true;
input string            InpNewsCurrencies     = "USD";
input int               InpNewsBufferBeforeMin= 30;
input int               InpNewsBufferAfterMin = 30;

input group "=== Day filter (T-10) ==="
input string            InpDayFilterFile      = "book_day_filter.json";

input group "=== Alerts (T-07) ==="
input bool              InpAlertPush          = true;
input bool              InpAlertMail          = false;
input bool              InpAlertHook          = false;
input string            InpAlertHookUrl       = "";

input group "=== Bridge (T-16) ==="
input string            InpBridgeFile         = "ml_signal_bridge.sqlite";
input int               InpBridgePollSeconds  = 3;

input group "=== Local inference (T-14/T-18) ==="
input string            InpNormParamsFile     = "book_normalization.json";
input string            InpWeightsFile        = "book_fc_weights.bin";
input int               InpWindow             = 16;     // feature window
input int               InpHiddenDim          = 60;

input group "=== Tester criterion (T-08) ==="
input double            InpDdWeight           = 0.25;
input int               InpMinTrades          = 30;

//--- module instances ----------------------------------------------
CFeatureEngine    *g_features   = NULL;
COpenCLInference  *g_inference  = NULL;
CRiskSizer        *g_risk       = NULL;
CTradeExecutor    *g_executor   = NULL;
CAlertDispatcher  *g_alerts     = NULL;
CPositionManager  *g_positions  = NULL;
CNewsGuard        *g_news       = NULL;
CDayFilter        *g_days       = NULL;
CSignalBridge     *g_bridge     = NULL;
CSignalJournal    *g_journal    = NULL;
CTesterCriterion  *g_criterion  = NULL;

datetime g_lastBarTime = 0;
string   g_asset;

//+------------------------------------------------------------------+
int OnInit()
{
   g_asset = InpAsset;
   if(StringLen(g_asset) == 0)
      g_asset = _Symbol;

   //--- feature engine (T-14): handles once, readiness-gated buffers
   g_features = new CFeatureEngine(g_asset, _Period, 14, 12, 26, 9);
   if(!g_features.Init())
   {
      Print("[NeuroTrader] FeatureEngine init failed - see log above");
      return INIT_FAILED;
   }
   if(!g_features.LoadNormalizationJson(InpNormParamsFile))
   {
      Print("[NeuroTrader] normalization params unavailable (",
            InpNormParamsFile, ") - fail-closed. Run "
            "scripts/create_initial_data_xauusd.py first.");
      return INIT_FAILED;
   }

   //--- local inference (T-18): EDGE mode only
   if(InpMode == NT_MODE_EDGE)
   {
      double weights[];
      if(!LoadWeights(InpWeightsFile, weights))
      {
         Print("[NeuroTrader] weights file missing (", InpWeightsFile,
               ") - EDGE mode unavailable (run scripts/export_fc_weights.py)");
         return INIT_FAILED;
      }
      g_inference = new COpenCLInference();
      if(!g_inference.Init(InpWindow * g_features.FeatureCount(),
                           InpHiddenDim, weights))
      {
         Print("[NeuroTrader] inference init failed (check InpWindow/"
               "InpHiddenDim against the *_meta.json of the weights file)");
         return INIT_FAILED;
      }
      PrintFormat("[NeuroTrader] EDGE mode active: %s",
                  g_inference.UsesOpenCL() ? "OpenCL GPU" : "CPU fallback");
   }

   //--- risk + execution
   g_risk     = new CRiskSizer(InpRiskPercent, InpMaxLots);
   g_executor = new CTradeExecutor(InpMagic, 20, 3, false);

   //--- alerts (T-07)
   g_alerts = new CAlertDispatcher(InpAlertPush, InpAlertMail,
                                   InpAlertHook ? InpAlertHookUrl : "");
   Print("[NeuroTrader] alerts: ", g_alerts.StartupHint());

   //--- position accompaniment (T-12)
   g_positions = new CPositionManager(InpMagic,
                                      (int)InpTrailStartPoints,
                                      (int)InpTrailDistancePoints,
                                      InpPartialFraction);

   //--- news guard (T-15): timer-refreshed cache, tester-safe
   g_news = new CNewsGuard(InpNewsBufferBeforeMin, InpNewsBufferAfterMin);
   g_news.SetCurrencies(InpNewsCurrencies);
   if(InpUseNewsGuard)
      g_news.Refresh();

   //--- day filter (T-10): fail-open by design
   g_days = new CDayFilter();
   g_days.LoadFromFile(InpDayFilterFile);

   //--- journal (T-22): full trace, own SQLite file
   g_journal = new CSignalJournal();
   if(!g_journal.Init())
      Print("[NeuroTrader] SignalJournal init failed - traces will be lost");

   //--- bridge (T-16): BRIDGE mode only; lazily (re)opened on the timer
   if(InpMode == NT_MODE_BRIDGE)
   {
      g_bridge = new CSignalBridge(InpBridgeFile);
      if(!g_bridge.Init())
      {
         //--- not fatal: the Python writer may start later
         Print("[NeuroTrader] signal bridge not available yet - "
               "will retry on timer");
         delete g_bridge;
         g_bridge = NULL;
      }
   }

   //--- tester criterion (T-08): passive collector, reports on deinit
   g_criterion = new CTesterCriterion(InpDdWeight, 10.0, InpMinTrades);
   Print("[NeuroTrader] ", g_criterion.TickModeReminder());

   //--- housekeeping timer: ALL SQLite I/O lives here, never in OnTick
   EventSetTimer(MathMax(1, InpBridgePollSeconds));
   PrintFormat("[NeuroTrader] initialized: asset=%s mode=%s magic=%I64d",
               g_asset, (InpMode == NT_MODE_BRIDGE) ? "BRIDGE" : "EDGE",
               InpMagic);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   if(g_criterion != NULL)
   {
      PrintFormat("[NeuroTrader] tester criterion score: %.4f",
                  g_criterion.Score());
      delete g_criterion;
   }
   if(g_features  != NULL) delete g_features;
   if(g_inference != NULL) delete g_inference;
   if(g_risk      != NULL) delete g_risk;
   if(g_executor  != NULL) delete g_executor;
   if(g_alerts    != NULL) delete g_alerts;
   if(g_positions != NULL) delete g_positions;
   if(g_news      != NULL) delete g_news;
   if(g_days      != NULL) delete g_days;
   if(g_bridge    != NULL) delete g_bridge;
   if(g_journal   != NULL) delete g_journal;
}

//+------------------------------------------------------------------+
//| Timer: bridge polling, news refresh, expiry housekeeping.        |
//| The ONLY place that touches the bridge database.                |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(InpUseNewsGuard && g_news != NULL)
      g_news.Refresh();

   if(InpMode != NT_MODE_BRIDGE)
      return;

   //--- (re)open the bridge lazily: the Python writer may appear later
   if(g_bridge == NULL)
   {
      g_bridge = new CSignalBridge(InpBridgeFile);
      if(!g_bridge.Init())
      {
         delete g_bridge;
         g_bridge = NULL;
         return;
      }
   }
   g_bridge.ExpireStale();

   SBridgeSignal signal;
   while(g_bridge.NextPending(signal))
      ProcessBridgeSignal(signal);
}

//+------------------------------------------------------------------+
//| Tick: bar-gated local inference + position accompaniment.        |
//+------------------------------------------------------------------+
void OnTick()
{
   //--- accompany open positions on every tick (T-12)
   if(g_positions != NULL)
      g_positions.ManageAll();

   if(InpMode != NT_MODE_EDGE || g_inference == NULL)
      return;

   //--- evaluate the model once per closed bar (book 5.2 discipline)
   datetime barTime = iTime(g_asset, _Period, 0);
   if(barTime == 0 || barTime == g_lastBarTime)
      return;
   g_lastBarTime = barTime;

   double features[];
   if(!g_features.BuildNormalizedWindow(InpWindow, features))
      return;                       // indicators not ready / no norm params

   double probability = 0.5;
   if(!g_inference.Forward(features, probability))
      return;

   int direction = (probability >= InpMinProbability) ? +1 : 0;
   if(direction == 0)
      return;

   string signalId = StringFormat("edge-%s-%s", g_asset,
                                  TimeToString(barTime, TIME_DATE | TIME_MINUTES));
   string context  = signalId;
   if(!PassesGates(context))
      return;

   if(g_journal != NULL)
      g_journal.LogDecision(signalId, g_asset, direction, probability,
                            g_features.FeaturesHash(features),
                            g_features.FeaturesJson(features),
                            "edge-inference", probability);

   ExecuteSignal(signalId, direction, probability,
                 g_features.FeaturesHash(features), 0.0, 0.0,
                 "edge-inference");
}

//+------------------------------------------------------------------+
//| Common gates. Returns false when the signal must be skipped.     |
//+------------------------------------------------------------------+
bool PassesGates(const string context)
{
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
   {
      PrintFormat("[NeuroTrader] skip %s: terminal autotrading disabled",
                  context);
      return false;
   }
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
   {
      PrintFormat("[NeuroTrader] skip %s: EA trading disabled", context);
      return false;
   }
   if(g_days != NULL && !g_days.TradingAllowedNow())
   {
      PrintFormat("[NeuroTrader] skip %s: day filter blocks %s",
                  context, g_days.BlockedDaysText());
      return false;
   }
   if(g_news != NULL && InpUseNewsGuard && g_news.IsBlackout())
   {
      PrintFormat("[NeuroTrader] skip %s: news blackout %s",
                  context, g_news.ActiveWindowText());
      return false;
   }
   double point = SymbolInfoDouble(g_asset, SYMBOL_POINT);
   if(point > 0.0)
   {
      double spreadPoints = (SymbolInfoDouble(g_asset, SYMBOL_ASK)
                           - SymbolInfoDouble(g_asset, SYMBOL_BID)) / point;
      if(spreadPoints > InpMaxSpreadPoints)
      {
         PrintFormat("[NeuroTrader] skip %s: spread %.0f > %.0f pts",
                     context, spreadPoints, InpMaxSpreadPoints);
         return false;
      }
   }
   return true;
}

//+------------------------------------------------------------------+
//| Bridge signal: gates -> size -> execute -> journal (T-16 path).  |
//+------------------------------------------------------------------+
void ProcessBridgeSignal(SBridgeSignal &signal)
{
   if(signal.asset != g_asset)
   {
      g_bridge.MarkStatus(signal.intentId, "skipped", "foreign asset");
      return;
   }
   if(!PassesGates(signal.intentId))
   {
      g_bridge.MarkStatus(signal.intentId, "skipped", "gate rejected");
      if(g_journal != NULL)
         g_journal.LogDecision(signal.intentId, signal.asset,
                               signal.direction, signal.probability,
                               signal.featuresHash, signal.comment,
                               "gate-reject", 0.0);
      return;
   }
   if(signal.probability < InpMinProbability)
   {
      g_bridge.MarkStatus(signal.intentId, "skipped",
                          StringFormat("probability %.3f < %.3f",
                                       signal.probability, InpMinProbability));
      return;
   }

   if(g_journal != NULL)
      g_journal.LogDecision(signal.intentId, signal.asset, signal.direction,
                            signal.probability, signal.featuresHash,
                            signal.comment, "bridge", signal.probability);

   ExecuteSignal(signal.intentId, signal.direction, signal.probability,
                 signal.featuresHash, signal.entryPrice, signal.slPrice,
                 signal.tpPrice, signal.comment);
}

//+------------------------------------------------------------------+
//| Shared execution path: plan stops (T-12) -> size (T-06) ->       |
//| send with retries (T-05) -> journal + bridge flip + alert.       |
//+------------------------------------------------------------------+
void ExecuteSignal(const string signalId, const int direction,
                   const double probability, const string featuresHash,
                   const double entryHint, const double slHint,
                   const double tpHint, const string comment)
{
   //--- geometry: prefer the signal's own SL/TP, fall back to points
   double point = SymbolInfoDouble(g_asset, SYMBOL_POINT);
   double bid   = SymbolInfoDouble(g_asset, SYMBOL_BID);
   double ask   = SymbolInfoDouble(g_asset, SYMBOL_ASK);
   double rawSl = slHint, rawTp = tpHint;
   if(rawSl <= 0.0)
      rawSl = (direction > 0) ? ask - InpStopPoints * point
                              : bid + InpStopPoints * point;
   if(rawTp <= 0.0)
      rawTp = (direction > 0) ? ask + InpTp2Points * point
                              : bid - InpTp2Points * point;

   //--- spread-adjusted, broker-validated stops (T-12)
   SPositionPlan plan = g_positions.PlanStops(g_asset, direction, rawSl, rawTp);
   if(!plan.ok)
   {
      PrintFormat("[NeuroTrader] %s rejected: %s", signalId, plan.reason);
      if(g_bridge != NULL)
         g_bridge.MarkStatus(signalId, "skipped", plan.reason);
      return;
   }

   //--- position sizing (T-06): round down, margin precheck
   double entry = (direction > 0) ? ask : bid;
   SRiskSizeResult risk;
   if(!g_risk.SizePosition(g_asset, direction, entry, plan.sl, risk) || !risk.ok)
   {
      PrintFormat("[NeuroTrader] %s not sized: %s", signalId, risk.reason);
      if(g_bridge != NULL)
         g_bridge.MarkStatus(signalId, "skipped", risk.reason);
      return;
   }

   //--- send with retries (T-05)
   SExecutionResult outcome;
   bool sent = g_executor.Execute(g_asset, direction, risk.lots,
                                  plan.sl, plan.tp, "NeuroTrader", outcome);

   //--- journal the outcome (T-22)
   if(g_journal != NULL)
      g_journal.LogExecution(signalId, risk.lots, entry, plan.sl, plan.tp,
                             outcome.retcode, outcome.order, outcome.deal,
                             sent ? "executed" : "failed");

   //--- flip the bridge row (T-16)
   if(g_bridge != NULL)
      g_bridge.MarkStatus(signalId, sent ? "executed" : "failed",
                          StringFormat("retcode %u (%s)",
                                       outcome.retcode, outcome.reason));

   //--- alert (T-07) with the execution facts in the hook payload
   if(sent)
   {
      string extra = StringFormat("\"signal_id\":\"%s\",\"retcode\":%u,"
                                  "\"sl\":%.2f,\"tp\":%.2f,\"risk\":%.2f",
                                  signalId, outcome.retcode, plan.sl, plan.tp,
                                  risk.riskMoney);
      g_alerts.DispatchSignal(g_asset, direction, probability, risk.lots, extra);
   }
}

//+------------------------------------------------------------------+
//| Weight file loader: flat little-endian doubles [w1|b1|w2|b2]     |
//| written by scripts/export_fc_weights.py.                         |
//+------------------------------------------------------------------+
bool LoadWeights(const string fileName, double &weights[])
{
   if(!FileIsExist(fileName))
      return false;
   int handle = FileOpen(fileName, FILE_READ | FILE_BIN);
   if(handle == INVALID_HANDLE)
      return false;
   int count = (int)(FileSize(handle) / sizeof(double));
   if(count <= 0 || !FileReadArray(handle, weights, 0, count))
   {
      FileClose(handle);
      return false;
   }
   FileClose(handle);
   return true;
}

//+------------------------------------------------------------------+
//| Tester: the T-08 criterion becomes the optimization target.      |
//+------------------------------------------------------------------+
double OnTester()
{
   if(g_criterion == NULL)
      return 0.0;
   g_criterion.AddEquityFrame(128);
   return g_criterion.Score();
}
