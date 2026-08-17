import React from 'react';
import { Zap, RefreshCw, ChevronRight, ShieldCheck } from 'lucide-react';
import { MatrixItem, AssetSymbol } from '../types';
import { formatNumber } from '../lib/utils';

interface SignalMatrixProps {
  signals: MatrixItem[];
  isLoading: boolean;
  onRefresh: () => void;
  onSelectAsset: (asset: AssetSymbol) => void;
}

export const SignalMatrix: React.FC<SignalMatrixProps> = ({
  signals,
  isLoading,
  onRefresh,
  onSelectAsset,
}) => {
  return (
    <section id="signal-matrix-section" className="bg-slate-900/80 border border-slate-800 rounded-xl p-5 md:p-6 shadow-xl">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 mb-4">
        <div className="flex items-center gap-2">
          <div className="p-1.5 rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20">
            <Zap className="w-4 h-4" />
          </div>
          <div>
            <h2 className="text-base md:text-lg font-bold text-slate-100 flex items-center gap-2">
              <span>Multi-Asset Real-Time Signal Matrix</span>
              <span className="text-[11px] bg-slate-800 text-slate-300 font-mono px-2 py-0.5 rounded border border-slate-700">
                5 Instruments
              </span>
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Causal Ensemble Inference • Timeframe Grid • Asymmetric ATR Target Brackets
            </p>
          </div>
        </div>

        <button
          id="matrix-refresh-btn"
          onClick={onRefresh}
          disabled={isLoading}
          className="flex items-center gap-1.5 bg-slate-800 hover:bg-slate-700 active:bg-slate-900 border border-slate-700 text-slate-200 text-xs px-3 py-1.5 rounded-lg transition-colors font-medium cursor-pointer disabled:opacity-50"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin text-amber-400' : ''}`} />
          <span>Refresh Matrix</span>
        </button>
      </div>

      {/* Table of signals */}
      <div className="overflow-x-auto rounded-lg border border-slate-800/80">
        <table className="w-full text-left text-xs font-mono">
          <thead className="bg-slate-950/80 text-slate-400 uppercase tracking-wider border-b border-slate-800">
            <tr>
              <th className="py-3 px-4">Instrument</th>
              <th className="py-3 px-4">Direction / Bias</th>
              <th className="py-3 px-4">ML Confidence</th>
              <th className="py-3 px-4">Market Regime</th>
              <th className="py-3 px-4">Session</th>
              <th className="py-3 px-4">TP Targets (1 / 2 / 3)</th>
              <th className="py-3 px-4">Stop Loss</th>
              <th className="py-3 px-4 text-right">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/60 bg-slate-900/40">
            {signals.length > 0 ? (
              signals.map((sig) => {
                const isLong = sig.bias === 'long';
                const isShort = sig.bias === 'short';
                const isAvailable = sig.available;

                return (
                  <tr
                    key={sig.asset}
                    className="hover:bg-slate-800/50 transition-colors group cursor-pointer"
                    onClick={() => onSelectAsset(sig.asset)}
                  >
                    <td className="py-3 px-4">
                      <div className="font-bold text-slate-100 flex items-center gap-1.5">
                        <span>{sig.asset}</span>
                        {sig.price && (
                          <span className="text-[10px] text-slate-400 font-normal">
                            ({formatNumber(sig.price)})
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      {isAvailable ? (
                        isLong ? (
                          <span className="inline-flex items-center px-2 py-0.5 rounded bg-emerald-950 border border-emerald-800 text-emerald-300 font-bold">
                            LONG / BUY
                          </span>
                        ) : isShort ? (
                          <span className="inline-flex items-center px-2 py-0.5 rounded bg-rose-950 border border-rose-800 text-rose-300 font-bold">
                            SHORT / SELL
                          </span>
                        ) : (
                          <span className="inline-flex items-center px-2 py-0.5 rounded bg-slate-800 text-slate-300">
                            NEUTRAL
                          </span>
                        )
                      ) : (
                        <span className="inline-flex items-center px-2 py-0.5 rounded bg-amber-950/40 border border-amber-800/40 text-amber-400 text-[10px]">
                          SHADOW MODE
                        </span>
                      )}
                    </td>
                    <td className="py-3 px-4">
                      {isAvailable && sig.confidence != null ? (
                        <div className="flex items-center gap-2">
                          <span className="text-amber-300 font-bold">
                            {(sig.confidence * 100).toFixed(1)}%
                          </span>
                          <div className="w-12 bg-slate-800 h-1.5 rounded-full overflow-hidden">
                            <div
                              className="bg-amber-400 h-full rounded-full"
                              style={{ width: `${sig.confidence * 100}%` }}
                            ></div>
                          </div>
                        </div>
                      ) : (
                        <span className="text-slate-500">—</span>
                      )}
                    </td>
                    <td className="py-3 px-4">
                      {sig.regime ? (
                        <span className="px-2 py-0.5 rounded bg-slate-950 border border-slate-800 text-slate-300 text-[11px]">
                          {sig.regime}
                        </span>
                      ) : (
                        <span className="text-slate-500">—</span>
                      )}
                    </td>
                    <td className="py-3 px-4">
                      <span className="text-slate-400 uppercase text-[11px]">
                        {sig.session || '—'}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-emerald-400 font-bold">
                      {isAvailable && sig.targets && sig.targets.length > 0
                        ? sig.targets.map((t) => formatNumber(t)).join(' / ')
                        : '—'}
                    </td>
                    <td className="py-3 px-4 text-rose-400 font-bold">
                      {isAvailable && sig.invalidation != null
                        ? formatNumber(sig.invalidation)
                        : '—'}
                    </td>
                    <td className="py-3 px-4 text-right">
                      <button
                        className="text-slate-400 group-hover:text-amber-400 flex items-center gap-0.5 text-xs font-sans ml-auto transition-colors"
                      >
                        <span>Inspect</span>
                        <ChevronRight className="w-3.5 h-3.5" />
                      </button>
                    </td>
                  </tr>
                );
              })
            ) : (
              <tr>
                <td colSpan={8} className="py-6 text-center text-slate-500">
                  Loading matrix signals...
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="mt-3 flex items-center justify-between text-[11px] text-slate-500 font-mono">
        <div className="flex items-center gap-1.5">
          <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />
          <span>Triple-barrier exit criteria • Breakeven armed at TP1</span>
        </div>
        <span>Click any instrument row to load candlestick chart</span>
      </div>
    </section>
  );
};
