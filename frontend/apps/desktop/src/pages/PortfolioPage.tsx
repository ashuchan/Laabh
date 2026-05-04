import React, { useState } from 'react';
import { usePortfolio, useHoldings, usePortfolioHistory } from '@laabh/shared';
import { formatINR, formatPct, formatCompact } from '@laabh/shared';
import type { ColumnDef } from '@tanstack/react-table';
import type { Holding } from '@laabh/shared';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts';
import { KPICard } from '../components/ui/Card';
import { DataTable } from '../components/tables/DataTable';
import { PageLoader, ErrorState } from '../components/ui/Spinner';
import { cn } from '../lib/cn';

const HISTORY_DAYS = [7, 14, 30, 90];

const holdingColumns: ColumnDef<Holding, unknown>[] = [
  { accessorKey: 'instrument_id', header: 'Instrument', size: 120 },
  {
    accessorKey: 'quantity',
    header: 'Qty',
    size: 60,
    cell: ({ getValue }) => <span>{getValue() as number}</span>,
  },
  {
    accessorKey: 'avg_buy_price',
    header: 'Avg Price',
    cell: ({ getValue }) => formatINR(getValue() as number),
  },
  {
    accessorKey: 'current_price',
    header: 'CMP',
    cell: ({ getValue }) => {
      const v = getValue() as number | null;
      return v !== null ? formatINR(v) : '—';
    },
  },
  {
    accessorKey: 'pnl',
    header: 'P&L',
    cell: ({ getValue }) => {
      const v = getValue() as number | null;
      if (v === null) return '—';
      return <span className={v >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}>{formatINR(v)}</span>;
    },
    sortingFn: (a, b, id) => ((a.getValue(id) as number) ?? -999999) - ((b.getValue(id) as number) ?? -999999),
  },
  {
    accessorKey: 'pnl_pct',
    header: 'P&L%',
    cell: ({ getValue }) => {
      const v = getValue() as number | null;
      if (v === null) return '—';
      return <span className={v >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}>{formatPct(v)}</span>;
    },
    sortingFn: (a, b, id) => ((a.getValue(id) as number) ?? -999) - ((b.getValue(id) as number) ?? -999),
  },
  {
    accessorKey: 'weight_pct',
    header: 'Weight',
    cell: ({ getValue }) => {
      const v = getValue() as number | null;
      return v !== null ? `${v.toFixed(1)}%` : '—';
    },
  },
];

export function PortfolioPage() {
  const [historyDays, setHistoryDays] = useState(30);
  const { data: portfolio, isLoading, isError } = usePortfolio();
  const { data: holdings } = useHoldings();
  const { data: history } = usePortfolioHistory(historyDays);

  if (isLoading) return <PageLoader />;
  if (isError || !portfolio) return <ErrorState message="Failed to load portfolio" />;

  const chartData = (history ?? []).map((s) => ({
    date: s.date.slice(5),
    portfolio: s.cumulative_pnl_pct,
    benchmark: s.benchmark_pnl_pct,
  }));

  return (
    <div className="flex flex-col gap-4 h-full min-h-0">
      {/* KPIs */}
      <div className="grid grid-cols-5 gap-3 shrink-0">
        <KPICard
          label="Total Value"
          value={formatCompact(portfolio.current_value)}
          sub={`Invested: ${formatCompact(portfolio.invested_value)}`}
        />
        <KPICard
          label="Total P&L"
          value={formatCompact(portfolio.total_pnl)}
          colorClass={portfolio.total_pnl >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}
          sub={formatPct(portfolio.total_pnl_pct)}
        />
        <KPICard
          label="Day P&L"
          value={formatCompact(portfolio.day_pnl)}
          colorClass={portfolio.day_pnl >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}
        />
        <KPICard
          label="Cash"
          value={formatCompact(portfolio.current_cash)}
          sub={`${((portfolio.current_cash / portfolio.current_value) * 100).toFixed(1)}% of portfolio`}
        />
        <KPICard
          label="Win Rate"
          value={`${(portfolio.win_rate * 100).toFixed(1)}%`}
          sub={`${portfolio.total_trades} total trades`}
          colorClass={portfolio.win_rate >= 0.5 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}
        />
      </div>

      {/* Portfolio chart */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4 shrink-0">
        <div className="flex items-center justify-between mb-3">
          <span className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-secondary)]">Portfolio vs {portfolio.benchmark_symbol}</span>
          <div className="flex gap-2">
            {HISTORY_DAYS.map((d) => (
              <button
                key={d}
                onClick={() => setHistoryDays(d)}
                className={cn('rounded px-2 py-0.5 text-[11px] border transition-colors',
                  historyDays === d
                    ? 'border-[var(--color-primary)] text-[var(--color-primary)]'
                    : 'border-[var(--color-border)] text-[var(--color-text-secondary)]')}
              >
                {d}d
              </button>
            ))}
          </div>
        </div>
        <ResponsiveContainer width="100%" height={160}>
          <LineChart data={chartData}>
            <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: 'var(--color-text-muted)' }} />
            <YAxis unit="%" tick={{ fontSize: 10, fill: 'var(--color-text-muted)' }} />
            <Tooltip
              contentStyle={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', fontSize: 11 }}
              formatter={(v: number) => [`${v?.toFixed(2)}%`]}
            />
            <Legend wrapperStyle={{ fontSize: 11, color: 'var(--color-text-secondary)' }} />
            <Line type="monotone" dataKey="portfolio" name="Portfolio" stroke="var(--color-primary)" dot={false} strokeWidth={2} />
            <Line type="monotone" dataKey="benchmark" name={portfolio.benchmark_symbol} stroke="var(--color-text-muted)" dot={false} strokeWidth={1.5} strokeDasharray="4 4" />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* Holdings table */}
      <div className="flex-1 min-h-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-auto">
        <div className="border-b border-[var(--color-border)] px-4 py-2 text-xs font-semibold uppercase tracking-wider text-[var(--color-text-secondary)]">
          Holdings ({holdings?.length ?? 0})
        </div>
        <DataTable data={holdings ?? []} columns={holdingColumns} getRowId={(r) => r.id} stickyHeader />
      </div>
    </div>
  );
}
