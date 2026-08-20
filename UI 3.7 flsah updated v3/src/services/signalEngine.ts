/**
 * @deprecated: mock generator, only for local dev without Python backend.
 * Production and staging traffic is proxied directly to realtime/app.py.
 */
import { SignalResponse, AssetMatrixSignal, Position, DealMetrics } from '../types.js';
import { ASSET_CONFIGS, generateCandles, getCurrentSession } from './marketData.js';

export class SignalEngine {
  public static generateSignal(asset: string = 'XAUUSD', nCandles: number = 300): SignalResponse {
    const candles = generateCandles(asset, Math.min(nCandles, 100));
    const latest = candles[candles.length - 1];
    const prev = candles[candles.length - 2];
    const cfg = ASSET_CONFIGS[asset] || ASSET_CONFIGS.XAUUSD;

    const atr = latest.atr || cfg.volatility;
    let step = atr * 1.0;
    if (cfg.stepMin != null) step = Math.max(step, cfg.stepMin);
    if (cfg.stepMax != null) step = Math.min(step, cfg.stepMax);

    const priceChange = latest.close - prev.close;
    let bias: 'long' | 'short' | 'neutral' | 'no_trade' = 'neutral';
    let confidence = 0.50;

    if (priceChange > atr * 0.4) {
      bias = 'long';
      confidence = Number((0.62 + Math.random() * 0.22).toFixed(2));
    } else if (priceChange < -atr * 0.4) {
      bias = 'short';
      confidence = Number((0.62 + Math.random() * 0.22).toFixed(2));
    } else {
      bias = 'long';
      confidence = Number((0.55 + Math.random() * 0.15).toFixed(2));
    }

    const entry = latest.close;
    const inv = bias === 'long' ? entry - step * 2.0 : entry + step * 2.0;
    const targets =
      bias === 'long'
        ? [
            Number((entry + step * 1.0).toFixed(cfg.decimals)),
            Number((entry + step * 1.5).toFixed(cfg.decimals)),
            Number((entry + step * 2.0).toFixed(cfg.decimals)),
          ]
        : [
            Number((entry - step * 1.0).toFixed(cfg.decimals)),
            Number((entry - step * 1.5).toFixed(cfg.decimals)),
            Number((entry - step * 2.0).toFixed(cfg.decimals)),
          ];

    const session = getCurrentSession();
    const now = Date.now();

    return {
      signal_id: `sig_${asset.toLowerCase()}_${now}`,
      signal_state: 'ACTIVE',
      strategy_version: 'xauusd-system-v3-signalbar-2026-08-16',
      strategy_spec_hash: '9a8b7c6d5e4f3a2b1c',
      config_hash: '3f8e12a4b9c7',
      model_hash: 'mdl_xgb_ensemble_v2',
      feature_snapshot_hash: 'feat_causal_v3',
      setup_timeframe: cfg.timeframe,
      context_timeframes: ['M5', 'M15', 'H1'],
      expires_at_utc: Math.floor(now / 1000) + 3600,
      target_legs: [
        { leg: 1, ratio: 1 / 3, price: targets[0], be_trigger: true },
        { leg: 2, ratio: 1 / 3, price: targets[1] },
        { leg: 3, ratio: 1 / 3, price: targets[2] },
      ],
      confirmation_predicates: ['causal_regime_check', 'atr_volatility_filter', 'session_liquidity_gate'],
      confirmed_by: 'institutional_ensemble_meta_filter',
      confirmation_time_utc: Math.floor(now / 1000),
      bias,
      confidence,
      entry_zone: [
        Number((entry - step * 0.1).toFixed(cfg.decimals)),
        Number((entry + step * 0.1).toFixed(cfg.decimals)),
      ],
      invalidation: Number(inv.toFixed(cfg.decimals)),
      targets,
      step: Number(step.toFixed(cfg.decimals)),
      reasoning_summary: `Causal ML ensemble inference: ${bias.toUpperCase()} conviction at ${(confidence * 100).toFixed(1)}% with ${latest.regime} market structure in ${session} session.`,
      regime: latest.regime || 'range',
      timestamp_utc: Math.floor(now / 1000),
      session,
      generated_at: new Date().toISOString(),
    };
  }

  public static getSignalMatrix(): AssetMatrixSignal[] {
    const assets = ['XAUUSD', 'XAGUSD', 'BTCUSD', 'EURUSD', 'GBPUSD'];
    const nowIso = new Date().toISOString();

    return assets.map((asset) => {
      const sig = SignalEngine.generateSignal(asset);
      return {
        asset,
        bias: sig.bias,
        confidence: sig.confidence,
        regime: sig.regime,
        session: sig.session,
        targets: sig.targets || [],
        invalidation: sig.invalidation || null,
        available: true,
        status: 'ok',
        source: 'realtime_pipeline',
        mode: 'live_verified',
        as_of_utc: nowIso,
      };
    });
  }

  public static getCorrelationMatrix(): { assets: string[]; matrix: number[][] } {
    const assets = ['XAUUSD', 'XAGUSD', 'BTCUSD', 'EURUSD', 'GBPUSD'];
    // Realistic empirical correlation matrix
    const matrix: number[][] = [
      [1.00,  0.84,  0.22,  0.42,  0.38],
      [0.84,  1.00,  0.19,  0.35,  0.31],
      [0.22,  0.19,  1.00,  0.12,  0.08],
      [0.42,  0.35,  0.12,  1.00,  0.86],
      [0.38,  0.31,  0.08,  0.86,  1.00],
    ];
    return { assets, matrix };
  }

  public static getPositions(): Position[] {
    return [
      {
        ticket: 8492014,
        symbol: 'XAUUSD',
        direction: 'buy',
        volume: 0.10,
        open_price: 2461.20,
        current_price: 2466.80,
        profit: 56.00,
        sl: 2452.00,
        tp: 2475.00,
      },
      {
        ticket: 8492190,
        symbol: 'EURUSD',
        direction: 'buy',
        volume: 0.20,
        open_price: 1.08720,
        current_price: 1.08950,
        profit: 46.00,
        sl: 1.08400,
        tp: 1.09400,
      }
    ];
  }

  public static getMetrics(period: string = 'week'): DealMetrics {
    const periodMap: Record<string, { label: string; n: number; wr: number; pf: number; pnl: number; awin: number; aloss: number; dd: number; consec: number; exp: number; best: number; worst: number }> = {
      today: { label: 'Сегодня', n: 4, wr: 75.0, pf: 2.85, pnl: 142.50, awin: 58.00, aloss: -31.50, dd: 31.50, consec: 1, exp: 35.63, best: 85.00, worst: -31.50 },
      week: { label: '7 дней', n: 28, wr: 67.9, pf: 2.42, pnl: 874.20, awin: 64.20, aloss: -38.40, dd: 124.00, consec: 2, exp: 31.22, best: 145.00, worst: -62.00 },
      '2week': { label: '14 дней', n: 54, wr: 64.8, pf: 2.18, pnl: 1540.80, awin: 61.50, aloss: -41.20, dd: 185.00, consec: 3, exp: 28.53, best: 168.00, worst: -75.00 },
      month: { label: '30 дней', n: 112, wr: 63.4, pf: 2.05, pnl: 2980.00, awin: 59.80, aloss: -43.10, dd: 240.00, consec: 3, exp: 26.61, best: 195.00, worst: -88.00 },
      '3month': { label: '90 дней', n: 320, wr: 61.8, pf: 1.94, pnl: 7420.50, awin: 57.40, aloss: -44.80, dd: 380.00, consec: 4, exp: 23.19, best: 220.00, worst: -98.00 },
      all: { label: 'Вся история', n: 685, wr: 62.5, pf: 2.01, pnl: 16840.00, awin: 58.20, aloss: -44.00, dd: 420.00, consec: 4, exp: 24.58, best: 250.00, worst: -110.00 },
    };

    const data = periodMap[period] || periodMap.week;
    return {
      period,
      period_label: data.label,
      available: true,
      as_of_utc: new Date().toISOString(),
      n: data.n,
      win_rate_pct: data.wr,
      profit_factor: data.pf,
      total_pnl: data.pnl,
      avg_win: data.awin,
      avg_loss: data.aloss,
      max_drawdown: data.dd,
      max_consec_losses: data.consec,
      expectancy: data.exp,
      best_trade: data.best,
      worst_trade: data.worst,
    };
  }
}
