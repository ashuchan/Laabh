import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useApiClient } from '../ApiClientProvider';

export function useResolveChainIssue() {
  const api = useApiClient();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (issueId: string) =>
      api.post(`/fno/chain-issues/${issueId}/resolve?resolved_by=desktop`).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['chain-issues'] });
      qc.invalidateQueries({ queryKey: ['source-health'] });
    },
  });
}

export function useBulkResolveChainIssues() {
  const api = useApiClient();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (issueIds: string[]) =>
      api.post('/reports/chain-issues/bulk-resolve', { issue_ids: issueIds, resolved_by: 'desktop' }).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['chain-issues'] });
      qc.invalidateQueries({ queryKey: ['source-health'] });
    },
  });
}
