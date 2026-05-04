import { useQuery } from '@tanstack/react-query';
import { useApiClient } from '../ApiClientProvider';
import { STALE_TIMES } from '../constants';
import type {
  ChainIssue,
  DailyReport,
  SignalPerformance,
  SignalPerformanceTimeseries,
  SourceHealth,
  StrategyDecision,
  TierCoverageHeatmap,
  TierRow,
} from '../../types';

export function useDailyReport(date?: string) {
  const api = useApiClient();
  const qs = date ? `?date=${date}` : '';
  return useQuery<DailyReport>({
    queryKey: ['daily-report', date ?? 'today'],
    queryFn: () => api.get(`/reports/daily${qs}`).then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}

export function useTierCoverage(opts?: {
  lookback_minutes?: number;
  tier?: 1 | 2;
  only_degraded?: boolean;
  limit?: number;
}) {
  const api = useApiClient();
  const params = new URLSearchParams();
  if (opts?.lookback_minutes) params.set('lookback_minutes', String(opts.lookback_minutes));
  if (opts?.tier) params.set('tier', String(opts.tier));
  if (opts?.only_degraded) params.set('only_degraded', 'true');
  if (opts?.limit) params.set('limit', String(opts.limit));
  const qs = params.toString();
  return useQuery<TierRow[]>({
    queryKey: ['tier-coverage', opts ?? {}],
    queryFn: () => api.get(`/reports/tier-coverage${qs ? '?' + qs : ''}`).then((r) => r.data),
    staleTime: STALE_TIMES.health,
  });
}

export function useTierCoverageHeatmap(opts?: { since?: string; buckets?: number }) {
  const api = useApiClient();
  const params = new URLSearchParams();
  if (opts?.since) params.set('since', opts.since);
  if (opts?.buckets) params.set('buckets', String(opts.buckets));
  const qs = params.toString();
  return useQuery<TierCoverageHeatmap>({
    queryKey: ['tier-coverage-heatmap', opts ?? {}],
    queryFn: () =>
      api.get(`/reports/tier-coverage/heatmap${qs ? '?' + qs : ''}`).then((r) => r.data),
    staleTime: STALE_TIMES.health,
  });
}

export function useSourceHealth() {
  const api = useApiClient();
  return useQuery<SourceHealth[]>({
    queryKey: ['source-health'],
    queryFn: () => api.get('/reports/source-health').then((r) => r.data),
    staleTime: STALE_TIMES.health,
    refetchInterval: STALE_TIMES.health,
  });
}

export function useChainIssues(status: 'open' | 'resolved' | 'all' = 'open') {
  const api = useApiClient();
  return useQuery<ChainIssue[]>({
    queryKey: ['chain-issues', status],
    queryFn: () => api.get(`/reports/chain-issues?status=${status}`).then((r) => r.data),
    staleTime: STALE_TIMES.health,
  });
}

export function useStrategyDecisions(opts?: { date?: string; decision_type?: string }) {
  const api = useApiClient();
  const params = new URLSearchParams();
  if (opts?.date) params.set('date', opts.date);
  if (opts?.decision_type) params.set('decision_type', opts.decision_type);
  const qs = params.toString();
  return useQuery<StrategyDecision[]>({
    queryKey: ['strategy-decisions', opts ?? {}],
    queryFn: () =>
      api.get(`/reports/strategy-decisions${qs ? '?' + qs : ''}`).then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}

export function useSignalPerformance(opts?: { days?: number; analyst_id?: string }) {
  const api = useApiClient();
  const params = new URLSearchParams();
  if (opts?.days) params.set('days', String(opts.days));
  if (opts?.analyst_id) params.set('analyst_id', opts.analyst_id);
  const qs = params.toString();
  return useQuery<SignalPerformance>({
    queryKey: ['signal-performance', opts ?? {}],
    queryFn: () =>
      api.get(`/reports/signal-performance${qs ? '?' + qs : ''}`).then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}

export function useSignalPerformanceTimeseries(opts?: { bucket?: string; days?: number }) {
  const api = useApiClient();
  const params = new URLSearchParams();
  if (opts?.bucket) params.set('bucket', opts.bucket);
  if (opts?.days) params.set('days', String(opts.days));
  const qs = params.toString();
  return useQuery<SignalPerformanceTimeseries>({
    queryKey: ['signal-performance-timeseries', opts ?? {}],
    queryFn: () =>
      api
        .get(`/reports/signal-performance/timeseries${qs ? '?' + qs : ''}`)
        .then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}
