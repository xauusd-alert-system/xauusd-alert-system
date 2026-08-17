/**
 * Canonical contracts mirroring backend models:
 * - realtime/data_envelope.py
 * - contracts/signal_spec.py
 * - data/ledger_events.py
 * - config/config.yaml
 */

export type FreshnessStatus = 'fresh' | 'stale' | 'offline' | 'waiting' | 'error';

export interface DataEnvelope<T = any> {
  available?: boolean;
  source: string;
  mode: string;
  as_of_utc?: string;
  as_of_utc_ms?: number | null;
  freshness_status: FreshnessStatus;
  ingest_lag_ms?: number | null;
  coverage?: number | null;
  last_successful_at_utc_ms?: number | null;
  reason?: string;
  [key: string]: any;
}

export type SignalState = 'watch' | 'armed' | 'confirmed' | 'rejected' | 'expired' | 'no_trade';
export type SignalBias = 'long' | 'short' | 'no_trade' | 'neutral';

export interface TargetLeg {
  price?: number;
  close_ratio?: number;
  ratio?: number;
  label?: string;
  leg?: number;
  be_trigger?: boolean;
}

export interface SignalResponse {
  signal_id: string;
  signal_state: SignalState | string;
  strategy_version: string;
  strategy_spec_hash: string;
  config_hash: string;
  model_hash?: string | null;
  feature_snapshot_hash?: string | null;
  setup_timeframe: string;
  context_timeframes: string[];
  expires_at_utc?: number | null;
  target_legs: TargetLeg[];
  confirmation_predicates: string[];
  confirmed_by?: string | null;
  confirmation_time_utc?: number | null;
  bias: SignalBias;
  confidence: number;
  entry_zone?: [number, number] | number[] | null;
  invalidation?: number | null;
  targets?: number[] | null;
  step?: number | null;
  reasoning_summary: string;
  regime: string;
  timestamp_utc: number;
  session: string;
  generated_at: string;
  // Envelopes:
  source?: string;
  mode?: string;
  freshness_status?: FreshnessStatus;
  as_of_utc_ms?: number | null;
  ingest_lag_ms?: number | null;
}

export interface SystemStatusResponse extends DataEnvelope {
  status: string;
  data_mode: string;
  available: boolean;
  deployment_mode: string;
  strategy_version: string;
  config_hash?: string;
  strategy_spec_hash?: string;
  balance: number | null;
  equity: number | null;
  floating_pnl: number | null;
  open_positions_count: number;
  circuit_breaker: boolean;
  trading_paused: boolean;
  execution_enabled_assets: string[];
  require_demo_account: boolean;
  as_of_utc: string;
  session?: string;
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
  status: 'ok' | 'error' | 'unavailable' | string;
  reason?: string;
  source: string;
  mode: string;
  as_of_utc: string;
  freshness_status?: FreshnessStatus;
}

export interface MatrixResponse extends DataEnvelope {
  signals: AssetMatrixSignal[];
}

export interface CorrelationResponse extends DataEnvelope {
  assets: string[];
  matrix: number[][];
  n_aligned_returns?: number;
}

export interface SentimentResponse extends DataEnvelope {
  asset?: string;
  score: number | null;
  bias: string | null;
  confidence: number | null;
  matched_terms: string[];
  reason?: string;
}

export interface MonteCarloResponse extends DataEnvelope {
  n_trades?: number;
  n_simulations?: number;
  horizon_trades?: number;
  initial_balance?: number;
  median_ending_equity?: number;
  mean_ending_equity?: number;
  profit_probability_pct?: number;
  var_95_usd?: number;
  var_99_usd?: number;
  cvar_95_usd?: number;
  cvar_99_usd?: number;
  max_drawdown_median_pct?: number;
  max_drawdown_95_pct?: number;
  max_drawdown_99_pct?: number;
  prob_of_ruin_pct?: number;
}

export interface PositionItem {
  ticket: number | null;
  symbol: string | null;
  direction: 'buy' | 'sell';
  volume: number;
  open_price: number;
  current_price: number;
  profit: number;
  sl: number;
  tp: number;
}

export interface PositionsResponse extends DataEnvelope {
  positions: PositionItem[];
}

export interface DealMetricsResponse extends DataEnvelope {
  period: string;
  period_label: string;
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

export interface InstitutionalMetricsResponse extends DataEnvelope {
  metrics: {
    manipulation_index?: {
      score: number;
      max: number;
      text: string;
      display: string;
      source_kind: string;
      lookback: number;
      data_status: string;
    };
    zone_strength?: {
      score: number;
      max: number;
      text: string;
      display: string;
      source_kind: string;
      lookback: number;
      data_status: string;
    };
    smf_ratio?: {
      ratio: number;
      text: string;
      display: string;
      source_kind: string;
      lookback: number;
      data_status: string;
    };
    liquidity_grab?: {
      score: number;
      max: number;
      text: string;
      display: string;
      source_kind: string;
      lookback: number;
      data_status: string;
    };
    delta_confidence?: {
      level: string;
      text: string;
      display: string;
      source_kind: string;
      lookback: number;
      data_status: string;
    };
    [key: string]: any;
  };
  report_text?: string | null;
}

export interface LedgerEventItem {
  event_id: string;
  schema_version: number;
  source: string;
  event_type: string;
  intent_id: string | null;
  asset_key: string | null;
  broker_symbol: string;
  magic_number: number | null;
  account_mode: string;
  precision: string;
  order_ticket: number | null;
  deal_ticket: number | null;
  position_ticket: number | null;
  deal_time_msc: number | null;
  retcode: number | null;
  requested_price: number | null;
  fill_price: number | null;
  filled_volume: number | null;
  volume_requested: number | null;
  spread_points: number | null;
  commission: number | null;
  swap: number | null;
  latency_ms: number | null;
  reason: string | null;
  signature_valid: boolean | number;
  received_at_utc_ms: number;
  payload: Record<string, any>;
}

export interface LedgerEventsResponse extends DataEnvelope {
  count: number;
  events: LedgerEventItem[];
}

export interface ExecutionQualityResponse extends DataEnvelope {
  stale?: boolean;
  events?: number;
  by_precision?: Record<string, {
    events: number;
    spread_points: Record<string, number | null>;
    latency_ms: Record<string, number | null>;
    adverse_slippage_price_units?: Record<string, number | null>;
  }>;
}

export interface ProvenanceAuditResponse extends DataEnvelope {
  group_id: string;
  lineage: {
    group?: {
      status: string;
      group_id?: string;
      mode?: string;
      side?: string;
      geometry_hash?: string;
      provenance_hash?: string | null;
      provenance_status?: string;
    };
    market_snapshot?: { status: string; source_id?: string };
    feature_snapshot?: { status: string; source_id?: string };
    model_inference?: { status: string; source_id?: string };
    profile?: { status: string; source_id?: string };
    broker_snapshot?: { status: string; source_id?: string };
    cost_snapshot?: { status: string; source_id?: string };
    ledger_events?: { status: string; events?: any[]; detail?: string };
  };
}
