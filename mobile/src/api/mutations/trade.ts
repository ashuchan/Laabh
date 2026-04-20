import { useMutation, useQueryClient } from '@tanstack/react-query';
import client from '../client';

export interface PlaceOrderInput {
  instrument_id: string;
  trade_type: 'BUY' | 'SELL';
  order_type?: 'MARKET' | 'LIMIT' | 'STOP_LOSS';
  quantity: number;
  limit_price?: number;
  trigger_price?: number;
  signal_id?: string;
  reason?: string;
}

export function useExecuteTrade() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: PlaceOrderInput) => client.post('/trades/', input).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['trades'] });
      qc.invalidateQueries({ queryKey: ['portfolio'] });
      qc.invalidateQueries({ queryKey: ['holdings'] });
    },
  });
}

export function useCancelOrder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (orderId: string) =>
      client.delete(`/trades/pending/${orderId}`).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pending-orders'] });
    },
  });
}
