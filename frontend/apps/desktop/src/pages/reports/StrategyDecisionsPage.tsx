import React, { useState } from 'react';
import { useSearch, useNavigate } from '@tanstack/react-router';
import { useStrategyDecisions } from '@laabh/shared';
import { formatIST, timeAgo } from '@laabh/shared';
import { format, subDays, addDays, parseISO } from 'date-fns';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import type { StrategyDecision } from '@laabh/shared';
import { Badge } from '../../components/ui/Badge';
import { Drawer } from '../../components/ui/Drawer';
import { PageLoader, ErrorState, EmptyState } from '../../components/ui/Spinner';
import { useDateStepShortcut } from '../../hooks/useDateStepShortcut';
import { cn } from '../../lib/cn';

const DECISION_TYPES = [
  { value: 'morning_allocation', label: 'Morning Alloc', color: 'var(--color-accent)', time: '09:15' },
  { value: 'intraday_action', label: 'Intraday', color: 'var(--color-hold)', time: '11:00' },
  { value: 'eod_squareoff', label: 'EOD Squareoff', color: 'var(--color-loss)', time: '15:15' },
];

function typeColor(type: string): string {
  return DECISION_TYPES.find((t) => t.value === type)?.color ?? 'var(--color-text-secondary)';
}

function typeLabel(type: string): string {
  return DECISION_TYPES.find((t) => t.value === type)?.label ?? type;
}

function TimelineEvent({
  decision,
  onClick,
  selected,
}: {
  decision: StrategyDecision;
  onClick: () => void;
  selected: boolean;
}) {
  const time = new Date(decision.as_of);
  const istMins = time.getUTCHours() * 60 + time.getUTCMinutes() + 5 * 60 + 30; // UTC+5:30
  const openMins = 9 * 60 + 15;
  const closeMins = 15 * 60 + 30;
  const pct = Math.min(Math.max(((istMins - openMins) / (closeMins - openMins)) * 100, 0), 100);
  const color = typeColor(decision.decision_type);

  return (
    <div
      className="absolute flex flex-col items-center cursor-pointer group"
      style={{ left: `calc(${pct}% - 8px)`, top: 0 }}
      onClick={onClick}
    >
      <div
        className={cn(
          'h-4 w-4 rounded-full border-2 transition-transform group-hover:scale-125',
          selected ? 'scale-125' : '',
        )}
        style={{ borderColor: color, background: selected ? color : 'var(--color-surface)' }}
      />
      <div className={cn(
        'mt-1 rounded border px-1.5 py-0.5 text-[10px] whitespace-nowrap',
        selected ? 'border-[var(--color-primary)] text-[var(--color-text)]' : 'border-[var(--color-border)] text-[var(--color-text-secondary)]',
      )}>
        {typeLabel(decision.decision_type)}
      </div>
    </div>
  );
}

export function StrategyDecisionsPage() {
  const search = useSearch({ from: '/reports/strategy-decisions' });
  const navigate = useNavigate({ from: '/reports/strategy-decisions' });
  const [selected, setSelected] = useState<StrategyDecision | null>(null);

  const dateStr = search.date ?? format(new Date(), 'yyyy-MM-dd');
  useDateStepShortcut(prevDay, nextDay);
  const { data, isLoading, isError } = useStrategyDecisions({
    date: dateStr,
    decision_type: search.type,
  });

  function prevDay() {
    navigate({ search: { date: format(subDays(parseISO(dateStr), 1), 'yyyy-MM-dd') } });
  }
  function nextDay() {
    navigate({ search: { date: format(addDays(parseISO(dateStr), 1), 'yyyy-MM-dd') } });
  }

  if (isLoading) return <PageLoader />;
  if (isError) return <ErrorState message="Failed to load strategy decisions" />;
  if (!data?.length) return (
    <div className="flex flex-col gap-4 h-full">
      <div className="flex items-center gap-3">
        <button onClick={prevDay} className="p-1 rounded hover:bg-[var(--color-surface-elevated)]"><ChevronLeft size={14} /></button>
        <span className="text-sm font-medium text-[var(--color-text)]">{dateStr}</span>
        <button onClick={nextDay} className="p-1 rounded hover:bg-[var(--color-surface-elevated)]"><ChevronRight size={14} /></button>
      </div>
      <EmptyState message="No strategy decisions for this date" />
    </div>
  );

  // Group by decision type for the filter tabs
  const types = [...new Set(data.map((d) => d.decision_type))];

  return (
    <div className="flex flex-col gap-4 h-full">
      {/* Header */}
      <div className="flex items-center gap-3 flex-wrap">
        <button onClick={prevDay} className="p-1 rounded hover:bg-[var(--color-surface-elevated)] text-[var(--color-text-secondary)]"><ChevronLeft size={14} /></button>
        <span className="text-sm font-medium text-[var(--color-text)]">{dateStr}</span>
        <button onClick={nextDay} className="p-1 rounded hover:bg-[var(--color-surface-elevated)] text-[var(--color-text-secondary)]"><ChevronRight size={14} /></button>

        <div className="ml-4 flex gap-2">
          <button
            onClick={() => navigate({ search: { date: dateStr } })}
            className={cn('rounded px-2 py-0.5 text-xs border transition-colors',
              !search.type ? 'border-[var(--color-primary)] text-[var(--color-primary)]' : 'border-[var(--color-border)] text-[var(--color-text-secondary)]')}
          >
            All
          </button>
          {types.map((t) => (
            <button
              key={t}
              onClick={() => navigate({ search: { date: dateStr, type: search.type === t ? undefined : t } })}
              className={cn('rounded px-2 py-0.5 text-xs border transition-colors',
                search.type === t ? 'border-[var(--color-primary)] text-[var(--color-primary)]' : 'border-[var(--color-border)] text-[var(--color-text-secondary)]')}
            >
              {typeLabel(t)}
            </button>
          ))}
        </div>
      </div>

      {/* Timeline bar (9:15–15:30) */}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <div className="flex items-center justify-between text-[10px] text-[var(--color-text-muted)] mb-2">
          <span>9:15</span>
          <span>11:00</span>
          <span>13:00</span>
          <span>15:00</span>
          <span>15:30</span>
        </div>
        <div className="relative h-12 border-b border-[var(--color-border)]">
          <div className="absolute inset-x-0 top-3 h-px bg-[var(--color-border)]" />
          {data.map((d) => (
            <TimelineEvent
              key={d.id}
              decision={d}
              onClick={() => setSelected(d)}
              selected={selected?.id === d.id}
            />
          ))}
        </div>
      </div>

      {/* Decision list */}
      <div className="flex flex-col gap-2 flex-1 overflow-auto">
        {data.map((d) => (
          <div
            key={d.id}
            onClick={() => setSelected(d)}
            className={cn(
              'rounded-lg border p-4 cursor-pointer transition-colors',
              selected?.id === d.id
                ? 'border-[var(--color-primary)] bg-[var(--color-surface)]'
                : 'border-[var(--color-border)] bg-[var(--color-surface)] hover:bg-[var(--color-surface-elevated)]',
            )}
          >
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ background: typeColor(d.decision_type) }}
                />
                <span className="text-sm font-medium text-[var(--color-text)]">{typeLabel(d.decision_type)}</span>
                <span className="text-xs text-[var(--color-text-secondary)]">{formatIST(d.as_of)}</span>
              </div>
              <div className="flex items-center gap-2 text-xs text-[var(--color-text-secondary)]">
                <span className="text-[var(--color-profit)]">{d.actions_executed} executed</span>
                <span>·</span>
                <span className="text-[var(--color-text-muted)]">{d.actions_skipped} skipped</span>
              </div>
            </div>
            {d.llm_reasoning && (
              <p className="text-xs text-[var(--color-text-secondary)] line-clamp-2 leading-relaxed">
                {d.llm_reasoning}
              </p>
            )}
          </div>
        ))}
      </div>

      {/* Detail drawer */}
      <Drawer
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        title={selected ? typeLabel(selected.decision_type) : ''}
        width="w-[560px]"
      >
        {selected && (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-3">
              {[
                { label: 'As Of', value: formatIST(selected.as_of) },
                { label: 'Model', value: selected.llm_model ?? '—' },
                { label: 'Risk Profile', value: selected.risk_profile ?? '—' },
                { label: 'Budget', value: selected.budget_available != null ? `₹${selected.budget_available.toLocaleString()}` : '—' },
                { label: 'Executed', value: String(selected.actions_executed) },
                { label: 'Skipped', value: String(selected.actions_skipped) },
              ].map(({ label, value }) => (
                <div key={label}>
                  <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">{label}</div>
                  <div className="text-sm text-[var(--color-text)]">{value}</div>
                </div>
              ))}
            </div>
            {selected.llm_reasoning && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2">LLM Reasoning</div>
                <div className="rounded border border-[var(--color-border)] bg-[var(--color-surface-elevated)] p-3 text-xs leading-relaxed text-[var(--color-text-secondary)] whitespace-pre-wrap max-h-96 overflow-y-auto">
                  {selected.llm_reasoning}
                </div>
              </div>
            )}
          </div>
        )}
      </Drawer>
    </div>
  );
}
