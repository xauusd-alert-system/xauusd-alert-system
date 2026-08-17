import React from 'react';
import { Server, Wallet, Layers, ShieldCheck, AlertTriangle } from 'lucide-react';
import { SystemStatus } from '../types';
import { formatCurrency } from '../lib/utils';

interface KpiGridProps {
  status: SystemStatus | null;
}

export const KpiGrid: React.FC<KpiGridProps> = ({ status }) => {
  const isCircuitBreaker = status?.circuit_breaker || false;
  const floatingPnl = status?.floating_pnl ?? 0;
  const pnlIsPositive = floatingPnl >= 0;

  return (
    <div id="kpi-grid-section" className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      {/* 1. System Mode */}
      <div id="kpi-card-mode" className="bg-slate-900/70 border border-slate-800 rounded-xl p-4.5 flex flex-col justify-between shadow-md">
        <div className="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
          <span>System Environment</span>
          <Server className="w-4 h-4 text-indigo-400" />
        </div>
        <div className="mt-3">
          <div className="text-xl md:text-2xl font-extrabold text-indigo-300 font-mono tracking-tight">
            {status?.data_mode ? status.data_mode.toUpperCase() : 'RESEARCH_SIM'}
          </div>
          <div className="text-[11px] text-slate-400 mt-1 flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-indigo-400"></span>
            <span>Source: {status?.source || 'Engine Memory Ledger'}</span>
          </div>
        </div>
      </div>

      {/* 2. Balance & Equity */}
      <div id="kpi-card-balance" className="bg-slate-900/70 border border-slate-800 rounded-xl p-4.5 flex flex-col justify-between shadow-md">
        <div className="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
          <span>Balance & Equity</span>
          <Wallet className="w-4 h-4 text-emerald-400" />
        </div>
        <div className="mt-3">
          <div className="text-xl md:text-2xl font-extrabold text-emerald-400 font-mono tracking-tight">
            {formatCurrency(status?.balance ?? 28450)}
          </div>
          <div className="text-[11px] text-slate-400 mt-1 flex items-center justify-between">
            <span>Equity: {formatCurrency(status?.equity ?? 28880)}</span>
            <span className={`font-mono font-medium ${pnlIsPositive ? 'text-emerald-400' : 'text-rose-400'}`}>
              ({pnlIsPositive ? '+' : ''}{formatCurrency(floatingPnl)})
            </span>
          </div>
        </div>
      </div>

      {/* 3. Open Positions */}
      <div id="kpi-card-positions" className="bg-slate-900/70 border border-slate-800 rounded-xl p-4.5 flex flex-col justify-between shadow-md">
        <div className="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
          <span>Active Open Trades</span>
          <Layers className="w-4 h-4 text-amber-400" />
        </div>
        <div className="mt-3">
          <div className="text-xl md:text-2xl font-extrabold text-amber-300 font-mono tracking-tight flex items-baseline gap-2">
            <span>{status?.open_positions_count ?? 2}</span>
            <span className="text-xs text-slate-500 font-normal">/ max 3 positions</span>
          </div>
          <div className="text-[11px] text-slate-400 mt-1">
            Exposure: 0.15 lots • Hedging allowed
          </div>
        </div>
      </div>

      {/* 4. Risk Manager */}
      <div id="kpi-card-risk" className="bg-slate-900/70 border border-slate-800 rounded-xl p-4.5 flex flex-col justify-between shadow-md">
        <div className="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
          <span>Institutional Risk Guard</span>
          {isCircuitBreaker ? (
            <AlertTriangle className="w-4 h-4 text-rose-400" />
          ) : (
            <ShieldCheck className="w-4 h-4 text-cyan-400" />
          )}
        </div>
        <div className="mt-3">
          <div className={`text-xl md:text-2xl font-extrabold font-mono tracking-tight ${isCircuitBreaker ? 'text-rose-500' : 'text-emerald-400'}`}>
            {isCircuitBreaker ? 'HALTED' : 'NORMAL'}
          </div>
          <div className="text-[11px] text-slate-400 mt-1">
            Circuit Breaker: 5.0% Max DD • 0.25% Stop Risk
          </div>
        </div>
      </div>
    </div>
  );
};
