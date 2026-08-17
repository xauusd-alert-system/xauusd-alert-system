export type AssetSymbol = 'XAUUSD' | 'XAGUSD' | 'BTCUSD' | 'EURUSD' | 'GBPUSD';

export type SignalBias = 'long' | 'short' | 'neutral' | 'no_trade';
export type MarketRegime = 'trend_up' | 'trend_down' | 'range' | 'compression' | 'reversal_watch' | 'no_trade';
export type TradingSession = 'london' | 'newyork' | 'asia' | 'off_session';
export type DeploymentMode = 'simulation' | 'research' | 'paper' | 'human_confirmed' | 'demo_systematic' | 'live_systematic';

export interface SystemStatus {
  status: string;
  data_mode: 'mock' | 'live' | 'paper' | string;
  available: boolean;
  source: string;
  mode: string;
  as_of_utc: string;
  balance: number | null;
  equity: number | null;
  floating_pnl: number | null;
  open_positions_count: number;
  circuit_breaker: boolean;
  trading_paused: boolean;
  execution_enabled_assets: string[];
  require_demo_account: boolean;
  deployment_mode: DeploymentMode;
  strategy_version: string;
  strategy_spec_hash: string;
  config_hash: string;
  freshness_status?: 'live' | 'delayed' | 'offline';
}

export interface SignalResponse {
  signal_id: string;
  signal_state: string;
  strategy_version: string;
  strategy_spec_hash: string;
  config_hash: string;
  model_hash?: string;
  feature_snapshot_hash?: string;
  setup_timeframe: string;
  context_timeframes: string[];
  expires_at_utc?: number;
  target_legs: Array<{
    leg: number;
    target_price: number;
    ratio: number;
    description: string;
  }>;
  confirmation_predicates: string[];
  confirmed_by?: string;
  confirmation_time_utc?: number;
  bias: SignalBias;
  confidence: number;
  entry_zone?: [number, number];
  invalidation?: number;
  targets?: number[];
  step?: number;
  reasoning_summary: string;
  regime: MarketRegime;
  timestamp_utc: number;
  session: TradingSession;
  generated_at: string;
  asset: AssetSymbol;
}

export interface MatrixItem {
  asset: AssetSymbol;
  bias: SignalBias | null;
  confidence: number | null;
  regime: MarketRegime | null;
  session: TradingSession | null;
  targets: number[];
  invalidation: number | null;
  available: boolean;
  status: 'ok' | 'unavailable' | 'error';
  source: string;
  mode: string;
  as_of_utc: string;
  reason?: string;
  price?: number;
  atr?: number;
}

export interface CorrelationMatrixData {
  available: boolean;
  assets: AssetSymbol[];
  matrix: number[][];
  as_of_utc: string;
  n_aligned_returns?: number;
  reason?: string;
}

export interface InstitutionalMetricItem {
  display: string;
  text: string;
  status: 'bullish' | 'bearish' | 'neutral' | 'caution';
}

export interface InstitutionalMetricsResponse {
  available: boolean;
  metrics: Record<string, InstitutionalMetricItem>;
  report_text: string | null;
  source: string;
  mode: string;
  as_of_utc: string;
  reason?: string;
}

export interface CandleData {
  timestamp: number;
  timeStr: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  fvgTop?: number;
  fvgBottom?: number;
  isOrderBlock?: boolean;
}

export interface ChartResponse {
  asset: AssetSymbol;
  timeframe: string;
  current_price: number;
  entry_price: number;
  sl_price: number;
  tp_prices: number[];
  step: number;
  regime: string;
  candles: CandleData[];
  svg?: string;
  as_of_utc: string;
}

export interface MacroSentimentResponse {
  available: boolean;
  score: number | null;
  bias: 'bullish' | 'bearish' | 'neutral' | null;
  confidence: number | null;
  matched_terms: string[];
  as_of_utc: string;
  reason?: string;
  catalysts?: Array<{
    title: string;
    impact: 'high' | 'medium' | 'low';
    sentiment: 'bullish' | 'bearish' | 'neutral';
    time: string;
  }>;
}

export interface MonteCarloResponse {
  available: boolean;
  as_of_utc: string;
  n_trades: number;
  var_95_usd: number;
  var_99_usd: number;
  expected_shortfall_usd: number;
  profit_probability_pct: number;
  prob_of_ruin_pct: number;
  median_terminal_balance: number;
  q05_terminal_balance: number;
  q95_terminal_balance: number;
  simulation_trajectories?: number[][];
  reason?: string;
}

export interface OpenPosition {
  ticket: number;
  symbol: AssetSymbol;
  direction: 'buy' | 'sell';
  volume: number;
  open_price: number;
  current_price: number;
  profit: number;
  sl: number;
  tp: number;
  open_time?: string;
}

export interface ClosedTradeMetrics {
  period: string;
  period_label: string;
  available: boolean;
  as_of_utc: string;
  n: number;
  win_rate_pct: number;
  profit_factor: number;
  total_pnl: number;
  avg_win: number;
  avg_loss: number;
  max_drawdown: number;
  max_consec_losses: number;
  expectancy: number;
  best_trade: number;
  worst_trade: number;
  trades_list?: Array<{
    id: string;
    symbol: string;
    side: 'buy' | 'sell';
    volume: number;
    pnl: number;
    r_multiple: number;
    close_time: string;
  }>;
}

export interface LedgerEvent {
  event_id: string;
  event_type: 'intent_created' | 'order_placed' | 'deal_added' | 'position_modified' | 'execution_reconciled' | 'health_heartbeat';
  source: string;
  received_at_utc_ms: number;
  asset_key?: string;
  intent_id?: string;
  group_id?: string;
  signature_valid: boolean;
  payload: Record<string, any>;
}

export interface ProvenanceLineage {
  group_id: string;
  available: boolean;
  lineage: {
    group: {
      status: string;
      group_id: string;
      mode: string;
      side: string;
      geometry_hash: string;
      provenance_hash: string | null;
      provenance_status: string;
    };
    market_snapshot: { status: string; source_id?: string };
    feature_snapshot: { status: string; source_id?: string };
    model_inference: { status: string; source_id?: string };
    profile: { status: string; source_id?: string };
    broker_snapshot: { status: string; source_id?: string };
    cost_snapshot: { status: string; source_id?: string };
    ledger_events?: {
      status: string;
      events: LedgerEvent[];
    };
  };
}

export interface AIChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  model?: string;
  groundingChunks?: any[];
  timestamp: number;
}
