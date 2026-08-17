import React from 'react';
import { Layers, ShieldCheck, ArrowUpRight, ArrowDownRight, Inbox } from 'lucide-react';
import { OpenPosition } from '../types';
import { formatCurrency, formatNumber } from '../lib/utils';

interface PositionsMonitorProps {
  positions: OpenPosition[];
}

export const PositionsMonitor: React.FC<PositionsMonitorProps> = ({ positions }) => {
  return (
    <div id="positions-monitor-card" className="bg-slate-900/80 border border-slate-800 rounded-xl p-5 shadow-xl flex flex-col justify-between">
      <div>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
              <Layers className="w-4 h-4" />
            </div>
            <h2 className="text-base font-bold text-slate-100">
              Active MT5 Positions
            </h2>
          </div>
          <span className="text-[11px] font-mono text-slate-400">
            Hedging Account • Netting Engine
          </span>
        </div>

        {/* Position cards */}
        <div className="space-y-2.5">
          {positions.length > 0 ? (
            positions.map((pos) => {
              const isBuy = pos.direction === 'buy';
              const pnlPositive = pos.profit >= 0;

              return (
                <div
                  key={pos.ticket}
                  className="bg-slate-950/60 border border-slate-800 rounded-lg p-3.5 flex items-center justify-between font-mono text-xs hover:border-slate-700 transition-colors"
                >
                  <div className="space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-slate-100 text-sm">{pos.symbol}</span>
                      <span
                        className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase flex items-center gap-0.5 ${
                          isBuy
                            ? 'bg-emerald-950 text-emerald-300 border border-emerald-800'
                            : 'bg-rose-950 text-rose-300 border border-rose-800'
                        }`}
                      >
                        {isBuy ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
                        {pos.direction}
                      </span>
                      <span className="text-slate-400 text-[11px]">
                        {pos.volume} lots
                      </span>
                    </div>

                    <div className="text-[11px] text-slate-400">
                      Entry: <span className="text-slate-200">{formatNumber(pos.open_price)}</span>
                      <span className="mx-1.5 text-slate-600">•</span>
                      Current: <span className="text-slate-200">{formatNumber(pos.current_price)}</span>
                    </div>
                  </div>

                  <div className="text-right space-y-0.5">
                    <div
                      className={`text-sm font-bold ${
                        pnlPositive ? 'text-emerald-400' : 'text-rose-400'
                      }`}
                    >
                      {pnlPositive ? '+' : ''}{formatCurrency(pos.profit)}
                    </div>
                    <div className="text-[10px] text-slate-500">
                      SL: {pos.sl ? formatNumber(pos.sl) : '—'} | TP: {pos.tp ? formatNumber(pos.tp) : '—'}
                    </div>
                  </div>
                </div>
              );
            })
          ) : (
            <div className="py-10 text-center text-slate-500 text-xs">
              <Inbox className="w-8 h-8 mx-auto mb-2 opacity-50" />
              <span>No open positions active in MT5 terminal</span>
            </div>
          )}
        </div>
      </div>

      <div className="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-slate-500 font-mono">
        <div className="flex items-center gap-1.5 text-emerald-400">
          <ShieldCheck className="w-3.5 h-3.5" />
          <span>Stop-Loss Guaranteed at Broker</span>
        </div>
        <span>Max Concurrent: 3</span>
      </div>
    </div>
  );
};
