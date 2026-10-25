# React port of the console — plan

Status: P0 done 2026-10-25. First step: the shell, Overview and Stock Research.

## Why

The console's data is real, but its interface is built by hand: about 2,000
`el(...)` calls assemble the DOM, 355 inline styles and 66 hex colours (53
distinct) sit in the JavaScript, and single page files reach 4,900 lines. The
port moves it to React components with one set of design tokens, typed and
validated API calls, and UI tests.

## Decisions (owner, 2026-10-25)

- **Side by side.** The React app is built from `web/` and served at `/app`.
  The current console stays at `/`, untouched, until enough pages are ported to
  switch.
- **Typed responses by hand.** None of the API's 220 operations declares a
  response schema in OpenAPI, so types cannot be generated. Each endpoint the
  app uses gets a TypeScript type and a Zod schema; responses are validated at
  the client boundary, and a mismatch is an explicit error, never a silent
  empty panel.
- **Design system copied in.** Tokens, the primitives needed and the chart
  theme hook come from the old `tl-research` workspace's `design-system`
  package into `web/src/ui`, as one package.
- **Design tokens only.** Colours live in `web/src/ui/tokens.css`, seeded from
  today's `ui/css/01-tokens.css` values plus the raw colours the CSS still uses
  (hover, active, selection). Charts read tokens through the theme hook. A test
  fails if a hex or `rgb()` colour appears anywhere else in `web/src`.

## SPEC amendment (approved by the owner 2026-10-25, applied in P0)

SPEC §2 locks the UI as a vanilla-JS SPA reusing the REFERENCE CSS verbatim
(D1a), and §3 locks the package tree. Proposed addition to §2:

> D1b (2026-10-25): a React + TypeScript console is built from `web/` and served
> at `/app`, page by page, alongside the vanilla console at `/`. It must meet
> §6 for every page it replaces. D1a (verbatim CSS) and D2 (no build step)
> apply to `ui/` only; `web/` has a Vite build and its own design tokens. When
> every page is ported, `/` switches to the React app and `ui/` is retired in a
> later decision.

§3 gains `web/` at the top level. §6 stays the acceptance contract.

## Scope of this step

| Area | Today | What the port must match (SPEC §6) |
| --- | --- | --- |
| Shell | `app.js`, `router.js`, `palette.js` | Topbar (palette search with ⌘K, universe selector, ENGINE health, ET clock), datastrip from `/api/status` over WS, rail navigation |
| Overview | `pages/overview.js`, 1,410 lines | Market strip, live ticker tape, watchlist with filter and live prices, panels, agent inbox merge |
| Stock Research | `pages/stock.js`, 1,259 lines | Any ticker loads; tabs Overview, Financials, Earnings & Estimates, Valuation & Peers, News & Sentiment, Alternative Data, Research; live price chart; real derived metrics; data freshness |

API used: `research` (company, price, news, estimates, altdata, valuation,
thesis, memory, financials, filings, resolve), `watchlists` (list, create),
`universes` (list, active, setActive, members, breadth), `macro`, `agents`
(inbox, findings), `lab` (hypotheses, backtests). WebSocket channels: `tape`,
`status`, `notifications`.

Rail items for pages not yet ported link to the old console (`/#agents`, …).

## Slices

Each slice ends green on its own checks and is committed alone.

### P0 — Scaffold and serving

- [x] `web/`: Vite, React 19, TypeScript (strict), TanStack Router (base `/app`)
  and Query, Zod, Vitest with Testing Library, ESLint.
- [x] Dev server proxies `/api` and `/ws` to the API on port 8200.
- [x] `tokens.css` and the no-raw-colour test.
- [x] Server: mount `web/dist` at `/app` when it exists, before the `/` mount;
  `TRADINGLIB_WEB_DIR` / `server.web_dir` like `ui_dir`. pytest for both
  present and absent.
- [x] CI: a Node job (install, lint, typecheck, test, build) next to the
  Python job.
- [x] SPEC amendment applied.

### P1 — Data layer

- [ ] Typed client keeping `api.js`'s rules: `ApiError` with status, URL and
  payload; `optional()` resolves null only on 404/501; reachability state for
  the ENGINE indicator.
- [ ] Zod schema and type per endpoint in scope; Query hooks per resource.
- [ ] WebSocket hook: subscribe/unsubscribe/listen, reconnect, visible degraded
  state.
- [ ] Tests mock `fetch` and the socket only; one test per endpoint rejects a
  malformed response.
- [ ] `npm run check:api`: validates every schema against a running local
  server (not run in CI).

### P2 — Shell

- [ ] Rail, topbar, datastrip, universe selector, ENGINE indicator, ET clock,
  command palette (pages and any ticker).
- [ ] Toasts; connection loss shows degraded, recovery returns to live.
- [ ] Tests for navigation, palette, degraded/recovered state.

### P3 — Overview

- [ ] Every panel from `overview.js`, mapped one by one in this file before
  building, with live tape and watchlist prices.
- [ ] Tests per panel: data, empty and error states.

### P4 — Stock Research

- [ ] Symbol form loads any ticker; all seven tabs; lightweight-charts price
  chart with live updates; revenue/EBITDA bars; freshness.
- [ ] Tests per tab: data, empty and error states.

### P5 — Verification

- [ ] Side by side against the old console on a running server: same symbols,
  same figures, every §6 item for these pages checked off here.
- [ ] README: how to run `web/`, and screenshots.

Rough size: P0 and P1 in one session, P2 and P3 in one, P4 and P5 in one.

## Not in this step

Strategy Lab, Agent Center, Market & Data, Docs and Settings pages; switching
`/` to the React app; retiring `ui/`; backend response models.
