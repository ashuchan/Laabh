import React, { useState, useMemo } from 'react';
import { useSearch, useNavigate } from '@tanstack/react-router';
import { useFNOCandidates, useFNOBanList } from '@laabh/shared';
import { formatScore, toNumber } from '@laabh/shared';
import { Filter, X } from 'lucide-react';
import type { ColumnDef } from '@tanstack/react-table';
import type { FNOCandidate } from '@laabh/shared';
import { DataTable } from '../../components/tables/DataTable';
import { Badge, statusBadgeVariant } from '../../components/ui/Badge';
import { Drawer } from '../../components/ui/Drawer';
import { PageLoader, ErrorState } from '../../components/ui/Spinner';
import { cn } from '../../lib/cn';

const IV_REGIMES = ['low', 'neutral', 'high'];
const OI_STRUCTURES = ['bullish', 'bearish', 'neutral', 'ambiguous'];
const PHASES = [1, 2, 3, 4];

function ScoreBar({ value }: { value: number | string | null }) {
  const n = toNumber(value);
  if (n === null) return <span className="text-[var(--color-text-muted)]">—</span>;
  const pct = Math.min(Math.max(n, 0), 1) * 100;
  const color = n >= 0.7 ? 'var(--color-profit)' : n >= 0.4 ? 'var(--color-hold)' : 'var(--color-loss)';
  return (
    <div className="flex items-center gap-1.5">
      <div className="h-1 w-16 rounded-full bg-[var(--color-border)]">
        <div className="h-full rounded-full" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span className="text-[11px]" style={{ color }}>{formatScore(n)}</span>
    </div>
  );
}

const columns: ColumnDef<FNOCandidate, unknown>[] = [
  { accessorKey: 'symbol', header: 'Symbol', size: 90 },
  {
    accessorKey: 'phase',
    header: 'Phase',
    cell: ({ getValue }) => <Badge variant="info">P{getValue() as number}</Badge>,
    size: 60,
  },
  {
    accessorKey: 'passed_liquidity',
    header: 'Liq',
    cell: ({ getValue }) => {
      const v = getValue();
      return v === null ? '—' : v ? <span className="text-[var(--color-profit)]">✓</span> : <span className="text-[var(--color-loss)]">✗</span>;
    },
    size: 40,
  },
  {
    accessorKey: 'composite_score',
    header: 'Score',
    cell: ({ getValue }) => <ScoreBar value={getValue() as number | string | null} />,
    sortingFn: (a, b, id) => {
      const av = toNumber(a.getValue(id)) ?? -1;
      const bv = toNumber(b.getValue(id)) ?? -1;
      return av - bv;
    },
  },
  {
    accessorKey: 'news_score',
    header: 'News',
    cell: ({ getValue }) => <ScoreBar value={getValue() as number | string | null} />,
    sortingFn: (a, b, id) => (toNumber(a.getValue(id)) ?? -1) - (toNumber(b.getValue(id)) ?? -1),
  },
  {
    accessorKey: 'convergence_score',
    header: 'Conv',
    cell: ({ getValue }) => <ScoreBar value={getValue() as number | string | null} />,
    sortingFn: (a, b, id) => (toNumber(a.getValue(id)) ?? -1) - (toNumber(b.getValue(id)) ?? -1),
  },
  {
    accessorKey: 'iv_regime',
    header: 'IV',
    cell: ({ getValue }) => {
      const v = getValue() as string | null;
      if (!v) return '—';
      const variant = v === 'low' ? 'success' : v === 'high' ? 'danger' : 'default';
      return <Badge variant={variant}>{v}</Badge>;
    },
  },
  {
    accessorKey: 'oi_structure',
    header: 'OI',
    cell: ({ getValue }) => {
      const v = getValue() as string | null;
      if (!v) return '—';
      const variant = v === 'bullish' ? 'success' : v === 'bearish' ? 'danger' : 'default';
      return <Badge variant={variant}>{v}</Badge>;
    },
  },
  {
    accessorKey: 'llm_decision',
    header: 'LLM',
    cell: ({ getValue }) => {
      const v = getValue() as string | null;
      if (!v) return <span className="text-[var(--color-text-muted)]">—</span>;
      const short = v.length > 30 ? v.slice(0, 30) + '…' : v;
      return <span className="text-[var(--color-text-secondary)] text-[11px]">{short}</span>;
    },
  },
];

export function FNOCandidatesPage() {
  const search = useSearch({ from: '/reports/fno-candidates' });
  const navigate = useNavigate({ from: '/reports/fno-candidates' });
  const [selected, setSelected] = useState<FNOCandidate | null>(null);

  const { data: banList } = useFNOBanList();
  const banSet = useMemo(() => new Set((banList ?? []).map((b) => b.symbol)), [banList]);

  const opts = {
    phase: search.phase,
    passed_only: search.passed === 'true',
    limit: 500,
  };

  const { data, isLoading, isError } = useFNOCandidates(opts);

  const filtered = useMemo(() => {
    if (!data) return [];
    return data.filter((c) => {
      if (search.iv_regime && c.iv_regime !== search.iv_regime) return false;
      if (search.oi_structure && c.oi_structure !== search.oi_structure) return false;
      return true;
    });
  }, [data, search.iv_regime, search.oi_structure]);

  function setFilter(key: string, value: string | number | undefined) {
    navigate({ search: (prev) => ({ ...prev, [key]: value }) });
  }

  if (isLoading) return <PageLoader />;
  if (isError) return <ErrorState message="Failed to load F&O candidates" />;

  return (
    <div className="flex gap-4 h-full min-h-0">
      {/* Filter sidebar */}
      <div className="w-52 shrink-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4 overflow-y-auto">
        <div className="flex items-center gap-2 mb-4">
          <Filter size={13} className="text-[var(--color-text-secondary)]" />
          <span className="text-xs font-semibold uppercase tracking-wider text-[var(--color-text-secondary)]">Filters</span>
          <button
            onClick={() => navigate({ search: {} })}
            className="ml-auto text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)]"
            title="Clear filters"
          >
            <X size={12} />
          </button>
        </div>

        <div className="flex flex-col gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2">Phase</div>
            <div className="flex flex-wrap gap-1">
              {PHASES.map((p) => (
                <button
                  key={p}
                  onClick={() => setFilter('phase', search.phase === p ? undefined : p)}
                  className={cn(
                    'rounded px-2 py-0.5 text-[11px] border transition-colors',
                    search.phase === p
                      ? 'border-[var(--color-primary)] text-[var(--color-primary)] bg-[var(--color-primary)]/10'
                      : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:border-[var(--color-text-muted)]',
                  )}
                >
                  {p}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={search.passed === 'true'}
                onChange={(e) => setFilter('passed', e.target.checked ? 'true' : undefined)}
                className="accent-[var(--color-primary)]"
              />
              <span className="text-xs text-[var(--color-text-secondary)]">Passed liquidity only</span>
            </label>
          </div>

          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2">IV Regime</div>
            <div className="flex flex-col gap-1">
              {IV_REGIMES.map((r) => (
                <button
                  key={r}
                  onClick={() => setFilter('iv_regime', search.iv_regime === r ? undefined : r)}
                  className={cn(
                    'rounded px-2 py-1 text-left text-[11px] border transition-colors',
                    search.iv_regime === r
                      ? 'border-[var(--color-primary)] text-[var(--color-primary)] bg-[var(--color-primary)]/10'
                      : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:border-[var(--color-text-muted)]',
                  )}
                >
                  {r}
                </button>
              ))}
            </div>
          </div>

          <div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2">OI Structure</div>
            <div className="flex flex-col gap-1">
              {OI_STRUCTURES.map((r) => (
                <button
                  key={r}
                  onClick={() => setFilter('oi_structure', search.oi_structure === r ? undefined : r)}
                  className={cn(
                    'rounded px-2 py-1 text-left text-[11px] border transition-colors',
                    search.oi_structure === r
                      ? 'border-[var(--color-primary)] text-[var(--color-primary)] bg-[var(--color-primary)]/10'
                      : 'border-[var(--color-border)] text-[var(--color-text-secondary)] hover:border-[var(--color-text-muted)]',
                  )}
                >
                  {r}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Ban list + badges */}
        <div className="mt-6 pt-4 border-t border-[var(--color-border)]">
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2">
            F&O Ban ({banSet.size})
          </div>
          <div className="flex flex-wrap gap-1">
            {[...banSet].slice(0, 10).map((s) => (
              <Badge key={s} variant="danger">{s}</Badge>
            ))}
            {banSet.size > 10 && <Badge variant="muted">+{banSet.size - 10}</Badge>}
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="flex-1 min-w-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] flex flex-col">
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-2">
          <span className="text-xs text-[var(--color-text-secondary)]">{filtered.length} candidates</span>
        </div>
        <div className="flex-1 overflow-auto">
          <DataTable
            data={filtered}
            columns={columns}
            onRowClick={setSelected}
            selectedRowId={selected?.id ?? null}
            getRowId={(r) => r.id}
            stickyHeader
          />
        </div>
      </div>

      {/* Detail drawer */}
      <Drawer open={Boolean(selected)} onClose={() => setSelected(null)} title={selected?.symbol ?? ''} width="w-[520px]">
        {selected && (
          <div className="flex flex-col gap-4">
            <div className="grid grid-cols-3 gap-3">
              {[
                { label: 'Phase', value: `Phase ${selected.phase}` },
                { label: 'IV Regime', value: selected.iv_regime ?? '—' },
                { label: 'OI Structure', value: selected.oi_structure ?? '—' },
                { label: 'Composite', value: formatScore(selected.composite_score) },
                { label: 'News Score', value: formatScore(selected.news_score) },
                { label: 'Convergence', value: formatScore(selected.convergence_score) },
                { label: 'Sentiment', value: formatScore(selected.sentiment_score) },
                { label: 'Macro Align', value: formatScore(selected.macro_align_score) },
                { label: 'FII/DII', value: formatScore(selected.fii_dii_score) },
              ].map(({ label, value }) => (
                <div key={label}>
                  <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">{label}</div>
                  <div className="text-sm text-[var(--color-text)]">{value}</div>
                </div>
              ))}
            </div>

            {selected.llm_thesis && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2">LLM Thesis</div>
                <div className="rounded border border-[var(--color-border)] bg-[var(--color-surface-elevated)] p-3 text-xs leading-relaxed text-[var(--color-text-secondary)] whitespace-pre-wrap">
                  {selected.llm_thesis}
                </div>
              </div>
            )}

            {selected.llm_decision && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2">LLM Decision</div>
                <div className="rounded border border-[var(--color-border)] bg-[var(--color-surface-elevated)] p-3 text-xs leading-relaxed text-[var(--color-text-secondary)] whitespace-pre-wrap">
                  {selected.llm_decision}
                </div>
              </div>
            )}
          </div>
        )}
      </Drawer>
    </div>
  );
}
