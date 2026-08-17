import React, { useState, useEffect } from 'react';
import { BarChart3, Activity, Crosshair, Target, ShieldAlert, Layers } from 'lucide-react';
import { AssetSymbol, ChartResponse, CandleData } from '../types';
import { formatCurrency, formatNumber } from '../lib/utils';

interface ChartSectionProps {
  selectedAsset: AssetSymbol;
  onSelectAsset: (asset: AssetSymbol) => void;
}

export const ChartSection: React.FC<ChartSectionProps> = ({
  selectedAsset,
  onSelectAsset,
}) => {
  const [chartData, setChartData] = useState<ChartResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hoveredCandle, setHoveredCandle] = useState<CandleData | null>(null);

  const assetsList: AssetSymbol[] = ['XAUUSD', 'XAGUSD', 'BTCUSD', 'EURUSD', 'GBPUSD'];

  useEffect(() => {
    let isMounted = true;
    const fetchChart = async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(`/api/chart/${selectedAsset}`);
        if (!res.ok) throw new Error(`Chart fetch error: ${res.statusText}`);
        const data = await res.json();
        if (isMounted) {
          setChartData(data);
          if (data.candles && data.candles.length > 0) {
            setHoveredCandle(data.candles[data.candles.length - 1]);
          }
        }
      } catch (err: any) {
        if (isMounted) setError(err.message || 'Failed to load chart');
      } finally {
        if (isMounted) setLoading(false);
      }
    };

    fetchChart();
    const interval = setInterval(fetchChart, 6000);
    return () => {
      isMounted = false;
      clearInterval(interval);
    };
  }, [selectedAsset]);

  // Compute SVG coordinates for the candlestick chart
  const candles = chartData?.candles || [];
  const minPrice = candles.length > 0
    ? Math.min(...candles.map(c => c.low), chartData?.sl_price || Infinity)
    : 0;
  const maxPrice = candles.length > 0
    ? Math.max(...candles.map(c => c.high), ...(chartData?.tp_prices || []))
    : 100;
  const priceRange = maxPrice - minPrice || 1;

  const svgWidth = 720;
  const svgHeight = 280;
  const paddingX = 40;
  const paddingY = 25;
  const chartHeight = svgHeight - paddingY * 2;
  const chartWidth = svgWidth - paddingX * 2;

  const getY = (val: number) => {
    return paddingY + chartHeight - ((val - minPrice) / priceRange) * chartHeight;
  };

  const candleWidth = Math.max(4, (chartWidth / Math.max(candles.length, 1)) * 0.65);
  const candleGap = chartWidth / Math.max(candles.length, 1);

  return (
    <div id="chart-section" className="bg-slate-900/80 border border-slate-800 rounded-xl p-5 md:p-6 shadow-xl flex flex-col justify-between">
      <div>
        {/* Header & Asset Switcher */}
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 mb-4">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20">
              <BarChart3 className="w-4 h-4" />
            </div>
            <div>
              <h2 className="text-base font-bold text-slate-100 flex items-center gap-2">
                <span>Live Candlestick Feed & SMC Execution Grid</span>
                <span className="text-[11px] bg-slate-800 text-amber-300 font-mono px-2 py-0.5 rounded border border-slate-700">
                  {chartData?.timeframe || 'M15'}
                </span>
              </h2>
            </div>
          </div>

          {/* Asset Selector Buttons */}
          <div className="flex flex-wrap gap-1.5 bg-slate-950/80 p-1 rounded-lg border border-slate-800">
            {assetsList.map((sym) => (
              <button
                key={sym}
                id={`chart-btn-${sym}`}
                onClick={() => onSelectAsset(sym)}
                className={`px-2.5 py-1 text-xs font-mono rounded transition-colors cursor-pointer ${
                  selectedAsset === sym
                    ? 'bg-amber-500 text-slate-950 font-bold shadow'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
                }`}
              >
                {sym}
              </button>
            ))}
          </div>
        </div>

        {/* Current Candle Inspect Bar */}
        <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 bg-slate-950/60 rounded-lg border border-slate-800/80 text-xs font-mono mb-3">
          <div className="flex items-center gap-3">
            <span className="text-slate-400">Asset: <strong className="text-amber-400">{selectedAsset}</strong></span>
            <span className="text-slate-500">|</span>
            <span className="text-slate-400">Price: <strong className="text-slate-100 font-bold">{formatCurrency(chartData?.current_price ?? 0)}</strong></span>
          </div>
          {hoveredCandle && (
            <div className="flex items-center gap-2.5 text-[11px] text-slate-400">
              <span>O: <span className="text-slate-200">{formatNumber(hoveredCandle.open)}</span></span>
              <span>H: <span className="text-emerald-400">{formatNumber(hoveredCandle.high)}</span></span>
              <span>L: <span className="text-rose-400">{formatNumber(hoveredCandle.low)}</span></span>
              <span>C: <span className="text-slate-200">{formatNumber(hoveredCandle.close)}</span></span>
              <span>Vol: <span className="text-cyan-300">{hoveredCandle.volume}</span></span>
            </div>
          )}
        </div>

        {/* Chart Canvas / SVG Display */}
        <div className="w-full bg-slate-950/90 rounded-lg border border-slate-800/90 p-2 overflow-x-auto relative min-h-[300px] flex items-center justify-center">
          {loading && !chartData ? (
            <div className="flex items-center gap-2 text-xs text-slate-400 font-mono">
              <Activity className="w-4 h-4 animate-spin text-amber-400" />
              <span>Fetching M15 candlestick series...</span>
            </div>
          ) : error ? (
            <div className="text-xs text-rose-400 font-mono">
              Chart feed offline: {error}
            </div>
          ) : (
            <svg
              viewBox={`0 0 ${svgWidth} ${svgHeight}`}
              className="w-full h-[280px] overflow-visible select-none"
            >
              {/* Grid Lines */}
              {[0, 0.25, 0.5, 0.75, 1].map((pct, i) => {
                const y = paddingY + chartHeight * pct;
                const price = maxPrice - priceRange * pct;
                return (
                  <g key={i}>
                    <line
                      x1={paddingX}
                      y1={y}
                      x2={svgWidth - paddingX}
                      y2={y}
                      stroke="#1e293b"
                      strokeDasharray="3 3"
                      strokeWidth="1"
                    />
                    <text
                      x={svgWidth - paddingX + 6}
                      y={y + 3}
                      fill="#64748b"
                      fontSize="9"
                      fontFamily="JetBrains Mono"
                    >
                      {formatNumber(price)}
                    </text>
                  </g>
                );
              })}

              {/* Execution Level Targets Overlay (TP1, TP2, TP3, SL, Entry) */}
              {chartData && (
                <>
                  {/* Stop Loss Line */}
                  <line
                    x1={paddingX}
                    y1={getY(chartData.sl_price)}
                    x2={svgWidth - paddingX}
                    y2={getY(chartData.sl_price)}
                    stroke="#f43f5e"
                    strokeWidth="1.5"
                    strokeDasharray="4 2"
                  />
                  <text
                    x={paddingX + 6}
                    y={getY(chartData.sl_price) - 4}
                    fill="#f43f5e"
                    fontSize="9"
                    fontWeight="bold"
                    fontFamily="JetBrains Mono"
                  >
                    STOP LOSS: {formatNumber(chartData.sl_price)}
                  </text>

                  {/* Entry Zone Line */}
                  <line
                    x1={paddingX}
                    y1={getY(chartData.entry_price)}
                    x2={svgWidth - paddingX}
                    y2={getY(chartData.entry_price)}
                    stroke="#38bdf8"
                    strokeWidth="1.5"
                  />
                  <text
                    x={paddingX + 6}
                    y={getY(chartData.entry_price) - 4}
                    fill="#38bdf8"
                    fontSize="9"
                    fontWeight="bold"
                    fontFamily="JetBrains Mono"
                  >
                    ENTRY ZONE: {formatNumber(chartData.entry_price)}
                  </text>

                  {/* Take Profit Target Lines */}
                  {chartData.tp_prices.map((tp, idx) => (
                    <g key={idx}>
                      <line
                        x1={paddingX}
                        y1={getY(tp)}
                        x2={svgWidth - paddingX}
                        y2={getY(tp)}
                        stroke="#10b981"
                        strokeWidth="1.5"
                        strokeDasharray="4 2"
                      />
                      <text
                        x={paddingX + 6}
                        y={getY(tp) - 4}
                        fill="#10b981"
                        fontSize="9"
                        fontWeight="bold"
                        fontFamily="JetBrains Mono"
                      >
                        TP{idx + 1}: {formatNumber(tp)}
                      </text>
                    </g>
                  ))}
                </>
              )}

              {/* Candlesticks */}
              {candles.map((candle, idx) => {
                const x = paddingX + idx * candleGap + candleGap / 2;
                const isBullish = candle.close >= candle.open;
                const candleColor = isBullish ? '#10b981' : '#f43f5e';
                const yOpen = getY(candle.open);
                const yClose = getY(candle.close);
                const yHigh = getY(candle.high);
                const yLow = getY(candle.low);
                const rectY = Math.min(yOpen, yClose);
                const rectHeight = Math.max(Math.abs(yClose - yOpen), 2);

                return (
                  <g
                    key={idx}
                    className="cursor-pointer transition-opacity hover:opacity-80"
                    onMouseEnter={() => setHoveredCandle(candle)}
                  >
                    {/* Fair Value Gap (FVG) highlight box */}
                    {candle.fvgTop && candle.fvgBottom && (
                      <rect
                        x={x - candleWidth * 1.8}
                        y={getY(candle.fvgTop)}
                        width={candleWidth * 3.6}
                        height={Math.max(2, getY(candle.fvgBottom) - getY(candle.fvgTop))}
                        fill="rgba(6, 182, 212, 0.15)"
                        stroke="rgba(6, 182, 212, 0.4)"
                        strokeDasharray="2 2"
                      />
                    )}

                    {/* Wick */}
                    <line
                      x1={x}
                      y1={yHigh}
                      x2={x}
                      y2={yLow}
                      stroke={candleColor}
                      strokeWidth="1.2"
                    />

                    {/* Candle Body */}
                    <rect
                      x={x - candleWidth / 2}
                      y={rectY}
                      width={candleWidth}
                      height={rectHeight}
                      fill={candleColor}
                      rx="1"
                    />
                  </g>
                );
              })}
            </svg>
          )}
        </div>
      </div>

      {/* Target Legs Legend */}
      <div className="mt-3 pt-3 border-t border-slate-800 flex flex-wrap items-center justify-between gap-3 text-xs font-mono">
        <div className="flex flex-wrap items-center gap-4">
          <span className="flex items-center gap-1.5 text-rose-400">
            <ShieldAlert className="w-3.5 h-3.5" />
            <span>Stop: 2.0x ATR</span>
          </span>
          <span className="flex items-center gap-1.5 text-sky-400">
            <Crosshair className="w-3.5 h-3.5" />
            <span>Entry Zone</span>
          </span>
          <span className="flex items-center gap-1.5 text-emerald-400">
            <Target className="w-3.5 h-3.5" />
            <span>TP1 (1.0x) • TP2 (1.5x) • TP3 (2.0x)</span>
          </span>
          <span className="flex items-center gap-1.5 text-cyan-400">
            <Layers className="w-3.5 h-3.5" />
            <span>Cyan Box = Imbalance FVG</span>
          </span>
        </div>
        <div className="text-slate-500">
          BE Trigger: Active on TP1 Fill
        </div>
      </div>
    </div>
  );
};
