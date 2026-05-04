import React, { useState } from 'react';
import { useActiveSignals } from '@laabh/shared';
import { formatIST, formatINR, formatPct } from '@laabh/shared';
import type { ColumnDef } from '@tanstack/react-table';
import type { Signal } from '@laabh/shared';
import { DataTable } from '../components/tables/DataTable';
import { Badge, actionBadgeVariant, statusBadgeVariant } from '../components/ui/Badge';
import { Drawer } from '../components/ui/Drawer';
import { PageLoader, ErrorState } from '../components/ui/Spinner';
import { RefreshCw } from 'lucide-react';

const columns: ColumnDef<Signal, unknown>[] = [
  {
    accessorKey: 'signal_date',
    header: 'Date',
    cell: ({ getValue }) => <span className="text-[var(--color-text-secondary)]">{formatIST(getValue() as string)}</span>,
    size: 130,
  },
  { accessorKey: 'instrument_id', header: 'Instrument', size: 100 },
  {
    accessorKey: 'action',
    header: 'Action',
    cell: ({ getValue }) => {
      const v = getValue() as string;
      return <Badge variant={actionBadgeVariant(v)}>{v}</Badge>;
    },
    size: 70,
  },
  {
    accessorKey: 'status',
    header: 'Status',
    cell: ({ getValue }) => {
      const v = getValue() as string;
      return <Badge variant={statusBadgeVariant(v)}>{v}</Badge>;
    },
  },
  {
    accessorKey: 'convergence_score',
    header: 'Conv',
    cell: ({ getValue }) => <span>{(getValue() as number).toFixed(2)}</span>,
  },
  {
    accessorKey: 'confidence',
    header: 'Conf',
    cell: ({ getValue }) => {
      const v = getValue() as number | null;
      return v !== null ? `${(v * 100).toFixed(0)}%` : '—';
    },
  },
  {
    accessorKey: 'entry_price',
    header: 'Entry',
    cell: ({ getValue }) => {
      const v = getValue() as number | null;
      return v !== null ? formatINR(v) : '—';
    },
  },
  {
    accessorKey: 'target_price',
    header: 'Target',
    cell: ({ getValue }) => {
      const v = getValue() as number | null;
      return v !== null ? formatINR(v) : '—';
    },
  },
  {
    accessorKey: 'stop_loss',
    header: 'SL',
    cell: ({ getValue }) => {
      const v = getValue() as number | null;
      return v !== null ? formatINR(v) : '—';
    },
  },
  {
    accessorKey: 'analyst_name_raw',
    header: 'Analyst',
    cell: ({ getValue }) => {
      const v = getValue() as string | null;
      return <span className="text-[var(--color-text-secondary)] text-[11px]">{v ?? '—'}</span>;
    },
  },
];

export function SignalsPage() {
  const [selected, setSelected] = useState<Signal | null>(null);
  const { data, isLoading, isError, refetch, isFetching } = useActiveSignals(100);

  if (isLoading) return <PageLoader />;
  if (isError) return <ErrorState message="Failed to load signals" />;

  return (
    <div className="flex flex-col gap-4 h-full min-h-0">
      <div className="flex items-center justify-between shrink-0">
        <span className="text-sm font-medium text-[var(--color-text)]">{data?.length ?? 0} active signals</span>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="flex items-center gap-1.5 rounded border border-[var(--color-border)] px-2 py-1 text-xs text-[var(--color-text-secondary)] hover:text-[var(--color-text)] transition-colors"
        >
          <RefreshCw size={11} className={isFetching ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>
      <div className="flex-1 min-h-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-auto">
        <DataTable
          data={data ?? []}
          columns={columns}
          onRowClick={setSelected}
          selectedRowId={selected?.id ?? null}
          getRowId={(r) => r.id}
          stickyHeader
        />
      </div>

      <Drawer open={Boolean(selected)} onClose={() => setSelected(null)} title={selected ? `${selected.instrument_id} — ${selected.action}` : ''} width="w-[440px]">
        {selected && (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-3">
              {[
                { label: 'Date', value: formatIST(selected.signal_date) },
                { label: 'Action', value: <Badge variant={actionBadgeVariant(selected.action)}>{selected.action}</Badge> },
                { label: 'Status', value: <Badge variant={statusBadgeVariant(selected.status)}>{selected.status}</Badge> },
                { label: 'Timeframe', value: selected.timeframe },
                { label: 'Entry', value: selected.entry_price != null ? formatINR(selected.entry_price) : '—' },
                { label: 'Target', value: selected.target_price != null ? formatINR(selected.target_price) : '—' },
                { label: 'Stop Loss', value: selected.stop_loss != null ? formatINR(selected.stop_loss) : '—' },
                { label: 'Convergence', value: selected.convergence_score.toFixed(2) },
                { label: 'Confidence', value: selected.confidence != null ? `${(selected.confidence * 100).toFixed(0)}%` : '—' },
                { label: 'Analyst', value: selected.analyst_name_raw ?? '—' },
                { label: 'Outcome P&L', value: selected.outcome_pnl_pct != null ? formatPct(selected.outcome_pnl_pct) : '—' },
              ].map(({ label, value }) => (
                <div key={label}>
                  <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">{label}</div>
                  <div className="text-sm text-[var(--color-text)]">{value}</div>
                </div>
              ))}
            </div>
            {selected.reasoning && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2">Reasoning</div>
                <div className="rounded border border-[var(--color-border)] bg-[var(--color-surface-elevated)] p-3 text-xs leading-relaxed text-[var(--color-text-secondary)] whitespace-pre-wrap">
                  {selected.reasoning}
                </div>
              </div>
            )}
          </div>
        )}
      </Drawer>
    </div>
  );
}
