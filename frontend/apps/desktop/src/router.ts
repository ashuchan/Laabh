import {
  createRootRoute,
  createRoute,
  createRouter,
  redirect,
  Outlet,
} from '@tanstack/react-router';
import React from 'react';
import { z } from 'zod';
import { Shell } from './components/layout/Shell';
import { PortfolioPage } from './pages/PortfolioPage';
import { SignalsPage } from './pages/SignalsPage';
import { WatchlistsPage } from './pages/WatchlistsPage';
import { AnalystsPage } from './pages/AnalystsPage';
import { DailyReportPage } from './pages/reports/DailyReportPage';
import { FNOCandidatesPage } from './pages/reports/FNOCandidatesPage';
import { StrategyDecisionsPage } from './pages/reports/StrategyDecisionsPage';
import { SignalPerformancePage } from './pages/reports/SignalPerformancePage';
import { SystemHealthPage } from './pages/reports/SystemHealthPage';

const rootRoute = createRootRoute({
  component: () =>
    React.createElement(Shell, null, React.createElement(Outlet)),
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  beforeLoad: () => { throw redirect({ to: '/portfolio' }); },
});

const portfolioRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/portfolio',
  component: PortfolioPage,
});

const signalsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/signals',
  component: SignalsPage,
});

const watchlistsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/watchlists',
  component: WatchlistsPage,
});

const analystsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/analysts',
  component: AnalystsPage,
});

const dailyReportRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/reports/daily',
  validateSearch: z.object({ date: z.string().optional() }).parse,
  component: DailyReportPage,
});

const fnoRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/reports/fno-candidates',
  validateSearch: z
    .object({
      phase: z.coerce.number().optional(),
      passed: z.string().optional(),
      iv_regime: z.string().optional(),
      oi_structure: z.string().optional(),
    })
    .parse,
  component: FNOCandidatesPage,
});

const strategyDecisionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/reports/strategy-decisions',
  validateSearch: z
    .object({ date: z.string().optional(), type: z.string().optional() })
    .parse,
  component: StrategyDecisionsPage,
});

const signalPerfRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/reports/signal-performance',
  validateSearch: z
    .object({ days: z.coerce.number().optional(), analyst_id: z.string().optional() })
    .parse,
  component: SignalPerformancePage,
});

const systemHealthRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/reports/system-health',
  validateSearch: z
    .object({ tier: z.coerce.number().optional(), degraded: z.coerce.number().optional() })
    .parse,
  component: SystemHealthPage,
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  portfolioRoute,
  signalsRoute,
  watchlistsRoute,
  analystsRoute,
  dailyReportRoute,
  fnoRoute,
  strategyDecisionsRoute,
  signalPerfRoute,
  systemHealthRoute,
]);

export const router = createRouter({ routeTree });

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
}
