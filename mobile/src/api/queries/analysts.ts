import { useQuery } from '@tanstack/react-query';
import client from '../client';
import { STALE_TIMES } from '../../utils/constants';

export interface Analyst {
  id: string;
  name: string;
  organization: string | null;
  total_signals: number;
  hit_rate: number;
  avg_return_pct: number;
  credibility_score: number;
  best_sector: string | null;
}

export function useAnalystLeaderboard(limit = 20) {
  return useQuery<Analyst[]>({
    queryKey: ['analysts'],
    queryFn: () => client.get(`/analysts/leaderboard?limit=${limit}`).then((r) => r.data),
    staleTime: STALE_TIMES.analysts,
  });
}
