import { useQuery } from '@tanstack/react-query';
import client from '../client';

export interface Watchlist {
  id: string;
  name: string;
  description: string | null;
  is_default: boolean;
}

export interface WatchlistItem {
  id: string;
  watchlist_id: string;
  instrument_id: string;
  target_buy_price: number | null;
  target_sell_price: number | null;
  price_alert_above: number | null;
  price_alert_below: number | null;
  alert_on_signals: boolean;
  notes: string | null;
}

export function useWatchlists() {
  return useQuery<Watchlist[]>({
    queryKey: ['watchlists'],
    queryFn: () => client.get('/watchlists/').then((r) => r.data),
    staleTime: 60_000,
  });
}

export function useWatchlistItems(watchlistId: string) {
  return useQuery<WatchlistItem[]>({
    queryKey: ['watchlist-items', watchlistId],
    queryFn: () => client.get(`/watchlists/${watchlistId}/items`).then((r) => r.data),
    staleTime: 30_000,
  });
}
