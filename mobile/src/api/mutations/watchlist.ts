import { useMutation, useQueryClient } from '@tanstack/react-query';
import client from '../client';

export function useAddToWatchlist(watchlistId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { instrument_id: string; alert_on_signals?: boolean }) =>
      client.post(`/watchlists/${watchlistId}/items`, input).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['watchlist-items', watchlistId] });
    },
  });
}

export function useRemoveFromWatchlist(watchlistId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (itemId: string) =>
      client.delete(`/watchlists/${watchlistId}/items/${itemId}`).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['watchlist-items', watchlistId] });
    },
  });
}
