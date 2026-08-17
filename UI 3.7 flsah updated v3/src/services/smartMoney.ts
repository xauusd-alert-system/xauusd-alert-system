/**
 * @deprecated: mock generator, only for local dev without Python backend.
 * Production and staging traffic is proxied directly to realtime/app.py.
 */
import { Candle } from '../types.js';

export const SOURCE_KIND = 'ohlcv_proxy';

export interface SmartMoneyResult {
  manipulation_index: {
    score: number;
    max: number;
    text: string;
    display: string;
    source_kind: string;
    lookback: number;
    data_status: string;
  };
  zone_strength: {
    score: number;
    max: number;
    text: string;
    display: string;
    source_kind: string;
    lookback: number;
    data_status: string;
  };
  smf_ratio: {
    ratio: number;
    text: string;
    display: string;
    source_kind: string;
    lookback: number;
    data_status: string;
  };
  liquidity_grab: {
    score: number;
    max: number;
    text: string;
    display: string;
    source_kind: string;
    lookback: number;
    data_status: string;
  };
  delta_confidence: {
    level: string;
    text: string;
    display: string;
    source_kind: string;
    lookback: number;
    data_status: string;
  };
  source_provenance: {
    source_kind: string;
    lookbacks: Record<string, number>;
    note: string;
  };
}

export function computeInstitutionalMetrics(candles: Candle[]): SmartMoneyResult {
  const nBars = candles.length;
  const slice20 = candles.slice(-20);
  const slice30 = candles.slice(-30);
  const slice50 = candles.slice(-50);

  // 1. Manipulation Index (1-10)
  let wickRatioSum = 0;
  let absorptionBars = 0;
  const avgVol = slice20.reduce((acc, c) => acc + c.volume, 0) / Math.max(slice20.length, 1);

  slice20.forEach((c) => {
    const hl = Math.max(c.high - c.low, 1e-6);
    const body = Math.abs(c.close - c.open);
    const wr = 1.0 - body / hl;
    wickRatioSum += wr;
    if (c.volume > avgVol * 1.3 && wr > 0.6) {
      absorptionBars++;
    }
  });
  const meanWick = wickRatioSum / Math.max(slice20.length, 1);
  const rawManip = meanWick * 4.0 + absorptionBars * 1.2 + 2.0;
  const manipScore = Math.max(1, Math.min(10, Math.round(rawManip)));
  const manipText =
    manipScore >= 7
      ? 'высокий уровень манипулятивного паттерна по свечному прокси (фитили/ложные пробои). Контекст-скор, не подтверждение реального потока.'
      : manipScore >= 5
      ? 'умеренная манипулятивная активность на локальных уровнях.'
      : 'низкий уровень манипулятивного паттерна по свечному прокси.';

  // 2. Zone Strength (0-100%)
  const currentClose = slice50[slice50.length - 1]?.close || 1.0;
  const swingHigh = Math.max(...slice50.map((c) => c.high));
  const swingLow = Math.min(...slice50.map((c) => c.low));
  const distHigh = Math.abs(currentClose - swingHigh);
  const distLow = Math.abs(currentClose - swingLow);
  const levelPrice = distHigh < distLow ? swingHigh : swingLow;
  const threshold = levelPrice * 0.002;
  const touches = slice50.filter((c) => Math.abs(c.close - levelPrice) < threshold).length;

  let baseStrength = touches >= 5 ? 25 : touches >= 3 ? 50 : 80;
  const zoneScore = Math.max(5, Math.min(95, Math.round(baseStrength - touches * 3)));
  const zoneText =
    zoneScore <= 30
      ? 'зона крайне слабая. Текущий уровень не является серьёзной поддержкой, вероятность ухода ниже высокая.'
      : zoneScore <= 60
      ? 'зона умеренной силы. Возможна локальная консолидация перед импульсом.'
      : 'сильная структурная зона по свечному прокси (частые касания уровня, объём у уровня). Прокси-скор, не подтверждение ликвидности.';

  // 3. SMF Ratio (Smart Money Flow Ratio)
  const volMedian = slice30.map((c) => c.volume).sort((a, b) => a - b)[Math.floor(slice30.length / 2)] || 100;
  let largeProg = 0;
  let smallProg = 0;
  for (let i = 1; i < slice30.length; i++) {
    const diff = Math.abs(slice30[i].close - slice30[i - 1].close);
    if (slice30[i].volume > volMedian) {
      largeProg += diff * slice30[i].volume;
    } else {
      smallProg += diff * slice30[i].volume;
    }
  }
  const rawRatio = smallProg <= 0 ? 2.0 : largeProg / Math.max(smallProg, 1e-6);
  const smfRatio = Number(Math.max(0.5, Math.min(5.0, rawRatio)).toFixed(2));
  const smfText =
    smfRatio >= 2.0
      ? `объёмный прокси направленного потока с коэффициентом ${smfRatio.toFixed(1)} к 1 (крупные бары vs мелкие). Не является реальным торговым потоком.`
      : smfRatio >= 1.2
      ? `преобладание крупно-объёмного прокси (${smfRatio.toFixed(2)}x). Прокси-оценка, не реальный поток.`
      : 'паритет крупно- и мелко-объёмных баров по прокси.';

  // 4. Liquidity Grab Score (1-10)
  let sweepCount = 0;
  for (let i = 5; i < slice30.length; i++) {
    const priorHigh = Math.max(...slice30.slice(i - 5, i).map((c) => c.high));
    const priorLow = Math.min(...slice30.slice(i - 5, i).map((c) => c.low));
    if (slice30[i].high > priorHigh && slice30[i].close < priorHigh) sweepCount++;
    if (slice30[i].low < priorLow && slice30[i].close > priorLow) sweepCount++;
  }
  const liqScore = Math.max(1, Math.min(10, Math.round(sweepCount * 2.2 + 2)));
  const liqText =
    liqScore >= 7
      ? 'активный паттерн прокси-свипа локальных уровней (фитиль за экстремум с возвратом). Паттерн-скор, не подтверждение ликвидности.'
      : liqScore >= 4
      ? 'локальные сборы стопов вблизи ключевых экстремумов.'
      : 'спокойный рынок, сбор стопов не выражен.';

  // 5. Delta Confidence (LOW/MEDIUM/HIGH/VERY HIGH)
  let signedDeltaSum = 0;
  let positiveBars = 0;
  slice30.forEach((c) => {
    const hl = Math.max(c.high - c.low, 1e-6);
    const pos = (c.close - c.low) / hl;
    const delta = (pos * 2.0 - 1.0) * c.volume;
    signedDeltaSum += delta;
    if (delta > 0) positiveBars++;
  });
  const consistency = positiveBars / Math.max(slice30.length, 1);
  let deltaLevel = 'MEDIUM';
  let deltaText = 'умеренная согласованность объёмного дельта-прокси.';

  if (consistency > 0.75) {
    deltaLevel = 'VERY HIGH';
    deltaText = 'сверхвысокая согласованность объёмного дельта-прокси (Покупатели). Не подтверждение реального потока.';
  } else if (consistency > 0.65) {
    deltaLevel = 'HIGH';
    deltaText = 'высокая согласованность объёмного дельта-прокси (Покупатели). Прокси-скор, не реальный поток.';
  } else if (consistency < 0.35) {
    deltaLevel = 'HIGH';
    deltaText = 'высокая согласованность объёмного дельта-прокси (Продавцы). Прокси-скор, не реальный поток.';
  } else if (consistency < 0.25) {
    deltaLevel = 'VERY HIGH';
    deltaText = 'сверхвысокая согласованность объёмного дельта-прокси (Продавцы). Не подтверждение реального потока.';
  } else {
    deltaLevel = 'MEDIUM';
    deltaText = 'умеренная согласованность объёмного дельта-прокси.';
  }

  const provenance = (name: string, lookback: number) => ({
    source_kind: SOURCE_KIND,
    lookback,
    data_status: nBars >= 10 ? 'sufficient' : 'insufficient',
  });

  return {
    manipulation_index: {
      score: manipScore,
      max: 10,
      text: manipText,
      display: `${manipScore}/10`,
      ...provenance('manipulation_index', 20),
    },
    zone_strength: {
      score: zoneScore,
      max: 100,
      text: zoneText,
      display: `${zoneScore}%`,
      ...provenance('zone_strength', 50),
    },
    smf_ratio: {
      ratio: smfRatio,
      text: smfText,
      display: `${smfRatio.toFixed(2)}`,
      ...provenance('smf_ratio', 30),
    },
    liquidity_grab: {
      score: liqScore,
      max: 10,
      text: liqText,
      display: `${liqScore}/10`,
      ...provenance('liquidity_grab', 30),
    },
    delta_confidence: {
      level: deltaLevel,
      text: deltaText,
      display: deltaLevel,
      ...provenance('delta_confidence', 30),
    },
    source_provenance: {
      source_kind: SOURCE_KIND,
      lookbacks: {
        manipulation_index: 20,
        zone_strength: 50,
        smf_ratio: 30,
        liquidity_grab: 30,
        delta_confidence: 30,
      },
      note: 'OHLCV proxy only; NOT real trade flow / L2 / MBO / on-chain',
    },
  };
}

export function formatInstitutionalMetricsReport(m: SmartMoneyResult): string {
  return (
    `📊 *Метрики по софту на текущий момент*\n\n` +
    `**Manipulation Index: ${m.manipulation_index.display}** — ${m.manipulation_index.text}\n\n` +
    `**Zone Strength: ${m.zone_strength.display}** — ${m.zone_strength.text}\n\n` +
    `**SMF Ratio: ${m.smf_ratio.display}** — ${m.smf_ratio.text}\n\n` +
    `**Liquidity Grab: ${m.liquidity_grab.display}** — ${m.liquidity_grab.text}\n\n` +
    `**Delta Confidence: ${m.delta_confidence.display}** — ${m.delta_confidence.text}\n\n` +
    `_Источник: OHLCV-прокси (не реальный торговый поток / L2 / MBO / on-chain). Прокси-паттерн, не подтверждение институционального участия._`
  );
}
