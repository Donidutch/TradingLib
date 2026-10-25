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
