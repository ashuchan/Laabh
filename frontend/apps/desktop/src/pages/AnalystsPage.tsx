import React from 'react';
import { useAnalystLeaderboard } from '@laabh/shared';
import { formatPct } from '@laabh/shared';
import type { ColumnDef } from '@tanstack/react-table';
import type { Analyst } from '@laabh/shared';
import { DataTable } from '../components/tables/DataTable';
import { PageLoader, ErrorState } from '../components/ui/Spinner';
import { cn } from '../lib/cn';

function ScoreBar({ value, max = 1 }: { value: number; max?: number }) {
  const pct = Math.min(Math.max(value / max, 0), 1) * 100;
  const color = value >= 0.7 * max ? 'var(--color-profit)' : value >= 0.4 * max ? 'var(--color-hold)' : 'var(--color-loss)';
  return (
    <div className="flex items-center gap-1.5">
      <div className="h-1 w-20 rounded-full bg-[var(--color-border)]">
        <div className="h-full rounded-full" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span className="text-[11px]" style={{ color }}>{(value * 100).toFixed(0)}</span>
    </div>
  );
}

const columns: ColumnDef<Analyst, unknown>[] = [
  {
    id: 'rank',
    header: '#',
    cell: ({ row }) => <span className="text-[var(--color-text-muted)]">{row.index + 1}</span>,
    size: 40,
    enableSorting: false,
  },
  { accessorKey: 'name', header: 'Name', size: 160 },
  {
    accessorKey: 'organization',
    header: 'Org',
    cell: ({ getValue }) => <span className="text-[var(--color-text-secondary)]">{(getValue() as string | null) ?? '—'}</span>,
  },
  {
    accessorKey: 'hit_rate',
    header: 'Hit Rate',
    cell: ({ getValue }) => <ScoreBar value={getValue() as number} />,
    sortingFn: (a, b, id) => (a.getValue(id) as number) - (b.getValue(id) as number),
  },
  {
    accessorKey: 'avg_return_pct',
    header: 'Avg Return',
    cell: ({ getValue }) => {
      const v = getValue() as number;
      return <span className={v >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}>{formatPct(v)}</span>;
    },
    sortingFn: (a, b, id) => (a.getValue(id) as number) - (b.getValue(id) as number),
  },
  {
    accessorKey: 'credibility_score',
    header: 'Credibility',
    cell: ({ getValue }) => <ScoreBar value={getValue() as number} />,
    sortingFn: (a, b, id) => (a.getValue(id) as number) - (b.getValue(id) as number),
  },
  {
    accessorKey: 'total_signals',
    header: 'Signals',
    cell: ({ getValue }) => <span>{getValue() as number}</span>,
    sortingFn: (a, b, id) => (a.getValue(id) as number) - (b.getValue(id) as number),
  },
  {
    accessorKey: 'best_sector',
    header: 'Best Sector',
    cell: ({ getValue }) => <span className="text-[var(--color-text-secondary)]">{(getValue() as string | null) ?? '—'}</span>,
  },
];

export function AnalystsPage() {
  const { data, isLoading, isError } = useAnalystLeaderboard(50);

  if (isLoading) return <PageLoader />;
  if (isError) return <ErrorState message="Failed to load analyst leaderboard" />;

  return (
    <div className="flex flex-col gap-4 h-full min-h-0">
      <div className="text-sm font-medium text-[var(--color-text)] shrink-0">
        Analyst Leaderboard ({data?.length ?? 0})
      </div>
      <div className="flex-1 min-h-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-auto">
        <DataTable
          data={data ?? []}
          columns={columns}
          getRowId={(r) => r.id}
          stickyHeader
        />
      </div>
    </div>
  );
}
