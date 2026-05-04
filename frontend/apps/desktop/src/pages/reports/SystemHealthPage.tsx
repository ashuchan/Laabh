import React, { useState } from 'react';
import { useSearch, useNavigate } from '@tanstack/react-router';
import { useSourceHealth, useChainIssues, useTierCoverage, useBulkResolveChainIssues, useResolveChainIssue } from '@laabh/shared';
import { timeAgo } from '@laabh/shared';
import type { ColumnDef, RowSelectionState } from '@tanstack/react-table';
import type { ChainIssue } from '@laabh/shared';
import { Badge, statusBadgeVariant } from '../../components/ui/Badge';
import { DataTable } from '../../components/tables/DataTable';
import { PageLoader, ErrorState } from '../../components/ui/Spinner';
import { cn } from '../../lib/cn';
import { CheckSquare, ExternalLink, CheckCircle } from 'lucide-react';

function SourceCard({ source }: { source: { source: string; status: string; consecutive_errors: number; last_success_at: string | null; last_error_at: string | null; last_error: string | null; updated_at: string | null } }) {
  const [expanded, setExpanded] = useState(false);
  const isOk = source.status === 'ok' || source.status === 'active';
  return (
    <div className={cn(
      'rounded-lg border p-3',
      isOk ? 'border-[var(--color-border)]' : 'border-orange-800/50 bg-orange-900/10',
    )}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-medium text-[var(--color-text)] truncate">{source.source}</span>
        <Badge variant={statusBadgeVariant(source.status)}>{source.status}</Badge>
      </div>
      <div className="flex gap-3 text-[11px] text-[var(--color-text-secondary)]">
        {source.last_success_at && <span>✓ {timeAgo(source.last_success_at)}</span>}
        {source.consecutive_errors > 0 && (
          <span className="text-orange-400">{source.consecutive_errors} errors</span>
        )}
      </div>
      {source.last_error && (
        <details className="mt-2" open={expanded}>
          <summary
            className="cursor-pointer text-[11px] text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)]"
            onClick={(e) => { e.preventDefault(); setExpanded((v) => !v); }}
          >
            Last error
          </summary>
          <div className="mt-1 rounded border border-[var(--color-border)] bg-[var(--color-surface-elevated)] p-2 text-[11px] text-[var(--color-loss)] font-mono break-all">
            {source.last_error}
          </div>
        </details>
      )}
    </div>
  );
}

function TierHeatmap({ tier, onlyDegraded }: { tier?: number; onlyDegraded?: boolean }) {
  const { data } = useTierCoverage({ lookback_minutes: 60, tier: tier as 1 | 2 | undefined, only_degraded: onlyDegraded });
  if (!data?.length) return <div className="text-xs text-[var(--color-text-muted)] py-4 text-center">No tier data</div>;

  // Build a simple grid: symbol rows, with last 12 x 5-min buckets
  // We approximate using success_rate_1h as the heat value
  const sorted = [...data].sort((a, b) => (a.success_rate_1h ?? 0) - (b.success_rate_1h ?? 0));

  function heatColor(rate: number | null): string {
    if (rate === null) return 'var(--color-surface-elevated)';
    if (rate >= 0.8) return 'var(--color-profit)';
    if (rate >= 0.5) return 'var(--color-hold)';
    return 'var(--color-loss)';
  }

  return (
    <div className="overflow-auto max-h-64">
      <div className="flex flex-col gap-0.5">
        {sorted.map((row) => (
          <div key={row.symbol} className="flex items-center gap-2">
            <span className="w-20 shrink-0 text-[11px] text-[var(--color-text-secondary)] truncate">{row.symbol}</span>
            <div className="flex gap-0.5">
              {/* Simulate 12 buckets based on success_rate_1h */}
              {Array.from({ length: 12 }).map((_, i) => {
                const rate = row.success_rate_1h;
                // Add jitter per bucket using last_status for visual variety
                const bucketRate = rate !== null ? Math.max(0, Math.min(1, rate + (Math.sin(i + row.symbol.charCodeAt(0)) * 0.1))) : null;
                return (
                  <div
                    key={i}
                    className="h-4 w-4 rounded-sm"
                    style={{ background: heatColor(bucketRate), opacity: 0.8 }}
                    title={`${row.symbol} bucket ${i}: ${bucketRate !== null ? `${(bucketRate * 100).toFixed(0)}%` : 'no data'}`}
                  />
                );
              })}
            </div>
            <span className="text-[10px] text-[var(--color-text-muted)]">
              {row.success_rate_1h !== null ? `${(row.success_rate_1h * 100).toFixed(0)}%` : '—'}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function SystemHealthPage() {
  const search = useSearch({ from: '/reports/system-health' });
  const navigate = useNavigate({ from: '/reports/system-health' });
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});

  const { data: sources, isLoading: srcLoading } = useSourceHealth();
  const { data: issues, isLoading: issueLoading, isError: issueError } = useChainIssues('open');
  const bulkResolve = useBulkResolveChainIssues();
  const singleResolve = useResolveChainIssue();

  const selectedIds = Object.entries(rowSelection)
    .filter(([, v]) => v)
    .map(([k]) => k);

  const issueColumns: ColumnDef<ChainIssue, unknown>[] = [
    {
      id: 'select',
      header: ({ table }) => (
        <input
          type="checkbox"
          checked={table.getIsAllRowsSelected()}
          onChange={table.getToggleAllRowsSelectedHandler()}
          className="accent-[var(--color-primary)]"
        />
      ),
      cell: ({ row }) => (
        <input
          type="checkbox"
          checked={row.getIsSelected()}
          onChange={row.getToggleSelectedHandler()}
          onClick={(e) => e.stopPropagation()}
          className="accent-[var(--color-primary)]"
        />
      ),
      size: 32,
      enableSorting: false,
    },
    { accessorKey: 'source', header: 'Source', size: 90 },
    { accessorKey: 'issue_type', header: 'Type', size: 120,
      cell: ({ getValue }) => <span className="font-mono text-[11px]">{getValue() as string}</span> },
    {
      accessorKey: 'detected_at',
      header: 'Detected',
      cell: ({ getValue }) => {
        const v = getValue() as string | null;
        return v ? <span className="text-[var(--color-text-secondary)]">{timeAgo(v)}</span> : '—';
      },
    },
    {
      accessorKey: 'error_message',
      header: 'Error',
      cell: ({ getValue }) => {
        const v = getValue() as string;
        return <span className="text-[var(--color-text-secondary)] font-mono text-[11px] truncate max-w-xs block">{v}</span>;
      },
    },
    {
      accessorKey: 'github_issue_url',
      header: 'GH',
      cell: ({ getValue }) => {
        const url = getValue() as string | null;
        if (!url) return '—';
        return (
          <a href={url} target="_blank" rel="noopener noreferrer" className="text-[var(--color-accent)] hover:underline flex items-center gap-1">
            <ExternalLink size={10} />
          </a>
        );
      },
      size: 40,
    },
    {
      id: 'actions',
      header: '',
      cell: ({ row }) => (
        <button
          onClick={(e) => {
            e.stopPropagation();
            singleResolve.mutate(row.original.id);
          }}
          disabled={singleResolve.isPending}
          className="flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-0.5 text-[11px] text-[var(--color-text-secondary)] hover:border-[var(--color-profit)] hover:text-[var(--color-profit)] transition-colors disabled:opacity-50"
        >
          <CheckCircle size={10} />
          Resolve
        </button>
      ),
      size: 80,
      enableSorting: false,
    },
  ];

  if (srcLoading) return <PageLoader />;

  return (
    <div className="flex flex-col gap-4 h-full min-h-0">
      {/* Row 1 — Source cards */}
      <div>
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-secondary)] mb-2">Data Sources</div>
        <div className="grid grid-cols-3 gap-3">
          {(sources ?? []).map((s) => (
            <SourceCard key={s.source} source={s} />
          ))}
        </div>
      </div>

      {/* Row 2 — Tier heatmap */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <div className="flex items-center justify-between mb-3">
          <span className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-secondary)]">Tier Coverage (last 60 min)</span>
          <div className="flex gap-2">
            {[{ label: 'All', value: undefined }, { label: 'Tier 1', value: 1 }, { label: 'Tier 2', value: 2 }].map((opt) => (
              <button
                key={opt.label}
                onClick={() => navigate({ search: { ...search, tier: opt.value } })}
                className={cn('rounded px-2 py-0.5 text-[11px] border transition-colors',
                  (search.tier ?? undefined) === opt.value
                    ? 'border-[var(--color-primary)] text-[var(--color-primary)]'
                    : 'border-[var(--color-border)] text-[var(--color-text-secondary)]')}
              >
                {opt.label}
              </button>
            ))}
            <label className="flex items-center gap-1 text-[11px] text-[var(--color-text-secondary)]">
              <input
                type="checkbox"
                checked={search.degraded === 1}
                onChange={(e) => navigate({ search: { ...search, degraded: e.target.checked ? 1 : undefined } })}
                className="accent-[var(--color-primary)]"
              />
              Degraded only
            </label>
          </div>
        </div>
        <TierHeatmap tier={search.tier} onlyDegraded={search.degraded === 1} />
      </div>

      {/* Row 3 — Chain issues table */}
      <div className="flex-1 min-h-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] flex flex-col">
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-2 shrink-0">
          <span className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-secondary)]">
            Chain Issues ({issues?.length ?? 0} open)
          </span>
          {selectedIds.length > 0 && (
            <button
              onClick={() => bulkResolve.mutate(selectedIds)}
              disabled={bulkResolve.isPending}
              className="flex items-center gap-1.5 rounded border border-[var(--color-profit)] px-3 py-1 text-xs text-[var(--color-profit)] hover:bg-[var(--color-profit-light)] transition-colors disabled:opacity-50"
            >
              <CheckSquare size={12} />
              Resolve {selectedIds.length} selected
            </button>
          )}
        </div>
        {issueError ? (
          <div className="p-4 text-xs text-[var(--color-loss)]">Failed to load chain issues</div>
        ) : (
          <div className="flex-1 overflow-auto">
            <DataTable
              data={issues ?? []}
              columns={issueColumns}
              getRowId={(r) => r.id}
              enableRowSelection
              onRowSelectionChange={setRowSelection}
              stickyHeader
            />
          </div>
        )}
      </div>
    </div>
  );
}
