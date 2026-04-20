import { useMutation, useQueryClient } from '@tanstack/react-query';
import client from '../client';

export interface SetAlertInput {
  watchlist_id: string;
  item_id: string;
  price_alert_above?: number;
  price_alert_below?: number;
  target_buy_price?: number;
  target_sell_price?: number;
}

export function useSetPriceAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ watchlist_id, item_id, ...body }: SetAlertInput) =>
      client
        .patch(`/watchlists/${watchlist_id}/items/${item_id}`, body)
        .then((r) => r.data),
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: ['watchlist-items', vars.watchlist_id] });
    },
  });
}
