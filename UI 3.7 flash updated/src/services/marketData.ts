/**
 * @deprecated: mock generator, only for local dev without Python backend.
 * Production and staging traffic is proxied directly to realtime/app.py.
 */
import { Candle } from '../types.js';

export interface AssetConfig {
  symbol: string;
  name: string;
  basePrice: number;
  volatility: number;
  timeframe: string;
  stepMin: number;
  stepMax: number;
  decimals: number;
}

export const ASSET_CONFIGS: Record<string, AssetConfig> = {
  XAUUSD: {
    symbol: 'XAUUSD',
    name: 'Gold vs US Dollar',
    basePrice: 2465.50,
    volatility: 4.20,
    timeframe: 'M15',
    stepMin: 3.0,
    stepMax: 9.0,
    decimals: 2,
  },
  XAGUSD: {
    symbol: 'XAGUSD',
    name: 'Silver vs US Dollar',
    basePrice: 28.40,
    volatility: 0.35,
    timeframe: 'M15',
    stepMin: 0.1,
    stepMax: 1.0,
    decimals: 3,
  },
  BTCUSD: {
    symbol: 'BTCUSD',
    name: 'Bitcoin vs US Dollar',
    basePrice: 59400.0,
    volatility: 350.0,
    timeframe: 'M5',
    stepMin: 20.0,
    stepMax: 50.0,
    decimals: 1,
  },
  EURUSD: {
    symbol: 'EURUSD',
    name: 'Euro vs US Dollar',
    basePrice: 1.0895,
    volatility: 0.0012,
    timeframe: 'H1',
    stepMin: 0.0005,
    stepMax: 0.005,
    decimals: 5,
  },
  GBPUSD: {
    symbol: 'GBPUSD',
    name: 'British Pound vs US Dollar',
    basePrice: 1.2840,
    volatility: 0.0016,
    timeframe: 'H1',
    stepMin: 0.0005,
    stepMax: 0.006,
    decimals: 5,
  },
};

// Seeded/deterministic candle generator
export function generateCandles(asset: string, count: number = 100): Candle[] {
  const cfg = ASSET_CONFIGS[asset] || ASSET_CONFIGS.XAUUSD;
  const candles: Candle[] = [];
  const now = Date.now();
  const stepMs = 5 * 60 * 1000; // 5 min default

  let currentClose = cfg.basePrice;
  const startTime = now - count * stepMs;

  for (let i = 0; i < count; i++) {
    const time = startTime + i * stepMs;
    const date = new Date(time);
    const hour = date.getUTCHours();

    // Session volatility multiplier
    let sessionVol = 1.0;
    if (hour >= 8 && hour < 13) sessionVol = 1.4; // London
    else if (hour >= 13 && hour < 22) sessionVol = 1.8; // New York
    else sessionVol = 0.7; // Asia

    // Sine trend + pseudo-random noise
    const trendCycle = Math.sin(i / 12) * cfg.volatility * 0.3;
    const noise = (Math.sin(i * 997 + 13) * 0.7 + Math.cos(i * 331 + 7) * 0.3) * cfg.volatility * sessionVol;
    const change = trendCycle + noise;

    const open = currentClose;
    const close = Number((open + change).toFixed(cfg.decimals));
    const high = Number((Math.max(open, close) + Math.abs(noise * 0.6) + 0.1 * cfg.volatility).toFixed(cfg.decimals));
    const low = Number((Math.min(open, close) - Math.abs(noise * 0.5) - 0.1 * cfg.volatility).toFixed(cfg.decimals));
    const volume = Math.floor(100 + Math.abs(Math.sin(i * 17) * 800) * sessionVol);

    currentClose = close;
    candles.push({
      timestamp_utc: Math.floor(time / 1000),
      open,
      high,
      low,
      close,
      volume,
    });
  }

  // Calculate ATR and Regimes
  const period = 14;
  for (let i = 0; i < candles.length; i++) {
    if (i < 1) {
      candles[i].atr = Number((candles[i].high - candles[i].low).toFixed(cfg.decimals));
      candles[i].regime = 'range';
      continue;
    }

    const tr = Math.max(
      candles[i].high - candles[i].low,
      Math.abs(candles[i].high - candles[i - 1].close),
      Math.abs(candles[i].low - candles[i - 1].close)
    );

    if (i < period) {
      candles[i].atr = Number(tr.toFixed(cfg.decimals));
    } else {
      const prevAtr = candles[i - 1].atr || tr;
      candles[i].atr = Number(((prevAtr * (period - 1) + tr) / period).toFixed(cfg.decimals));
    }

    // Determine regime
    const lookback = Math.min(i, 20);
    const slice = candles.slice(i - lookback, i + 1);
    const startP = slice[0].close;
    const endP = slice[slice.length - 1].close;
    const diff = endP - startP;
    const atrVal = candles[i].atr || 1.0;

    if (diff > 1.8 * atrVal) {
      candles[i].regime = 'trend_up';
    } else if (diff < -1.8 * atrVal) {
      candles[i].regime = 'trend_down';
    } else if (atrVal < cfg.volatility * 0.4) {
      candles[i].regime = 'compression';
    } else {
      candles[i].regime = 'range';
    }
  }

  return candles;
}

export function getCurrentSession(): string {
  const hour = new Date().getUTCHours();
  if (hour >= 0 && hour < 8) return 'asia';
  if (hour >= 8 && hour < 13) return 'london';
  if (hour >= 13 && hour < 22) return 'newyork';
  return 'off_session';
}
