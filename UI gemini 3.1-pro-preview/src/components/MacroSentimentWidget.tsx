import React from 'react';
import { Newspaper, TrendingUp, TrendingDown, Minus, Flame } from 'lucide-react';
import { MacroSentimentResponse } from '../types';
import { formatPercent } from '../lib/utils';

interface MacroSentimentWidgetProps {
  sentiment: MacroSentimentResponse | null;
}

export const MacroSentimentWidget: React.FC<MacroSentimentWidgetProps> = ({ sentiment }) => {
  const score = sentiment?.score ?? 0.68;
  const bias = sentiment?.bias ?? 'bullish';
  const confidence = sentiment?.confidence ?? 0.81;

  const isBullish = bias === 'bullish';
  const isBearish = bias === 'bearish';

  return (
    <div id="macro-sentiment-card" className="bg-slate-900/80 border border-slate-800 rounded-xl p-5 shadow-xl flex flex-col justify-between">
      <div>
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
              <Newspaper className="w-4 h-4" />
            </div>
            <h2 className="text-sm font-bold text-slate-100 uppercase tracking-wide">
              Macro AI Sentiment
            </h2>
          </div>
          <span className="text-[11px] font-mono text-slate-400">
            Gold Bias: <strong className={isBullish ? 'text-emerald-400' : isBearish ? 'text-rose-400' : 'text-slate-300'}>{bias.toUpperCase()}</strong>
          </span>
        </div>

        {/* Sentiment Meter Gauge */}
        <div className="p-3.5 bg-slate-950/60 rounded-lg border border-slate-800/80 space-y-2.5">
          <div className="flex justify-between items-center text-xs">
            <span className="text-slate-400">Net Macro Driver:</span>
            <div className="flex items-center gap-1 font-bold font-mono">
              {isBullish ? (
                <TrendingUp className="w-3.5 h-3.5 text-emerald-400" />
              ) : isBearish ? (
                <TrendingDown className="w-3.5 h-3.5 text-rose-400" />
              ) : (
                <Minus className="w-3.5 h-3.5 text-slate-400" />
              )}
              <span className={isBullish ? 'text-emerald-400' : isBearish ? 'text-rose-400' : 'text-slate-300'}>
                {score > 0 ? `+${score.toFixed(2)}` : score.toFixed(2)}
              </span>
            </div>
          </div>

          <div className="flex justify-between items-center text-xs">
            <span className="text-slate-400">AI Conviction Floor:</span>
            <span className="font-mono font-bold text-amber-300">
              {formatPercent(confidence)}
            </span>
          </div>

          {/* Bar gauge */}
          <div className="w-full bg-slate-800 h-1.5 rounded-full overflow-hidden relative">
            <div
              className={`h-full transition-all duration-500 rounded-full ${
                isBullish ? 'bg-emerald-400' : isBearish ? 'bg-rose-400' : 'bg-slate-400'
              }`}
              style={{ width: `${Math.min(100, Math.max(10, ((score + 1) / 2) * 100))}%` }}
            ></div>
          </div>

          {/* Key tags / Macro catalyst factors */}
          <div className="pt-1 text-[11px] text-slate-400">
            <div className="flex items-center gap-1 text-slate-500 mb-1.5">
              <Flame className="w-3 h-3 text-amber-400" />
              <span>Key Macro Catalysts:</span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {(sentiment?.matched_terms || ['Fed Rate Cut Probability', 'US CPI Cooling', 'Safe Haven Inflow']).map((term, i) => (
                <span key={i} className="px-2 py-0.5 rounded bg-slate-900 text-slate-300 border border-slate-700/60 font-mono text-[10px]">
                  {term}
                </span>
              ))}
            </div>
          </div>
        </div>
      </div>

      <div className="mt-3 pt-2 text-[10px] text-slate-500 font-mono flex justify-between">
        <span>News Buffer Guard: Active (±30m)</span>
        <span>Red Zone: Disabled</span>
      </div>
    </div>
  );
};
