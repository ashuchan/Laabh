import { useQuery } from '@tanstack/react-query';
import { useApiClient } from '../ApiClientProvider';
import type { Watchlist, WatchlistItem } from '../../types';

export function useWatchlists() {
  const api = useApiClient();
  return useQuery<Watchlist[]>({
    queryKey: ['watchlists'],
    queryFn: () => api.get('/watchlists/').then((r) => r.data),
    staleTime: 60_000,
  });
}

export function useWatchlistItems(watchlistId: string) {
  const api = useApiClient();
  return useQuery<WatchlistItem[]>({
    queryKey: ['watchlist-items', watchlistId],
    queryFn: () => api.get(`/watchlists/${watchlistId}/items`).then((r) => r.data),
    staleTime: 30_000,
    enabled: Boolean(watchlistId),
  });
}
