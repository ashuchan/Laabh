import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from '@tanstack/react-router';
import { useQueryClient } from '@tanstack/react-query';
import { Sidebar } from './Sidebar';
import { Topbar } from './Topbar';
import { CommandPalette } from '../ui/CommandPalette';

export function Shell({ children }: { children: React.ReactNode }) {
  const [cmdOpen, setCmdOpen] = useState(false);
  const navigate = useNavigate();
  const qc = useQueryClient();

  const navTo = useCallback(
    (to: string) => {
      void navigate({ to } as Parameters<typeof navigate>[0]);
    },
    [navigate],
  );

  useEffect(() => {
    let buffer = '';
    let bufferTimer: ReturnType<typeof setTimeout> | null = null;

    function handleKey(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement).tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

      // ⌘K / Ctrl+K — command palette
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setCmdOpen((o) => !o);
        return;
      }

      // r — refresh
      if (e.key === 'r' && !e.metaKey && !e.ctrlKey) {
        void qc.invalidateQueries();
        return;
      }

      // Escape — close command palette
      if (e.key === 'Escape') {
        setCmdOpen(false);
        return;
      }

      // vim-style "g <letter>" shortcuts
      buffer += e.key;
      if (bufferTimer) clearTimeout(bufferTimer);
      bufferTimer = setTimeout(() => { buffer = ''; }, 600);

      if (buffer === 'gd') { navTo('/reports/daily'); buffer = ''; }
      else if (buffer === 'gf') { navTo('/reports/fno-candidates'); buffer = ''; }
      else if (buffer === 'gs') { navTo('/reports/strategy-decisions'); buffer = ''; }
      else if (buffer === 'gp') { navTo('/reports/signal-performance'); buffer = ''; }
      else if (buffer === 'gh') { navTo('/reports/system-health'); buffer = ''; }
    }

    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [navTo, qc]);

  return (
    <div className="flex h-screen overflow-hidden bg-[var(--color-bg)]">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <Topbar onOpenCommandPalette={() => setCmdOpen(true)} />
        <main className="flex-1 overflow-auto p-4">{children}</main>
      </div>
      <CommandPalette open={cmdOpen} onClose={() => setCmdOpen(false)} />
    </div>
  );
}
