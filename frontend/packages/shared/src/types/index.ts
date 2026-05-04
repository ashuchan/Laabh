// ─── Portfolio ───────────────────────────────────────────────────────────────

export interface Portfolio {
  id: string;
  name: string;
  initial_capital: number;
  current_cash: number;
  invested_value: number;
  current_value: number;
  total_pnl: number;
  total_pnl_pct: number;
  day_pnl: number;
  benchmark_symbol: string;
  total_trades: number;
  win_rate: number;
  is_active: boolean;
}

export interface Holding {
  id: string;
  instrument_id: string;
  quantity: number;
  avg_buy_price: number;
  invested_value: number;
  current_price: number | null;
  current_value: number | null;
  pnl: number | null;
  pnl_pct: number | null;
  day_change_pct: number | null;
  weight_pct: number | null;
}

export interface Snapshot {
  date: string;
  total_value: number | null;
  cash: number | null;
  day_pnl: number | null;
  day_pnl_pct: number | null;
  cumulative_pnl_pct: number | null;
  benchmark_value: number | null;
  benchmark_pnl_pct: number | null;
}

// ─── Signals ─────────────────────────────────────────────────────────────────

export interface Signal {
  id: string;
  instrument_id: string;
  source_id: string;
  analyst_id: string | null;
  analyst_name_raw: string | null;
  action: 'BUY' | 'SELL' | 'HOLD' | 'WATCH';
  timeframe: string;
  entry_price: number | null;
  target_price: number | null;
  stop_loss: number | null;
  current_price_at_signal: number | null;
  confidence: number | null;
  reasoning: string | null;
  convergence_score: number;
  status: string;
  outcome_pnl_pct: number | null;
  signal_date: string;
  expiry_date: string | null;
}

// ─── Reports ─────────────────────────────────────────────────────────────────

export interface DailyReport {
  date: string;
  pipeline_completeness: {
    total_scheduled: number;
    ran: number;
    skipped: string[];
    jobs: Record<string, Array<{ status: string; runs: number; last_run: string }>>;
  };
  chain_health: {
    total: number;
    ok: number;
    fallback: number;
    missed: number;
    ok_pct: number;
    fallback_pct: number;
    missed_pct: number;
    nse_share_pct: number;
    by_status: Record<string, { count: number; avg_latency_ms: number | null; p95_latency_ms: number | null }>;
    issues: Array<{ type: string; count: number; filed: number }>;
  };
  llm_activity: {
    callers: Array<{
      caller: string;
      row_count: number;
      tokens_in: number;
      tokens_out: number;
      avg_latency_ms: number | null;
      p95_latency_ms: number | null;
    }>;
    total_rows: number;
    total_tokens_in: number;
    total_tokens_out: number;
    estimated_cost_usd: number;
  };
  trading: {
    proposed: number;
    filled: number;
    scaled_out: number;
    closed_target: number;
    closed_stop: number;
    closed_time: number;
    day_pnl: number;
    by_strategy: Record<string, { count: number; pnl: number }>;
    by_status: Record<string, number>;
    decision_quality: Array<{
      symbol: string;
      strategy: string;
      status: string;
      final_pnl: number | null;
      thesis_excerpt: string | null;
    }>;
  };
  candidates: Record<string, number>;
  vix_stats: {
    tick_count?: number;
    avg_value?: number;
    min_value?: number;
    max_value?: number;
  };
  source_health: Array<{ source: string; status: string; consecutive_errors: number }>;
  surprises: string[];
}

export interface TierRow {
  symbol: string;
  tier: number;
  last_attempt: string | null;
  last_status: string | null;
  success_rate_1h: number | null;
  source_breakdown: Record<string, number>;
}

export interface SourceHealth {
  source: string;
  status: string;
  consecutive_errors: number;
  last_success_at: string | null;
  last_error_at: string | null;
  last_error: string | null;
  updated_at: string | null;
}

export interface ChainIssue {
  id: string;
  source: string;
  instrument_id: string | null;
  issue_type: string;
  error_message: string;
  detected_at: string | null;
  github_issue_url: string | null;
  resolved_at: string | null;
}

export interface StrategyDecision {
  id: string;
  portfolio_id: string;
  decision_type: 'morning_allocation' | 'intraday_action' | 'eod_squareoff';
  as_of: string;
  risk_profile: string | null;
  budget_available: number | null;
  llm_model: string | null;
  llm_reasoning: string | null;
  actions_executed: number;
  actions_skipped: number;
  created_at: string;
}

export interface SignalPerformanceRow {
  id: string;
  instrument_id: string;
  symbol: string | null;
  action: string;
  status: string;
  entry_price: number | null;
  target_price: number | null;
  outcome_pnl_pct: number | null;
  convergence_score: number;
  confidence: number | null;
  analyst_id: string | null;
  analyst_name_raw: string | null;
  signal_date: string;
}

export interface SignalPerformance {
  total: number;
  resolved: number;
  hits: number;
  misses: number;
  hit_rate: number;
  avg_pnl_pct: number | null;
  rows: SignalPerformanceRow[];
}

export interface SignalPerformanceTimeseries {
  buckets: Array<{ date: string; hit_rate: number; count: number }>;
}

export interface TierCoverageHeatmap {
  symbols: string[];
  buckets: string[];
  cells: Array<{ symbol: string; bucket: string; success_rate: number | null }>;
}

// ─── FNO ─────────────────────────────────────────────────────────────────────

export interface FNOCandidate {
  id: string;
  instrument_id: string;
  symbol: string | null;
  run_date: string;
  phase: number;
  passed_liquidity: boolean | null;
  atm_oi: number | null;
  atm_spread_pct: number | string | null;
  avg_volume_5d: number | null;
  news_score: number | string | null;
  sentiment_score: number | string | null;
  fii_dii_score: number | string | null;
  macro_align_score: number | string | null;
  convergence_score: number | string | null;
  composite_score: number | string | null;
  technical_pass: boolean | null;
  iv_regime: string | null;
  oi_structure: string | null;
  llm_thesis: string | null;
  llm_decision: string | null;
  config_version: string | null;
  created_at: string;
}

export interface IVHistory {
  instrument_id: string;
  date: string;
  atm_iv: number | string;
  iv_rank_52w: number | string | null;
  iv_percentile_52w: number | string | null;
}

export interface VIXTick {
  timestamp: string;
  vix_value: number | string;
  regime: 'low' | 'neutral' | 'high';
}

export interface FNOBan {
  symbol: string;
  ban_date: string;
  is_active: boolean;
}

// ─── Watchlists ───────────────────────────────────────────────────────────────

export interface Watchlist {
  id: string;
  name: string;
  description: string | null;
  is_default: boolean;
}

export interface WatchlistItem {
  id: string;
  watchlist_id: string;
  instrument_id: string;
  target_buy_price: number | null;
  target_sell_price: number | null;
  price_alert_above: number | null;
  price_alert_below: number | null;
  alert_on_signals: boolean;
  notes: string | null;
}

// ─── Analysts ────────────────────────────────────────────────────────────────

export interface Analyst {
  id: string;
  name: string;
  organization: string | null;
  total_signals: number;
  hit_rate: number;
  avg_return_pct: number;
  credibility_score: number;
  best_sector: string | null;
}

// ─── Trades ──────────────────────────────────────────────────────────────────

export interface Trade {
  id: string;
  portfolio_id: string;
  instrument_id: string;
  trade_type: 'BUY' | 'SELL';
  order_type: string;
  quantity: number;
  price: number;
  brokerage: number;
  stt: number;
  total_cost: number | null;
  status: string;
  executed_at: string | null;
}

export interface PlaceOrderInput {
  instrument_id: string;
  trade_type: 'BUY' | 'SELL';
  order_type?: 'MARKET' | 'LIMIT' | 'STOP_LOSS';
  quantity: number;
  limit_price?: number;
  trigger_price?: number;
  signal_id?: string;
  reason?: string;
}

export interface SetAlertInput {
  watchlist_id: string;
  item_id: string;
  price_alert_above?: number;
  price_alert_below?: number;
  target_buy_price?: number;
  target_sell_price?: number;
}

// ─── Price WebSocket ──────────────────────────────────────────────────────────

export interface PriceData {
  ltp: number;
  change_pct: number;
  updatedAt: number;
}
