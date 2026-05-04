import React from 'react';
import { cn } from '../../lib/cn';

type Variant = 'default' | 'success' | 'danger' | 'warning' | 'info' | 'muted';

const variants: Record<Variant, string> = {
  default: 'bg-[var(--color-surface-elevated)] text-[var(--color-text-secondary)]',
  success: 'bg-[var(--color-profit-light)] text-[var(--color-profit)]',
  danger: 'bg-[var(--color-loss-light)] text-[var(--color-loss)]',
  warning: 'bg-orange-900/30 text-orange-400',
  info: 'bg-blue-900/30 text-[var(--color-accent)]',
  muted: 'bg-[var(--color-surface-elevated)] text-[var(--color-text-muted)]',
};

interface BadgeProps {
  variant?: Variant;
  children: React.ReactNode;
  className?: string;
}

export function Badge({ variant = 'default', children, className }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium',
        variants[variant],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function actionBadgeVariant(action: string): Variant {
  if (action === 'BUY') return 'success';
  if (action === 'SELL') return 'danger';
  if (action === 'HOLD') return 'warning';
  return 'info';
}

export function statusBadgeVariant(status: string): Variant {
  if (status === 'active') return 'success';
  if (status === 'resolved_hit') return 'success';
  if (status === 'resolved_miss') return 'danger';
  if (status === 'expired') return 'muted';
  if (status === 'open') return 'info';
  if (status === 'error') return 'danger';
  if (status === 'ok') return 'success';
  if (status === 'degraded') return 'warning';
  return 'default';
}
