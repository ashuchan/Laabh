import { useQuery } from '@tanstack/react-query';
import client from '../client';

export interface Trade {
  id: string;
  portfolio_id: string;
  instrument_id: string;
  trade_type: 'BUY' | 'SELL';
  order_type: string;
  quantity: number;
  price: number;
  brokerage: number;
  stt: number;
  total_cost: number | null;
  status: string;
  executed_at: string | null;
}

export function useTrades(limit = 50) {
  return useQuery<Trade[]>({
    queryKey: ['trades', limit],
    queryFn: () => client.get(`/trades/?limit=${limit}`).then((r) => r.data),
    staleTime: 30_000,
  });
}

export function usePendingOrders() {
  return useQuery<Trade[]>({
    queryKey: ['pending-orders'],
    queryFn: () => client.get('/trades/pending').then((r) => r.data),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}
