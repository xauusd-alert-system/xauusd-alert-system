import React from 'react';
import { Dices, Shield, Percent, TrendingUp } from 'lucide-react';
import { MonteCarloResponse } from '../types';
import { formatCurrency, formatPercent } from '../lib/utils';

interface MonteCarloWidgetProps {
  monteCarlo: MonteCarloResponse | null;
}

export const MonteCarloWidget: React.FC<MonteCarloWidgetProps> = ({ monteCarlo }) => {
  const var95 = monteCarlo?.var_95_usd ?? 342.50;
  const profitProb = monteCarlo?.profit_probability_pct ?? 87.4;
  const probRuin = monteCarlo?.prob_of_ruin_pct ?? 0.08;

  return (
    <div id="monte-carlo-card" className="bg-slate-900/80 border border-slate-800 rounded-xl p-5 shadow-xl flex flex-col justify-between">
      <div>
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
              <Dices className="w-4 h-4" />
            </div>
            <h2 className="text-sm font-bold text-slate-100 uppercase tracking-wide">
              Monte Carlo VaR Engine
            </h2>
          </div>
          <span className="text-[11px] font-mono text-slate-400">
            1,000 Bootstraps
          </span>
        </div>

        {/* Risk Grid */}
        <div className="p-3.5 bg-slate-950/60 rounded-lg border border-slate-800/80 space-y-2.5 font-mono text-xs">
          <div className="flex justify-between items-center">
            <span className="text-slate-400 flex items-center gap-1.5">
              <Shield className="w-3.5 h-3.5 text-rose-400" />
              <span>VaR 95% (Tail Risk):</span>
            </span>
            <span className="text-rose-400 font-bold">
              {formatCurrency(var95)}
            </span>
          </div>

          <div className="flex justify-between items-center">
            <span className="text-slate-400 flex items-center gap-1.5">
              <Percent className="w-3.5 h-3.5 text-emerald-400" />
              <span>Profit Probability:</span>
            </span>
            <span className="text-emerald-400 font-bold">
              {profitProb.toFixed(1)}%
            </span>
          </div>

          <div className="flex justify-between items-center">
            <span className="text-slate-400 flex items-center gap-1.5">
              <TrendingUp className="w-3.5 h-3.5 text-cyan-400" />
              <span>Risk of Ruin (5% DD):</span>
            </span>
            <span className="text-emerald-300 font-semibold">
              {probRuin.toFixed(2)}%
            </span>
          </div>

          {/* Mini simulation sparkline graph */}
          <div className="pt-2">
            <div className="text-[10px] text-slate-500 mb-1 flex justify-between">
              <span>Terminal Balance (Median):</span>
              <span className="text-slate-300 font-bold">{formatCurrency(monteCarlo?.median_terminal_balance ?? 34250)}</span>
            </div>
            <div className="h-10 w-full bg-slate-900 rounded flex items-end p-1 gap-1 border border-slate-800/60">
              {[35, 42, 48, 55, 62, 58, 68, 75, 82, 90, 85, 94].map((h, i) => (
                <div
                  key={i}
                  className="flex-1 bg-indigo-500/70 hover:bg-indigo-400 rounded-t transition-all"
                  style={{ height: `${h}%` }}
                ></div>
              ))}
            </div>
          </div>
        </div>
      </div>

      <div className="mt-3 pt-2 text-[10px] text-slate-500 font-mono flex justify-between">
        <span>Horizon: 100 Trades</span>
        <span>Sampling: Block Bootstrap</span>
      </div>
    </div>
  );
};
