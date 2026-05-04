import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import client from '../client';
import { STALE_TIMES } from '../../utils/constants';

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

export function useFNOCandidates(opts?: {
  run_date?: string;
  phase?: number;
  passed_only?: boolean;
  limit?: number;
}) {
  const params = new URLSearchParams();
  if (opts?.run_date) params.set('run_date', opts.run_date);
  if (opts?.phase) params.set('phase', String(opts.phase));
  if (opts?.passed_only) params.set('passed_only', 'true');
  params.set('limit', String(opts?.limit ?? 100));
  return useQuery<FNOCandidate[]>({
    queryKey: ['fno-candidates', opts ?? {}],
    queryFn: () => client.get(`/fno/candidates?${params.toString()}`).then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}

export function useVIXHistory(limit = 30) {
  return useQuery<VIXTick[]>({
    queryKey: ['vix', limit],
    queryFn: () => client.get(`/fno/vix?limit=${limit}`).then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}

export function useFNOBanList() {
  return useQuery<FNOBan[]>({
    queryKey: ['fno-ban-list'],
    queryFn: () => client.get('/fno/ban-list?active_only=true').then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}

export function useResolveChainIssue() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (issueId: string) =>
      client.post(`/fno/chain-issues/${issueId}/resolve?resolved_by=mobile`).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['chain-issues'] });
      qc.invalidateQueries({ queryKey: ['source-health'] });
    },
  });
}
