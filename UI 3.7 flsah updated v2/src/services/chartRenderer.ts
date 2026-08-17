/**
 * @deprecated: mock generator, only for local dev without Python backend.
 * Production and staging traffic is proxied directly to realtime/app.py.
 */
import { Candle } from '../types.js';

export class ChartRenderer {
  /**
   * Generates standalone SVG markup of candlesticks with overlay price levels.
   */
  public static renderSvgCandlestick(
    candles: Candle[],
    symbol: string,
    entryPrice?: number,
    slPrice?: number,
    tpPrices?: number[],
    width: number = 700,
    height: number = 320
  ): string {
    if (!candles || candles.length < 2) {
      return `<svg width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg"><rect width="100%" height="100%" fill="#1e293b"/><text x="50%" y="50%" fill="#94a3b8" text-anchor="middle">No chart data</text></svg>`;
    }

    const n = Math.min(candles.length, 35);
    const sliceCandles = candles.slice(-n);

    let minP = Math.min(...sliceCandles.map((c) => c.low));
    let maxP = Math.max(...sliceCandles.map((c) => c.high));

    if (entryPrice != null) {
      minP = Math.min(minP, entryPrice);
      maxP = Math.max(maxP, entryPrice);
    }
    if (slPrice != null) {
      minP = Math.min(minP, slPrice);
      maxP = Math.max(maxP, slPrice);
    }
    if (tpPrices && tpPrices.length > 0) {
      for (const tp of tpPrices) {
        minP = Math.min(minP, tp);
        maxP = Math.max(maxP, tp);
      }
    }

    const pRange = Math.max(maxP - minP, 1e-6);
    const padTop = 35;
    const padBot = 35;
    const plotH = height - padTop - padBot;

    const scaleY = (price: number) => {
      return padTop + plotH * (1.0 - (price - minP) / pRange);
    };

    const candleW = Math.max((width - 80) / n, 4);

    const svg: string[] = [
      `<svg width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg" style="background:#0b1120; font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; border-radius: 8px;">`,
      `<rect width="100%" height="100%" fill="#0b1120"/>`,
      `<text x="20" y="24" fill="#f8fafc" font-size="14" font-weight="bold">${symbol} M5 Setup &bull; Live Action</text>`,
    ];

    // Grid lines
    for (let i = 0; i < 5; i++) {
      const gridP = minP + pRange * (i / 4.0);
      const gy = scaleY(gridP);
      svg.push(`<line x1="20" y1="${gy.toFixed(1)}" x2="${width - 20}" y2="${gy.toFixed(1)}" stroke="#1e293b" stroke-dasharray="3,3"/>`);
      svg.push(`<text x="${width - 70}" y="${(gy - 4).toFixed(1)}" fill="#64748b" font-size="10">${gridP.toFixed(2)}</text>`);
    }

    // Candles
    sliceCandles.forEach((row, idx) => {
      const cx = 30 + idx * candleW + candleW / 2.0;
      const oY = scaleY(row.open);
      const cY = scaleY(row.close);
      const hY = scaleY(row.high);
      const lY = scaleY(row.low);

      const isGreen = row.close >= row.open;
      const color = isGreen ? '#10b981' : '#f43f5e';

      // Wick
      svg.push(`<line x1="${cx.toFixed(1)}" y1="${hY.toFixed(1)}" x2="${cx.toFixed(1)}" y2="${lY.toFixed(1)}" stroke="${color}" stroke-width="1.5"/>`);

      // Body
      const topB = Math.min(oY, cY);
      const botB = Math.max(oY, cY);
      const bodyH = Math.max(botB - topB, 2);
      svg.push(
        `<rect x="${(cx - candleW * 0.35).toFixed(1)}" y="${topB.toFixed(1)}" width="${(candleW * 0.7).toFixed(1)}" height="${bodyH.toFixed(1)}" fill="${color}" rx="1"/>`
      );
    });

    // Levels
    if (entryPrice != null) {
      const ey = scaleY(entryPrice);
      svg.push(`<line x1="20" y1="${ey.toFixed(1)}" x2="${width - 20}" y2="${ey.toFixed(1)}" stroke="#38bdf8" stroke-width="2" stroke-dasharray="4,4"/>`);
      svg.push(`<text x="25" y="${(ey - 6).toFixed(1)}" fill="#38bdf8" font-size="11" font-weight="bold">ENTRY: ${entryPrice.toFixed(2)}</text>`);
    }

    if (slPrice != null) {
      const sy = scaleY(slPrice);
      svg.push(`<line x1="20" y1="${sy.toFixed(1)}" x2="${width - 20}" y2="${sy.toFixed(1)}" stroke="#f43f5e" stroke-width="2"/>`);
      svg.push(`<text x="25" y="${(sy - 6).toFixed(1)}" fill="#f43f5e" font-size="11" font-weight="bold">STOP: ${slPrice.toFixed(2)}</text>`);
    }

    if (tpPrices && tpPrices.length > 0) {
      tpPrices.forEach((tp, i) => {
        const ty = scaleY(tp);
        svg.push(`<line x1="20" y1="${ty.toFixed(1)}" x2="${width - 20}" y2="${ty.toFixed(1)}" stroke="#10b981" stroke-width="1.5" stroke-dasharray="2,2"/>`);
        svg.push(`<text x="${width - 120}" y="${(ty - 6).toFixed(1)}" fill="#10b981" font-size="11" font-weight="bold">TP${i + 1}: ${tp.toFixed(2)}</text>`);
      });
    }

    svg.push('</svg>');
    return svg.join('\n');
  }
}
