import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useApiClient } from '../ApiClientProvider';
import type { PlaceOrderInput } from '../../types';

export function useExecuteTrade() {
  const api = useApiClient();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: PlaceOrderInput) => api.post('/trades/', input).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['trades'] });
      qc.invalidateQueries({ queryKey: ['portfolio'] });
      qc.invalidateQueries({ queryKey: ['holdings'] });
    },
  });
}

export function useCancelOrder() {
  const api = useApiClient();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (orderId: string) =>
      api.delete(`/trades/pending/${orderId}`).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pending-orders'] });
    },
  });
}
