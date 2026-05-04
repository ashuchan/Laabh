import React, { useState } from 'react';
import { useSearch, useNavigate } from '@tanstack/react-router';
import { useDailyReport, useVIXHistory } from '@laabh/shared';
import { formatIST, formatINR, formatCompact, timeAgo } from '@laabh/shared';
import { ChevronLeft, ChevronRight, Calendar, AlertTriangle } from 'lucide-react';
import { format, addDays, subDays, parseISO } from 'date-fns';
import {
  LineChart,
  Line,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Card, KPICard } from '../../components/ui/Card';
import { Badge, statusBadgeVariant } from '../../components/ui/Badge';
import { PageLoader, ErrorState } from '../../components/ui/Spinner';
import { Drawer } from '../../components/ui/Drawer';
import { DataTable } from '../../components/tables/DataTable';
import { useDateStepShortcut } from '../../hooks/useDateStepShortcut';
import type { ColumnDef } from '@tanstack/react-table';
import { cn } from '../../lib/cn';

function PipelineTimeline({ jobs }: { jobs: Record<string, Array<{ status: string; runs: number; last_run: string }>> }) {
  return (
    <div className="flex flex-wrap gap-2">
      {Object.entries(jobs).map(([name, runs]) => {
        const last = runs[runs.length - 1];
        const color =
          last?.status === 'success'
            ? 'bg-[var(--color-profit)] text-black'
            : last?.status === 'error'
              ? 'bg-[var(--color-loss)] text-white'
              : 'bg-orange-500 text-white';
        return (
          <div key={name} className="group relative">
            <div
              className={cn(
                'h-3 w-3 rounded-full cursor-pointer',
                color,
              )}
              title={`${name}: ${last?.status} (${last?.runs} runs)`}
            />
            <div className="pointer-events-none absolute -top-8 left-0 hidden group-hover:block bg-[var(--color-surface-elevated)] border border-[var(--color-border)] rounded px-2 py-1 text-[10px] whitespace-nowrap z-10">
              <div className="font-medium">{name}</div>
              <div className="text-[var(--color-text-secondary)]">{last?.status} · {timeAgo(last?.last_run)}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function VIXSparkline() {
  const { data } = useVIXHistory(30);
  if (!data?.length) return null;
  const chart = data.map((t) => ({
    x: formatIST(t.timestamp).split(',')[0],
    v: typeof t.vix_value === 'string' ? parseFloat(t.vix_value) : t.vix_value,
  }));
  const last = chart[chart.length - 1]?.v;
  const color = last > 20 ? 'var(--color-loss)' : last > 15 ? 'var(--color-hold)' : 'var(--color-profit)';
  return (
    <div>
      <div className="text-xs text-[var(--color-text-secondary)] mb-1">VIX · <span style={{ color }}>{last?.toFixed(1)}</span></div>
      <ResponsiveContainer width="100%" height={50}>
        <LineChart data={chart}>
          <Line type="monotone" dataKey="v" stroke={color} dot={false} strokeWidth={1.5} />
          <RechartsTooltip
            contentStyle={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)', fontSize: 11 }}
            formatter={(v: number) => [v.toFixed(2), 'VIX']}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

interface DecisionQualityRow {
  symbol: string;
  strategy: string;
  status: string;
  final_pnl: number | null;
  thesis_excerpt: string | null;
}

export function DailyReportPage() {
  const search = useSearch({ from: '/reports/daily' });
  const navigate = useNavigate({ from: '/reports/daily' });
  const [selectedDecision, setSelectedDecision] = useState<DecisionQualityRow | null>(null);

  const dateStr = search.date ?? format(new Date(), 'yyyy-MM-dd');
  const { data, isLoading, isError, refetch } = useDailyReport(dateStr);
  useDateStepShortcut(prevDay, nextDay);

  function prevDay() {
    navigate({ search: { date: format(subDays(parseISO(dateStr), 1), 'yyyy-MM-dd') } });
  }
  function nextDay() {
    navigate({ search: { date: format(addDays(parseISO(dateStr), 1), 'yyyy-MM-dd') } });
  }
  function goToday() {
    navigate({ search: {} });
  }

  const dqColumns: ColumnDef<DecisionQualityRow, unknown>[] = [
    { accessorKey: 'symbol', header: 'Symbol', size: 90 },
    { accessorKey: 'strategy', header: 'Strategy', size: 120 },
    {
      accessorKey: 'status',
      header: 'Status',
      cell: ({ getValue }) => {
        const v = getValue() as string;
        return <Badge variant={statusBadgeVariant(v)}>{v}</Badge>;
      },
    },
    {
      accessorKey: 'final_pnl',
      header: 'P&L',
      cell: ({ getValue }) => {
        const v = getValue() as number | null;
        if (v === null) return <span className="text-[var(--color-text-muted)]">—</span>;
        return (
          <span className={v >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}>
            {formatINR(v)}
          </span>
        );
      },
    },
    {
      accessorKey: 'thesis_excerpt',
      header: 'Thesis',
      cell: ({ getValue }) => {
        const v = getValue() as string | null;
        return <span className="text-[var(--color-text-secondary)] truncate max-w-xs block">{v ?? '—'}</span>;
      },
    },
  ];

  if (isLoading) return <PageLoader />;
  if (isError || !data) return <ErrorState message="Failed to load daily report" />;

  const { pipeline_completeness, trading, llm_activity, chain_health, vix_stats, source_health, surprises } = data;

  return (
    <div className="flex gap-4 h-full min-h-0">
      {/* Left col — date stepper + surprises */}
      <div className="w-72 shrink-0 flex flex-col gap-4">
        {/* Date stepper */}
        <Card>
          <div className="flex items-center justify-between gap-2">
            <button onClick={prevDay} className="p-1 rounded hover:bg-[var(--color-surface-elevated)] text-[var(--color-text-secondary)]" title="Previous day ([)">
              <ChevronLeft size={14} />
            </button>
            <div className="flex items-center gap-1.5 text-sm font-medium text-[var(--color-text)]">
              <Calendar size={13} />
              {dateStr}
            </div>
            <button onClick={nextDay} className="p-1 rounded hover:bg-[var(--color-surface-elevated)] text-[var(--color-text-secondary)]" title="Next day (])">
              <ChevronRight size={14} />
            </button>
          </div>
          <button onClick={goToday} className="mt-2 w-full rounded border border-[var(--color-border)] py-1 text-xs text-[var(--color-text-secondary)] hover:text-[var(--color-text)] hover:border-[var(--color-primary)] transition-colors">
            Today
          </button>
        </Card>

        {/* Surprises */}
        {surprises.length > 0 && (
          <Card title="Surprises">
            <div className="flex flex-col gap-2">
              {surprises.map((s, i) => (
                <div key={i} className="flex items-start gap-2 text-xs text-[var(--color-text-secondary)]">
                  <AlertTriangle size={12} className="text-[var(--color-high)] mt-0.5 shrink-0" />
                  <span>{s}</span>
                </div>
              ))}
            </div>
          </Card>
        )}

        {/* Source health */}
        <Card title="Source Health">
          <div className="flex flex-col gap-1.5">
            {source_health.map((sh) => (
              <div key={sh.source} className="flex items-center justify-between">
                <span className="text-xs text-[var(--color-text-secondary)] truncate">{sh.source}</span>
                <Badge variant={statusBadgeVariant(sh.status)}>{sh.status}</Badge>
              </div>
            ))}
          </div>
        </Card>

        {/* VIX sparkline */}
        <Card title="VIX">
          <VIXSparkline />
          {vix_stats.avg_value && (
            <div className="mt-2 grid grid-cols-3 gap-2 text-[10px] text-[var(--color-text-secondary)]">
              <div><div className="text-[var(--color-text)]">{vix_stats.avg_value?.toFixed(1)}</div>avg</div>
              <div><div className="text-[var(--color-profit)]">{vix_stats.min_value?.toFixed(1)}</div>min</div>
              <div><div className="text-[var(--color-loss)]">{vix_stats.max_value?.toFixed(1)}</div>max</div>
            </div>
          )}
        </Card>
      </div>

      {/* Middle col — pipeline + trading */}
      <div className="flex-1 flex flex-col gap-4 min-w-0">
        {/* Pipeline */}
        <Card title="Pipeline Jobs">
          <div className="flex items-center justify-between mb-3">
            <div className="text-xs text-[var(--color-text-secondary)]">
              {pipeline_completeness.ran}/{pipeline_completeness.total_scheduled} ran
              {pipeline_completeness.skipped.length > 0 && (
                <span className="ml-2 text-orange-400">· {pipeline_completeness.skipped.length} skipped</span>
              )}
            </div>
            <div className="w-32 h-1.5 rounded-full bg-[var(--color-border)] overflow-hidden">
              <div
                className="h-full rounded-full bg-[var(--color-profit)]"
                style={{ width: `${(pipeline_completeness.ran / pipeline_completeness.total_scheduled) * 100}%` }}
              />
            </div>
          </div>
          <PipelineTimeline jobs={pipeline_completeness.jobs} />
        </Card>

        {/* Trading KPIs */}
        <div className="grid grid-cols-4 gap-3">
          <KPICard
            label="Day P&L"
            value={formatCompact(trading.day_pnl)}
            colorClass={trading.day_pnl >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]'}
          />
          <KPICard label="Proposed" value={trading.proposed} />
          <KPICard label="Filled" value={trading.filled} />
          <KPICard
            label="Win Rate"
            value={`${((trading.closed_target / Math.max(trading.closed_target + trading.closed_stop, 1)) * 100).toFixed(0)}%`}
            sub={`${trading.closed_target} hit / ${trading.closed_stop} stop`}
          />
        </div>

        {/* Decision Quality table */}
        <Card title="Decision Quality" className="flex-1">
          <DataTable
            data={trading.decision_quality}
            columns={dqColumns}
            onRowClick={setSelectedDecision}
            selectedRowId={selectedDecision?.symbol ?? null}
            getRowId={(r) => r.symbol}
            stickyHeader
          />
        </Card>
      </div>

      {/* Right col — chain health + LLM */}
      <div className="w-64 shrink-0 flex flex-col gap-4">
        {/* Chain health donut summary */}
        <Card title="Chain Ingestion">
          <div className="flex flex-col gap-2">
            {[
              { label: 'OK', value: chain_health.ok, pct: chain_health.ok_pct, color: 'var(--color-profit)' },
              { label: 'Fallback', value: chain_health.fallback, pct: chain_health.fallback_pct, color: 'var(--color-hold)' },
              { label: 'Missed', value: chain_health.missed, pct: chain_health.missed_pct, color: 'var(--color-loss)' },
            ].map((item) => (
              <div key={item.label}>
                <div className="flex justify-between text-xs mb-1">
                  <span className="text-[var(--color-text-secondary)]">{item.label}</span>
                  <span style={{ color: item.color }}>{item.value} ({item.pct.toFixed(0)}%)</span>
                </div>
                <div className="h-1 rounded-full bg-[var(--color-border)]">
                  <div className="h-full rounded-full" style={{ width: `${item.pct}%`, background: item.color }} />
                </div>
              </div>
            ))}
            <div className="mt-1 text-[10px] text-[var(--color-text-muted)]">
              NSE share: {chain_health.nse_share_pct.toFixed(0)}%
            </div>
          </div>
        </Card>

        {/* LLM cost */}
        <Card title="LLM Activity">
          <div className="mb-2 flex justify-between">
            <span className="text-xs text-[var(--color-text-secondary)]">Est. cost</span>
            <span className="text-xs text-[var(--color-accent)]">${llm_activity.estimated_cost_usd.toFixed(4)}</span>
          </div>
          <div className="mb-3 flex justify-between">
            <span className="text-xs text-[var(--color-text-secondary)]">Tokens in/out</span>
            <span className="text-xs text-[var(--color-text)]">
              {(llm_activity.total_tokens_in / 1000).toFixed(1)}k / {(llm_activity.total_tokens_out / 1000).toFixed(1)}k
            </span>
          </div>
          <div className="flex flex-col gap-1">
            {llm_activity.callers.map((c) => (
              <div key={c.caller} className="flex justify-between text-[11px]">
                <span className="text-[var(--color-text-secondary)] truncate">{c.caller}</span>
                <span className="text-[var(--color-text-muted)]">{c.row_count}</span>
              </div>
            ))}
          </div>
        </Card>

        {/* Candidates summary */}
        <Card title="Candidates by Phase">
          <div className="flex flex-col gap-1">
            {Object.entries(data.candidates).map(([phase, count]) => (
              <div key={phase} className="flex justify-between text-xs">
                <span className="text-[var(--color-text-secondary)]">Phase {phase}</span>
                <span className="text-[var(--color-text)]">{count}</span>
              </div>
            ))}
          </div>
        </Card>
      </div>

      {/* Decision detail drawer */}
      <Drawer open={Boolean(selectedDecision)} onClose={() => setSelectedDecision(null)} title={selectedDecision?.symbol ?? ''} width="w-[420px]">
        {selectedDecision && (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <div className="text-[10px] text-[var(--color-text-secondary)] uppercase tracking-wider mb-1">Strategy</div>
                <div className="text-sm">{selectedDecision.strategy}</div>
              </div>
              <div>
                <div className="text-[10px] text-[var(--color-text-secondary)] uppercase tracking-wider mb-1">Status</div>
                <Badge variant={statusBadgeVariant(selectedDecision.status)}>{selectedDecision.status}</Badge>
              </div>
              <div>
                <div className="text-[10px] text-[var(--color-text-secondary)] uppercase tracking-wider mb-1">P&L</div>
                <div className={cn('text-sm font-medium', selectedDecision.final_pnl != null && selectedDecision.final_pnl >= 0 ? 'text-[var(--color-profit)]' : 'text-[var(--color-loss)]')}>
                  {selectedDecision.final_pnl != null ? formatINR(selectedDecision.final_pnl) : '—'}
                </div>
              </div>
            </div>
            <div>
              <div className="text-[10px] text-[var(--color-text-secondary)] uppercase tracking-wider mb-2">LLM Thesis</div>
              <div className="rounded border border-[var(--color-border)] bg-[var(--color-surface-elevated)] p-3 text-xs leading-relaxed text-[var(--color-text-secondary)] whitespace-pre-wrap">
                {selectedDecision.thesis_excerpt ?? 'No thesis recorded'}
              </div>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  );
}
