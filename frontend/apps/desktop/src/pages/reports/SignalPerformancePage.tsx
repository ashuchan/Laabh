import React, { useState, useMemo } from 'react';
import { useSearch, useNavigate } from '@tanstack/react-router';
import { useSignalPerformance, useSignalPerformanceTimeseries, useAnalystLeaderboard } from '@laabh/shared';
import { formatPct, formatIST } from '@laabh/shared';
import type { ColumnDef } from '@tanstack/react-table';
import type { SignalPerformanceRow, Analyst } from '@laabh/shared';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts';
import { DataTable } from '../../components/tables/DataTable';
import { Badge, actionBadgeVariant, statusBadgeVariant } from '../../components/ui/Badge';
import { KPICard } from '../../components/ui/Card';
import { Drawer } from '../../components/ui/Drawer';
import { PageLoader, ErrorState } from '../../components/ui/Spinner';
import { cn } from '../../lib/cn';

const DAYS_OPTIONS = [7, 14, 30, 60, 90, 180];

const columns: ColumnDef<SignalPerformanceRow, unknown>[] = [
  {
    accessorKey: 'signal_date',
    header: 'Date',
    cell: ({ getValue }) => <span className="text-[var(--color-text-secondary)]">{formatIST(getValue() as string)}</span>,
    size: 130,
  },
  { accessorKey: 'symbol', header: 'Symbol', size: 90 },
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
      return <Badge variant={statusBadgeVariant(v)}>{v.replace('resolved_', '')}</Badge>;
    },
  },
  {
    accessorKey: 'outcome_pnl_pct',
    header: 'P&L %',
    cell: ({ getValue }) => {
      const v = getValue() as number | null;
      if (v === null) return <span className="text-[var(--color-text-muted)]">—</span>;
      return <span className={v >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}>{formatPct(v)}</span>;
    },
    sortingFn: (a, b, id) => ((a.getValue(id) as number) ?? -999) - ((b.getValue(id) as number) ?? -999),
  },
  {
    accessorKey: 'convergence_score',
    header: 'Conv',
    cell: ({ getValue }) => <span className="text-[var(--color-text-secondary)]">{(getValue() as number).toFixed(2)}</span>,
  },
  {
    accessorKey: 'confidence',
    header: 'Conf',
    cell: ({ getValue }) => {
      const v = getValue() as number | null;
      return v !== null ? <span className="text-[var(--color-text-secondary)]">{(v * 100).toFixed(0)}%</span> : '—';
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

export function SignalPerformancePage() {
  const search = useSearch({ from: '/reports/signal-performance' });
  const navigate = useNavigate({ from: '/reports/signal-performance' });
  const [selectedRow, setSelectedRow] = useState<SignalPerformanceRow | null>(null);
  const [analystFilter, setAnalystFilter] = useState('');

  const days = search.days ?? 30;
  const { data, isLoading, isError } = useSignalPerformance({ days, analyst_id: search.analyst_id });
  const { data: tsData } = useSignalPerformanceTimeseries({ bucket: 'day', days });
  const { data: analysts } = useAnalystLeaderboard(50);

  const filteredAnalysts = useMemo(() => {
    if (!analysts) return [];
    const q = analystFilter.toLowerCase();
    return analysts.filter((a) => a.name.toLowerCase().includes(q));
  }, [analysts, analystFilter]);

  if (isLoading) return <PageLoader />;
  if (isError || !data) return <ErrorState message="Failed to load signal performance" />;

  const chartData = tsData?.buckets.map((b) => ({
    date: b.date.slice(5),
    hr: b.hit_rate * 100,
    count: b.count,
  })) ?? [];

  return (
    <div className="flex flex-col gap-4 h-full min-h-0">
      {/* Top pane — KPIs + controls */}
      <div className="flex items-start gap-4 flex-wrap">
        {/* KPIs */}
        <div className="grid grid-cols-4 gap-3 flex-1">
          <KPICard
            label="Hit Rate"
            value={`${(data.hit_rate * 100).toFixed(1)}%`}
            colorClass={data.hit_rate >= 0.5 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}
            sub={`${data.hits} hits / ${data.misses} misses`}
          />
          <KPICard
            label="Avg P&L"
            value={data.avg_pnl_pct != null ? formatPct(data.avg_pnl_pct) : '—'}
            colorClass={data.avg_pnl_pct != null && data.avg_pnl_pct >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}
          />
          <KPICard label="Total Signals" value={data.total} sub={`${data.resolved} resolved`} />
          <div className="flex flex-col gap-1 justify-center">
            {DAYS_OPTIONS.map((d) => (
              <button
                key={d}
                onClick={() => navigate({ search: { days: d, analyst_id: search.analyst_id } })}
                className={cn(
                  'rounded px-2 py-0.5 text-[11px] border transition-colors',
                  days === d
                    ? 'border-[var(--color-primary)] text-[var(--color-primary)]'
                    : 'border-[var(--color-border)] text-[var(--color-text-secondary)]',
                )}
              >
                {d}d
              </button>
            ))}
          </div>
        </div>

        {/* Rolling hit rate chart */}
        {chartData.length > 0 && (
          <div className="w-64">
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-secondary)] mb-1">Rolling Hit Rate (28d)</div>
            <ResponsiveContainer width="100%" height={80}>
              <LineChart data={chartData}>
                <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fontSize: 9, fill: 'var(--color-text-muted)' }} />
                <YAxis domain={[0, 100]} tick={{ fontSize: 9, fill: 'var(--color-text-muted)' }} unit="%" />
                <Tooltip
                  contentStyle={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', fontSize: 11 }}
                  formatter={(v: number) => [`${v.toFixed(1)}%`, 'Hit Rate']}
                />
                <Line type="monotone" dataKey="hr" stroke="var(--color-primary)" dot={false} strokeWidth={1.5} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      {/* Bottom pane — table + analyst filter */}
      <div className="flex gap-4 flex-1 min-h-0">
        {/* Analyst filter sidebar */}
        <div className="w-48 shrink-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] flex flex-col">
          <div className="p-2 border-b border-[var(--color-border)]">
            <input
              value={analystFilter}
              onChange={(e) => setAnalystFilter(e.target.value)}
              placeholder="Filter analyst…"
              className="w-full bg-transparent text-xs text-[var(--color-text)] placeholder-[var(--color-text-muted)] outline-none"
            />
          </div>
          <div className="flex-1 overflow-y-auto py-1">
            <button
              onClick={() => navigate({ search: { days, analyst_id: undefined } })}
              className={cn(
                'w-full text-left px-3 py-1.5 text-xs transition-colors',
                !search.analyst_id ? 'text-[var(--color-primary)] bg-[var(--color-surface-elevated)]' : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-elevated)]',
              )}
            >
              All analysts
            </button>
            {filteredAnalysts.map((a) => (
              <button
                key={a.id}
                onClick={() => navigate({ search: { days, analyst_id: a.id } })}
                className={cn(
                  'w-full text-left px-3 py-1.5 text-xs transition-colors',
                  search.analyst_id === a.id ? 'text-[var(--color-primary)] bg-[var(--color-surface-elevated)]' : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-elevated)]',
                )}
              >
                <div className="truncate">{a.name}</div>
                <div className="text-[10px] text-[var(--color-text-muted)]">{(a.hit_rate * 100).toFixed(0)}% HR</div>
              </button>
            ))}
          </div>
        </div>

        {/* Signal table */}
        <div className="flex-1 min-w-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-auto">
          <DataTable
            data={data.rows}
            columns={columns}
            onRowClick={setSelectedRow}
            selectedRowId={selectedRow?.id ?? null}
            getRowId={(r) => r.id}
            stickyHeader
          />
        </div>
      </div>

      {/* Signal detail drawer */}
      <Drawer
        open={Boolean(selectedRow)}
        onClose={() => setSelectedRow(null)}
        title={selectedRow ? `${selectedRow.symbol} — ${selectedRow.action}` : ''}
        width="w-[440px]"
      >
        {selectedRow && (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-3">
              {[
                { label: 'Signal Date', value: formatIST(selectedRow.signal_date) },
                { label: 'Action', value: <Badge variant={actionBadgeVariant(selectedRow.action)}>{selectedRow.action}</Badge> },
                { label: 'Status', value: <Badge variant={statusBadgeVariant(selectedRow.status)}>{selectedRow.status.replace('resolved_', '')}</Badge> },
                { label: 'Entry Price', value: selectedRow.entry_price != null ? `₹${selectedRow.entry_price}` : '—' },
                { label: 'Target Price', value: selectedRow.target_price != null ? `₹${selectedRow.target_price}` : '—' },
                { label: 'Outcome P&L', value: selectedRow.outcome_pnl_pct != null ? formatPct(selectedRow.outcome_pnl_pct) : '—' },
                { label: 'Convergence', value: selectedRow.convergence_score.toFixed(2) },
                { label: 'Confidence', value: selectedRow.confidence != null ? `${(selectedRow.confidence * 100).toFixed(0)}%` : '—' },
                { label: 'Analyst', value: selectedRow.analyst_name_raw ?? '—' },
              ].map(({ label, value }) => (
                <div key={label}>
                  <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">{label}</div>
                  <div className="text-sm text-[var(--color-text)]">{value}</div>
                </div>
              ))}
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
