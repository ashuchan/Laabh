import { useQuery } from '@tanstack/react-query';
import client from '../client';
import { STALE_TIMES } from '../../utils/constants';

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

export function useDailyReport(date?: string) {
  const qs = date ? `?date=${date}` : '';
  return useQuery<DailyReport>({
    queryKey: ['daily-report', date ?? 'today'],
    queryFn: () => client.get(`/reports/daily${qs}`).then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}

export function useTierCoverage(opts?: {
  lookback_minutes?: number;
  tier?: 1 | 2;
  only_degraded?: boolean;
  limit?: number;
}) {
  const params = new URLSearchParams();
  if (opts?.lookback_minutes) params.set('lookback_minutes', String(opts.lookback_minutes));
  if (opts?.tier) params.set('tier', String(opts.tier));
  if (opts?.only_degraded) params.set('only_degraded', 'true');
  if (opts?.limit) params.set('limit', String(opts.limit));
  const qs = params.toString();
  return useQuery<TierRow[]>({
    queryKey: ['tier-coverage', opts ?? {}],
    queryFn: () => client.get(`/reports/tier-coverage${qs ? '?' + qs : ''}`).then((r) => r.data),
    staleTime: STALE_TIMES.health,
  });
}

export function useSourceHealth() {
  return useQuery<SourceHealth[]>({
    queryKey: ['source-health'],
    queryFn: () => client.get('/reports/source-health').then((r) => r.data),
    staleTime: STALE_TIMES.health,
    refetchInterval: STALE_TIMES.health,
  });
}

export function useChainIssues(status: 'open' | 'resolved' | 'all' = 'open') {
  return useQuery<ChainIssue[]>({
    queryKey: ['chain-issues', status],
    queryFn: () => client.get(`/reports/chain-issues?status=${status}`).then((r) => r.data),
    staleTime: STALE_TIMES.health,
  });
}

export function useStrategyDecisions(opts?: { date?: string; decision_type?: string }) {
  const params = new URLSearchParams();
  if (opts?.date) params.set('date', opts.date);
  if (opts?.decision_type) params.set('decision_type', opts.decision_type);
  const qs = params.toString();
  return useQuery<StrategyDecision[]>({
    queryKey: ['strategy-decisions', opts ?? {}],
    queryFn: () =>
      client.get(`/reports/strategy-decisions${qs ? '?' + qs : ''}`).then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}

export function useSignalPerformance(opts?: { days?: number; analyst_id?: string }) {
  const params = new URLSearchParams();
  if (opts?.days) params.set('days', String(opts.days));
  if (opts?.analyst_id) params.set('analyst_id', opts.analyst_id);
  const qs = params.toString();
  return useQuery<SignalPerformance>({
    queryKey: ['signal-performance', opts ?? {}],
    queryFn: () =>
      client.get(`/reports/signal-performance${qs ? '?' + qs : ''}`).then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}
