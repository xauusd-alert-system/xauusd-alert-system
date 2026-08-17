export interface Candle {
  timestamp_utc: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  atr?: number;
  regime?: string;
}

export interface SignalResponse {
  signal_id: string;
  signal_state: string;
  strategy_version: string;
  strategy_spec_hash: string;
  config_hash: string;
  model_hash?: string | null;
  feature_snapshot_hash?: string | null;
  setup_timeframe: string;
  context_timeframes: string[];
  expires_at_utc?: number | null;
  target_legs: Array<Record<string, any>>;
  confirmation_predicates: string[];
  confirmed_by?: string | null;
  confirmation_time_utc?: number | null;
  bias: 'long' | 'short' | 'neutral' | 'no_trade';
  confidence: number;
  entry_zone?: number[] | null;
  invalidation?: number | null;
  targets?: number[] | null;
  step?: number | null;
  reasoning_summary: string;
  regime: string;
  timestamp_utc: number;
  session: string;
  generated_at: string;
}

export interface AssetMatrixSignal {
  asset: string;
  bias: string | null;
  confidence: number | null;
  regime: string | null;
  session: string | null;
  targets: number[];
  invalidation: number | null;
  available: boolean;
  status: string;
  source: string;
  mode: string;
  as_of_utc: string;
}

export interface Position {
  ticket: number;
  symbol: string;
  direction: 'buy' | 'sell';
  volume: number;
  open_price: number;
  current_price: number;
  profit: number;
  sl: number;
  tp: number;
}

export interface DealMetrics {
  period: string;
  period_label: string;
  available: boolean;
  as_of_utc: string;
  n?: number;
  win_rate_pct?: number;
  profit_factor?: number;
  total_pnl?: number;
  avg_win?: number;
  avg_loss?: number;
  max_drawdown?: number;
  max_consec_losses?: number;
  expectancy?: number;
  best_trade?: number;
  worst_trade?: number;
}
