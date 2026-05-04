import { useQuery } from '@tanstack/react-query';
import { useApiClient } from '../ApiClientProvider';
import type { Trade } from '../../types';

export function useTrades(limit = 50) {
  const api = useApiClient();
  return useQuery<Trade[]>({
    queryKey: ['trades', limit],
    queryFn: () => api.get(`/trades/?limit=${limit}`).then((r) => r.data),
    staleTime: 30_000,
  });
}

export function usePendingOrders() {
  const api = useApiClient();
  return useQuery<Trade[]>({
    queryKey: ['pending-orders'],
    queryFn: () => api.get('/trades/pending').then((r) => r.data),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}
