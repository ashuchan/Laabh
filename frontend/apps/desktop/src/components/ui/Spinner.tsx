import React from 'react';
import { cn } from '../../lib/cn';

export function Spinner({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        'h-4 w-4 animate-spin rounded-full border-2 border-[var(--color-border)] border-t-[var(--color-primary)]',
        className,
      )}
    />
  );
}

export function PageLoader() {
  return (
    <div className="flex h-full items-center justify-center">
      <Spinner className="h-6 w-6" />
    </div>
  );
}

export function ErrorState({ message }: { message?: string }) {
  return (
    <div className="flex h-full items-center justify-center text-[var(--color-loss)] text-sm">
      {message ?? 'Failed to load data'}
    </div>
  );
}

export function EmptyState({ message }: { message?: string }) {
  return (
    <div className="flex h-full items-center justify-center text-[var(--color-text-muted)] text-sm">
      {message ?? 'No data'}
    </div>
  );
}
