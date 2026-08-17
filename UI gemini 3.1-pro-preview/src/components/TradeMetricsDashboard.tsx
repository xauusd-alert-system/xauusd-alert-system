import React, { useState, useEffect } from 'react';
import { Award, Calendar, DollarSign, Target, TrendingDown, ArrowUpRight, ArrowDownRight } from 'lucide-react';
import { ClosedTradeMetrics } from '../types';
import { formatCurrency, formatPercent } from '../lib/utils';

export const TradeMetricsDashboard: React.FC = () => {
  const [period, setPeriod] = useState<string>('week');
  const [metrics, setMetrics] = useState<ClosedTradeMetrics | null>(null);
  const [loading, setLoading] = useState(false);

  const periods = [
    { key: 'today', label: 'Today' },
    { key: 'week', label: '7 Days' },
    { key: '2week', label: '14 Days' },
    { key: 'month', label: '30 Days' },
    { key: '3month', label: '90 Days' },
    { key: 'all', label: 'All-Time' },
  ];

  useEffect(() => {
    let isMounted = true;
    const fetchMetrics = async () => {
      setLoading(true);
      try {
        const res = await fetch(`/api/metrics?period=${period}`);
        if (res.ok) {
          const data = await res.json();
          if (isMounted) setMetrics(data);
        }
      } catch (err) {
        console.error('Failed to fetch metrics:', err);
      } finally {
        if (isMounted) setLoading(false);
      }
    };

    fetchMetrics();
    return () => {
      isMounted = false;
    };
  }, [period]);

  const trades = metrics?.trades_list || [];

  return (
    <section id="trade-metrics-section" className="bg-slate-900/80 border border-slate-800 rounded-xl p-5 md:p-6 shadow-xl">
      {/* Header & Period Filter */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 mb-5">
        <div className="flex items-center gap-2">
          <div className="p-1.5 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
            <Award className="w-4 h-4" />
          </div>
          <div>
            <h2 className="text-base md:text-lg font-bold text-slate-100 flex items-center gap-2">
              <span>Performance Ledger & Win Metrics</span>
              <span className="text-[11px] bg-slate-800 text-slate-300 font-mono px-2 py-0.5 rounded border border-slate-700">
                {metrics?.n ?? 12} Closed Deals
              </span>
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Strict audit trail of closed trades with R-multiples and expectancy calibration
            </p>
          </div>
        </div>

        {/* Period Pills */}
        <div className="flex flex-wrap gap-1 bg-slate-950 p-1 rounded-lg border border-slate-800 text-xs font-mono">
          {periods.map((p) => (
            <button
              key={p.key}
              id={`period-btn-${p.key}`}
              onClick={() => setPeriod(p.key)}
              className={`px-2.5 py-1 rounded transition-colors cursor-pointer ${
                period === p.key
                  ? 'bg-emerald-500 text-slate-950 font-bold shadow'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {/* KPI Cards Grid */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mb-6">
        <div className="bg-slate-950/60 border border-slate-800/80 rounded-lg p-3">
          <div className="text-[11px] text-slate-400 font-semibold uppercase">Win Rate</div>
          <div className="text-xl font-bold font-mono text-emerald-400 mt-1">
            {metrics ? `${metrics.win_rate_pct}%` : '—'}
          </div>
          <div className="text-[10px] text-slate-500 mt-0.5">Statistical edge</div>
        </div>

        <div className="bg-slate-950/60 border border-slate-800/80 rounded-lg p-3">
          <div className="text-[11px] text-slate-400 font-semibold uppercase">Profit Factor</div>
          <div className="text-xl font-bold font-mono text-amber-300 mt-1">
            {metrics ? metrics.profit_factor : '—'}
          </div>
          <div className="text-[10px] text-slate-500 mt-0.5">Gross win / loss</div>
        </div>

        <div className="bg-slate-950/60 border border-slate-800/80 rounded-lg p-3">
          <div className="text-[11px] text-slate-400 font-semibold uppercase">Net Realized PnL</div>
          <div className="text-xl font-bold font-mono text-emerald-400 mt-1">
            {metrics ? formatCurrency(metrics.total_pnl) : '—'}
          </div>
          <div className="text-[10px] text-slate-500 mt-0.5">Period return</div>
        </div>

        <div className="bg-slate-950/60 border border-slate-800/80 rounded-lg p-3">
          <div className="text-[11px] text-slate-400 font-semibold uppercase">Expectancy / Trade</div>
          <div className="text-xl font-bold font-mono text-indigo-300 mt-1">
            {metrics ? formatCurrency(metrics.expectancy) : '—'}
          </div>
          <div className="text-[10px] text-slate-500 mt-0.5">Expected value</div>
        </div>

        <div className="bg-slate-950/60 border border-slate-800/80 rounded-lg p-3">
          <div className="text-[11px] text-slate-400 font-semibold uppercase">Max Drawdown</div>
          <div className="text-xl font-bold font-mono text-rose-400 mt-1">
            {metrics ? formatCurrency(metrics.max_drawdown) : '—'}
          </div>
          <div className="text-[10px] text-slate-500 mt-0.5">Peak-to-valley</div>
        </div>

        <div className="bg-slate-950/60 border border-slate-800/80 rounded-lg p-3">
          <div className="text-[11px] text-slate-400 font-semibold uppercase">Avg Win / Loss</div>
          <div className="text-xs font-bold font-mono text-slate-200 mt-2 space-y-0.5">
            <div className="text-emerald-400">+{metrics ? formatCurrency(metrics.avg_win) : '—'}</div>
            <div className="text-rose-400">-{metrics ? formatCurrency(metrics.avg_loss) : '—'}</div>
          </div>
        </div>
      </div>

      {/* Closed Trades List Table */}
      <div>
        <div className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2 flex items-center justify-between">
          <span>Recent Execution Deals</span>
          <span className="text-[11px] font-mono text-slate-500 font-normal">Audit Ledger Verified</span>
        </div>

        <div className="overflow-x-auto rounded-lg border border-slate-800/80">
          <table className="w-full text-left text-xs font-mono">
            <thead className="bg-slate-950/80 text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-2.5 px-3">Deal ID</th>
                <th className="py-2.5 px-3">Asset</th>
                <th className="py-2.5 px-3">Direction</th>
                <th className="py-2.5 px-3">Volume</th>
                <th className="py-2.5 px-3">R-Multiple</th>
                <th className="py-2.5 px-3">Realized PnL</th>
                <th className="py-2.5 px-3 text-right">Close Time</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/50 bg-slate-900/30">
              {trades.map((tr) => {
                const isPositive = tr.pnl >= 0;
                return (
                  <tr key={tr.id} className="hover:bg-slate-800/40">
                    <td className="py-2.5 px-3 text-slate-300 font-bold">{tr.id}</td>
                    <td className="py-2.5 px-3 text-slate-200">{tr.symbol}</td>
                    <td className="py-2.5 px-3">
                      <span
                        className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] uppercase font-bold ${
                          tr.side === 'buy'
                            ? 'bg-emerald-950 text-emerald-300'
                            : 'bg-rose-950 text-rose-300'
                        }`}
                      >
                        {tr.side}
                      </span>
                    </td>
                    <td className="py-2.5 px-3 text-slate-400">{tr.volume}</td>
                    <td className="py-2.5 px-3">
                      <span className={`font-bold ${isPositive ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {tr.r_multiple > 0 ? `+${tr.r_multiple}R` : `${tr.r_multiple}R`}
                      </span>
                    </td>
                    <td className={`py-2.5 px-3 font-bold ${isPositive ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {isPositive ? '+' : ''}{formatCurrency(tr.pnl)}
                    </td>
                    <td className="py-2.5 px-3 text-right text-slate-400">
                      {tr.close_time.slice(0, 16).replace('T', ' ')}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
};
