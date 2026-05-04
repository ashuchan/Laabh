import { useQuery } from '@tanstack/react-query';
import { useApiClient } from '../ApiClientProvider';
import { STALE_TIMES } from '../constants';
import type { Holding, Portfolio, Snapshot } from '../../types';

export function usePortfolio() {
  const api = useApiClient();
  return useQuery<Portfolio>({
    queryKey: ['portfolio'],
    queryFn: () => api.get('/portfolio/').then((r) => r.data),
    staleTime: STALE_TIMES.portfolio,
  });
}

export function useHoldings() {
  const api = useApiClient();
  return useQuery<Holding[]>({
    queryKey: ['holdings'],
    queryFn: () => api.get('/portfolio/holdings').then((r) => r.data),
    staleTime: STALE_TIMES.portfolio,
  });
}

export function usePortfolioHistory(days = 30) {
  const api = useApiClient();
  return useQuery<Snapshot[]>({
    queryKey: ['portfolio-history', days],
    queryFn: () => api.get(`/portfolio/history?days=${days}`).then((r) => r.data),
    staleTime: 300_000,
  });
}
