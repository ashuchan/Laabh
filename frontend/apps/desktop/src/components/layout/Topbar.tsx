import React, { useMemo } from 'react';
import { useRouterState } from '@tanstack/react-router';
import { RefreshCw, Radio, Search } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { usePriceStore } from '@laabh/shared';
import { cn } from '../../lib/cn';

function MarketStatusPill() {
  const status = useMemo(() => {
    const now = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const day = now.getDay();
    if (day === 0 || day === 6) return { isOpen: false, label: 'Closed' };
    const mins = now.getHours() * 60 + now.getMinutes();
    if (mins >= 9 * 60 + 15 && mins < 15 * 60 + 30) {
      const left = 15 * 60 + 30 - mins;
      return { isOpen: true, label: `Open · ${Math.floor(left / 60)}h ${left % 60}m left` };
    }
    if (mins < 9 * 60 + 15) {
      const left = 9 * 60 + 15 - mins;
      return { isOpen: false, label: `Opens in ${left}m` };
    }
    return { isOpen: false, label: 'Closed' };
  }, []);

  return (
    <span
      className={cn(
        'flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium',
        status.isOpen
          ? 'bg-[var(--color-profit-light)] text-[var(--color-profit)]'
          : 'bg-[var(--color-surface-elevated)] text-[var(--color-text-secondary)]',
      )}
    >
      <span
        className={cn(
          'h-1.5 w-1.5 rounded-full',
          status.isOpen ? 'animate-pulse bg-[var(--color-profit)]' : 'bg-[var(--color-text-muted)]',
        )}
      />
      {status.label}
    </span>
  );
}

function Breadcrumbs() {
  const { location } = useRouterState();
  const parts = location.pathname.split('/').filter(Boolean);
  if (parts.length === 0) return null;

  return (
    <nav className="flex items-center gap-1 text-[11px] text-[var(--color-text-secondary)]">
      {parts.map((part, i) => (
        <React.Fragment key={i}>
          {i > 0 && <span className="text-[var(--color-text-muted)]">›</span>}
          <span className={i === parts.length - 1 ? 'text-[var(--color-text)]' : ''}>
            {part.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())}
          </span>
        </React.Fragment>
      ))}
    </nav>
  );
}

function LiveCounter() {
  const prices = usePriceStore((s) => s.prices);
  const wsStatus = usePriceStore((s) => s.wsStatus);
  const count = Object.keys(prices).length;

  return (
    <span
      className={cn(
        'flex items-center gap-1 text-[11px]',
        wsStatus === 'open' ? 'text-[var(--color-profit)]' : 'text-[var(--color-text-muted)]',
      )}
      title={`WebSocket: ${wsStatus}`}
    >
      <Radio size={11} />
      {count} live
    </span>
  );
}

interface TopbarProps {
  onOpenCommandPalette: () => void;
}

export function Topbar({ onOpenCommandPalette }: TopbarProps) {
  const qc = useQueryClient();

  return (
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-[var(--color-border)] bg-[var(--color-surface)] px-4 gap-4">
      <Breadcrumbs />

      <div className="flex items-center gap-3 ml-auto">
        <button
          onClick={onOpenCommandPalette}
          className="flex items-center gap-1.5 rounded border border-[var(--color-border)] bg-[var(--color-surface-elevated)] px-2 py-1 text-[11px] text-[var(--color-text-secondary)] hover:text-[var(--color-text)] transition-colors"
        >
          <Search size={11} />
          <span>Search</span>
          <kbd className="ml-1 rounded bg-[var(--color-border)] px-1 py-0.5 text-[10px]">⌘K</kbd>
        </button>

        <LiveCounter />
        <MarketStatusPill />

        <button
          onClick={() => qc.invalidateQueries()}
          title="Refresh all data (r)"
          className="flex items-center gap-1 rounded px-2 py-1 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-elevated)] hover:text-[var(--color-text)] transition-colors"
        >
          <RefreshCw size={13} />
          <span>Refresh</span>
        </button>
      </div>
    </header>
  );
}
