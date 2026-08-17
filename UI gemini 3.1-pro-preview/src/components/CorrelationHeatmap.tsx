import React from 'react';
import { Network, AlertCircle } from 'lucide-react';
import { CorrelationMatrixData, AssetSymbol } from '../types';

interface CorrelationHeatmapProps {
  data: CorrelationMatrixData | null;
  onSelectAsset?: (asset: AssetSymbol) => void;
}

export const CorrelationHeatmap: React.FC<CorrelationHeatmapProps> = ({
  data,
  onSelectAsset,
}) => {
  const assets = data?.assets || ['XAUUSD', 'XAGUSD', 'BTCUSD', 'EURUSD', 'GBPUSD'];
  const matrix = data?.matrix || [
    [1.0, 0.84, 0.32, -0.48, -0.36],
    [0.84, 1.0, 0.28, -0.42, -0.31],
    [0.32, 0.28, 1.0, -0.15, -0.08],
    [-0.48, -0.42, -0.15, 1.0, 0.82],
    [-0.36, -0.31, -0.08, 0.82, 1.0],
  ];

  const getCellColor = (val: number, isDiag: boolean) => {
    if (isDiag) return 'bg-slate-800/80 text-slate-100 font-bold';
    if (val >= 0.8) return 'bg-emerald-900/70 text-emerald-200 font-bold border border-emerald-700/50';
    if (val <= -0.8) return 'bg-rose-900/70 text-rose-200 font-bold border border-rose-700/50';
    if (val > 0.3) return 'bg-emerald-950/40 text-emerald-400';
    if (val < -0.3) return 'bg-rose-950/40 text-rose-400';
    return 'bg-slate-900/50 text-slate-400';
  };

  return (
    <div id="correlation-heatmap-card" className="bg-slate-900/80 border border-slate-800 rounded-xl p-5 shadow-xl flex flex-col justify-between">
      <div>
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
              <Network className="w-4 h-4" />
            </div>
            <h2 className="text-base font-bold text-slate-100">
              Rolling Correlation Heatmap
            </h2>
          </div>
          <span className="text-[11px] font-mono text-slate-400">
            Window: 500 bars M5
          </span>
        </div>

        {/* 5x5 Matrix table */}
        <div className="overflow-x-auto">
          <table className="w-full text-center text-xs font-mono">
            <thead>
              <tr className="border-b border-slate-800 text-slate-400">
                <th className="p-2 text-left"></th>
                {assets.map((a) => (
                  <th key={a} className="p-2 font-bold text-slate-300">
                    {a}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/50">
              {assets.map((rowAsset, rIdx) => (
                <tr key={rowAsset}>
                  <td
                    className="p-2 font-bold text-slate-300 text-left cursor-pointer hover:text-amber-400"
                    onClick={() => onSelectAsset && onSelectAsset(rowAsset)}
                  >
                    {rowAsset}
                  </td>
                  {assets.map((colAsset, cIdx) => {
                    const val = matrix[rIdx]?.[cIdx] ?? 0;
                    const isDiag = rIdx === cIdx;
                    return (
                      <td key={colAsset} className="p-1.5">
                        <div
                          className={`py-1.5 px-2 rounded text-[11px] ${getCellColor(val, isDiag)}`}
                        >
                          {val.toFixed(2)}
                        </div>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Diversification note */}
      <div className="mt-4 pt-3 border-t border-slate-800/80 flex flex-col gap-2 text-[11px] text-slate-400">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1">
            <span className="w-2.5 h-2.5 rounded bg-emerald-700"></span>
            <span>Positive (≥ +0.80)</span>
          </div>
          <div className="flex items-center gap-1">
            <span className="w-2.5 h-2.5 rounded bg-rose-700"></span>
            <span>Inverse (≤ -0.80)</span>
          </div>
        </div>
        <div className="text-slate-500 flex items-center gap-1">
          <AlertCircle className="w-3.5 h-3.5 text-amber-400 flex-shrink-0" />
          <span>Cluster cap: Max 0.40% equity risk across correlated pairs</span>
        </div>
      </div>
    </div>
  );
};
