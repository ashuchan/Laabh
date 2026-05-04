import { useQuery } from '@tanstack/react-query';
import { useApiClient } from '../ApiClientProvider';
import { STALE_TIMES } from '../constants';
import type { FNOBan, FNOCandidate, VIXTick } from '../../types';

export function useFNOCandidates(opts?: {
  run_date?: string;
  phase?: number;
  passed_only?: boolean;
  limit?: number;
}) {
  const api = useApiClient();
  const params = new URLSearchParams();
  if (opts?.run_date) params.set('run_date', opts.run_date);
  if (opts?.phase) params.set('phase', String(opts.phase));
  if (opts?.passed_only) params.set('passed_only', 'true');
  params.set('limit', String(opts?.limit ?? 200));
  return useQuery<FNOCandidate[]>({
    queryKey: ['fno-candidates', opts ?? {}],
    queryFn: () => api.get(`/fno/candidates?${params.toString()}`).then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}

export function useVIXHistory(limit = 30) {
  const api = useApiClient();
  return useQuery<VIXTick[]>({
    queryKey: ['vix', limit],
    queryFn: () => api.get(`/fno/vix?limit=${limit}`).then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}

export function useFNOBanList() {
  const api = useApiClient();
  return useQuery<FNOBan[]>({
    queryKey: ['fno-ban-list'],
    queryFn: () => api.get('/fno/ban-list?active_only=true').then((r) => r.data),
    staleTime: STALE_TIMES.reports,
  });
}
