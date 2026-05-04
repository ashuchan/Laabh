import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useApiClient } from '../ApiClientProvider';
import type { SetAlertInput } from '../../types';

export function useAddToWatchlist(watchlistId: string) {
  const api = useApiClient();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { instrument_id: string; alert_on_signals?: boolean }) =>
      api.post(`/watchlists/${watchlistId}/items`, input).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['watchlist-items', watchlistId] });
    },
  });
}

export function useRemoveFromWatchlist(watchlistId: string) {
  const api = useApiClient();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (itemId: string) =>
      api.delete(`/watchlists/${watchlistId}/items/${itemId}`).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['watchlist-items', watchlistId] });
    },
  });
}

export function useSetPriceAlert() {
  const api = useApiClient();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ watchlist_id, item_id, ...body }: SetAlertInput) =>
      api.patch(`/watchlists/${watchlist_id}/items/${item_id}`, body).then((r) => r.data),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ['watchlist-items', vars.watchlist_id] });
    },
  });
}
