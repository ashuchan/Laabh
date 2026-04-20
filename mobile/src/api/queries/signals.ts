import { useQuery } from '@tanstack/react-query';
import client from '../client';
import { STALE_TIMES } from '../../utils/constants';

export interface Signal {
  id: string;
  instrument_id: string;
  source_id: string;
  analyst_id: string | null;
  analyst_name_raw: string | null;
  action: 'BUY' | 'SELL' | 'HOLD' | 'WATCH';
  timeframe: string;
  entry_price: number | null;
  target_price: number | null;
  stop_loss: number | null;
  current_price_at_signal: number | null;
  confidence: number | null;
  reasoning: string | null;
  convergence_score: number;
  status: string;
  outcome_pnl_pct: number | null;
  signal_date: string;
  expiry_date: string | null;
}

export function useActiveSignals(limit = 50) {
  return useQuery<Signal[]>({
    queryKey: ['signals', 'active'],
    queryFn: () => client.get(`/signals/active?limit=${limit}`).then((r) => r.data),
    staleTime: STALE_TIMES.signals,
    refetchInterval: STALE_TIMES.signals,
  });
}

export function useSignalDetail(signalId: string) {
  return useQuery<Signal>({
    queryKey: ['signal', signalId],
    queryFn: () => client.get(`/signals/${signalId}`).then((r) => r.data),
    staleTime: STALE_TIMES.signals,
  });
}

export function useSignals(params?: { status?: string; action?: string; instrument_id?: string }) {
  const q = new URLSearchParams(params as Record<string, string>).toString();
  return useQuery<Signal[]>({
    queryKey: ['signals', params],
    queryFn: () => client.get(`/signals/?${q}`).then((r) => r.data),
    staleTime: STALE_TIMES.signals,
  });
}
