import React, { useState, useEffect } from 'react';
import { Header } from './components/Header';
import { KpiGrid } from './components/KpiGrid';
import { SmartMoneyMetrics } from './components/SmartMoneyMetrics';
import { ChartSection } from './components/ChartSection';
import { SignalMatrix } from './components/SignalMatrix';
import { CorrelationHeatmap } from './components/CorrelationHeatmap';
import { PositionsMonitor } from './components/PositionsMonitor';
import { MacroSentimentWidget } from './components/MacroSentimentWidget';
import { MonteCarloWidget } from './components/MonteCarloWidget';
import { TradeMetricsDashboard } from './components/TradeMetricsDashboard';
import { AIAssistant } from './components/AIAssistant';
import { ProvenanceModal } from './components/ProvenanceModal';
import { TelegramPreviewModal } from './components/TelegramPreviewModal';
import {
  SystemStatus,
  InstitutionalMetricsResponse,
  MatrixItem,
  CorrelationMatrixData,
  OpenPosition,
  MacroSentimentResponse,
  MonteCarloResponse,
  SignalResponse,
  AssetSymbol,
} from './types';
import { ShieldCheck, Send, Activity } from 'lucide-react';

export function App() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [institutional, setInstitutional] = useState<InstitutionalMetricsResponse | null>(null);
  const [matrixSignals, setMatrixSignals] = useState<MatrixItem[]>([]);
  const [correlation, setCorrelation] = useState<CorrelationMatrixData | null>(null);
  const [positions, setPositions] = useState<OpenPosition[]>([]);
  const [sentiment, setSentiment] = useState<MacroSentimentResponse | null>(null);
  const [monteCarlo, setMonteCarlo] = useState<MonteCarloResponse | null>(null);
  const [currentSignal, setCurrentSignal] = useState<SignalResponse | null>(null);
  
  const [selectedAsset, setSelectedAsset] = useState<AssetSymbol>('XAUUSD');
  const [isLoading, setIsLoading] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);

  // Modals
  const [isProvenanceOpen, setIsProvenanceOpen] = useState(false);
  const [isTelegramOpen, setIsTelegramOpen] = useState(false);

  const fetchAllData = async () => {
    setIsLoading(true);
    try {
      const [
        statusRes,
        smcRes,
        matrixRes,
        corrRes,
        posRes,
        sentRes,
        mcRes,
        sigRes,
      ] = await Promise.all([
        fetch('/api/status'),
        fetch('/api/institutional-metrics'),
        fetch('/api/matrix'),
        fetch('/api/correlation'),
        fetch('/api/positions'),
        fetch('/api/sentiment'),
        fetch('/api/monte-carlo'),
        fetch(`/signal?asset=${selectedAsset}`),
      ]);

      if (statusRes.ok) setStatus(await statusRes.json());
      if (smcRes.ok) setInstitutional(await smcRes.json());
      if (matrixRes.ok) {
        const mData = await matrixRes.json();
        setMatrixSignals(mData.signals || []);
      }
      if (corrRes.ok) setCorrelation(await corrRes.json());
      if (posRes.ok) {
        const pData = await posRes.json();
        setPositions(pData.positions || []);
      }
      if (sentRes.ok) setSentiment(await sentRes.json());
      if (mcRes.ok) setMonteCarlo(await mcRes.json());
      if (sigRes.ok) setCurrentSignal(await sigRes.json());

      setLastUpdated(new Date().toISOString().slice(11, 19) + ' UTC');
    } catch (err) {
      console.error('Data fetch error:', err);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchAllData();
    const interval = setInterval(fetchAllData, 8000);
    return () => clearInterval(interval);
  }, [selectedAsset]);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-3 sm:p-5 md:p-8 space-y-6 max-w-[1600px] mx-auto">
      {/* 1. Header with branding, status & honesty disclaimer */}
      <Header
        status={status}
        isLoading={isLoading}
        onRefresh={fetchAllData}
        lastUpdated={lastUpdated}
      />

      {/* 2. Top-level KPIs: Mode, Balance, Positions, Risk */}
      <KpiGrid status={status} />

      {/* 3. Smart Money Concepts (SMC) & Institutional Microstructure */}
      <SmartMoneyMetrics data={institutional} />

      {/* 4. Chart & Quantitative Intelligence Row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Main interactive candlestick chart (2 cols on large screen) */}
        <div className="lg:col-span-2">
          <ChartSection
            selectedAsset={selectedAsset}
            onSelectAsset={(asset) => setSelectedAsset(asset)}
          />
        </div>

        {/* Side widgets: Macro Sentiment + Monte Carlo VaR */}
        <div className="space-y-6 flex flex-col justify-between">
          <MacroSentimentWidget sentiment={sentiment} />
          <MonteCarloWidget monteCarlo={monteCarlo} />
        </div>
      </div>

      {/* 5. Multi-Asset Real-Time Signal Matrix */}
      <SignalMatrix
        signals={matrixSignals}
        isLoading={isLoading}
        onRefresh={fetchAllData}
        onSelectAsset={(asset) => setSelectedAsset(asset)}
      />

      {/* 6. Correlation Heatmap & Positions Monitor */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <CorrelationHeatmap
          data={correlation}
          onSelectAsset={(asset) => setSelectedAsset(asset)}
        />
        <PositionsMonitor positions={positions} />
      </div>

      {/* 7. Historical Trade Performance & Ledger Metrics */}
      <TradeMetricsDashboard />

      {/* 7.5 AI Assistant */}
      <AIAssistant />

      {/* 8. Bottom Action Footer Bar */}
      <footer id="main-footer" className="bg-slate-900/60 border border-slate-800/80 rounded-xl p-4 flex flex-col sm:flex-row justify-between items-center gap-4 text-xs font-mono text-slate-400">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
          <span>Causal Multi-Asset Engine Active • MT5 Bridge Connected</span>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <button
            id="open-provenance-modal-btn"
            onClick={() => setIsProvenanceOpen(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition-colors cursor-pointer"
          >
            <ShieldCheck className="w-4 h-4 text-emerald-400" />
            <span>Audit Lineage & Provenance</span>
          </button>

          <button
            id="open-telegram-modal-btn"
            onClick={() => setIsTelegramOpen(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-sky-950/70 hover:bg-sky-900/80 text-sky-300 border border-sky-800 transition-colors cursor-pointer"
          >
            <Send className="w-4 h-4 text-sky-400" />
            <span>Simulate Telegram Alert</span>
          </button>
        </div>
      </footer>

      {/* Provenance Audit Modal */}
      <ProvenanceModal
        isOpen={isProvenanceOpen}
        onClose={() => setIsProvenanceOpen(false)}
      />

      {/* Telegram Alert Preview Modal */}
      <TelegramPreviewModal
        isOpen={isTelegramOpen}
        onClose={() => setIsTelegramOpen(false)}
        signal={currentSignal}
      />
    </div>
  );
}

export default App;
