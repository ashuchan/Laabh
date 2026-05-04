import { useQuery } from '@tanstack/react-query';
import { useApiClient } from '../ApiClientProvider';
import { STALE_TIMES } from '../constants';
import type { Signal } from '../../types';

export function useActiveSignals(limit = 50) {
  const api = useApiClient();
  return useQuery<Signal[]>({
    queryKey: ['signals', 'active'],
    queryFn: () => api.get(`/signals/active?limit=${limit}`).then((r) => r.data),
    staleTime: STALE_TIMES.signals,
    refetchInterval: STALE_TIMES.signals,
  });
}

export function useSignalDetail(signalId: string) {
  const api = useApiClient();
  return useQuery<Signal>({
    queryKey: ['signal', signalId],
    queryFn: () => api.get(`/signals/${signalId}`).then((r) => r.data),
    staleTime: STALE_TIMES.signals,
    enabled: Boolean(signalId),
  });
}

export function useSignals(params?: { status?: string; action?: string; instrument_id?: string }) {
  const api = useApiClient();
  const q = new URLSearchParams(params as Record<string, string>).toString();
  return useQuery<Signal[]>({
    queryKey: ['signals', params],
    queryFn: () => api.get(`/signals/?${q}`).then((r) => r.data),
    staleTime: STALE_TIMES.signals,
  });
}
