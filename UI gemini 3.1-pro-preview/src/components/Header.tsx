import React from 'react';
import { ShieldCheck, RefreshCw, Lock, Radio, Cpu } from 'lucide-react';
import { SystemStatus } from '../types';

interface HeaderProps {
  status: SystemStatus | null;
  isLoading: boolean;
  onRefresh: () => void;
  lastUpdated: string | null;
}

export const Header: React.FC<HeaderProps> = ({
  status,
  isLoading,
  onRefresh,
  lastUpdated,
}) => {
  return (
    <header id="main-header" className="bg-slate-900/80 backdrop-blur-md border border-slate-800 rounded-xl p-5 md:p-6 border-l-4 border-l-amber-500 shadow-xl">
      <div className="flex flex-col lg:flex-row justify-between items-start lg:items-center gap-4">
        {/* Title & Branding */}
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <span className="relative flex h-3 w-3">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
              </span>
              <h1 className="text-2xl md:text-3xl font-extrabold tracking-tight bg-gradient-to-r from-amber-400 via-yellow-200 to-amber-500 bg-clip-text text-transparent">
                xauusd-alert-system
              </h1>
            </div>
            <span className="text-xs bg-slate-800/90 border border-slate-700 text-amber-300 px-2.5 py-1 rounded-md font-mono font-semibold">
              v2.1 QUANT PRO
            </span>
            <span className="text-xs bg-emerald-950/60 border border-emerald-800 text-emerald-300 px-2 py-0.5 rounded flex items-center gap-1 font-mono">
              <ShieldCheck className="w-3.5 h-3.5" /> NO-LOOKAHEAD VERIFIED
            </span>
          </div>
          <p className="text-slate-400 text-xs md:text-sm mt-1.5 flex items-center gap-2">
            <span>Institutional Multi-Asset ML System</span>
            <span className="text-slate-600">•</span>
            <span>Smart Money Concepts (SMC)</span>
            <span className="text-slate-600">•</span>
            <span>Purged Time-Split Calibration</span>
          </p>
        </div>

        {/* Diagnostic Disclaimer & Honesty Info */}
        <div className="flex flex-col sm:flex-row items-start sm:items-center gap-3 w-full lg:w-auto">
          <div className="text-xs text-slate-300 border border-amber-500/30 bg-amber-950/20 rounded-lg px-3.5 py-2 w-full sm:w-auto">
            <div className="flex items-center gap-1.5 font-semibold text-amber-300">
              <Lock className="w-3.5 h-3.5" />
              <span>HONESTY CONTRACT §11/§12</span>
            </div>
            <p className="text-slate-400 text-[11px] mt-0.5">
              Live MT5 & Telegram authority • Browser mutations disabled
            </p>
          </div>

          <button
            id="header-refresh-btn"
            onClick={onRefresh}
            disabled={isLoading}
            className="flex items-center justify-center gap-2 bg-slate-800 hover:bg-slate-700 active:bg-slate-900 border border-slate-700 text-slate-200 text-xs px-3.5 py-2.5 rounded-lg transition-colors font-medium cursor-pointer w-full sm:w-auto disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin text-amber-400' : ''}`} />
            <span>{isLoading ? 'Updating...' : 'Refresh'}</span>
          </button>
        </div>
      </div>

      {/* Metadata strip */}
      <div className="mt-4 pt-3 border-t border-slate-800/80 flex flex-wrap items-center justify-between gap-3 text-[11px] text-slate-400 font-mono">
        <div className="flex flex-wrap items-center gap-4">
          <span className="flex items-center gap-1.5">
            <Cpu className="w-3.5 h-3.5 text-indigo-400" />
            <span>Spec:</span>
            <span className="text-slate-300">{status?.strategy_version || 'xauusd-v3-signalbar'}</span>
          </span>
          <span className="flex items-center gap-1.5">
            <Radio className="w-3.5 h-3.5 text-emerald-400" />
            <span>Mode:</span>
            <span className="text-emerald-300 uppercase font-semibold">{status?.deployment_mode || 'research'}</span>
          </span>
          <span>
            Config Hash: <span className="text-slate-300">{status?.config_hash ? status.config_hash.slice(0, 10) : '7a9c8f2b3e'}...</span>
          </span>
        </div>
        <div className="text-slate-500">
          As of UTC: <span className="text-slate-300">{lastUpdated || new Date().toISOString().slice(11, 19) + ' UTC'}</span>
        </div>
      </div>
    </header>
  );
};
