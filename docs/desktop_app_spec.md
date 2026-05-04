# Laabh Desktop App — Build Spec

_Last updated: 2026-05-05_

## 1. Why a separate desktop app

The mobile app (`/mobile`) is built with Expo + React Native and tuned for finger-tap UX on a phone. The reports/diagnostics surface (Daily Report, F&O Candidates, Strategy Decisions, Signal Performance, System Health) is **information-dense by nature** — long tables, multi-pane layouts, side-by-side LLM reasoning, and infrequent but precise inputs. A laptop in front of the trading desk is the right form factor for that workflow; a 6-inch phone screen is not.

`expo start --web` is technically possible but inherits mobile constraints (small touch targets, bottom tabs that waste horizontal space, broken charts via `react-native-wagmi-charts` on web, `Alert.alert` mapping to `window.confirm`). Rather than fight the framework, build a desktop-native frontend that **shares the data layer** with mobile and **diverges on layout/interactions**.

## 2. Goals & non-goals

### Goals
- **Desktop-first UX** — sidebar navigation, dense tables, hover states, keyboard shortcuts, multi-column layouts that exploit ≥1280px width.
- **Reuse the FastAPI backend unchanged** — same `/portfolio`, `/signals`, `/reports/*`, `/fno/*`, `/trades/*`, `/ws/prices` endpoints.
- **Share code between mobile & desktop where it pays** — TypeScript types, query hooks, formatters, possibly Zustand stores. UI components do not transfer.
- **Single-user, LAN-only** — same threat model as today; no auth, no multi-tenant concerns.
- **Fast cold start** — Vite dev server <1s reload; production bundle served as static files (no SSR).

### Non-goals
- **Not a public web app.** Stays inside the home network. No SEO, no analytics, no CDN.
- **Not Electron.** A browser tab on the trading laptop is sufficient; packaging as a native binary adds maintenance cost without functional benefit. Revisit only if push-style notifications or system tray integration become necessary.
- **Not feature parity day-one.** Trade execution can stay mobile-only initially; the desktop app is reports-and-monitoring first.

## 3. Tech stack

| Concern | Choice | Rationale |
|---|---|---|
| Bundler | **Vite 5** | Fastest DX, zero-config TS, native ES modules, well-supported React plugin. |
| Language | **TypeScript 5** | Same as mobile; lets us share the query layer. |
| UI framework | **React 18** | Same mental model as mobile; lets engineers context-switch cheaply. |
| Styling | **Tailwind CSS v4 + CSS variables** | Utility-first matches the dense-table use case; CSS vars expose the existing `colors.ts` palette to Tailwind tokens. |
| Component primitives | **Radix UI Primitives** (Dialog, Tabs, Select, Tooltip, Popover, Toast) | Unstyled, accessible, keyboard-first. Pair with Tailwind for visuals. |
| Tables | **TanStack Table v8** | The single most-used pattern in this app (tier coverage, signal performance, F&O candidates, strategy decisions, holdings). Sortable, virtualized, column-pinning. |
| Charts | **Recharts** (or **uPlot** if perf is tight) | Recharts is React-native and handles the portfolio time-series + VIX + chain-success rate visualisations we need. uPlot is the escape hatch if we ever stream tick data. |
| Data fetching | **@tanstack/react-query v5** | Same library mobile uses. Hooks transfer with at most an import-path change. |
| Routing | **TanStack Router** (or React Router 7) | TanStack Router has first-class TS, search-param parsing, and pairs with TanStack Query. RR7 is the safer pick if the team is unfamiliar — choose one before kickoff, do not mix. |
| State | **Zustand** | Already in mobile (`priceStore`, `settingsStore`); reuse same package. |
| Icons | **Lucide React** | Replaces emoji icons used in mobile; works on desktop where emoji rendering is inconsistent. |
| WebSocket | Native `WebSocket` API | Same `/ws/prices` server endpoint; thin wrapper hook. |
| Date utils | **date-fns** | Already a transitive dep; smaller than dayjs for tree-shake. |
| Forms | **React Hook Form + Zod** | Only needed if we add the trade ticket later; defer until then. |

**Versions are floors, not pins.** Lock at scaffold time and bump intentionally.

## 4. Repo layout

Convert the current single-package layout into a workspace. Pick **pnpm workspaces** (lighter than Nx/Turbo, no daemons, plays nicely with Expo).

```
laabh/                                  # Python backend stays at root
├── src/                                # FastAPI, collectors, models — unchanged
├── database/
├── ...
└── frontend/                           # NEW — pnpm workspace root for JS
    ├── package.json                    # workspace manifest
    ├── pnpm-workspace.yaml
    ├── tsconfig.base.json
    ├── packages/
    │   └── shared/                     # @laabh/shared
    │       ├── src/
    │       │   ├── api/
    │       │   │   ├── client.ts       # axios factory (takes baseURL)
    │       │   │   ├── queries/        # ↓ moved from mobile/src/api/queries
    │       │   │   │   ├── portfolio.ts
    │       │   │   │   ├── signals.ts
    │       │   │   │   ├── reports.ts
    │       │   │   │   ├── fno.ts
    │       │   │   │   └── ...
    │       │   │   └── mutations/
    │       │   ├── types/              # response/request interfaces
    │       │   ├── formatters/         # formatINR, formatPct, timeAgo, ...
    │       │   └── stores/             # priceStore (logic only — no RN deps)
    │       ├── package.json
    │       └── tsconfig.json
    ├── apps/
    │   ├── mobile/                     # ← current /mobile dir moves here
    │   │   ├── package.json            # depends on @laabh/shared
    │   │   ├── App.tsx
    │   │   └── src/
    │   │       ├── api/                # 3-line re-exports from @laabh/shared
    │   │       ├── components/         # RN-only UI
    │   │       ├── screens/
    │   │       └── ...
    │   └── desktop/                    # NEW
    │       ├── index.html
    │       ├── vite.config.ts
    │       ├── package.json
    │       ├── tailwind.config.ts
    │       ├── postcss.config.js
    │       └── src/
    │           ├── main.tsx            # Vite entry
    │           ├── app.tsx             # router + providers
    │           ├── routes/             # TanStack Router file-based or table
    │           │   ├── __root.tsx
    │           │   ├── index.tsx       # → home dashboard
    │           │   ├── reports/
    │           │   │   ├── daily.tsx
    │           │   │   ├── fno-candidates.tsx
    │           │   │   ├── strategy-decisions.tsx
    │           │   │   ├── signal-performance.tsx
    │           │   │   └── system-health.tsx
    │           │   ├── portfolio.tsx
    │           │   ├── signals.tsx
    │           │   ├── watchlists.tsx
    │           │   └── analysts.tsx
    │           ├── components/
    │           │   ├── layout/
    │           │   │   ├── Sidebar.tsx
    │           │   │   ├── Topbar.tsx
    │           │   │   └── Shell.tsx
    │           │   ├── ui/             # Radix-wrapped primitives
    │           │   ├── charts/         # PortfolioChart, VIXSparkline, ...
    │           │   └── tables/         # ReportTable wrapper around TanStack
    │           ├── hooks/
    │           ├── lib/
    │           │   └── client.ts       # axios instance with desktop baseURL
    │           └── styles/
    │               └── globals.css     # Tailwind + CSS vars from colors.ts
    └── README.md
```

Files that **stay where they are** under `apps/mobile/src/`: `components/`, `screens/`, `navigation/`, `hooks/`. They depend on RN.

Files that **move to `packages/shared/src/`**: everything in `mobile/src/api/`, `mobile/src/utils/formatters.ts`, the platform-agnostic parts of `mobile/src/utils/constants.ts` (the `STALE_TIMES` and market-hours constants — but **not** `BACKEND_URL`, which differs per app).

`colors.ts` is interesting: shared semantically (profit=green, loss=red) but mobile uses RN style objects, desktop uses Tailwind/CSS-vars. Extract a `palette.ts` of raw hex → both apps consume; each app adapts to its styling system.

## 5. Layout & navigation

### Shell
A two-column fixed layout: collapsible sidebar (240px expanded, 56px collapsed icon-only) + main content. Top bar inside the main pane shows breadcrumbs, market status pill, refresh-all button, and a "live" tick counter (number of symbols streaming).

```
┌──────────┬─────────────────────────────────────────────┐
│          │  Reports › Daily              ⏵ Market open │
│ ▾ Home   ├─────────────────────────────────────────────┤
│   Signal │                                             │
│   Portfo │                  page content               │
│   Watchl │                                             │
│ ▾ Report │                                             │
│   Daily  │                                             │
│   F&O    │                                             │
│   Strat  │                                             │
│   Signal │                                             │
│   Health │                                             │
│ ▾ Tools  │                                             │
│   Trade  │                                             │
│   Analys │                                             │
│ ─────    │                                             │
│   Setngs │                                             │
└──────────┴─────────────────────────────────────────────┘
```

### Routing
URL is the source of truth. Examples:
- `/reports/daily?date=2026-04-30`
- `/reports/fno-candidates?phase=3&passed=true`
- `/reports/system-health?tier=1&degraded=1`
- `/reports/signal-performance?days=90`
- `/reports/strategy-decisions?date=2026-04-30&type=morning_allocation`

This makes filters bookmarkable, shareable via Slack, and reproducible from the URL bar — three things the mobile app cannot do.

### Keyboard shortcuts
Minimum set:
- `g d` → Daily Report, `g f` → F&O, `g s` → Strategy, `g p` → Signal Perf, `g h` → System Health (vim-style "go to" prefix)
- `r` → refetch current page
- `[` / `]` → previous / next day on date-stepped pages
- `cmd/ctrl+k` → command palette (route jump + symbol search)

## 6. Screen-by-screen redesign

The mobile screens are organized by section because phones force vertical scrolls. On desktop, redesign for **multi-pane density**.

### Daily Report (`/reports/daily`)
**Mobile:** vertical scroll, one section per card, 3-stat row max.

**Desktop:** 3-column grid above the fold:
- **Left col (320px):** date stepper, calendar pop-over, "today" button. Surprises panel pinned here so it's always visible regardless of scroll.
- **Middle col (flex):** Pipeline timeline (10 jobs as a ribbon — green/yellow/red dots with last_run timestamp on hover), Trading P&L card with sparkline, Decision Quality table (full width, sortable by P&L).
- **Right col (320px):** Chain ingestion donut, LLM cost gauge, VIX mini-chart, source-health stack.

Decision Quality table on desktop shows the **full LLM thesis** in a side drawer when a row is clicked — no truncation.

### F&O Candidates (`/reports/fno-candidates`)
**Master-detail layout.** Left: filter sidebar (phase, passed-only, IV regime multi-select, OI-structure multi-select, score range slider). Center: TanStack Table with sortable columns for every score, virtualized (we may have 500 rows). Right: detail panel that slides in when a row is selected, showing the full score grid, LLM thesis, and LLM decision.

VIX + ban-list move to top bar as small badges, always visible.

### Strategy Decisions (`/reports/strategy-decisions`)
**Timeline view.** A vertical timeline anchored to the trading day (9:15–15:30 IST), with decision events plotted at their `as_of` time. Click an event → expanded card with full LLM reasoning, executed/skipped breakdown, and a **table of trades** that resulted from that decision (joined via `portfolio_id` + time range). The mobile screen had no trade-attribution; desktop adds it.

### Signal Performance (`/reports/signal-performance`)
**Two-pane.** Top pane: KPI cards (hit rate, avg P&L, total resolved) + a **rolling hit-rate line chart** (28-day window) so the trend is visible. Bottom pane: full TanStack Table of recent signals — sortable by date/symbol/conv/outcome, filterable by analyst (autocomplete), action, status. Row click opens drawer with the original `reasoning` and a price-action chart from `signal_date` → `outcome_date`.

### System Health (`/reports/system-health`)
**Three rows, no scroll for the common case:**
1. Source health: 3-card row (NSE / Dhan / Angel One) with status pill, time-since-last-success, last-error in a `<details>` expander.
2. Tier coverage: a heatmap (rows = symbols, columns = last 60 minutes in 5-min buckets, color = success rate) — far more glanceable than the mobile list.
3. Chain issues: TanStack Table, columns = source / type / detected / message / GitHub / actions. "Resolve" is a button per row; bulk-resolve via row selection + toolbar.

The heatmap is the desktop-only innovation here — would not fit on a phone screen.

## 7. Shared layer — concrete contract

`packages/shared` exports:

```ts
// @laabh/shared
export * from './types';                  // Portfolio, Signal, FNOCandidate, ...
export * from './formatters';             // formatINR, formatPct, formatIST, ...
export { createApiClient } from './api/client';

// hooks accept the axios client as the first arg OR read from a Provider
export {
  useDailyReport, useTierCoverage, useSourceHealth, useChainIssues,
  useStrategyDecisions, useSignalPerformance,
} from './api/queries/reports';
export {
  usePortfolio, useHoldings, usePortfolioHistory,
} from './api/queries/portfolio';
// ... etc
```

**Critical refactor:** the current mobile queries import a singleton axios client. To make them shareable, switch to a **provider pattern**:

```ts
// packages/shared/src/api/ApiClientProvider.tsx
const Ctx = createContext<AxiosInstance | null>(null);
export const ApiClientProvider = ({ client, children }) => (
  <Ctx.Provider value={client}>{children}</Ctx.Provider>
);
export const useApiClient = () => {
  const c = useContext(Ctx);
  if (!c) throw new Error('ApiClientProvider missing');
  return c;
};
```

Each query rewrites:
```ts
// before
queryFn: () => client.get('/reports/daily').then(r => r.data)
// after
queryFn: () => api.get('/reports/daily').then(r => r.data)   // api from useApiClient()
```

This is a one-time refactor of ~10 query files. Mobile and desktop each create their own axios instance with the right baseURL and pass it to the provider.

## 8. WebSocket / live prices

The desktop `useLivePrice` hook is functionally identical to mobile but lives in `apps/desktop` because it uses browser `WebSocket` directly (mobile uses RN `WebSocket`, which has different reconnect semantics). The `priceStore` (Zustand) **logic** is shared via `@laabh/shared/stores/priceStore` — the store accepts a `wsFactory: (url: string) => WebSocket` so each platform plugs in its own.

## 9. Backend changes

**None required for the MVP.** Every endpoint the desktop needs already exists. CORS is already wide open in `src/api/app.py` (`allow_origins=["*"]`), which is fine for LAN-only.

**Potential additions if/when desktop grows:**
- `GET /reports/signal-performance/timeseries?bucket=day` — for the rolling hit-rate chart.
- `GET /reports/tier-coverage/heatmap?since=&buckets=12` — pre-aggregated for the heatmap.
- `POST /reports/chain-issues/bulk-resolve` — for the bulk-action use case.

Build these only when the screens demand them; do not pre-empt.

## 10. Build & deploy

### Dev
```
pnpm --filter @laabh/desktop dev          # Vite dev server on :5173
uvicorn src.api.app:app --reload          # backend on :8000
```
Vite proxies `/portfolio`, `/signals`, `/reports`, `/fno`, `/trades`, `/ws` to `:8000`. No CORS gymnastics in dev.

### Prod (LAN)
```
pnpm --filter @laabh/desktop build
```
Outputs `apps/desktop/dist/` (~200KB gz). Two deployment options:
1. **Serve from FastAPI:** mount `StaticFiles` at `/app` in `src/api/app.py`. Single process, single port. Recommended.
2. **Serve via nginx/caddy:** standalone, proxies API to FastAPI. Useful only if multiple frontends or TLS.

The trading laptop opens `http://laabh.local:8000/app` (or `localhost:8000/app`) in Chrome and pins the tab.

## 11. Testing strategy

- **Shared package:** Vitest + react-testing-library. Test the formatter functions and the query hooks (with MSW mocking the FastAPI endpoints). Currently the mobile app has zero JS tests; this is a green-field.
- **Desktop app:** Playwright for end-to-end. One smoke test per route ("loads without error and renders one row of expected content"). Run against a backend pointed at the test DB.
- **No Storybook initially.** Components grow organically and rarely need isolated previews; revisit after 6 months if the table-component count exceeds ~10.

## 12. Phasing

**Phase 0 — Prep (1 day)**
- Create `frontend/` workspace, move `mobile/` into `apps/mobile/`, scaffold `packages/shared/`.
- Refactor mobile to consume `@laabh/shared` (queries + formatters + types). Verify mobile still builds and runs.
- Verify the existing FNO/reports endpoints behave identically — no regression.

**Phase 1 — Desktop shell (1 day)**
- Vite + Tailwind + Radix + TanStack Router scaffold.
- Sidebar + topbar layout. Stub routes for every screen.
- API client wired to backend, react-query devtools enabled.

**Phase 2 — Reports screens (2-3 days)**
- Daily Report (3-col grid).
- F&O Candidates (master-detail with table).
- Strategy Decisions (timeline).
- Signal Performance (KPIs + table).
- System Health (heatmap + table).

**Phase 3 — Polish (1 day)**
- Keyboard shortcuts, command palette, dark mode toggle (default dark to match mobile).
- Pin the tab on the trading laptop, hook into FastAPI static-files mount, smoke-test for a week.

**Phase 4 — Optional, on-demand**
- Trade ticket on desktop (currently mobile-only is fine).
- Live price ticker in topbar (depends on whether desktop needs to be left open all day).
- Custom backend aggregation endpoints if any screen feels slow.

**Total to MVP: 5-7 working days.**

## 13. Open questions

1. **TanStack Router vs React Router 7?** Decide before scaffold. TanStack is technically nicer; RR7 is more common knowledge. No wrong answer; pick once.
2. **Shared Zustand stores or per-app?** `priceStore` is the only one with platform-coupled bits (the WS implementation). `settingsStore` is pure logic — could be shared, but probably easier to duplicate (~30 lines).
3. **Single bundle vs route-split?** With ~10 routes and 200KB total, code-splitting is premature. Defer.
4. **Auth before this ships?** Currently the FastAPI is open on the LAN. If the desktop app makes the surface area larger (e.g. a guest on Wi-Fi can hit the trade endpoint), add a single bearer-token check now. Otherwise punt.
5. **What happens to `/mobile/`?** If the move to `apps/mobile/` is too disruptive mid-flight, leave it where it is and have the desktop app reach into `../mobile/src/api/` via a `tsconfig` path alias. Less clean, fewer breakages. Prefer the workspace move only if mobile is in a quiet patch.

## 14. Risks

- **Workspace migration breaks mobile.** Mitigation: do it in a feature branch, run `npx expo start` after every step. Keep the `mobile/` rename as the last commit so the diff stays reviewable.
- **TanStack Router learning curve.** Spend a half day on the docs before screen #2; do not learn it on the fly while building screens.
- **Tailwind color tokens drift from mobile.** Mitigation: codegen — `palette.ts` is the source, a build script emits both `colors.ts` (mobile) and `tailwind.config.ts` color extension (desktop). One-time tooling.
- **Recharts perf on 500-row F&O candidate table.** Mitigation: TanStack Table virtualization handles row count; Recharts is only on KPI cards. The heatmap may need a custom SVG/canvas renderer if it exceeds a few hundred cells — measure before optimizing.

## 15. Definition of done (Phase 1-3)

- All five reports screens render with real backend data on `localhost:8000`.
- Pull-to-refresh equivalents (toolbar refresh button + auto-refetch on focus) work on every screen.
- URL filters round-trip — refreshing the page preserves state.
- Mobile app still builds and runs after the workspace migration.
- Backend `/reports/*` endpoints have at least one Playwright smoke test exercised against them.
- The trading laptop can open `http://localhost:8000/app` and use the Daily Report screen during a market session without DX complaints.
