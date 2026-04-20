import { useQuery } from '@tanstack/react-query';
import client from '../client';
import { STALE_TIMES } from '../../utils/constants';

export interface Portfolio {
  id: string;
  name: string;
  initial_capital: number;
  current_cash: number;
  invested_value: number;
  current_value: number;
  total_pnl: number;
  total_pnl_pct: number;
  day_pnl: number;
  benchmark_symbol: string;
  total_trades: number;
  win_rate: number;
  is_active: boolean;
}

export interface Holding {
  id: string;
  instrument_id: string;
  quantity: number;
  avg_buy_price: number;
  invested_value: number;
  current_price: number | null;
  current_value: number | null;
  pnl: number | null;
  pnl_pct: number | null;
  day_change_pct: number | null;
  weight_pct: number | null;
}

export interface Snapshot {
  date: string;
  total_value: number | null;
  cash: number | null;
  day_pnl: number | null;
  day_pnl_pct: number | null;
  cumulative_pnl_pct: number | null;
  benchmark_value: number | null;
  benchmark_pnl_pct: number | null;
}

export function usePortfolio() {
  return useQuery<Portfolio>({
    queryKey: ['portfolio'],
    queryFn: () => client.get('/portfolio/').then((r) => r.data),
    staleTime: STALE_TIMES.portfolio,
  });
}

export function useHoldings() {
  return useQuery<Holding[]>({
    queryKey: ['holdings'],
    queryFn: () => client.get('/portfolio/holdings').then((r) => r.data),
    staleTime: STALE_TIMES.portfolio,
  });
}

export function usePortfolioHistory(days = 30) {
  return useQuery<Snapshot[]>({
    queryKey: ['portfolio-history', days],
    queryFn: () => client.get(`/portfolio/history?days=${days}`).then((r) => r.data),
    staleTime: 300_000,
  });
}
