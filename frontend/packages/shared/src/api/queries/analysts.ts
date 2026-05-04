import { useQuery } from '@tanstack/react-query';
import { useApiClient } from '../ApiClientProvider';
import { STALE_TIMES } from '../constants';
import type { Analyst } from '../../types';

export function useAnalystLeaderboard(limit = 20) {
  const api = useApiClient();
  return useQuery<Analyst[]>({
    queryKey: ['analysts'],
    queryFn: () => api.get(`/analysts/leaderboard?limit=${limit}`).then((r) => r.data),
    staleTime: STALE_TIMES.analysts,
  });
}
