import React, { useState } from 'react';
import { Cpu, Copy, Check, Sparkles, TrendingUp } from 'lucide-react';
import { InstitutionalMetricsResponse } from '../types';

interface SmartMoneyMetricsProps {
  data: InstitutionalMetricsResponse | null;
}

export const SmartMoneyMetrics: React.FC<SmartMoneyMetricsProps> = ({ data }) => {
  const [copied, setCopied] = useState(false);

  const handleCopyReport = () => {
    if (!data?.report_text) return;
    navigator.clipboard.writeText(data.report_text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2500);
  };

  const metricsEntries = data?.metrics ? Object.entries(data.metrics) : [];

  return (
    <section id="smart-money-section" className="bg-slate-900/80 border border-slate-800 rounded-xl p-5 md:p-6 border-l-4 border-l-cyan-500 shadow-xl">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 mb-4">
        <div className="flex items-center gap-2.5">
          <div className="p-2 rounded-lg bg-cyan-950/60 border border-cyan-800/60 text-cyan-400">
            <Cpu className="w-5 h-5" />
          </div>
          <div>
            <h2 className="text-base md:text-lg font-bold text-slate-100 flex items-center gap-2">
              <span>Smart Money Concepts (SMC) & Institutional Microstructure</span>
              <span className="text-[11px] bg-cyan-950 text-cyan-300 border border-cyan-800 px-2 py-0.5 rounded font-mono">
                M15 / H1
              </span>
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Liquidity Sweeps, Order Blocks, Imbalances (FVG), Market Structure & Dealing Range
            </p>
          </div>
        </div>

        <button
          id="copy-smc-report-btn"
          onClick={handleCopyReport}
          disabled={!data?.report_text}
          className="flex items-center gap-1.5 bg-slate-800 hover:bg-slate-700 active:bg-slate-900 border border-slate-700 text-slate-200 text-xs px-3 py-2 rounded-lg transition-colors font-medium cursor-pointer disabled:opacity-40"
        >
          {copied ? (
            <>
              <Check className="w-3.5 h-3.5 text-emerald-400" />
              <span className="text-emerald-300">Report Copied!</span>
            </>
          ) : (
            <>
              <Copy className="w-3.5 h-3.5 text-slate-400" />
              <span>Copy Institutional Report</span>
            </>
          )}
        </button>
      </div>

      {/* Grid of SMC metrics */}
      {metricsEntries.length > 0 ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3.5 mt-4">
          {metricsEntries.map(([title, item], idx) => {
            const isBullish = item.status === 'bullish';
            const isBearish = item.status === 'bearish';

            return (
              <div
                key={idx}
                className="bg-slate-950/60 border border-slate-800/80 rounded-lg p-3.5 flex flex-col justify-between hover:border-slate-700 transition-colors"
              >
                <div>
                  <div className="flex items-center justify-between text-xs font-semibold text-slate-400 uppercase tracking-wider mb-1.5">
                    <span>{title}</span>
                    <Sparkles className="w-3.5 h-3.5 text-cyan-400" />
                  </div>
                  <div className={`font-mono text-xs font-bold ${
                    isBullish ? 'text-emerald-400' : isBearish ? 'text-rose-400' : 'text-amber-300'
                  }`}>
                    {item.display}
                  </div>
                  <p className="text-[11px] text-slate-400 mt-2 leading-relaxed">
                    {item.text}
                  </p>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="p-4 bg-slate-950/50 rounded-lg border border-slate-800 text-center text-xs text-slate-400">
          Loading institutional metrics...
        </div>
      )}

      <div className="mt-4 pt-3 border-t border-slate-800/60 flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-500 font-mono">
        <div className="flex items-center gap-2">
          <TrendingUp className="w-3.5 h-3.5 text-emerald-400" />
          <span>Source: <strong className="text-slate-400">{data?.source || 'mt5_closed_candles:XAUUSD'}</strong></span>
        </div>
        <div>
          Causal invariant: No forward leakage across bar boundary
        </div>
      </div>
    </section>
  );
};
