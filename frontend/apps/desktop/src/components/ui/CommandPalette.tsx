import React, { useState, useEffect, useRef } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { useNavigate } from '@tanstack/react-router';
import { Search, ChevronRight } from 'lucide-react';
import { cn } from '../../lib/cn';

const ROUTES = [
  { label: 'Portfolio', path: '/portfolio', description: 'Holdings and P&L overview' },
  { label: 'Signals', path: '/signals', description: 'Active trading signals' },
  { label: 'Watchlists', path: '/watchlists', description: 'Tracked instruments' },
  { label: 'Analysts', path: '/analysts', description: 'Analyst leaderboard' },
  { label: 'Daily Report', path: '/reports/daily', description: 'Daily pipeline summary' },
  { label: 'F&O Candidates', path: '/reports/fno-candidates', description: 'Options trade candidates' },
  { label: 'Strategy Decisions', path: '/reports/strategy-decisions', description: 'LLM allocation decisions' },
  { label: 'Signal Performance', path: '/reports/signal-performance', description: 'Hit rate and returns' },
  { label: 'System Health', path: '/reports/system-health', description: 'Source and tier health' },
];

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(0);
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);

  const filtered = ROUTES.filter(
    (r) =>
      r.label.toLowerCase().includes(query.toLowerCase()) ||
      r.description.toLowerCase().includes(query.toLowerCase()),
  );

  useEffect(() => {
    if (open) {
      setQuery('');
      setSelected(0);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  useEffect(() => {
    setSelected(0);
  }, [query]);

  function go(path: string) {
    void navigate({ to: path } as Parameters<typeof navigate>[0]);
    onClose();
  }

  function handleKey(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelected((s) => Math.min(s + 1, filtered.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelected((s) => Math.max(s - 1, 0));
    } else if (e.key === 'Enter' && filtered[selected]) {
      go(filtered[selected].path);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60 z-50" />
        <Dialog.Content
          className="fixed left-1/2 top-[20%] z-50 w-[520px] -translate-x-1/2 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] shadow-2xl overflow-hidden"
          onKeyDown={handleKey}
        >
          <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-4 py-3">
            <Search size={14} className="text-[var(--color-text-secondary)]" />
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search pages…"
              className="flex-1 bg-transparent text-sm text-[var(--color-text)] placeholder-[var(--color-text-muted)] outline-none"
            />
            <kbd className="rounded bg-[var(--color-surface-elevated)] px-1.5 py-0.5 text-[10px] text-[var(--color-text-muted)]">ESC</kbd>
          </div>

          <div className="max-h-80 overflow-y-auto py-1">
            {filtered.length === 0 ? (
              <p className="px-4 py-3 text-xs text-[var(--color-text-muted)]">No results</p>
            ) : (
              filtered.map((item, i) => (
                <button
                  key={item.path}
                  onClick={() => go(item.path)}
                  className={cn(
                    'flex w-full items-center justify-between px-4 py-2 text-left transition-colors',
                    i === selected
                      ? 'bg-[var(--color-surface-elevated)] text-[var(--color-text)]'
                      : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-elevated)]',
                  )}
                >
                  <div>
                    <div className="text-sm">{item.label}</div>
                    <div className="text-[11px] text-[var(--color-text-muted)]">{item.description}</div>
                  </div>
                  <ChevronRight size={12} className="text-[var(--color-text-muted)]" />
                </button>
              ))
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
