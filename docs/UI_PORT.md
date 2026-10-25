# React port of the console — plan

Status: P0–P5 done 2026-10-25. The first step (shell, Overview, Stock Research) is complete. First step: the shell, Overview and Stock Research.

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
- **Tailwind v4 (owner, 2026-10-25, at P2).** The copied primitives are written
  with Tailwind classes, so the app uses Tailwind v4 with its default palette
  removed: every colour utility maps to a `--ds-*` token (`web/src/ui/theme.css`),
  and a test fails on a colour utility the theme does not define.

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

API used: `status` and `search` (shell), `research` (company, price, ranges,
news, estimates, altdata, valuation, thesis, memory, financials, filings,
freshness, resolve), `watchlists` (list, create, add and remove symbol),
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

- [x] Typed client keeping `api.js`'s rules: `ApiError` with status, URL and
  payload; `optional()` resolves null only on 404/501; reachability state for
  the ENGINE indicator.
- [x] Zod schema and type per endpoint in scope; Query hooks per resource.
- [x] WebSocket hook: subscribe/unsubscribe/listen, reconnect, visible degraded
  state.
- [x] Tests mock `fetch` and the socket only; one test per endpoint rejects a
  malformed response.
- [x] `npm run check:api`: validates every schema against a running local
  server (not run in CI). `TRADINGLIB_URL` picks the server, `SYMBOLS` the
  tickers (default AAPL, MSFT, NVDA, SPY), and `SAVE_FIXTURES=1` rewrites the
  unit-test fixtures in `web/src/api/__fixtures__` from the first ticker.

### P2 — Shell

- [x] Rail, topbar, datastrip, universe selector, ENGINE indicator, ET clock,
  command palette (pages and any ticker), PICK mode (SPEC §6 topbar).
- [x] Overview and Stock Research routes say they are not ported yet and link
  to the current console; rail and palette entries for unported pages open it.
- [x] Toasts; connection loss shows degraded, recovery returns to live.
- [x] Tests for navigation, palette, degraded/recovered state.

### P3 — Overview

- [x] Every panel from `overview.js`, mapped one by one in this file before
  building, with live tape and watchlist prices.
- [x] Tests per panel: data, empty and error states.
- [x] Checked against the old Overview on a running server: every strip,
  health, quote, fundamentals and market-context figure and title matched.

Panel map (`ui/js/pages/overview.js` → `web/src/pages/overview/`). Each row is
what the old page shows and where it comes from; the port keeps all of it,
including the "—" with a reason in the tooltip where there is no value.

| # | Panel | Source | Shows | Empty / error |
| --- | --- | --- | --- | --- |
| 1 | Market strip | `/api/status` (+ `status` channel, from the shell's cache) | DATA LIVE / IDLE / LIVE FEED LOST / BACKEND UNREACHABLE; research gateway ms; "n of m sources connected"; last ingest age; delayed series (amber when > 0) | "—" until the first status |
| 2 | Ticker tape | active scope symbols (first 14) + `tape` channel | label "● SCOPE NAME" (title: feed, delayed or live, symbol count, or the tape error); per symbol last price and ▲/▼ change % | "no universe members"; "● UNIVERSE UNAVAILABLE" with reason |
| 3 | Watchlist | `/api/watchlists` (default list), names from universe members or `/api/research/resolve`, prices from the tape | badge "watched / universe size"; filter by prefix; add a ticker (creates "Primary research" when there is no list); remove ✕; row sublabel "Name · Sector" shortened, full text in title; click or Enter loads the symbol in the centre | "No watchlist yet" with how to create one; "This watchlist is empty."; "No symbol matches"; load error in red; add/remove errors as toasts |
| 4 | System & data health | `/api/status` | research engine status (green when healthy), market prices LIVE / DELAYED with the stream detail, fundamentals coverage "covered / total" with missing symbols in the title, or MEASURING / NO UNIVERSE; delayed series; badge live / offline from the socket | "—" until the first status |
| 5 | Quote and chart | `/company/{symbol}` (price, name), `/company/{symbol}/price?range=1y` (area chart, last bar OHLC and volume), tape quotes for the live price and the chart's last point | ticker form; price; OPEN / HIGH / LOW / VOL of the newest bar | red note under the chart: company or price error, vendor `error`, "no price history" |
| 6 | Fundamentals | `/company/{symbol}` metrics | symbol; QUALITY SCORE "—" with why (no composite score in this payload); market cap, forward P/E, revenue growth (signed, toned), FCF margin (signed, toned), ROIC, net cash — each with its basis in the title | "—" with the metric's reason; all "—" when the company call fails |
| 7 | Market context | SPY `price?range=2y`, `universes/sp500/breadth?window=200`, `^VIX price?range=1m`, tape quotes | regime RISK-ON / RISK-OFF (SPY vs its own 200-day average, figures in title); S&P constituents above their 200-day average (%, with excluded members in title); advance / decline ratio of tape quotes; VIX last close, toned against the previous close | "—" with the reason for each row (too few closes, no breadth reading, waiting for the first tape cycle, vendor error) |
| 8 | Research inbox | `/api/agents/inbox` (optional) | badge "n items"; first 6 items: title, type · symbol · source · age, urgency tone, reason in title; click opens the Agent Center (current console) on the item's route and context | "Nothing needs a decision"; "Agent inbox not served yet" on 404; error in red |
| 9 | Intel tabs, loaded on first view, reset on scope change | News: SPY `news?limit=9`; Earnings: `/estimates` for the first 6 scope symbols, two at a time; FRED: `/api/macro/series`; Trends: `/altdata` for the first 4 scope symbols, one at a time | News: lead story plus 4 rows (age, publisher, sentiment label and score), open in a new tab; Earnings: table date, company, BMO / AMC / time ET, est. EPS, est. revenue (current quarter), sorted by date; FRED: one cell per series, % for percent units, toned by change, details in title, "n of m series carry a current level" plus stale notes; Trends: one cell per symbol with interest value | each tab says what it is loading, what is empty ("The vendor returned no market news.", "No universe members to look up.", no upcoming dates) and errors per symbol |

Differences from the old page, on purpose: the chart keeps lightweight-charts'
TradingView logo, because its licence asks for attribution and the old console
removes the logo without crediting it anywhere else; the intel panel waits for
the active scope before it renders, so a tab chosen early is not reset when the
scope arrives.

Behaviour kept from the old page: `#overview/<SYMBOL>` becomes `/app/?symbol=SYM`;
the tape subscription carries watchlist symbols, the centre symbol and the
first 14 scope symbols (at most 30); changing the active scope reloads the
tape, names and intel tabs; the first scope symbol (or SPY) is the default
centre symbol.

### P4 — Stock Research

- [x] Symbol form loads any ticker; all seven tabs; lightweight-charts price
  chart with live updates; revenue/EBITDA bars; freshness.
- [x] Tests per tab: data, empty and error states.
- [x] Checked against the old page on a running server (AAPL): the head and
  every figure on all seven tabs matched; an unknown ticker shows the server's
  reason in both.

Map (`ui/js/pages/stock.js` → `web/src/pages/stock/`). The symbol is in the
path, `/app/stock/NVDA` (the old `#stock/NVDA`); `/app/stock` opens GRAB, the
old default. Payloads are shared between tabs, and a tab loads the first time
it is shown.

| Area | Source | Shows | Empty / error |
| --- | --- | --- | --- |
| Head | `/company/{symbol}`, `tape.{SYMBOL}` channel | ticker form (any ticker), company name, meta line, price (red when down; live quote replaces it), MARKET CAP, NEXT EARNINGS "n DAYS" (date and basis in title), DATA FRESHNESS CURRENT / STALE with each component's age in the title | unknown ticker: "SYM could not be loaded" with the server's reason, every head figure "—", and a toast |
| Overview · Price & events | `/price?range=` (1D 5D 1M 3M 6M 1Y 5Y MAX, default 1Y), live quotes | line chart; on daily ranges the live point moves the newest bar, on 1D / 5D it appends | red note: request error, or "no bars for SYM over RANGE — vendor error" |
| Overview · Fundamental snapshot | `/company` metrics | revenue, YoY growth (signed), gross margin, FCF margin (signed), EV / sales, net cash, each with its basis | "—" with the reason; the company error in red |
| Overview · Research thesis | `/api/lab/hypotheses?q=` and `/thesis` (both optional) | badge "n sources"; the lead hypothesis and up to 3 rows (status, owner, age), or "No hypothesis recorded for SYM" with why; CATALYSTS and KEY RISKS labels, each with its measurement in the title | "nothing above the thresholds" / "not loaded"; error in red |
| Overview · Revenue & adjusted EBITDA | `/company` `revenue_ebitda` | one revenue and one EBITDA bar per year, scaled to the largest, negative in red, values in titles; badge first–last year | "The vendor published no annual revenue or EBITDA for SYM." |
| Overview · News & sentiment | `/news?limit=25` | badge = aggregate label (toned), title with the recency-weighted compound; first 4 items | "The vendor returned no news for SYM."; error |
| Overview · Alternative signals | `/altdata` | Google Trends, app rank composite (↑ / ↓), news sentiment (signed); badge = Trends timeframe | "—" with the signal's basis and reason; error |
| Financials | `/financials` | annual table in millions (revenue, gross profit, adj. EBITDA, free cash flow; EBITDA and FCF toned), currency badge; margin progression chart (gross and EBITDA margin lines) | "The vendor published no annual statements for SYM."; "Not enough reported margins to draw a progression."; error |
| Earnings & Estimates | `/estimates` | history newest first: quarter, EPS est. / actual, surprise and price reaction (toned, reaction date in title); consensus FY revenue, FY EPS (analysts in title), 30-day revision, dispersion | "The vendor published no earnings history for SYM."; error |
| Valuation & Peers | `/valuation` | EV / sales, EV / EBITDA, FCF yield, reverse-DCF growth; peer table (company with name in title, the subject highlighted, growth toned, EV / sales, FCF margin) and the peer basis | "No peers could be resolved from the configured universes."; error |
| News & Sentiment | `/news?limit=25` | badge "SYM · label"; "n items scored by vaderSentiment", counts by label and the recency-weighted compound; every item (label, publisher, age; link and compound in title) | "The vendor returned no news for SYM."; error |
| Alternative Data | `/altdata` | cards: Google Trends, app rank composite, web traffic YoY (signed), job postings 90d (a level, unsigned and untoned); signal detail: Trends keyword (geo in title), Trends 90-day change, news sentiment, job board, Trends fetched age | "—" with basis and reason per signal; error |
| Research | `/filings`, `/news`, `/memory`, `/api/lab/hypotheses?q=`, `/api/agents/findings?symbol=` | research memory (current thesis, open questions, sources: filings, news, knowledge entries, form counts); linked agent findings (first 6: title, status); SEC filings table (first 12: filed, form, document link, period) | memory unavailable; "No agent has recorded a finding for SYM yet."; findings not served; EDGAR's error or "EDGAR returned no filings for SYM." |

### P5 — Verification

- [x] Side by side against the old console on a running server: same symbols,
  same figures, every §6 item for these pages checked off here.
- [x] README: how to run `web/`, and screenshots.
- [x] Each page's code loads on first visit (main bundle 336 kB, was 723 kB).

Figure check (2026-10-25, scripted): the old and new pages loaded side by side
in frames on one server, each tab opened in both, and every figure on it (prices,
percentages, multiples, counts, dates, CURRENT / RISK-ON style states) compared
as a multiset. Overview: 59 figures, identical. Stock Research for AAPL, MSFT,
NVDA, SPY and GRAB, head plus all seven tabs: identical, with two differences
that are not figures:

- The range buttons: the old page renders "1D5D1M…" as one run of text, so
  the comparison splits it differently. Same eight ranges.
- Financials for MSFT, NVDA and GRAB: the old table has an extra first year
  column of "—". That year's row is entirely null; the old filter meant to drop
  it, but `isFinite(Number(null))` is true in JavaScript. The port drops it.

SPEC §6 for the ported pages:

| §6 item | Status | How it was checked |
| --- | --- | --- |
| Topbar palette, ⌘K works | done | tests; live: ⌘K, search "nv", NVDA opened |
| PICK mode + count + Export | done | tests: mark, persist, export, clear |
| Universe selector drives tape / screens / scanner scope | done | tests; live on a scratch copy of the data: selecting the watchlist set it server-side and the tape followed |
| ENGINE HEALTHY from real health | done | tests for each state; live: DEGRADED with the server's subsystem list |
| Live ET clock | done | tests; live |
| Datastrip: every figure from `/api/status`, updated over WS | done | tests (REST and channel); live, same figures as the old strip |
| Overview: market strip | done | figure check |
| Overview: ticker tape (live) | done | tests; live tape cycles |
| Overview: watchlist with filter + live prices | done | tests; live on the scratch copy: names, prices and changes from the tape |
| Overview: panels as in REFERENCE | done | panel map in P3, figure check |
| Overview: agent inbox merge | done | tests; live (empty inbox) |
| Stock Research: symbol form loads ANY ticker | done | tests; live, including an unknown ticker showing the server's reason |
| Stock Research: the seven tabs | done | figure check on five tickers |
| Price chart (lightweight-charts, live-updating) | done | tests (live point, intraday ranges); live chart |
| Fundamental snapshot (real derived metrics) | done | figure check |
| Revenue / EBITDA bars | done | tests; live |
| News + sentiment | done | tests; live (the vendor returned no news this weekend, same on both pages) |
| Alt signals (Trends, news sentiment) | done | figure check |
| Data freshness real | done | tests; live CURRENT with per-component ages |
| Palette: navigation, "Open active backtest", symbol research jump | done | tests; the backtest action opens the Strategy Lab in the current console, since the lab is not ported |
| Toasts for action feedback | done | tests; live (scope switch, unknown ticker) |
| Connection loss → degraded; recovery → live | done | tests; live in P2 by stopping and restarting the API |

Not covered by this step: the Strategy Lab, Agent Center, Market & Data, Docs
and Settings items of §6, which stay in the vanilla console.

Rough size: P0 and P1 in one session, P2 and P3 in one, P4 and P5 in one.

## Not in this step

Strategy Lab, Agent Center, Market & Data, Docs and Settings pages; switching
`/` to the React app; retiring `ui/`; backend response models.

## Step 2 — the remaining pages (planned 2026-10-25)

Port Settings, Docs, Market & Data, the Strategy Lab and the Agent Center the
way step 1 ported Overview and Stock Research, then switch `/` to the React
app. Same rules: no stubs, no invented values, a missing value says why, every
response validated, each slice green on its own and committed alone.

Size: step 1 covered 78 COVERAGE.md rows (sections A–C) in about 2,700 lines of
old page code. Step 2 covers the other 293 rows in about 14,400 lines:

| Page | Old file | Lines | COVERAGE rows | Live channels |
| --- | --- | --- | --- | --- |
| Settings | `settings.js` | 487 | H: 7 | none |
| Docs | `docs.js` | 311 | G: 3 | none |
| Market & Data | `data.js` | 3,995 | F: 94 | `ingest`, `quality`, `notifications` |
| Strategy Lab | `lab.js` | 4,689 | D: 62 | `lab` |
| Agent Center | `agents.js` | 4,944 | E: 127 | `runs`, `approvals` |

What is new compared with step 1: these pages write (create, update, delete,
run, approve), they use large tables and forms, and Settings changes the whole
app (theme, density, timezone).

### How each page slice works

1. **Map first.** The page's COVERAGE.md section already lists every element
   and its endpoint; the slice copies those rows into a panel map here, adds
   empty and error states, and marks anything the port does differently.
2. **Data layer.** Zod schema and type per endpoint the page reads, mutation
   hooks for what it writes, `check:api` extended to cover them, fixtures
   saved from a real server.
3. **Build** the page, with its live channels.
4. **Tests** per panel: data, empty and error states; per write: request body,
   success feedback, failure feedback.
5. **Compare** with the old page on a running server using the comparison tool
   from S0, and fix every figure that differs or record why it differs.
6. **Rail and palette** entries for the page switch from the old console to the
   app.

### Decisions for the owner (approve at the first commit)

- **Writes are checked only on a throwaway server.** `check:api` gains a write
  mode that starts its own API on a temporary copy of `data/` and never runs
  against port 8200 or the real data directory. Read checks stay as they are.
- **Destructive actions ask first.** Delete, reject and cancel get a
  confirmation dialog in the React app, also where the old console acted on one
  click. The request is unchanged.
- **The comparison tool is committed.** The figure check used in P5 becomes
  `tools/compare_consoles.py` (headless Chromium over the DevTools protocol, no
  new Python dependency beyond `websockets`), so every slice and CI-less local
  runs can repeat it.
- **Light theme ported as tokens.** The old Settings page ships a light theme
  as raw CSS overrides; the port defines the same colours as a light token set
  in `tokens.css` under `[data-theme="light"]`.
- **The switch is its own decision (S10).** Nothing in S0–S9 changes `/`.

### S0 — Shared foundation

- [x] Primitives copied from the design system as needed: field, select,
  switch, a confirm dialog on the modal, and the data table (adds
  `@tanstack/react-table`).
- [x] One mutation pattern: `useMutation` plus cache invalidation by key, a
  toast on success, the server's reason in place and in a toast on failure.
  Tests for the pattern.
- [x] Live channel hooks for `lab`, `runs`, `approvals`, `ingest` and `quality`,
  with schemas for their frames (captured from a running server first).
- [x] `check:api --writes` on a throwaway server, as above.
- [x] `tools/compare_consoles.py`: old page vs `/app` page per route and tab,
  figures compared as multisets, differences printed; P5's run reproduced with
  it.

### S1 — Settings and Docs

- [x] Settings: Appearance (theme, density, timezone) and Research defaults
  (benchmark, default universe), each written to `PUT /api/settings` and read
  back; the API-keys card, write-only end to end (status and source shown,
  never a key).
- [x] Applied app-wide at load, not only on the page: `data-theme` and
  `data-density` on `<html>` backed by light and density token sets, and the
  topbar clock in the chosen timezone.
- [x] Docs: methodology and product guide, the five entries opening their
  articles, version from `/api/health`, shortcuts from the shell.
- [x] Tests; compare; rail entries for both move into the app.

Done 2026-10-25. Compared with `tools/compare_consoles.py settings docs`: 6 of 6
areas match. Write checks cover saving one field at a time and a key that is
stored, never returned, and cleared, on the throwaway server only.

Map (`settings.js`, `docs.js` → `web/src/pages/settings/`, `web/src/pages/docs/`;
COVERAGE.md G and H):

| Area | Source | Shows and does | Empty / error |
| --- | --- | --- | --- |
| Appearance | `GET /api/settings` | theme button ("Switch to light theme"), density select, timezone select, the UTC note; each change is `PUT /api/settings` with only that field, then the page shows the server's answer and a toast | load error in place of the page; save error as a toast and the page reloads |
| Research defaults | same | primary benchmark (the stored one added when it is not in the options), default universe; changing the universe also moves the topbar selector and refreshes status | as above |
| API keys | same | per key: label, "configured · source · saved date" or "not configured · reads ENV", a password field, Save (refuses an empty field with a warning), Clear (only for stored keys); badge "n / m configured"; the storage note. The response never carries a key | as above |
| Provider status | same | each provider configured or not, with its note; LLM budgets per run, daily, monthly and the config path | as above |
| Applied app-wide | `GET /api/settings` at load | `data-theme`, `data-density` on `<html>`; topbar clock in the chosen zone with label ET, UTC or the city; a `restart_detail` in a save answer is shown as a warning | stays on the defaults and logs the reason |
| Docs | static, plus `GET /api/health` | two panels (Methodology, Product guide) listing five articles; selecting one opens it below and puts it in the URL (`/app/docs/<id>`); footer with the build version | version stays "TradingLib" when health fails |

Deliberate difference: the Keyboard shortcuts article lists this app's routes
(`/app/stock/NVDA`, …) instead of the old hash routes, and the light theme is a
token set rather than per-class overrides, so it covers every page of the app.

### S2 — Market & Data I: collections and screener (F.1, F.2: 31 rows)

- [x] Universes and watchlists: list, create, edit, delete, set active,
  members with point-in-time membership, snapshots and change log, refresh,
  coverage.
- [x] Screener: rule builder with live funnel counts from
  `POST /api/screens/preview`, save, run, alerts, and "Backtest this screen"
  creating a real lab backtest.
- [x] Routes `/data/collections` and `/data/screener`; tests; compare.

Done 2026-10-25. `tools/compare_consoles.py data`: 2 of 2 pages match (118
figures, including the screener's live per-rule funnel counts); the full run
over every ported page: 49 of 49 areas, 1,060 figures. Write checks cover every
collection and screener write on the throwaway server. Notes: Delete now asks
first (agreed in the plan); the copied select primitive takes a
`wrapperClassName` so selects can sit inline; the membership announcement for
screen readers is ported too, which the comparison caught.

Map (`data.js` lines 386–2044 → `web/src/pages/data/`; COVERAGE.md F.1, F.2).
Market & Data's frame is built here too; S3 adds its last three tabs, which
until then open in the current console.

| Area | Source | Shows and does | Empty / error |
| --- | --- | --- | --- |
| Frame | — | head "MARKET & DATA · Collections, screening, ingestion, and data quality", "+ New watchlist" and "+ New universe", five tabs as `/data/<tab>` | — |
| Collections table | `GET /api/universes?include_members=true`, `GET /api/watchlists` | one row per universe and watchlist: name, type, construction (built-in, published constituents, description or manual), symbols, refresh, status (Active, Ready, Not fetched, Default); rows focusable, Enter selects; badge "n collections"; the "How collections work" card | "No universes or watchlists yet — …"; error in the table and the badge "failed" |
| Point-in-time membership | `GET /api/universes/{id}/members?as_of=YYYY-06-30` (as-of slider 2016 → today), `GET /api/watchlists/{id}` | title "name · membership @ date", count, basis chip (snapshot, membership dates, current), up to 200 rows: ticker, name, added → removed, ACTIVE or REMOVED date; the survivorship note | "No members stored yet — use Refresh members"; "No members at this as-of date."; error row |
| Actions | `POST /api/universes/active`, `POST …/refresh`, `POST …/snapshot`, `PUT`, `DELETE` | Set active (not on the active one), Refresh members (built-ins), Freeze snapshot, Edit, Delete (not built-ins; confirmed first, the agreed change); each with a toast | the server's reason in a toast |
| Change log | `GET /api/universes/{id}/changelog?limit=6` | per snapshot: date, symbol count, net change, source; baseline note; + IN / − OUT with the rule that did it; Δ CORP and Δ RULE rows; rule footer "Rule def vN · rule" | "No snapshot recorded yet. …"; watchlists: "Watchlists carry no snapshot history." |
| Coverage | `POST /api/screens/preview` with no rules at the as-of date, `GET /api/quality/datasets` | requested, evaluated, priced, fundamentals, options IV as n/m, toned complete or not; source and truncation note | "measuring…"; error; watchlists: "Coverage is computed for universes." |
| Collection dialog | `POST /api/universes`, `POST /api/watchlists`, `PUT` either | type (fixed when editing), name, description, construction, refresh, symbols or rules, make default; ignored-field warning from the server | name required; the server's reason in a toast |
| Screener: saved screens, base universe | `GET /api/screens`, `GET /api/universes` | saved list (active rules · universe), + NEW SCREEN, base universe select with its member note | "No saved screens yet. Running a screen saves it." |
| Screener: criteria | `GET /api/screens/metrics`, `POST /api/screens/preview` per rule prefix | rule rows: field, operator (> < top pctl is …), value with unit ($bn for market cap, %, ×), scope for percentile rules, source; per-rule funnel "before → after" from real previews (debounced, newest wins); add and remove rule (at least one); rank by; "View as query" | funnel "preview failed" and a toast |
| Screener: results | `POST /api/screens` or `PUT` (save), `POST /api/screens/{id}/run` | Run screen saves then runs; top 12 by the rank metric: #, symbol, composite (green ≥ 90), ROIC, momentum percentile; result profile: sector mix, median market cap, turnover vs last run, overlap with the live book; the run's own stage counts replace the previews; run warnings as toasts | "No names pass this screen — loosen a rule." |
| Screener: actions | `POST /api/screens/{id}/queue-backtest`, `POST /api/universes`, `PUT /api/screens/{id}` tags, `POST /api/screens/alerts/check` | Backtest this screen (benchmark from Settings) then opens the backtest in the Strategy Lab (current console until S5); Save as universe (needs a run); Pin + alert | the server's reason in a toast |
| Membership alerts | `GET /api/screens/alerts?limit=20`, `POST /api/screens/alerts/{id}/acknowledge`, `notifications` frames with `event: screen_alert` | watched count, monitor not running warning, alerts with age and count change, Acknowledge | "Pin a screen to watch its membership. …"; "No membership change since the baseline …" |

### S3 — Market & Data II: sources, ingestion, quality (F.3–F.5: 63 rows)

- [x] Source Catalog: statuses, detail, subscription configuration (including
  granularity and the "applied or not, and why" answer), active subscriptions.
- [x] Data Ingestion: the full configure form with every SourceConfig field
  (SPEC §4 mapping), protocol-dependent fields, custom poll intervals validated
  as the old page does, Test connection, Save, backfill.
- [x] Data Quality: summary, per-dataset checks, incidents, outliers, tickets,
  SLA, run checks; live `ingest` and `quality` frames.
- [x] Routes `/data/<tab>`; tests; compare.

Done 2026-10-25. `tools/compare_consoles.py data`: 5 of 5 pages match (428
figures on the throwaway copy); the full run over every ported page on the real
server: 52 of 52 areas, 1,116 figures. Write checks cover every ingestion and
quality write on the throwaway server (11 of 11). Notes: removing a
subscription and deleting a source ask first (agreed in the plan); the ages in
the two tables tick every five seconds as the old ones did, which the
comparison caught; the modal primitive now names its dialog after its title;
an action's success toast can carry a warn tone (a probe that did not connect,
a backfill already running); a live frame invalidates the tab's queries and
React Query keeps at most one fetch per query running, in place of the old
hand-written coalescing. All five Market & Data tabs now open in this app, and
`#data/<tab>` hashes from search and the palette land here.

Map (`data.js` lines 2049–3983 → `web/src/pages/data/`; COVERAGE.md F.3–F.5).

| Area | Source | Shows and does | Empty / error |
| --- | --- | --- | --- |
| Catalog sidebar | `GET /api/ingest/sources`, `GET /api/ingest/subscriptions` | badge "n sources"; one button per source with its status pill (Connected, Degraded, Disconnected, Credentials required, Not probed) and "protocol · domain"; "+ Request new source" opens Data Ingestion with an empty form; the protocol key | load error in the detail panel |
| Catalog detail | same | name, pill, endpoint, "BUILT-IN / USER SOURCE · PROTOCOL · AUTH … ✓ or — NOT CONFIGURED · schedule"; Credentials (configured: says so; otherwise opens Settings); protocols supported (one enabled); this source's subscriptions as selectable cards; observed state: status, last ingest, rows, latency, last probe, last error | "No ingestion sources are registered in this build."; "No subscription configured for this source yet. …" |
| Subscription panel | same, `POST /api/ingest/subscriptions` | credential warning or ready note; channel (default from the domain); feed mode with the honest note about streaming or polling; preserve point-in-time; frequency chips from the source's options with the note on whether the resolution reaches the fetch (the API's verdict when a subscription has it, else the prediction); poll interval presets plus a custom field, validated (45s, 2m, 4h, 1d), with the cost and bar-alignment advice; scope (universes, watchlists, all, custom symbols); the estimate line; + Add subscription (disabled without credentials), Dry run, Use custom configuration | warnings from the API's `feed_mode_detail` and `granularity_detail` as toasts |
| Poll interval write | `PUT /api/ingest/sources/{id}` with the source re-posted | "now polls every …"; refused with an explanation when the source stores a key (re-posting would clear it) | unrecognised interval refused before sending |
| Backfill | `POST /api/ingest/sources/{id}/backfill?range=&end=`, `ingest` frames `backfill_*` | protocol chips; FROM / TO with the format note and configured default; Run backfill (disabled without credentials); progress and the result written under it from live frames | "No history endpoint — …"; 409 `backfill_running` as a warning; failure as a toast |
| Active subscriptions | same | per row: source, channel · granularity, protocol abbreviation, frequency, scope, last ingest age, status (PAUSED · …), Remove (confirmed first) | "No subscriptions configured. …" |
| Ingestion form | `POST /api/ingest/sources/test`, `POST /api/ingest/sources`, `PUT …/{id}` | every SourceConfig field (SPEC §4): name, domain, protocol (label, placeholder, hint and default frequency follow it), endpoint, auth (fields relabel per auth type; key value write-only), format, frequency (custom cron), scope (custom symbols), timezone, retry policy, backfill on first connection (range, batch, custom start date), advanced JSON; Test connection with the result facts (reachable, authenticated, status, latency, tested at, message, error, inferred schema, sample); Save; editing a source fills the form and warns that a stored key must be re-entered | name required; test and save errors in place and as toasts |
| Configured sources | `GET /api/ingest/sources`, `POST …/{id}/test`, `POST …/{id}/enabled`, `DELETE …/{id}` | badge "connected / total connected"; per row: source, domain, protocol, frequency, backfill, freshness age, coverage rows, status (error in title), Test, Edit, Pause or Resume, Delete (user sources, confirmed first); the lifecycle note | "No ingestion sources are registered."; error row |
| Quality cards | `GET /api/quality/summary` | overall quality (bar, green at 95%+), stale series, missing values, failed checks | "no run yet" |
| Incidents | `GET /api/quality/incidents?include_resolved=true`, `POST …/{id}/resolve` | badge "n open"; check, dataset, severity (toned), status, Resolve on open ones | no checks run yet / every check passed |
| Check suite | `GET /api/quality/series`, `GET /api/quality/sla`, `GET /api/quality/datasets`, `GET /api/quality/tickets`, `POST /api/quality/run`, `POST /api/quality/tickets` | last run age; six named checks (gap scan, outlier detection, stale tickers, schema drift, dupe rows, lookahead) with scope, result (NOT RUN, CLEAN, n FLAGS…) and failing datasets in the title; Review / List opens the failing datasets; SLA rows for sources past their cadence with Ticket; Run checks; stored vs scored counts; open tickets | "No check has been recorded yet. …"; "NO SOURCES" / "ALL WITHIN CADENCE" |
| Outlier queue | `GET /api/quality/outliers?limit=200`, `POST …/{id}/decide` | badge open and escalated counts; up to 25 open and 10 decided: label, value, z, date, basis, change, the detector's suggestion (its button highlighted); Quarantine, Accept value, Escalate or clear escalation, undo on decided ones; the note on what quarantine does | "Nothing flagged. …" |
| Live | `ingest`, `quality` frames | the open tab reloads, coalesced; backfill frames go to the backfill line | — |

Deliberate differences: removing a subscription and deleting a source ask
first (agreed in the plan).

### S4 — Strategy Lab I: frame, hypotheses, experiments (part of D)

- [x] Lab header, five-card funnel and tab bar as routes `/lab/<tab>?id=`, so
  deep links and the palette land on an entity.
- [x] Hypotheses: list, detail, create, edit, status changes, evidence, delete.
- [x] Experiments: split and full views, create, run, the run log streaming
  over `lab`.
- [x] Tests; compare.

Done 2026-10-25. `tools/compare_consoles.py lab` on a seeded throwaway copy: 7
of 7 views match (487 figures: both tabs, every hypothesis and experiment by
id); the full run on the real server, where the lab is empty: 54 of 54 areas,
1,148 figures. The compare tool reads the entity ids from the server it
compares. Write checks cover every hypothesis and experiment write, a real
experiment run with its streamed log, and a backtest of its config (13 of 13).
Notes: the shared query keys for the palette's lab reads stay separate from
the lab's own, and writes refresh both by prefix; a lab tab's count is part of
its accessible name ("HYPOTHESES 2"); the compare and form views were checked
by hand on the seeded copy, including a real run streaming its log.

Map (`lab.js` lines 1–1470 and 1449–2916 → `web/src/pages/lab/`; COVERAGE.md D).
The lab is empty on the real server, so S4's comparison and fixtures use a
throwaway copy seeded with two hypotheses, three experiments (two run for
real), one validation report and one screen run. Backtests, Validation,
Evaluation and Pipeline open in the current console until S5 and S6.

| Area | Source | Shows and does | Empty / error |
| --- | --- | --- | --- |
| Header | `GET /api/universes/active` | "> STRATEGY LAB", the crumb (tab, selection or "new …", the active universe), + NEW EXPERIMENT, + NEW HYPOTHESIS | — |
| Funnel | `GET /api/lab/evaluation` funnel, else the loaded experiments | five cards RESEARCH → LIVE counting experiments that reached each stage (killed count as research), captions with the conversion rates; a card filters the experiment list to that stage and toggles off | counts from the experiment list when evaluation fails |
| Tab bar | `GET /api/lab` summary, report and strategy counts | six tabs with counts as `/lab/<tab>?id=`; Backtests, Validation, Evaluation, Pipeline open in the current console (↗) | counts fall back to list lengths |
| Hypothesis list | `GET /api/lab/hypotheses?include_evidence=true`, experiments | "H-n · title", owner pill (blue for agent), "n experiments · n evidence · STATUS" toned; selecting one puts its id in the URL | "No hypotheses yet" with a create button; load error |
| Hypothesis detail | same, backtests, validation reports, universes | id, title, status; WOULD FALSIFY THIS; statement; rationale; provenance (origin, owner, universe, experiments, created, updated, tags); linked evidence with detach ✕ and the + LINK form (kind, ref, label); experiments under it with stage, judged Sharpe and gates; + NEW EXPERIMENT (preselects it), EDIT, MARK <status> for every other state, DELETE (confirmed); status history newest first | "Select a hypothesis …"; "Nothing linked yet."; "No experiments yet. …" |
| Hypothesis form | `POST /api/lab/hypotheses`, `PUT …/{id}` | title, statement, falsifier (required, said so), rationale, owner, origin, universe scope, tags, status (new only); Cancel | "Title, statement and falsifier are all required."; the server's refusal |
| Hypothesis writes | `POST …/{id}/status`, `DELETE …/{id}`, `POST …/{id}/evidence`, `DELETE …/{id}/evidence/{eid}` | toasts "Created H-n", "Saved H-n", "H-n → STATUS", "Deleted H-n", "Evidence linked", "Evidence detached" | "Evidence needs a reference."; refusals in a toast |
| Agent activity | `GET /api/agents/runs?limit=12`, `GET /api/agents/definitions` | badge live, autonomous, idle or none; up to six runs: the acting agent and what its stage is doing; opens the run in the Agent Center (current console until S7) | "No agent runs yet"; "Agent Center unreachable" |
| Experiment list | `GET /api/lab/experiments`, backtests, reports | "n of m"; stage chip filter; text filter; origin filter (manual, agent, screen, derived from runs); rows with compare checkbox, name, stage pill, origin pill, hypothesis, judged Sharpe, gates; COMPARE SELECTED (n) | "No experiments yet" / "Nothing at this filter"; load error |
| Experiment detail | same, `GET /api/lab/experiments/{id}` | name, stage, hypothesis link; id, signal, universe or symbol count, created; OOS SHARPE, DEFLATED, MAX DD, TRADES, UNIVERSE; strategy as rules or the stored JSON; acceptance criteria; its backtests (run links open the Backtests tab, current console until S5); analyses chips and + SWEEP, + MONTE CARLO, + WALK-FORWARD (current console until S5); experiment runs (IC, seconds, PASS / NOT MET, status) | "No completed backtest yet …"; "Never run."; runs error |
| Experiment actions | `POST …/{id}/run`, `POST /api/lab/backtests/run`, `DELETE …/{id}` | ▷ RUN EXPERIMENT with the live log; ▷ RUN BACKTEST WITH CURRENT CONFIG; ⑂ SEND TO PIPELINE (current console until S6); CLONE & EDIT; COMPARE WITH…; DELETE (confirmed) | the server's reason in a toast and in the log |
| Run log | `lab` frames kind `experiment` | started, log lines, complete or failed; duplicates of the stored log dropped; at most 400 lines | "No run log yet. …" |
| Experiment form | `POST /api/lab/experiments`, gates, quality summary | name, hypothesis, kind, IC horizon, owner, notes; the BacktestConfig editor (period and data, portfolio, signal and costs); acceptance criteria; "THIS RUN WILL BE JUDGED BY" with gate names and store quality; SAVE EXPERIMENT, ▷ SAVE AND RUN; seeded by clone or hypothesis | "The experiment needs a name."; "Choose a universe or list explicit symbols." |
| Compare | loaded data | 2–4 experiments: metrics with the best value marked, config diff of differing fields only, verdict per experiment; ← BACK | "Select 2–4 experiments to compare."; "Tick a second experiment …" |
| Screening | `GET /api/screens`, `GET /api/screens/runs?limit=50` | screens with universe, rules, last run, survivors; RUN (persisted run); → BACKTEST queues one and opens it (current console until S5) | "No screens saved yet. …"; load error |
| Live | `lab` frames | experiment, backtest, validation, promotion and analysis events refetch what they touch; an open form is never wiped | — |

Deliberate difference: deleting a hypothesis or an experiment uses the
confirm dialog instead of the browser's.

### S5 — Strategy Lab II: backtests and analyses

- [ ] Backtests: list, queue, run, results with the equity curve and the fan
  chart, artifact bundle, tearsheet PDF and returns CSV downloads, delete.
- [ ] Analyses: sweeps, walk-forward, Monte Carlo, counts.
- [ ] Palette "Open active backtest" lands in the app.
- [ ] Tests; compare.

### S6 — Strategy Lab III: validation, evaluation, pipeline tab

- [ ] Validation gates, promotion and approvals (withdraw included), pending
  promotions, strategies.
- [ ] Evaluation scorecard (all eight evidence rows with their reasons),
  candidates, tracking, refresh.
- [ ] Pipeline tab: screens, quality and universes panels, recent agent and
  screen runs.
- [ ] Tests; compare; every §6 Strategy Lab item checked here.

### S7 — Agent Center I: the human gate and the registries (E.1, E.4–E.6, E.8, E.9: 65 rows)

- [ ] Overview with the inbox merge and live counters.
- [ ] Approval Review: approve resumes the run, reject kills it, both
  confirmed; live `approvals` frames.
- [ ] Findings with search and the evidence explorer; Knowledge with
  full-text search; Audit Log, filterable; Configuration with writable budgets,
  provider status and spend.
- [ ] Routes `/agents/<view>`, the old hash aliases mapped; tests; compare.

### S8 — Agent Center II: Pipeline Builder, AutoPipeline, Agent Studio (E.2, E.3, E.7: 62 rows)

- [ ] Pipeline Builder: saved pipelines and versions, stage cards, add,
  duplicate and remove a stage, per-stage configuration (agent, model, scope,
  objective, include-in-run, contracts, handoff), the immutable approval stage,
  locking of approved pipelines.
- [ ] Run inspector (preview, inputs, outputs, logs, artifacts) updating live
  over `runs`; run ledger and run comparison; "New run" really runs.
- [ ] AutoPipeline schedules with guardrails and their runs; Agent Studio CRUD
  with each agent's run record.
- [ ] Tests; compare. This is the largest slice and may split in two at the
  run inspector.

### S9 — Verification

- [ ] `tools/compare_consoles.py` over every route and tab, several symbols
  and entities, on the real data read-only and on a scratch copy for pages
  that need entities to exist.
- [ ] Every remaining §6 item checked off here, as P5 did for step 1.
- [ ] COVERAGE.md re-pointed from `ui/` to `web/` files; README screenshots
  for the new pages.

### S10 — Switch `/` to the React app (needs its own SPEC decision)

- [ ] SPEC amendment D1c, for owner approval: `/` serves `web/dist`; the
  vanilla console moves to `/legacy/` for one release, then `ui/` is deleted
  in a separate commit.
- [ ] Old hash links (`/#stock/NVDA`, `/#tl/lab/backtests?id=…`) redirect to
  their new routes.
- [ ] pytest updated (the smoke tests that read `ui/`), CI unchanged otherwise.

Rough size: S0 one session; S1 one; S2 and S3 one each; S4–S6 one each; S7
one; S8 one or two; S9 one; S10 short.
