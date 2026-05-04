# Laabh Frontend

pnpm workspace containing the mobile app (Expo/React Native) and the new desktop app (Vite/React).

## Structure

```
frontend/
├── packages/
│   └── shared/          # @laabh/shared — types, formatters, query hooks, stores
├── apps/
│   ├── mobile/          # (move here eventually — currently at /mobile)
│   └── desktop/         # Vite + React + Tailwind + TanStack Router
└── tsconfig.base.json
```

## Quick Start

```bash
# Install all workspace deps
cd frontend
pnpm install

# Run desktop dev server (proxies API to localhost:8000)
pnpm dev:desktop

# Build desktop for production
pnpm build:desktop
```

The built desktop app is served by FastAPI at `http://localhost:8000/app`.

## Keyboard Shortcuts (Desktop)

| Key | Action |
|-----|--------|
| `g d` | → Daily Report |
| `g f` | → F&O Candidates |
| `g s` | → Strategy Decisions |
| `g p` | → Signal Performance |
| `g h` | → System Health |
| `r` | Refresh current page data |
| `[` / `]` | Previous / next day (date-stepped pages) |
| `⌘K` / `Ctrl+K` | Open command palette |
