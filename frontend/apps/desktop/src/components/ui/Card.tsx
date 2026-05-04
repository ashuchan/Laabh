import React from 'react';
import { cn } from '../../lib/cn';

interface CardProps {
  children: React.ReactNode;
  className?: string;
  title?: string;
}

export function Card({ children, className, title }: CardProps) {
  return (
    <div className={cn('rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]', className)}>
      {title && (
        <div className="border-b border-[var(--color-border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wider text-[var(--color-text-secondary)]">
          {title}
        </div>
      )}
      <div className="p-4">{children}</div>
    </div>
  );
}

export function KPICard({
  label,
  value,
  sub,
  colorClass,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  colorClass?: string;
}) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="text-[11px] text-[var(--color-text-secondary)] mb-1 uppercase tracking-wider">{label}</div>
      <div className={cn('text-xl font-bold', colorClass ?? 'text-[var(--color-text)]')}>{value}</div>
      {sub && <div className="mt-1 text-[11px] text-[var(--color-text-muted)]">{sub}</div>}
    </div>
  );
}
