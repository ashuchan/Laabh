import React, { useState } from 'react';
import { Link, useRouterState } from '@tanstack/react-router';
import {
  BarChart2,
  TrendingUp,
  List,
  Star,
  Users,
  ChevronDown,
  ChevronRight,
  Activity,
  Calendar,
  Zap,
  GitBranch,
  Heart,
  Settings,
  PanelLeftClose,
  PanelLeftOpen,
} from 'lucide-react';
import { cn } from '../../lib/cn';

interface NavItem {
  label: string;
  to: string;
  icon: React.ReactNode;
  shortcut?: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
  defaultOpen?: boolean;
}

const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Main',
    defaultOpen: true,
    items: [
      { label: 'Portfolio', to: '/portfolio', icon: <BarChart2 size={15} /> },
      { label: 'Signals', to: '/signals', icon: <TrendingUp size={15} /> },
      { label: 'Watchlists', to: '/watchlists', icon: <Star size={15} /> },
      { label: 'Analysts', to: '/analysts', icon: <Users size={15} /> },
    ],
  },
  {
    label: 'Reports',
    defaultOpen: true,
    items: [
      { label: 'Daily', to: '/reports/daily', icon: <Calendar size={15} />, shortcut: 'g d' },
      { label: 'F&O Candidates', to: '/reports/fno-candidates', icon: <Zap size={15} />, shortcut: 'g f' },
      { label: 'Strategy', to: '/reports/strategy-decisions', icon: <GitBranch size={15} />, shortcut: 'g s' },
      { label: 'Signal Perf', to: '/reports/signal-performance', icon: <Activity size={15} />, shortcut: 'g p' },
      { label: 'System Health', to: '/reports/system-health', icon: <Heart size={15} />, shortcut: 'g h' },
    ],
  },
];

function NavLink({ item, collapsed }: { item: NavItem; collapsed: boolean }) {
  const { location } = useRouterState();
  const active = location.pathname === item.to || location.pathname.startsWith(item.to + '/');
  const titleAttr = collapsed
    ? item.shortcut ? `${item.label} (${item.shortcut})` : item.label
    : undefined;

  return (
    <Link
      to={item.to}
      title={titleAttr}
      className={cn(
        'flex items-center gap-2 rounded px-2 py-1.5 text-xs transition-colors',
        'hover:bg-[var(--color-surface-elevated)] hover:text-[var(--color-text)]',
        active
          ? 'bg-[var(--color-surface-elevated)] text-[var(--color-primary)] font-medium'
          : 'text-[var(--color-text-secondary)]',
        collapsed && 'justify-center px-0',
      )}
    >
      <span className="shrink-0">{item.icon}</span>
      {!collapsed && (
        <>
          <span className="truncate flex-1">{item.label}</span>
          {item.shortcut && (
            <kbd className="ml-auto shrink-0 rounded bg-[var(--color-border)] px-1 py-0.5 text-[9px] text-[var(--color-text-muted)] font-mono">
              {item.shortcut}
            </kbd>
          )}
        </>
      )}
    </Link>
  );
}

function NavGroupSection({
  group,
  collapsed,
}: {
  group: NavGroup;
  collapsed: boolean;
}) {
  const [open, setOpen] = useState(group.defaultOpen ?? true);

  return (
    <div className="mb-2">
      {!collapsed && (
        <button
          onClick={() => setOpen((o) => !o)}
          className="flex w-full items-center gap-1 px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)]"
        >
          {open ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
          {group.label}
        </button>
      )}
      {(open || collapsed) && (
        <div className="flex flex-col gap-0.5">
          {group.items.map((item) => (
            <NavLink key={item.to} item={item} collapsed={collapsed} />
          ))}
        </div>
      )}
    </div>
  );
}

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <aside
      className={cn(
        'flex h-full flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)] transition-all duration-200',
        collapsed ? 'w-14' : 'w-60',
      )}
    >
      {/* Logo */}
      <div className={cn('flex h-12 items-center border-b border-[var(--color-border)] px-3', collapsed && 'justify-center px-0')}>
        {collapsed ? (
          <span className="text-base font-bold text-[var(--color-primary)]">L</span>
        ) : (
          <span className="text-sm font-bold tracking-wide text-[var(--color-text)]">
            <span className="text-[var(--color-primary)]">Laabh</span>
          </span>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto p-2">
        {NAV_GROUPS.map((g) => (
          <NavGroupSection key={g.label} group={g} collapsed={collapsed} />
        ))}
      </nav>

      {/* Collapse toggle */}
      <div className={cn('border-t border-[var(--color-border)] p-2', collapsed && 'flex justify-center')}>
        {!collapsed && (
          <Link
            to="/settings"
            className="flex items-center gap-2 rounded px-2 py-1.5 text-xs text-[var(--color-text-secondary)] hover:bg-[var(--color-surface-elevated)] hover:text-[var(--color-text)]"
          >
            <Settings size={15} />
            Settings
          </Link>
        )}
        <button
          onClick={() => setCollapsed((c) => !c)}
          className="mt-1 flex w-full items-center justify-center gap-1 rounded px-2 py-1.5 text-xs text-[var(--color-text-muted)] hover:bg-[var(--color-surface-elevated)] hover:text-[var(--color-text-secondary)]"
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? <PanelLeftOpen size={14} /> : <PanelLeftClose size={14} />}
          {!collapsed && <span>Collapse</span>}
        </button>
      </div>
    </aside>
  );
}
