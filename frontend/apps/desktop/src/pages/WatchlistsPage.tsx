import React, { useState } from 'react';
import { useWatchlists, useWatchlistItems } from '@laabh/shared';
import { formatINR } from '@laabh/shared';
import { PageLoader, ErrorState, EmptyState } from '../components/ui/Spinner';
import { Badge } from '../components/ui/Badge';
import { cn } from '../lib/cn';

export function WatchlistsPage() {
  const { data: watchlists, isLoading, isError } = useWatchlists();
  const [activeId, setActiveId] = useState<string | null>(null);

  const defaultId = watchlists?.find((w) => w.is_default)?.id;
  const selectedId = activeId ?? defaultId ?? watchlists?.[0]?.id ?? '';

  const { data: items, isLoading: itemsLoading } = useWatchlistItems(selectedId);

  if (isLoading) return <PageLoader />;
  if (isError) return <ErrorState message="Failed to load watchlists" />;
  if (!watchlists?.length) return <EmptyState message="No watchlists" />;

  return (
    <div className="flex gap-4 h-full min-h-0">
      {/* Watchlist selector */}
      <div className="w-48 shrink-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-y-auto">
        {watchlists.map((wl) => (
          <button
            key={wl.id}
            onClick={() => setActiveId(wl.id)}
            className={cn(
              'w-full text-left px-3 py-2.5 text-xs border-b border-[var(--color-border)] transition-colors',
              selectedId === wl.id
                ? 'bg-[var(--color-surface-elevated)] text-[var(--color-text)]'
                : 'text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-elevated)]',
            )}
          >
            <div className="flex items-center justify-between">
              <span className="truncate font-medium">{wl.name}</span>
              {wl.is_default && <Badge variant="info">default</Badge>}
            </div>
            {wl.description && (
              <div className="mt-0.5 text-[10px] text-[var(--color-text-muted)] truncate">{wl.description}</div>
            )}
          </button>
        ))}
      </div>

      {/* Items */}
      <div className="flex-1 min-w-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]">
        {itemsLoading ? (
          <PageLoader />
        ) : !items?.length ? (
          <EmptyState message="No items in this watchlist" />
        ) : (
          <div className="overflow-auto h-full">
            <table className="w-full border-collapse text-xs">
              <thead className="sticky top-0 z-10">
                <tr>
                  {['Instrument', 'Alert Above', 'Alert Below', 'Buy Target', 'Sell Target', 'Signal Alerts', 'Notes'].map((h) => (
                    <th key={h} className="border-b border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-secondary)]">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id} className="border-b border-[var(--color-border)] hover:bg-[var(--color-surface-elevated)]">
                    <td className="px-3 py-2 font-medium text-[var(--color-text)]">{item.instrument_id}</td>
                    <td className="px-3 py-2 text-[var(--color-text-secondary)]">{item.price_alert_above != null ? formatINR(item.price_alert_above) : '—'}</td>
                    <td className="px-3 py-2 text-[var(--color-text-secondary)]">{item.price_alert_below != null ? formatINR(item.price_alert_below) : '—'}</td>
                    <td className="px-3 py-2 text-[var(--color-profit)]">{item.target_buy_price != null ? formatINR(item.target_buy_price) : '—'}</td>
                    <td className="px-3 py-2 text-[var(--color-loss)]">{item.target_sell_price != null ? formatINR(item.target_sell_price) : '—'}</td>
                    <td className="px-3 py-2">
                      <Badge variant={item.alert_on_signals ? 'success' : 'muted'}>
                        {item.alert_on_signals ? 'on' : 'off'}
                      </Badge>
                    </td>
                    <td className="px-3 py-2 text-[var(--color-text-secondary)] max-w-xs truncate">{item.notes ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
