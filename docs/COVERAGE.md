# TradingLib — Coverage matrix (SPEC §7)

Every interactive element and dynamic value in the reference file
the reference UI design (REFERENCE),
mapped to the TradingLib UI module, the REST endpoint or WebSocket channel it uses,
and the backend module that actually implements it.

**Method.** REFERENCE was read page by page: the static markup (lines 886–2168)
plus every script block that generates UI at runtime — `vendor/app-tl.js`,
`vendor/strategy-lab-v6.js`, `vendor/market-data-v2.js`,
`vendor/source-catalog-v3.js`, `vendor/agent-pipeline-v4*.js`,
`vendor/agent-control-v5*.js`, `features/pipeline-v4-in-v5.js`,
`features/control-room-merge.js`, `features/agent-center-tl-tabs.js`,
`features/overview-inbox-merge.js`, `features/agent-studio-merge.js`,
`features/universe-pit.js`, `features/screener.js`, `features/data-quality.js`,
`features/active-subs.js`, `features/poll-interval.js`, `features/tl-agents.js`,
`core/router.js`, `core/pick-mode.js`. Each TradingLib row was verified by reading
the handler in `ui/js/**` and the route/engine in `tradinglib/**` — not by assuming
an endpoint exists because it is named in the SPEC.

**Backend-column audit.** A previous pass cited two symbols that do not exist
(`cpcv_pbo`, `regime_robustness` on row D41; the real functions are
`cscv_pbo()` and `regime_breakdown()`). Every Backend cell has since been
checked mechanically: each `tradinglib/…` and `ui/…` path is confirmed to exist,
and every code-shaped token in the column has a real `def`/`class` in the tree.
As of this revision the column has **no phantom paths and no invented symbols**;
all 222 REST routes and the WS endpoint cited here resolve to a real
`@router.<verb>` decorator.

**The `path:line` anchors are NOT reliable — treat them as a hint, not a
citation.** The UI modules have grown since the anchors were written and many
have drifted by hundreds of lines: `lab.js` `renderCandidateVerdict` is cited at
`2660` and defined at `4017`, `equityPanel` at `2274`/`3516`, `openCompare` at
`486`/`536`; `data.js` `renderCatalog` at `1840`/`2064`, `renderPollBlock` at
`2131`/`2567`, `renderQuality` at `2827`/`3294`, `renderCheckSuite` at
`2900`/`3373`; `agents.js:3025` is cited for the daily-budget input and is the
runs-table header; `validation.py:652` is cited for `evaluate_gates`, which is
at `999`. The *file* and the *named symbol* in each cell were re-verified and
are correct — grep the symbol, do not jump to the line.

**Status legend**

| Status | Meaning |
| --- | --- |
| **Wired** | The element exists in TradingLib, is driven by a real endpoint/channel, and the backend genuinely computes the value. |
| **Partial** | Present but reduced: a control that is deliberately disabled with a stated reason, a value that renders `—` because no source is configured, or a reference control replaced by a different real one. |
| **Missing** | No TradingLib counterpart. |
| **N/A** | REFERENCE static prose/decoration with nothing to wire (SPEC §7 allows this for docs text), or a REFERENCE surface excluded by the SPEC-locked page tree (§3) — those are listed separately in section J. |

Verification state at the time of writing: `.venv/bin/pytest -q` → **621 passed,
10 skipped, 0 failed**.

**Full pipeline, exercised end to end with a real LLM (SPEC §8 acceptance 4).**
`RUN-5CE175BC` of Full Research Cycle v2 ran all five agent stages to
`succeeded` through the Claude Code CLI (Scanner → Research → Hypothesis →
Quant → Validation, $12.31 recorded in the spend ledger), blocked at the Human
Approval stage, and resumed to `succeeded` when the gate was approved through
`POST /api/agents/approvals/{id}`. Two defects had to be fixed first, both
recorded here because they are launch-time traps rather than code paths:

* **the server must not run on uvloop.** uvicorn selects uvloop whenever it is
  installed; uvloop spawns subprocesses by `fork()`ing the server and running
  Python's post-fork handlers in the child, which finalises the `curl_cffi`
  handles yfinance holds and kills the child with `SIGSEGV` before it can
  `exec`. Every agent stage died with `CLIConnectionError: Cannot write to
  terminated process (exit code: -11)`. `python -m tradinglib.api` pins
  `--loop asyncio`; the lifespan logs a warning if it finds itself on uvloop.
* **`ClaudeAgentOptions.env` is an overlay, not the child's environment.** The
  provider was passing `dict(os.environ)`, which put back the `CLAUDECODE`
  variable the SDK deliberately strips so a spawned CLI does not believe it is
  inside a Claude Code parent.

**Approval gate, exercised end to end.** The human-approval stage was driven
through the UI, not merely read: with the LLM stages excluded from the run,
stage 6 reached `AWAITING APPROVAL`, the Approval Review and Overview badges
appeared, the decision panel refused to enable Approve / Reject / Request more
validation until a ≥12-character rationale was entered, approving resumed the
run to `succeeded`, and `/api/agents/audit` recorded `approval approved` by an
actor of kind `human` with the comment, followed by `run complete`. Rows
E83–E87 and E90 rest on that observation.

---

## A. Shell — topbar, datastrip, rail, overlays

REFERENCE lines 889–930, 2143–2161; `core/router.js` (7305–7485); `core/pick-mode.js` (8967–9196).

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| A1 | Topbar search button `#globalSearch` + `⌘K` | `ui/js/palette.js` (`bindHotkeys`) | — | — | Wired |
| A2 | Command palette input, ↑/↓/Enter | `ui/js/palette.js:164` `search()` | `GET /api/search?q=` | `tradinglib/api/routes/search.py:224` | Wired |
| A3 | Palette action "Go to Overview" | `ui/js/palette.js` STATIC_ACTIONS | — (router) | `ui/js/router.js` | Wired |
| A4 | Palette action "Research <symbol>" | `ui/js/palette.js` | `GET /api/search`, `#stock/<sym>` | `tradinglib/api/routes/search.py` | Wired (fixes REFERENCE's single hard-coded ticker) |
| A5 | Palette action "Open active backtest" → Strategy Lab backtests tab | `ui/js/palette.js:370` `activeBacktest` | `GET /api/lab/backtests` | `tradinglib/research/backtest.py` (`BacktestEngine`) | Wired (fixes REFERENCE dead-end) |
| A6 | Palette actions "Open Agent Center" / "Open Market & Data" | `ui/js/palette.js` | — | — | Wired |
| A7 | UNIVERSE `<select>` (drives tape/screen scope; REFERENCE lists watchlists beside the universes — `Watchlist: Semis`) | `ui/js/app.js` `loadUniverses()` — one option per universe plus one per watchlist, valued `universe:<id>` / `watchlist:<id>` | `GET /api/universes`, `GET /api/watchlists`, `POST /api/universes/active` (accepts either reference) | `tradinglib/data/universe.py` — `set_active_scope()` / `set_active_watchlist()` keep exactly one row active across both tables, and `active_scope()` is what `tradinglib/api/live.py` `_fallback_symbols()` (tape) and `tradinglib/research/screen/funnel.py` `_resolve_universe()` (screens) read, so selecting a watchlist really re-scopes both | Wired |
| A8 | `● ENGINE HEALTHY` | `ui/js/app.js` (`ds.engine`) | `GET /api/status`, WS `status`, API reachability | `tradinglib/api/routes/status.py:208` | Wired |
| A9 | ET clock | `ui/js/app.js:57` (`Intl`, America/New_York) | — | — | Wired |
| A10 | Datastrip `● DATA LIVE` | `ui/js/app.js` | `GET /api/status` + WS `status` | `tradinglib/api/routes/status.py:211` | Wired |
| A11 | Datastrip `GATEWAY 32MS` | `ui/js/app.js` (`dsGateway`) | `GET /api/status.gateway_ms` | `tradinglib/data/ingest/gateway.py` | Wired |
| A12 | Datastrip `13 SOURCES CONNECTED` | `ui/js/app.js` (`dsSources`) | `GET /api/status.sources_connected` | `tradinglib/data/ingest/catalog.py` (real probe state) | Wired (same copy shape as REFERENCE — one number, no denominator; the configured total goes in the element's `title`, and the Source Catalog shows the full connected / credentials-required / not-probed split) |
| A13 | Datastrip `LAST INGEST 1M AGO` | `ui/js/app.js` (`dsLastIngest`) | `GET /api/status.last_ingest` | `tradinglib/data/ingest/scheduler.py:last_ingest_at` | Wired |
| A14 | Datastrip `1 DELAYED SERIES` | `ui/js/app.js` (`dsDelayed`) | `GET /api/status.delayed_series` | `tradinglib/data/ingest/gateway.py` | Wired |
| A15 | Datastrip `TRIALS LOGGED 143` | `ui/js/app.js` (`dsTrials`) | `GET /api/status.trials_logged` | `tradinglib/research/backtest.py` (trial ledger) | Wired |
| A16 | Datastrip `SPEND $23/$120` | `ui/js/app.js` (`dsSpend`) | `GET /api/status.spend` | `tradinglib/agents/runner.py:spend`, `tradinglib/config.py` budgets | Wired |
| A17 | Rail: 7 workspace buttons | `ui/index.html:62`, `ui/js/router.js` | — | — | Wired |
| A18 | Hash routes `#<key>`, `#agents/<sub>`, `#tl/lab[/sub]`, launch-hash capture | `ui/js/router.js:111` `parseHash` | — | — | Wired |
| A19 | Toast target `#toast` | `ui/js/components/toast.js` | — | — | Wired |
| A20 | PICK mode toggle + count + Export (dialog, copy, download, clear) | `ui/js/components/pick.js` | — (localStorage) | — | Wired |
| A21 | Connection-loss degraded state / recovery | `ui/js/stream.js`, `ui/js/api.js:189` `_setReachable` | WS `/ws` heartbeat + reconnect backoff | `tradinglib/api/ws.py` | Wired (SPEC §6 acceptance test) |
| A22 | WS subscribe protocol + channels `tape/status/ingest/quality/runs/lab/approvals/notifications` | `ui/js/stream.js` | `/ws` | `tradinglib/api/ws.py:45` `CHANNELS`, `tradinglib/api/live.py` | Wired |
| A23 | Orphan shim `#ac-shim` (`#themeButton`, `#commandButton`, `#menuButton`, `#sidebar`, `#pageTitle`) | — | — | — | N/A (REFERENCE scaffolding for its verbatim vendor scripts; TradingLib has no vendor scripts to satisfy) |

---

## B. Overview (`#view-overview`)

REFERENCE lines 932–954, plus `features/overview-inbox-merge.js` (6771–6864).

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| B1 | Market strip (DATA LIVE / gateway / sources / last ingest / delayed) | `ui/js/pages/overview.js:148,321` | `GET /api/status`, WS `status` | `tradinglib/api/routes/status.py` | Wired |
| B2 | Ticker tape label `● US QUALITY 50` | `overview.js:379` `renderTape` | `GET /api/universes/active` | `tradinglib/data/universe.py` | Wired |
| B3 | Tape items (symbol, price, % change, live updates) | `overview.js:403,628` | WS `tape` | `tradinglib/data/providers/*`, `tradinglib/api/live.py:49` | Wired |
| B4 | Watchlist panel + `6 / 50` badge | `overview.js:419` | `GET /api/watchlists` | `tradinglib/api/routes/watchlists.py:74` | Wired |
| B5 | Watchlist filter input | `overview.js:441,460` | — (client filter) | — | Wired |
| B6 | Watchlist rows: live price/% per symbol | `overview.js:532` | WS `tape` | `tradinglib/data/providers/*` | Wired |
| B6a | Watchlist row sublabel (`NVIDIA · Technology`) | `overview.js` — `sublabelFor()` via `shortName()` / `shortSector()`; the unabbreviated text stays on the element's `title` | `GET /api/universes/{id}/members`, `GET /api/research/resolve` | `tradinglib/data/universe.py` (member name + sector), `tradinglib/research/company.py` `resolve()` for symbols the scope does not carry | Wired (the legal name and full GICS sector are shortened to REFERENCE's forms so the row stays two lines instead of wrapping to three) |
| B7 | Watchlist row click → chart symbol | `overview.js:547,668` | `GET /api/research/company/{sym}` | `tradinglib/research/company.py` | Wired |
| B8 | Add / remove watchlist symbol | `overview.js:571,591` | `POST/DELETE /api/watchlists/{id}/symbols` | `tradinglib/api/routes/watchlists.py:150,159` | Wired |
| B9 | System & data health: Research engine | `overview.js:346` | `GET /api/status.engine_health` | `tradinglib/api/routes/status.py:208` | Wired |
| B10 | …Market prices LIVE | `overview.js:351` | `GET /api/status.market_stream` | `tradinglib/data/providers/alpaca_provider.py` stream state | Wired |
| B11 | …`Fundamentals 49/50` | `overview.js:362` | `GET /api/status.fundamentals_coverage` | `tradinglib/research/company.py` — `CompanyResearch.measure_fundamentals_coverage()` resolves every member of the active scope through the cached fundamentals fetch and counts the ones carrying a market cap *and* trailing revenue; `fundamentals_coverage()` serves the last measurement and schedules a background refresh so the datastrip never blocks on a vendor. `None` renders "MEASURING", never a zero | Wired |
| B12 | …`FRED series 1 DELAYED` | `overview.js:369` — relabelled "Delayed series" | `GET /api/status.delayed_series` | `tradinglib/data/ingest/gateway.py` | Wired |
| B13 | Quote-search form `#overviewSymbolForm` + Load | `overview.js:193` | `GET /api/research/resolve`, `/company/{sym}` | `tradinglib/api/routes/research.py:49,61` | Wired |
| B14 | Price + OHLC (open/high/low/vol) | `overview.js:192–225` | `GET /api/research/company/{sym}` | `tradinglib/research/company.py` | Wired |
| B15 | Overview price chart (live-updating) | `overview.js:668`, `ui/js/components/charts.js` | `GET /api/research/company/{sym}/price?range=`, WS `tape` | `tradinglib/research/company.py`, `tradinglib/data/store.py` | Wired |
| B16 | Fundamentals panel: symbol, quality grade + score | `overview.js:226` | `GET /api/research/company/{sym}` | `tradinglib/research/company.py` | Wired |
| B17 | …MARKET CAP / FWD P/E / REVENUE GROWTH / FCF MARGIN / ROIC / NET CASH | `overview.js:226–253` | same | `tradinglib/research/company.py` (derived) | Wired |
| B18 | Market context: Regime | `overview.js:763` (SPY vs 200-day) | `GET /api/research/company/SPY/price` | `tradinglib/research/company.py` | Wired |
| B19 | …`S&P above 200D 71%` | `overview.js` market-context row `S&P above 200D` | `GET /api/universes/sp500/breadth?window=200` | `tradinglib/data/universe.py` `moving_average_breadth()`; route `tradinglib/api/routes/universes.py` | Wired (constituent breadth over the stored S&P 500 closes; names without 200 sessions are excluded and listed, and coverage below 20 names returns `percent: null` plus the reason) |
| B20 | …Advance / decline | `overview.js:797` | WS `tape` (live quotes) | — (client aggregation over real quotes) | Wired |
| B21 | …VIX | `overview.js` market-context VIX row | `GET /api/research/company/%5EVIX/price` | `tradinglib/data/universe.py` `_SYMBOL_RE` now admits a leading caret, so index tickers resolve; real yfinance history | Wired |
| B22 | Intel tabs (News / Earnings calendar / FRED macro / Google Trends) | `overview.js:277,827` | — | — | Wired (tab switching) |
| B23 | Intel · News: top story + 4 rows + sentiment tone | `overview.js:837` | `GET /api/research/company/SPY/news` | `tradinglib/research/company.py` (VADER scorer) | Wired |
| B24 | Intel · Earnings calendar table (date/company/timing/est EPS/est revenue) | `overview.js:900–954` | `GET /api/research/company/{sym}/estimates` per member | `tradinglib/research/company.py` | Wired |
| B25 | Intel · FRED macro: 10Y TREASURY / FED FUNDS / CORE CPI YOY / UNEMPLOYMENT | `overview.js` `loadIntelFred()` | `GET /api/macro/series` | `tradinglib/data/macro.py` `MacroService` (Parquet store first, keyless FRED CSV refresh, YoY transform for core CPI); route `tradinglib/api/routes/macro.py` | Wired (real levels; a series that cannot be fetched reports `value: null` with the reason) |
| B26 | Intel · Google Trends: 4 interest scores | `overview.js:987` | `GET /api/research/company/{sym}/altdata` | `tradinglib/data/providers/trends_provider.py` | Wired |
| B27 | Intel scope badge `SCOPE: GENERAL MARKET` | `overview.js:277` | active universe | `tradinglib/data/universe.py` | Wired |
| B28 | Agent inbox merge panel on Overview | `overview.js:271,1030` | `GET /api/agents/inbox` | `tradinglib/agents/approvals.py`, `tradinglib/agents/findings.py` | Wired |

---

## C. Stock Research (`#view-stock`)

REFERENCE lines 956–966.

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| C1 | Symbol form `#stockSymbolForm` + Load (ANY ticker) | `ui/js/pages/stock.js` (head build) | `GET /api/research/resolve`, `/company/{sym}` | `tradinglib/api/routes/research.py:49,61` | Wired (fixes REFERENCE dead path) |
| C2 | Company name / exchange · sector · region | `stock.js` head | `GET /api/research/company/{sym}` | `tradinglib/research/company.py` | Wired |
| C3 | Stock price | `stock.js` head | same + WS `tape` | `tradinglib/data/providers/*` | Wired |
| C4 | MARKET CAP | `stock.js` head | same | `tradinglib/research/company.py` | Wired |
| C5 | NEXT EARNINGS | `stock.js:169` | `GET /api/research/company/{sym}` | `tradinglib/research/company.py` | Wired |
| C6 | DATA FRESHNESS | `stock.js:324` `paintFreshness` | `GET /api/research/company/{sym}/freshness` | `tradinglib/research/company.py` | Wired |
| C7 | Seven tabs (Overview…Research) | `stock.js:172` | — | — | Wired |
| C8 | Overview · Price & events chart + range switcher | `stock.js` (RANGES 1D…MAX), `components/charts.js` | `GET /api/research/company/{sym}/price?range=` | `tradinglib/research/company.py`, `tradinglib/data/store.py` | Wired (REFERENCE has no range switcher; TradingLib adds a real one) |
| C9 | Overview · Fundamental snapshot (REVENUE, YOY, GROSS MARGIN, FCF MARGIN, EV/SALES, NET CASH) | `stock.js:267` | `GET /api/research/company/{sym}` | `tradinglib/research/company.py` | Wired |
| C10 | Overview · Research thesis card, CATALYSTS, KEY RISKS | `stock.js:601` `loadThesis()` + `stock.js:658` `thesisRow()` | `GET /api/lab/hypotheses`, `GET /api/research/company/{symbol}/thesis` | `tradinglib/research/hypotheses.py`; `tradinglib/research/company.py:1076` `thesis_items()` + `:2796` `CompanyResearch.thesis()` | Wired (headline = the recorded hypotheses; the two rows are *measured* — each entry fires from one rule over this company's consensus revisions, trailing margins, balance sheet, news sentiment and coverage warnings, and carries that measurement as its tooltip. TradingLib does not editorialise, so nothing here is generated prose) |
| C11 | Overview · Revenue & adjusted EBITDA bars | `stock.js:231,318` | `GET /api/research/company/{sym}` (`revenue_ebitda`) | `tradinglib/research/company.py` | Wired |
| C12 | Overview · News & sentiment rows | `stock.js:509` | `GET /api/research/company/{sym}/news` | `tradinglib/research/company.py` | Wired |
| C13 | Overview · Alternative signals (Trends / app rank / news sentiment) | `stock.js` alt-signals panel | `GET /api/research/company/{sym}/altdata` | `tradinglib/research/company.py` `interest_rank_composite()` — the percentile rank of the newest Google Trends reading inside its 5-year distribution and its 90-day change | Wired (a search-interest rank, not an app-store position; `basis` says so on the tile) |
| C14 | Financials · historical statement table | `stock.js` financials tab | `GET /api/research/company/{sym}/financials` | `tradinglib/research/company.py`, `tradinglib/data/providers/edgar_provider.py` | Wired |
| C15 | Financials · margin progression chart | `stock.js` financials tab | same | `tradinglib/research/company.py` | Wired |
| C16 | Earnings · history table (est/actual/surprise/reaction) | `stock.js` earnings tab | `GET /api/research/company/{sym}/estimates` | `tradinglib/research/company.py` (reactions from real prices) | Wired |
| C17 | Earnings · consensus estimates (FY revenue, EBITDA, 30d revision, dispersion) | `stock.js` earnings tab | same | `tradinglib/research/company.py` | Wired |
| C18 | Valuation · snapshot (EV/Sales, EV/EBITDA, FCF yield, reverse-DCF growth) | `stock.js` valuation tab | `GET /api/research/company/{sym}/valuation` | `tradinglib/research/company.py` | Wired |
| C19 | Valuation · peer comparison table | `stock.js` valuation tab | same | `tradinglib/research/company.py` | Wired |
| C20 | News & Sentiment tab list | `stock.js` news tab | `GET /api/research/company/{sym}/news` | `tradinglib/research/company.py` | Wired |
| C21 | Alt Data · GOOGLE TRENDS card | `stock.js` alt tab | `GET /api/research/company/{sym}/altdata` | `tradinglib/data/providers/trends_provider.py` | Wired |
| C22 | Alt Data · APP RANK COMPOSITE card | `stock.js:926` `statCard("APP RANK COMPOSITE", signals.app_rank_composite)` | `GET /api/research/company/{sym}/altdata` | `tradinglib/research/company.py:390` `interest_rank_composite()`, assigned at `company.py:2353` — percentile rank of the newest Google Trends reading inside its 5-year distribution plus its 90-day move | Wired (a search-interest rank, not an app-store chart position; the tile's `basis` string says so) |
| C23 | Alt Data · WEB TRAFFIC YOY card | `stock.js` alt tab | `GET /api/research/company/{sym}/altdata` | `tradinglib/research/company.py` `interest_year_over_year()` — 12 weeks of search interest against the same weeks a year earlier | Wired (search interest is the stated proxy; no measured-traffic vendor is implied) |
| C24 | Alt Data · JOB POSTINGS 90D card | `stock.js` alt tab (`statCard(..., {percent, level})`) | `GET /api/research/company/{symbol}/altdata` | `tradinglib/data/providers/jobs_provider.py` (`JobPostingsProvider`: public Greenhouse / Ashby / Lever board APIs, keyless) → `tradinglib/research/company.py:401` `job_postings_signal()` | Wired (the value is the 90-day hiring rate — roles first published in the trailing 90 days as a share of the roles listed now — with the board, slug and counts in the tooltip. A company with no public ATS board renders `—` and names the slugs that were tried; the board is also a catalog source, `id=jobs`) |
| C25 | Research · Research memory (thesis, open questions, sources) | `stock.js` research tab | `GET /api/research/company/{sym}/memory` | `tradinglib/research/memory.py` `ResearchMemoryComposer` — the standing thesis is the recorded hypothesis or finding for the symbol, and open questions come from the hypotheses' own falsifiers | Wired (each row names which object it came from; with nothing recorded it says so rather than writing a narrative) |
| C26 | Research · Linked agent findings (+ `data-open-hypothesis` link) | `stock.js:583` | `GET /api/agents/findings`, `GET /api/lab/hypotheses` | `tradinglib/agents/findings.py` | Wired |

---

## D. Strategy Lab (`#page-lab`)

REFERENCE lines 1293–1491 plus `vendor/strategy-lab-v6.js` (3251–4060) and the merged `.ac` panels at 1329, 1367–1369, 1404–1405.

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| D1 | Header crumb `/ all experiments` | `ui/js/pages/lab.js:602` | — | — | Wired |
| D2 | `+ NEW EXPERIMENT` / `+ NEW HYPOTHESIS` header buttons | `lab.js` (`btn-newexp`, `btn-newhypo`) | `POST /api/lab/experiments`, `/hypotheses` | `tradinglib/research/experiments.py`, `hypotheses.py` | Wired |
| D3 | Funnel cards RESEARCH/VALIDATED/PAPER/APPROVED/LIVE with counts + captions | `lab.js` `renderFunnel()` | `GET /api/lab/evaluation` (`funnel.stages`) | `tradinglib/research/evaluation.py`, `tradinglib/api/routes/lab.py:558` | Wired (a funnel counts how far each experiment *got*, so each card is its stage plus everything past it: RESEARCH is every experiment logged — the same number the EXPERIMENTS tab badge shows — and each later card is a subset of the one before. Captions are computed from those same counts, so a caption can never contradict the number above it) |
| D4 | Funnel card click = stage filter | `lab.js` `filters.stage` + `reachedStage()` | client filter over `/api/lab/experiments` | — | Wired (selects the same population the card counted: experiments that reached that stage or went past it, chipped as "reached <stage>") |
| D5 | Tabbar HYPOTHESES/EXPERIMENTS/BACKTESTS/VALIDATION/EVALUATION/PIPELINE + counts | `lab.js:31` TABS | `GET /api/lab` | `tradinglib/api/routes/lab.py` | Wired (no TypeError — REFERENCE's tabbar bug fixed) |
| D6 | Hypotheses list `#hypolist` | `lab.js` | `GET /api/lab/hypotheses` | `tradinglib/research/hypotheses.py` | Wired |
| D7 | Hypothesis detail: status, origin, experiments, trials charged, falsifier, statement | `lab.js` | `GET /api/lab/hypotheses/{id}` | `tradinglib/research/hypotheses.py` | Wired |
| D8 | Hypothesis · LINKED EVIDENCE rows | `lab.js` | `POST/DELETE /api/lab/hypotheses/{id}/evidence` | `tradinglib/api/routes/lab.py:307,326` | Wired |
| D9 | Hypothesis · `+ NEW EXPERIMENT` | `lab.js` (`newExperimentHypothesis`) | `POST /api/lab/experiments` | `tradinglib/research/experiments.py` | Wired |
| D10 | Hypothesis · `MARK FALSIFIED` | `lab.js` | `POST /api/lab/hypotheses/{id}/status` | `tradinglib/api/routes/lab.py:295` | Wired |
| D11 | Hypothesis new/edit form + CANCEL/SAVE | `lab.js:1133` | `POST/PUT /api/lab/hypotheses` | `tradinglib/research/hypotheses.py` | Wired |
| D12 | Hypothesis delete | `lab.js:1237` | `DELETE /api/lab/hypotheses/{id}` | `tradinglib/api/routes/lab.py:287` | Wired |
| D13 | Merged `.ac` panel "Agent activity" on the hypotheses tab (REFERENCE line 1329) | `lab.js` hypotheses tab — real agent runs rendered as actor + sentence lines | `GET /api/agents/runs` | `tradinglib/agents/runner.py` run records | Wired |
| D14 | Experiments list + count | `lab.js:1318` | `GET /api/lab/experiments` | `tradinglib/research/experiments.py` | Wired |
| D15 | Experiment filter input `#exp-q` | `lab.js` (`filters.expQuery`) | client filter | — | Wired |
| D16 | Experiment origin `<select>` (All/Manual/Agent/Screen) | `lab.js` (`filters.expOrigin`) | `GET /api/lab/experiments?origin=` | `tradinglib/api/routes/lab.py:341` | Wired |
| D17 | `COMPARE SELECTED (n)` + checkboxes + "select 2–4" | `lab.js:486,1935` `openCompare` | `GET /api/lab/experiments/runs` | `tradinglib/api/routes/lab.py:378` | Wired |
| D18 | `+ NEW EXPERIMENT` (list footer) and the new-experiment form | `lab.js:1834` | `POST /api/lab/experiments` | `tradinglib/research/experiments.py` | Wired |
| D19 | Experiment detail: metrics strip (Sharpe/Deflated/MaxDD/Trades/Universe) | `lab.js` | `GET /api/lab/experiments/{id}` + `/runs` | `tradinglib/research/experiments.py`, `backtest.py`, `validation.py` | Wired |
| D20 | Experiment detail: rules table | `lab.js` (`strategyView`) | `GET /api/lab/experiments/{id}` | `tradinglib/research/backtest.py` (`SignalSpec`) | Wired |
| D21 | Experiment detail: run history rows | `lab.js` | `GET /api/lab/experiments/{id}/runs` | `tradinglib/api/routes/lab.py:434` | Wired |
| D22 | `▷ RUN` experiment + live run log | `lab.js` (`runExperiment`) | `POST /api/lab/experiments/{id}/run`, WS `lab` | `tradinglib/research/experiments.py`, `tradinglib/api/live.py:53` | Wired |
| D23 | `CLONE & EDIT` | `lab.js:1420` | `POST /api/lab/experiments` | `tradinglib/research/experiments.py` | Wired |
| D24 | `COMPARE WITH…` | `lab.js:1425` | as D17 | — | Wired |
| D25 | ATTACHED ANALYSES chips + `+ SWEEP` (REFERENCE 3518) | `lab.js` experiment detail | `GET /api/lab/analyses`, `POST /api/lab/analyses/sweep`, `GET /api/lab/analyses/options` | `tradinglib/research/analyses.py` `AnalysisEngine.run_sweep()` — cartesian grid over a whitelisted parameter set, one shared price panel, per-cell stats, best cell and a `robust` flag | Wired |
| D26 | `+ MONTE CARLO` analysis + block-bootstrap view (REFERENCE 3519, 3659) | `lab.js` experiment detail — `+ MONTE CARLO` button and cone view | `POST /api/lab/analyses/monte-carlo` | `tradinglib/research/analyses.py` `AnalysisEngine.run_monte_carlo()` — seeded block-bootstrap resample, persisted against the backtest | Wired |
| D27 | `+ WALK-FORWARD` analysis + folds view (REFERENCE 3519, 3692) | `lab.js` experiment detail | `POST /api/lab/analyses/walk-forward`, `GET /api/lab/analyses/{id}` | `tradinglib/research/analyses.py` `AnalysisEngine.run_walk_forward()` — anchored/rolling folds, selection on the train window only, stitched OOS curve and walk-forward efficiency | Wired |
| D28 | `← BACK` from an analysis view (`#an-back`) | `lab.js` — analysis views render with an `#an-back` control as the only way out | — | — | Wired |
| D29 | Backtest log list `#btlist` + filter | `lab.js` (`filters.btQuery`) | `GET /api/lab/backtests` | `tradinglib/research/backtest.py` | Wired |
| D30 | `+ NEW BACKTEST` form, `▷ RUN BACKTEST`, `CANCEL` | `lab.js:2187` | `POST /api/lab/backtests`, `/backtests/run`, `/backtests/{id}/run` | `tradinglib/api/routes/lab.py:487,504,539` | Wired |
| D31 | `DRY RUN · VALIDATE CONFIG ONLY` (REFERENCE `#bf-dry`) | `lab.js` backtest form — dry-run button and result box | `POST /api/lab/backtests/validate` | `tradinglib/api/routes/lab.py` `validate_backtest_config()` — parses the config, resolves the universe, loads the panel and returns symbols/covered window/warnings/issues without writing a ledger row | Wired |
| D32 | Backtest results: stats + equity curve chart | `lab.js:2274` `equityPanel` | `GET /api/lab/backtests/{id}/results` | `tradinglib/research/backtest.py` | Wired |
| D33 | Backtest delete | `lab.js` | `DELETE /api/lab/backtests/{id}` | `tradinglib/api/routes/lab.py:547` | Wired |
| D34 | `OPEN EXPERIMENT →` / `CLONE CONFIG → NEW BACKTEST` | `lab.js` | client navigation + `POST /api/lab/backtests` | `tradinglib/research/backtest.py` | Wired |
| D35 | Merged `.ac` panel "Price & signals" with ▲BUY/▼SELL markers and in-position shading (REFERENCE 1367) | `lab.js` backtests tab — per-symbol close with entry/exit markers from the trade ledger | `GET /api/lab/backtests/{id}/trades`, price series endpoints | `tradinglib/research/backtest.py` `build_trade_ledger()` | Wired |
| D36 | Merged `.ac` panel "Trade log" table (#, entry, exit, return, why) (REFERENCE 1368) | `lab.js` `tradeLogPanel()` renders the stored ledger from the `/results` payload; a run past the cap says which trades are missing. `/trades` has no UI caller — it pages over the same stored `trade_log` blob `/results` already ships in full, and the 500-trade cap is applied once at write time, so it can return nothing the page lacks | `GET /api/lab/backtests/{id}/results` (table) · `GET /api/lab/backtests/{id}/trades` (API clients) | `tradinglib/research/backtest.py` `build_trade_ledger()` / `BacktestResult.trade_log(limit=500)`, persisted in the `trade_log` column | Wired |
| D37 | Merged `.ac` panel "Equity path fan chart · 200 shown / 5,000 calculated" (REFERENCE 1369) | `lab.js` backtest results — fan chart from `/fan`, cones cached per backtest | `GET /api/lab/backtests/{id}/fan` (`reused` flag when the stored analysis matches) | `tradinglib/research/analyses.py` `run_monte_carlo()` — deterministic seed, so a reused cone and a fresh one are the same cone | Wired |
| D38 | `DOWNLOAD RETURNS CSV` (REFERENCE 3251-block) | `lab.js` backtest results — download link via `api.lab.returnsCsvUrl` | `GET /api/lab/backtests/{id}/returns.csv` | `tradinglib/api/routes/lab.py` `backtest_returns_csv()` — date/equity/return/drawdown (+benchmark), with a header comment stating whether the curve is every bar or a sample | Wired |
| D39 | `EXPORT TEARSHEET PDF` | `lab.js` backtest results — download link via `api.lab.tearsheetUrl` | `GET /api/lab/backtests/{id}/tearsheet.pdf` | `tradinglib/api/routes/lab.py` `backtest_tearsheet()` — self-contained PDF of the run's real stats | Wired |
| D40 | `ARTIFACT BUNDLE · cfg+data+code` | `lab.js` backtest results — pinned-bundle panel, cached per backtest | `GET /api/lab/backtests/{id}/artifact` | `tradinglib/research/backtest.py` `BacktestResult.artifact_bundle()` — sha256 of the canonical config, of the actual price bytes, and of the research-engine source; persisted in the `artifact_bundle` column | Wired |
| D41 | Gate suite (OOS Sharpe, deflated Sharpe, PBO, regime robustness, lookahead, artifact bundle…) | `lab.js` validation tab | `GET /api/lab/validation/gates` | `tradinglib/research/validation.py` — `cscv_pbo()` at `validation.py:497` (CSCV over C(8,4) splits), `regime_breakdown()` at `validation.py:596` (trailing-vol buckets), `lookahead_scan()` at `validation.py:688`, `BacktestResult.artifact_bundle()` at `backtest.py:1109`; 12 gates | Wired (a single-trial family passes PBO with the reason stated, rather than silently) |
| D42 | Validation · TRIAL LEDGER (total, agent cycles, manual runs, sweep cells, append-only) | `lab.js` validation tab — sweep/walk-forward batch rows with cell counts (tooltip explains each cell is a separate look at the data) | `GET /api/lab`, `GET /api/lab/analyses/counts` | `tradinglib/research/analyses.py` `AnalysisEngine.counts()` returns sweeps / sweep_cells / walk_forwards / walk_forward_folds | Wired |
| D43 | Validation · candidate verdict score + PASS/FAIL/WARN gate rows | `lab.js:2660` `renderCandidateVerdict` | `GET /api/lab/validation/{backtest_id}/gates` | `tradinglib/research/validation.py:652` `evaluate_gates` | Wired |
| D44 | Validation · `▷ EVALUATE GATES` / `RE-EVALUATE GATES` | `lab.js:2712` | `POST /api/lab/validation/{backtest_id}` | `tradinglib/api/routes/lab.py:640` | Wired |
| D45 | Validation · BLOCKING ISSUES panel | `lab.js:2700` | same gate report | `tradinglib/research/validation.py` | Wired |
| D46 | Validation · data-quality note ("0 unlogged runs" / incidents) | `lab.js:2650` | `GET /api/quality/summary` | `tradinglib/data/quality.py` | Wired |
| D47 | Evaluation · SCORECARD table (frame: candidate columns, GATES PASSED row, VERDICT row) | `lab.js` `scorecardPanel()` | `GET /api/lab/evaluation` (`scorecard`) | `tradinglib/research/evaluation.py` `EvaluationEngine.scorecard()` | Wired (all eight of REFERENCE's evidence rows now render, plus TradingLib's own Max drawdown / Excess vs benchmark / Trades / Annual turnover; the GATES PASSED, VERDICT and STAGE rows come from the newest validation report — see D47a for the evidence rows) |
| D47a | Evaluation · SCORECARD evidence rows `Sweep stability`, `MC: P(Sharpe>1)`, `MC: p95 max DD`, `WFA positive folds`, `Corr. to live book`, `Capacity est.` (REFERENCE 1413–1424) | `lab.js` `scorecardPanel()` renders each cell from the row's `values`/`displays`, its `tones` (`ok`/`warn`) and its `notes` (the cell's `title`); an uncomputable cell renders `—` carrying the reason, `formatScorecard()`/`usdShort()` do the units | `GET /api/lab/evaluation` (`scorecard.rows[]`, `scorecard.live_book`) | `evaluation.py` `EvaluationEngine._sweep_cell()` / `_monte_carlo_cells()` / `_walk_forward_cell()` join the **newest completed** analysis of each kind recorded against that candidate's backtest (`analyses.py` `AnalysisEngine.list_analyses(backtest_id=…, kind=…)`): sweep stability = `median_metric / best_metric` with the `robust` flag as IN REGION ✓ / PEAK ONLY, `prob_above_one` and `p95_max_drawdown` from the Monte-Carlo summary, `positive_folds / scored` from the walk-forward summary; a candidate with no such analysis renders `—` with `no sweep/MC/WFA analysis recorded`. `_correlation_cell()` uses the new `tradinglib/research/livebook.py` `LiveBookEngine`: it rebuilds the paper book's daily equity from `tradinglib.trade.portfolio.PortfolioStore` fills + positions marked with stored daily closes (read-only; `trade/` is untouched) and Pearson-correlates it against the candidate's stored equity-curve returns (`returns_from_curve()`), refusing with `no live book: …` under 20 overlapping sessions rather than returning 0.0. `_capacity_cell()` uses the new `tradinglib/research/capacity.py` `CapacityEngine`: `min over held names of (participation × ADV_usd / weight)` at 5% participation over a 63-session ADV read from stored volume, holdings from the run's open trades (`holdings_from_trade_log()`); the participation/no-impact assumption is in every cell's tooltip and a missing volume history refuses the bound instead of computing it over part of the book | Wired |
| D47b | Evaluation · SCORECARD candidate column links (`data-open="v12"` / `"crowd"` / `"volrg"`, REFERENCE 1413) — click a candidate to open it | `lab.js` `scorecardPanel()` — each candidate header is a `span.ref` whose click selects that experiment and switches to the EXPERIMENTS tab | client navigation over `GET /api/lab/evaluation` (`scorecard.candidates[].experiment_id`) | `tradinglib/research/evaluation.py` `EvaluationEngine.scorecard()` | Wired |
| D48 | Evaluation · `EXPORT REPORT` | `lab.js:2787,2851` | client download of the served scorecard | `tradinglib/research/evaluation.py` | Wired |
| D49 | Evaluation · DECISION LOG | `lab.js:2930` | `GET /api/lab/validation` (report + promotion history) | `tradinglib/research/validation.py` | Wired |
| D50 | Evaluation · "Add comment…" footer | — | — | — | N/A (inert text in REFERENCE) |
| D51 | Evaluation · PAPER tracking panel (`day n/90`, kill markers) | `lab.js` `trackingPanel` | `GET /api/lab/evaluation/tracking`, `/{id}/decay` | `tradinglib/research/evaluation.py` | Wired |
| D52 | Evaluation · refresh tracking | `lab.js:3001` | `POST /api/lab/evaluation/refresh` | `tradinglib/api/routes/lab.py:760` | Wired |
| D53 | Pipeline · ADVANCE STRATEGY form (experiment, target stage, gate checks, override notes) | `lab.js` `renderPipeline` | `GET /api/lab/validation/{id}/gates` | `tradinglib/research/validation.py` | Wired |
| D54 | Pipeline · `▷ ADVANCE` (promotion) | `lab.js` | `POST /api/lab/validation/{backtest_id}/promote` | `tradinglib/api/routes/lab.py:691` | Wired |
| D55 | Pipeline · PROMOTION QUEUE rows | `lab.js` | `GET /api/lab/validation/strategies` | `tradinglib/api/routes/lab.py:610` | Wired |
| D56 | Pipeline · strategy status change (hold / kill / demote) | `lab.js:3144` | `POST /api/lab/validation/strategies/{id}/status` | `tradinglib/api/routes/lab.py:621` | Wired |
| D57 | Pipeline · STAGE HISTORY | `lab.js` | promotion history in `/api/lab/validation` | `tradinglib/research/validation.py` | Wired |
| D58 | Pipeline · "needs 2nd approval" / two-approver policy | `lab.js` validation tab — sign-off panel: who signed, who is still needed, withdraw | `POST/GET /api/lab/validation/{id}/approvals`, `DELETE …/approvals/{actor}` | `tradinglib/research/validation.py` promotion sign-offs — two different named approvers required; the first signature parks the promotion, the same actor cannot sign twice | Wired |
| D59 | Screening funnel/screen panels surfaced in the Lab | `lab.js:638,707` | `GET /api/screens/runs` | `tradinglib/research/screen/funnel.py` | Wired |
| D60 | Inert `#sl-*` subpages (REFERENCE 1482–1490) | — | — | — | N/A (REFERENCE scaffolding for `app-tl.js`) |

---

## E. Agent Center (`#view-agents`)

The final REFERENCE Agent Center is the v5 Control Room with nine workspaces
(`features/control-room-merge.js` 6216–6428 flattens 10 → 7, then
`features/agent-center-tl-tabs.js` 6429–6770 re-adds Knowledge and AutoPipeline).
TradingLib reproduces the same nine (`ui/js/pages/agents.js:38` `VIEWS`).

### E.1 Overview / Research Inbox — REFERENCE 5115–5152, 6771–6864

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| E1 | Inbox search input | `agents.js:553` | `GET /api/agents/inbox` (client filter) | `tradinglib/agents/approvals.py`, `findings.py` | Wired |
| E2 | Queue segmented control (Needs attention / Assigned to me / All research) | `agents.js:558` | same | same | Wired |
| E3 | Inbox `Type` select | `agents.js` inbox toolbar | `GET /api/agents/inbox` (`types`) | `tradinglib/agents/inbox.py` `INBOX_TYPES` + `InboxComposer` — Approval / Validation / Experiment / Data / Agent, each produced by a real source | Wired |
| E4 | Attention-queue item list (symbol, urgency, title, reason, age) | `agents.js:518` | `GET /api/agents/inbox` | `tradinglib/agents/approvals.py` | Wired |
| E5 | Item detail (origin, owner/agent, age, evidence confidence + bar) | `agents.js:607` | same | same | Wired |
| E6 | Primary action → route (Review candidate / Open run / Open finding) | `agents.js:634,643,650` | navigates to approval / pipeline / findings | — | Wired |
| E7 | `Mark reviewed` | `agents.js:657` | `POST /api/agents/findings/{id}/acknowledge` | `tradinglib/api/routes/agents.py:1072` | Wired |
| E8 | Empty-state `Clear filters` button (REFERENCE `data-clear-inbox`) | `agents.js` — `clearInboxFilters()` / `inboxFiltered()`; rendered in both the list and the detail panel as `.v5-empty` with `[data-clear-inbox]` | — (client filter state) | — (`state.inboxQuery` / `inboxQueue` / `inboxType`) | Wired (a filter-emptied queue now says so and offers the way back; a genuinely empty queue keeps its own distinct wording) |
| E9 | KPI strip ACTIVE / SCHEDULED / PAUSED / FINDINGS PENDING / GUARDRAIL BLOCKS TODAY / AGENT SPEND | `agents.js:538–546` | `GET /api/agents/runs`, `/autopipeline`, `/findings`, `/audit`, `/config` | `tradinglib/agents/runner.py`, `autopipeline.py`, `findings.py`, `audit.py` | Wired |
| E10 | AGENTS NOW panel | `agents.js:672` | `GET /api/agents/definitions` | `tradinglib/agents/registry.py` | Wired |
| E11 | RECENT RUNS panel | `agents.js:695` | `GET /api/agents/runs` | `tradinglib/agents/runner.py` | Wired |
| E12 | LATEST FINDINGS panel | `agents.js:709` | `GET /api/agents/findings` | `tradinglib/agents/findings.py` | Wired |
| E13 | KNOWLEDGE ADDED THIS WEEK panel | `agents.js:725` | `GET /api/agents/knowledge` | `tradinglib/agents/knowledge.py` | Wired |

### E.2 Pipeline Builder — REFERENCE `features/pipeline-v4-in-v5.js` 5288–6215

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| E14 | Saved-pipeline `<select>` (`data-pipeline-select`) | `agents.js` `renderPipeline` | `GET /api/agents/pipelines` | `tradinglib/agents/pipeline.py` | Wired |
| E15 | Version picker (`data-pipeline-version`) | `agents.js` | `GET /api/agents/pipelines/{id}/versions` | `tradinglib/api/routes/agents.py:683` | Wired |
| E16 | `Save new version` | `agents.js` | `POST /api/agents/pipelines/{id}/versions` | `tradinglib/api/routes/agents.py:698` | Wired |
| E17 | `Duplicate pipeline` | `agents.js` | `POST /api/agents/pipelines/{id}/duplicate` | `tradinglib/api/routes/agents.py:673` | Wired |
| E18 | New pipeline (`data-pipeline-new`) | `agents.js` | `POST /api/agents/pipelines` | `tradinglib/agents/pipeline.py` | Wired |
| E19 | Delete pipeline | `agents.js` | `DELETE /api/agents/pipelines/{id}` | `tradinglib/api/routes/agents.py:665` | Wired |
| E20 | Flow strip / stage cards with live state and per-stage model | `agents.js` (PROGRESS_CLASS; `stageModel()` prints the model the stage would really run on) | WS `runs`, `GET /api/agents/pipelines/{id}` (`effective_model`) | `tradinglib/agents/runner.py` → `tradinglib/api/live.py:50`; `tradinglib/api/routes/agents.py` `_annotate_effective_models()` resolves stage override → agent model → provider default, the same chain `PipelineRunner._run_agent_stage()` walks, so a card cannot read "no model" for a stage that runs | Wired |
| E21 | Stage select (`data-stage-select`) | `agents.js` | `GET /api/agents/pipelines/{id}` | `tradinglib/agents/pipeline.py` | Wired |
| E22 | Add stage | `agents.js` | `POST /api/agents/pipelines/{id}/stages` | `tradinglib/api/routes/agents.py:708` | Wired |
| E23 | Duplicate stage (`data-stage-duplicate`) | `agents.js` | `POST /api/agents/pipelines/{id}/stages/{sid}/duplicate` | `tradinglib/api/routes/agents.py:742` | Wired |
| E24 | Remove stage (`data-stage-remove`) | `agents.js` | `DELETE /api/agents/pipelines/{id}/stages/{sid}` | `tradinglib/api/routes/agents.py:734` | Wired |
| E25 | Move stage up/down (`data-stage-move`) | `agents.js` | `POST /api/agents/pipelines/{id}/stages/order` | `tradinglib/api/routes/agents.py:754` | Wired |
| E26 | `Include stage in pipeline` toggle (`data-stage-toggle`) | `agents.js` | `PUT /api/agents/pipelines/{id}/stages/{sid}` | `tradinglib/agents/pipeline.py` | Wired |
| E27 | Stage config: name, assigned agent, model, objective, tools, scope, budget, timeout, retries | `agents.js:995–1100` (`data-stage-field`) | `PUT …/stages/{sid}`, `GET /api/agents/models` | `tradinglib/agents/pipeline.py`, `tradinglib/agents/providers.py` | Wired |
| E28 | Stage routing: INPUT CONTRACT / OUTPUT CONTRACT / HANDOFF (four routing fields) | `agents.js` (`pass_condition`, `fail_condition`, `stop_condition`, handoff) | `PUT …/stages/{sid}` | `tradinglib/agents/pipeline.py` | Wired |
| E29 | `Save stage` / `Reset stage` | `agents.js:1043,1660` | `PUT …/stages/{sid}` | `tradinglib/agents/pipeline.py` | Wired |
| E30 | `Run this stage` (single-stage execution) | `agents.js` `runSingleStage()` | `POST /api/agents/pipelines/{id}/stages/{sid}/run` | `tradinglib/agents/runner.py` `PipelineRunner.run_stage()` — a real run with every other stage switched off for that run only | Wired (refuses the human-approval boundary and an unassigned stage, with the reason) |
| E31 | `Preview graph` / `Graph preview` | `agents.js` | `GET /api/agents/pipelines/{id}/validate` | `tradinglib/api/routes/agents.py:764` | Wired |
| E32 | `Run` pipeline (`data-pipeline-run`) | `agents.js` | `POST /api/agents/pipelines/{id}/run` | `tradinglib/agents/runner.py` | Wired |
| E33 | Run inspector tabs Preview / Inputs / Outputs / Logs / Artifacts | `agents.js:95` `INSPECTOR_TABS` | `GET /api/agents/runs/{id}`, `/logs`, `/artifacts`, `/artifacts/{aid}` | `tradinglib/agents/runner.py` | Wired |
| E34 | Live log streaming in the inspector | `agents.js` | WS `runs` | `tradinglib/api/live.py:50` | Wired |
| E35 | Runs table: id, name, pipeline, version, status, started, duration, cost, symbols, counts, stage progress, objects | `agents.js` runs table | `GET /api/agents/runs` | `tradinglib/agents/runner.py` | Wired |
| E36 | Run detail: metrics, CONCLUSION, warning | `agents.js` | `GET /api/agents/runs/{id}` | `tradinglib/agents/runner.py` | Wired |
| E36a | Runs table `Open in builder` (REFERENCE 5989, `data-v4-run`) | `agents.js:1697` `ui.button("Open in builder", …)` — loads that run's pipeline/version into the builder | `GET /api/agents/pipelines/{id}`, `GET /api/agents/runs/{id}` | `tradinglib/agents/pipeline.py`, `runner.py` | Wired |
| E37 | `Export run` | `agents.js:1436` | client download of the served run payload | `tradinglib/agents/runner.py` | Wired |
| E38 | Cancel a run | `agents.js:1743` | `POST /api/agents/runs/{id}/cancel` | `tradinglib/api/routes/agents.py:872` | Wired |
| E39 | Run inspector `Pause` | `agents.js` `pauseRun()` | `POST /api/agents/runs/{id}/pause` | `tradinglib/agents/runner.py` `pause()` — stops at the next stage boundary; the in-flight stage finishes because its tokens are already spent | Wired |
| E40 | `Replay from stage…` (`data-run-replay`) | `agents.js` `replayRun()` | `POST /api/agents/runs/{id}/replay` | `tradinglib/agents/runner.py` `PipelineRunner.replay()` — creates a NEW run linked by `replay_of`; stages before the chosen one are inherited and recorded as skipped, so the original run is never mutated | Wired |
| E41 | Run inspector `Retry failed stage` | `agents.js` `retryRun()` | `POST /api/agents/runs/{id}/retry` | `tradinglib/agents/runner.py` `retry()` — rewinds to the first stage that did not succeed and resets it and everything after it | Wired |
| E35a | `views.runs` toolbar `Search runs` input + `Status` select (REFERENCE `vendor/agent-control-v5-runs.js` 5207, `data-run-search` / `data-run-status`) | — | — | — | N/A (`features/control-room-merge.js` 6244 composes the Pipeline tab from `parts: ["pipeline"]` only and 6329 aliases `#agents/runs` → `#agents/pipeline`, so `views.runs` never renders in the composed reference app. The run table TradingLib reproduces is the v4 builder's own (`features/pipeline-v4-in-v5.js`, `data-v4-run`), which has no search or status filter) |
| E42 | Run comparison: RUN A / RUN B pickers (`data-run-pick`), side-by-side metrics | `agents.js` `renderRunComparison()` | `GET /api/agents/runs/compare?a=&b=` | `tradinglib/api/routes/agents.py` `compare_runs()` — stages paired by id with status/model/token/cost/duration deltas | Wired |
| E43 | `Clear selection` (run comparison) | `agents.js:1590` — `Clear` button, shown once both sides are picked; resets `compareA`/`compareB`/`comparison` and re-renders | — (client state) | — | Wired (REFERENCE labels it "Clear selection"; TradingLib labels it "Clear") |
| E43a | Run comparison `⇄ Swap` (REFERENCE 5930, `data-cmp-swap`) | `agents.js:1687` (`data-cmp-swap`) | `GET /api/agents/runs/compare` | `tradinglib/agents/runner.py` | Wired (swaps RUN A / RUN B and re-reads the comparison, because every delta in the payload is signed B−A; disabled until two different runs are picked) |
| E44 | `Open Approval Review →` from a run | `agents.js` | `GET /api/agents/approvals` | `tradinglib/agents/approvals.py` | Wired |
| E45 | `View in audit log →` | `agents.js` (`auditRunId`) | `GET /api/agents/audit?run_id=` | `tradinglib/agents/audit.py` | Wired |
| E46 | `data-v4-catalog` / `data-catalog-add` stage-catalogue picker | `agents.js` (add-stage picker over the agent registry) | `GET /api/agents/definitions` | `tradinglib/agents/registry.py` | Wired |

### E.3 AutoPipeline — REFERENCE 1927–1984 + `features/tl-agents.js` 8826–8966

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| E47 | AutoPipeline header (`1 active`, stage string) | `agents.js:1800` | `GET /api/agents/autopipeline` | `tradinglib/agents/autopipeline.py` | Wired |
| E48 | Schedule list + detail (cadence, next/last run, runs today, spend today, max concurrent) | `agents.js:1828,1863` | `GET /api/agents/autopipeline` | `tradinglib/agents/autopipeline.py` | Wired |
| E49 | Create schedule (pipeline, name, cron, timezone, max runs/day, max spend/day, max concurrent, objective) | `agents.js:1930` | `POST /api/agents/autopipeline` | `tradinglib/api/routes/agents.py:1253` | Wired |
| E50 | Enable / disable a schedule | `agents.js:1947` | `PUT /api/agents/autopipeline/{id}` | `tradinglib/api/routes/agents.py:1277` | Wired |
| E51 | `RUN NOW` | `agents.js:1960` | `POST /api/agents/autopipeline/{id}/run` | `tradinglib/api/routes/agents.py:1299` | Wired |
| E52 | Delete a schedule | `agents.js` | `DELETE /api/agents/autopipeline/{id}` | `tradinglib/api/routes/agents.py:1291` | Wired |
| E53 | Recent runs / run detail for a schedule | `agents.js:1900` | `GET /api/agents/runs?schedule_id=` | `tradinglib/agents/runner.py` | Wired |
| E54 | Per-run parameter form: QUERY, TICKERS, TOP N TICKERS, SCAN MODE | `agents.js` `briefFields()` | `POST /api/agents/autopipeline` (`inputs`) | `tradinglib/agents/autopipeline.py` `Schedule.inputs`, handed to the first stage by `PipelineRunner.start(inputs=...)` | Wired |
| E55 | `Resolve tickers` toggle | `agents.js` AutoPipeline form — ON by default; OFF records `resolve_tickers=false` in the run brief the first stage receives verbatim ("Run inputs"), the directive to take the ticker list as given | `POST /api/agents/autopipeline` (`inputs.resolve_tickers`) | `tradinglib/agents/runner.py` `build_stage_user_prompt()` hands the brief to the stage; the scanner's `resolve_universe` tool is what the directive governs | Wired |
| E56 | `Auto-advance on pass` toggle (`#pl-adv-toggle`) | `agents.js` `autoAdvanceToggle()` | `POST/PUT /api/agents/autopipeline` (`auto_advance`) | `tradinglib/agents/autopipeline.py` `AutoPipeline.advance()` — sets an experiment the run produced to `validated` only when a validation report for its backtest passed every gate; always records why it did or did not advance | Wired |
| E57 | ADVANCED CONFIGURATION: SECTORS, CATALYST WINDOW (H), UNIVERSE | `agents.js` AutoPipeline form — sectors and catalyst window land in the run brief like the query; UNIVERSE is a real universe id picked from `GET /api/universes` | `POST /api/agents/autopipeline` (`inputs.sectors` / `inputs.catalyst_window_hours` / `inputs.universe`), `GET /api/universes` | `tradinglib/agents/runner.py` — the brief reaches the stage as "Run inputs"; the universe id feeds the stage's `resolve_universe` tool | Wired |
| E58 | Per-run model overrides: SCANNER / RESEARCH / STRATEGY / QUANT MODEL selects | `agents.js` `modelOverrideFields()` | `POST /api/agents/autopipeline` (`model_overrides`), `POST /api/agents/pipelines/{id}/run` (`stage_overrides`) | `tradinglib/agents/runner.py` `apply_stage_overrides()` — applied to the run's frozen document only; the saved pipeline is untouched and the run records what it actually ran | Wired |
| E59 | `STAGES · CLICK TO TOGGLE` chips | `agents.js` AutoPipeline form — one chip per stage of the chosen pipeline; a dim chip is skipped for this schedule's runs, and the locked approval boundary renders as "required" instead of a toggle | `POST /api/agents/autopipeline` (`stage_toggles`), `POST …/pipelines/{id}/run` (`stage_overrides`) | `tradinglib/agents/runner.py` `apply_stage_overrides()` — include_in_run on the run's frozen pipeline copy; switching off the human-approval boundary is refused | Wired |
| E60 | Cost estimate note (`6 stages · est $9.40 · ~12 min`) | `agents.js` flow-panel estimate note | `GET /api/agents/pipelines/{id}/estimate` | `tradinglib/agents/runner.py` `PipelineRunner.estimate()` — mean tokens/seconds from this workspace's succeeded stage rows, priced at the model each stage would resolve; every row names its basis and an unpriced model is excluded from the total | Wired |
| E61 | `▷ RUN PIPELINE` | `agents.js` (Pipeline Builder `Run`, E32) | `POST /api/agents/pipelines/{id}/run` | `tradinglib/agents/runner.py` | Wired |

### E.4 Findings + Evidence Explorer — REFERENCE 5266–5287

The Evidence Explorer is a REFERENCE *view*, not a REFERENCE *tab*: the final
nav row is the seven `ORDER` entries at REFERENCE 6315 plus Knowledge and
AutoPipeline merged in at 6721 — nine buttons, no Evidence. TradingLib matches that
(`ui/js/pages/agents.js` marks the view `nav:false`), so the explorer is reached
the way REFERENCE reaches it: a finding's or a run's "Trace" link, or the
`#tl/agents/evidence` hash.


| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| E62 | Findings search input | `agents.js:2021` | `GET /api/agents/findings?q=` | `tradinglib/agents/findings.py` | Wired |
| E63 | Findings registry table (finding, symbol, confidence, run, agent, hypothesis, approval, evidence) | `agents.js:2021–2060` | `GET /api/agents/findings` | `tradinglib/agents/findings.py` | Wired |
| E64 | Run link (`data-v5-route="runs"`) | `agents.js` | `GET /api/agents/runs/{id}` | `tradinglib/agents/runner.py` | Wired |
| E65 | Agent link (`data-v5-route="agents"`) | `agents.js` | `GET /api/agents/definitions/{id}` | `tradinglib/agents/registry.py` | Wired |
| E66 | Finding detail + evidence cards (supports / contradicts) | `agents.js:2075` | `GET /api/agents/findings/{id}` | `tradinglib/agents/findings.py` | Wired |
| E67 | Edit / delete a finding | `agents.js:2096` | `PUT`/`DELETE /api/agents/findings/{id}` | `tradinglib/api/routes/agents.py:1050,1064` | Wired |
| E68 | Evidence Explorer spine (Sources → … → Decision) | `agents.js` `renderEvidence()` | `GET /api/agents/evidence` | `tradinglib/agents/findings.py` `EvidenceGraph.build()` over real foreign keys; an engine that is not mounted drops its column with a note | Wired |
| E69 | Evidence Explorer object-type select | `agents.js` evidence toolbar | `GET /api/agents/evidence` (`types`) | `tradinglib/agents/findings.py` `LINEAGE_TYPES` | Wired |
| E70 | Evidence Explorer: focus segmented (All relationships / Upstream / Downstream) | `agents.js` evidence toolbar | `GET /api/agents/evidence?focus=` | `tradinglib/agents/findings.py` `EvidenceGraph.related()` | Wired |
| E71 | Evidence Explorer `Center selected node` | `agents.js` evidence toolbar | `GET /api/agents/evidence?finding_id=` | `tradinglib/agents/findings.py` `EvidenceGraph.build(finding_id=…)` | Wired |
| E72 | Evidence Explorer: 7-column lineage graph (Source→Claim→Finding→Hypothesis→Experiment→Validation→Decision) with node buttons | `agents.js` `renderEvidence()` | `GET /api/agents/evidence` | `tradinglib/agents/findings.py` `EvidenceGraph.build()` — every edge is a stored foreign key | Wired |
| E73 | Evidence Explorer node inspector (confidence, freshness, produced by, audit events, upstream/downstream) | `agents.js` node inspector | `GET /api/agents/evidence` | `tradinglib/agents/findings.py` `LineageNode` | Wired |
| E74 | Evidence focus: All relationships / Upstream / Downstream | `agents.js` evidence toolbar | `GET /api/agents/evidence?focus=` | `tradinglib/agents/findings.py` `EvidenceGraph.related()` | Wired |
| E74a | Node inspector `Open originating run` (REFERENCE 5277, `data-v5-route="runs"`) | `agents.js:2781` | `GET /api/agents/runs/{id}` | `tradinglib/agents/runner.py` | Wired |
| E74b | Node inspector `Open audit trail` (REFERENCE 5277, `data-v5-route="audit"`) | `agents.js:2788` | `GET /api/agents/audit?run_id=` | `tradinglib/agents/audit.py` | Wired |

### E.5 Knowledge — REFERENCE 1885–1894

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| E75 | Knowledge search input | `agents.js` | `GET /api/agents/knowledge?q=` | `tradinglib/agents/knowledge.py` | Wired |
| E76 | Knowledge list `#knowlist` | `agents.js` | `GET /api/agents/knowledge` | `tradinglib/agents/knowledge.py` | Wired |
| E77 | Knowledge detail `#knowdetail` | `agents.js` | `GET /api/agents/knowledge/{id}` | `tradinglib/api/routes/agents.py:1142` | Wired |
| E78 | Create / update / delete a knowledge entry | `agents.js` | `POST`/`PUT`/`DELETE /api/agents/knowledge` | `tradinglib/api/routes/agents.py:1133,1150,1164` | Wired |

### E.6 Approval Review — REFERENCE `vendor/agent-control-v5-approval.js` 5224–5250

The gate below is not a rendering of approval state: it blocks a real run. See
the "Approval gate, exercised end to end" note at the top of this file for the
observed run that these rows were verified against.

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| E79 | Decision dossier (thesis, mechanism, support, contradictions, disagreements, warnings) | `agents.js:2370` | `GET /api/agents/approvals/{id}` | `tradinglib/agents/approvals.py` | Wired |
| E80 | Evidence spine | `agents.js:2306` | same | `tradinglib/agents/approvals.py` | Wired |
| E81 | Reproducibility block | `agents.js:2415` | `GET /api/agents/runs/{id}` | `tradinglib/agents/runner.py` | Wired |
| E82 | `Open run artifacts` | `agents.js:2419` | `GET /api/agents/runs/{id}/artifacts` | `tradinglib/api/routes/agents.py:847` | Wired |
| E83 | Decision rationale textarea (minimum length) | `agents.js:2451` | — | — | Wired |
| E84 | `Conditions or review trigger` input | `agents.js:2457` | folded into the decision comment | `tradinglib/agents/approvals.py` | Wired |
| E85 | `Approve for paper research` → resumes the run | `agents.js:2462,2495` | `POST /api/agents/approvals/{id}` `{decision:"approve"}` | `tradinglib/api/routes/agents.py:966`, `tradinglib/agents/approvals.py` | Wired |
| E86 | `Reject candidate` → kills the run | `agents.js:2463,2496` | same, `{decision:"reject"}` | same | Wired |
| E87 | `Request more validation` + `Validation request target` select | `agents.js` `renderDecisionPanel()` | `POST /api/agents/approvals/{id}` (`decision: request_validation`), `GET /api/agents/approvals/validation-targets` | `tradinglib/agents/approvals.py` `ApprovalGate.request_validation()` — not a third verdict: the request stays pending and the run stays blocked, and the ask is recorded on the approval and in the audit log | Wired |
| E88 | `Trace` from a finding into the Evidence Explorer | `agents.js` findings table | `GET /api/agents/evidence?finding_id=` | `tradinglib/agents/findings.py` `EvidenceGraph` | Wired |
| E89 | Stage filter `All stages` on the dossier | `agents.js` | `GET /api/agents/runs/{id}` stages | `tradinglib/agents/runner.py` | Wired |
| E90 | Decided-state panel (who, when, rationale) | `agents.js:2440` | `GET /api/agents/approvals/{id}` | `tradinglib/agents/approvals.py` | Wired |

### E.7 Agent Studio — REFERENCE `features/agent-studio-merge.js` 6865–7240

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| E91 | Kind filter (All kinds / Pipeline stage / Standing worker) | `agents.js` `renderStudio()` (`[data-asm-kind]`) | `GET /api/agents/definitions` | `tradinglib/agents/registry.py` | Wired |
| E91a | `views.agents` toolbar `Search agents` input + `Status` select (REFERENCE `vendor/agent-control-v5-agents.js` 5257, `data-agent-query` / `data-agent-status`) | — | — | — | N/A (`features/agent-studio-merge.js` 6865–7240 replaces that view's markup with the `.asm-filters` kind chips E91 reproduces, and sets `views.agents.change` / `.input` to `() => undefined` at 7163–7164, so the search box and status select are dead in the composed reference app) |
| E92 | Agent inventory table (Agent, Kind, Status·last active, Ver, Model, Eval, Fail, Spend) | `agents.js:2555–2600` | `GET /api/agents/definitions` (+ per-agent run stats) | `tradinglib/agents/registry.py`, `runner.py` | Wired |
| E93 | Detail tabs Overview / Config / Cycles / Runs | `agents.js:2621` | `GET /api/agents/definitions/{id}`, `/runs?agent_id=` | `tradinglib/agents/registry.py`, `runner.py` | Wired |
| E94 | System instructions / prompt editing | `agents.js:2717` | `PUT /api/agents/definitions/{id}` | `tradinglib/agents/registry.py` | Wired |
| E95 | Model, budget, timeout, retries, tools, permissions fields | `agents.js:2676–2840` | `PUT /api/agents/definitions/{id}`, `GET /api/agents/models` | `tradinglib/agents/registry.py`, `providers.py` | Wired |
| E96 | Create agent / duplicate agent / delete agent | `agents.js` | `POST /api/agents/definitions`, `…/duplicate`, `DELETE …` | `tradinglib/api/routes/agents.py:525,588,580` | Wired |
| E97 | `Open Pipeline →` / `See Cycles →` cross-links | `agents.js:2622` | — | — | Wired |
| E98 | `Version history` list | `agents.js` Agent Studio → Versions tab | `GET/POST /api/agents/definitions/{id}/versions`, `POST …/versions/{v}/restore` | `tradinglib/agents/registry.py` `AgentVersionRow` + `versions()`/`version()`/`save_version()`/`restore()` — every edit freezes the configuration it replaced, and a restore moves history forward rather than rewinding it | Wired |
| E98a | Config tab `PRESET` chips (Fast / Quick / Full) on the loaned `#cfgform` (REFERENCE `data-preset`) | `agents.js` — `renderAgentConfig()` renders `[data-preset]` chips from the definition; `presetValue()` layers the selected preset over the stored values | `GET /api/agents/definitions` (`presets`), `PUT /api/agents/definitions/{id}` | `tradinglib/agents/registry.py` — `AgentDefinition.presets`, `PRESET_FIELDS`, `AgentRegistry._clean_presets()`, shipped dials in `_EFFORT_PRESETS` (backfilled onto existing rows by `_backfill_presets()` without bumping the version) | Wired (a preset may only move the cost/latency knobs and scope — never the prompt, model or tool grants; selecting one rewrites the form, saving persists it as an ordinary edit) |
| E98b | `#ag-config` header `↺ RESET` / `▣ SAVE` (REFERENCE 1990) | — | — | — | N/A (the TL agent-policy subpage was culled by `features/agent-center-tl-tabs.js`; only `#cfgsel`/`#cfgform` are loaned into Agent Studio, and its own save is E94/E95) |

### E.8 Audit Log — REFERENCE 1897–1923, 5286

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| E99 | Immutable audit table (Time, Actor, Action, Object, Result) | `agents.js` audit view | `GET /api/agents/audit` | `tradinglib/agents/audit.py` | Wired |
| E100 | Object buttons routing to run / approval | `agents.js` | `GET /api/agents/runs/{id}` / `/approvals/{id}` | `tradinglib/agents/runner.py`, `approvals.py` | Wired |
| E101 | Audit filter (by run) | `agents.js` (`auditRunId`) | `GET /api/agents/audit?run_id=` | `tradinglib/api/routes/agents.py:1175` | Wired |
| E102 | Guardrail list with `enforced by` | `agents.js` configuration tab | `GET /api/agents/audit` (`guardrails`) | `tradinglib/agents/approvals.py` `AgentPolicy.guardrails()` — seven rows, each naming the code that enforces it | Wired |
| E103 | Audit log `export CSV / JSON` | `agents.js` `exportAudit()` | `GET /api/agents/audit/export?format=csv` / `…=json` | `tradinglib/agents/audit.py` `AuditLog.export()` returning body, media type and filename | Wired |

### E.9 Configuration — REFERENCE 5286 (`views.configuration`)

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| E104 | `Candidate disposition` (locked "Mandatory human approval") | `agents.js:3000` | `GET /api/agents/config` | `tradinglib/agents/approvals.py` | Wired |
| E105 | Configuration · `Create findings and hypotheses` autonomy select | `agents.js` `policySelect()` | `GET/PUT /api/agents/config` (`research_generation`) | `tradinglib/agents/approvals.py` `AgentPolicy`; enforced in `LibraryToolset._require_autonomy()` | Wired |
| E106 | Configuration · `Run sandbox experiments` autonomy select | `agents.js` `policySelect()` | `GET/PUT /api/agents/config` (`sandbox_experiments`) | `tradinglib/agents/approvals.py` `AgentPolicy`; enforced on the `run_backtest` tool | Wired |
| E107 | `Daily research budget` input | `agents.js:3025` | `PUT /api/agents/config` | `tradinglib/config.py` budgets, `tradinglib/agents/runner.py` enforcement | Wired |
| E108 | Configuration · `Maximum concurrent runs` | `agents.js` configuration tab | `GET/PUT /api/agents/config` (`max_concurrent_runs`) | `tradinglib/agents/runner.py` `check_concurrency()`, enforced in `start()` | Wired |
| E109 | Configuration · `Artifact retention` select | `agents.js` `policySelect()` | `GET/PUT /api/agents/config` (`artifact_retention`) | `tradinglib/agents/approvals.py` `AgentPolicy` + `RETENTION_CHOICES` | Wired |
| E110 | Configuration · notification `Approval requests` | `agents.js` `policySelect()` | `GET/PUT /api/agents/config` (`notify_approval_requests`) | `tradinglib/agents/approvals.py` `AgentPolicy` + `NOTIFICATION_CHOICES` | Wired |
| E111 | Configuration · notification `Failed validation` | `agents.js` `policySelect()` | `GET/PUT /api/agents/config` (`notify_failed_validation`) | `tradinglib/agents/approvals.py` `AgentPolicy` + `NOTIFICATION_CHOICES` | Wired |
| E112 | Configuration · notification `Stale evidence` | `agents.js` `policySelect()` | `GET/PUT /api/agents/config` (`notify_stale_evidence`) | `tradinglib/agents/approvals.py` `AgentPolicy` + `NOTIFICATION_CHOICES` | Wired |
| E113 | Per-run / monthly budget inputs + `Save budgets` | `agents.js:3022,3028,3032` | `PUT /api/agents/config` | `tradinglib/config.py`, `tradinglib/agents/runner.py` | Wired (TradingLib addition beyond REFERENCE) |
| E114 | Spend panel (today / month / all time / charges) | `agents.js:3039` | `GET /api/agents/config` | `tradinglib/agents/runner.py:spend` | Wired |
| E115 | Providers panel (available / verified / model count) | `agents.js:3063` | `GET /api/agents/config`, `GET /api/agents/models` | `tradinglib/agents/providers.py` (SPEC D3 honesty) | Wired |
| E116 | Credentials panel (configured / not configured) | `agents.js:3078` | `GET /api/agents/config.keys` | `tradinglib/config.py` | Wired |
| E117 | AutoPipeline timer status | `agents.js:3087` | `GET /api/agents/config.autopipeline` | `tradinglib/agents/autopipeline.py` | Wired |
| E118 | Header `+ New run` button (REFERENCE 969) | `agents.js` (Pipeline `Run`) | `POST /api/agents/pipelines/{id}/run` | `tradinglib/agents/runner.py` | Wired |
| E119 | Header `3 active · 1 idle` badge | `agents.js:376,538` | `GET /api/agents/runs`, `/definitions` | `tradinglib/agents/runner.py` | Wired |

---

## F. Market & Data (`#view-data`)

REFERENCE `vendor/market-data-v2.js` (4067–4261), `vendor/source-catalog-v3.js` (4262–4492),
`features/universe-pit.js` (7486–7831), `features/screener.js` (7832–8302),
`features/poll-interval.js` (8303–8463), `features/data-quality.js` (8464–8650),
`features/active-subs.js` (8651–8813).

### F.1 Universes & Watchlists

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| F1 | `+ New watchlist` / `+ New universe` toolbar buttons | `ui/js/pages/data.js` (`newWatchlist`, `newUniverse`) | opens the collection dialog | — | Wired |
| F2 | Collection dialog: type segmented, name, description, construction method, refresh, symbols/rules, `Make default` | `data.js:1078–1090` | `POST /api/universes`, `POST /api/watchlists` | `tradinglib/data/universe.py`, `tradinglib/api/routes/watchlists.py:87` | Wired |
| F3 | Collections table (Name, Type, Construction, Symbols, Refresh, Status) | `data.js:556–560` | `GET /api/universes`, `GET /api/watchlists` | `tradinglib/data/universe.py` | Wired |
| F4 | Row select → membership panel | `data.js:841` | `GET /api/universes/{id}/members`, `GET /api/watchlists/{id}` | `tradinglib/api/routes/universes.py:199` | Wired |
| F5 | Rename / edit a collection | `data.js:1078,1082` | `PUT /api/universes/{id}`, `PUT /api/watchlists/{id}` | `tradinglib/api/routes/universes.py:169` | Wired |
| F6 | Delete a collection | `data.js:804,805` | `DELETE /api/universes/{id}`, `/api/watchlists/{id}` | `tradinglib/api/routes/universes.py:191` | Wired |
| F7 | Make active (global selector) | `data.js:771` | `POST /api/universes/active` | `tradinglib/api/routes/universes.py:145` | Wired |
| F8 | Refresh a built-in universe | `data.js:782` | `POST /api/universes/{id}/refresh` | `tradinglib/data/universe.py` | Wired |
| F9 | PIT as-of year slider `#asof` | `data.js:689,720,835` | `GET /api/universes/{id}/members?as_of=` | `tradinglib/data/universe.py` (snapshots + membership dates) | Wired |
| F10 | Membership table (TICKER, NAME, IN UNIVERSE, STATUS TODAY) | `data.js:856` `renderMembership` | same | `tradinglib/data/universe.py` | Wired |
| F11 | `PIT ✓` basis chip | `data.js:876` | same (`snapshot` / `membership-dates` / none) | `tradinglib/data/universe.py` | Wired |
| F12 | Survivorship note | `data.js:700,939` | derived from the membership basis | `tradinglib/data/universe.py` | Wired |
| F13 | Take snapshot / delete snapshot | `data.js:791,930` | `POST /api/universes/{id}/snapshot`, `DELETE …/snapshots/{as_of}` | `tradinglib/api/routes/universes.py:221,242` | Wired |
| F14 | CHANGE LOG · LAST REBALANCE (+ IN / − OUT / Δ CORP) | `data.js` — `renderChangeLog()` prints `+ IN` / `− OUT` with the rule in force, `Δ CORP` per detected action, and `renderRuleFooter()` the rule revision | `GET /api/universes/{id}/changelog` | `tradinglib/data/universe.py` — `UniverseRegistry.change_log()` diffs consecutive snapshots, `rule_revisions()` versions the construction rule (snapshots carry a point-in-time `rule` column), and `detect_corporate_actions()` pairs an add with a drop when SEC EDGAR resolves both tickers to one CIK (this is what identifies GOOG/GOOGL) | Wired (a pairing EDGAR cannot confirm is simply absent, never guessed) |
| F15 | Coverage tiles incl. **OPTIONS IV** | `data.js` `loadCoverage()` | `GET /api/quality/datasets` | `YFinanceProvider.get_option_iv()` (ATM call/put per expiry, linearly interpolated to 30d, payload states the method); `LibraryConnector` `options` dataset writing `options/iv30_{SYMBOL}`; built-in source `options-iv` | Wired (daily; backfill disabled because an option chain is a snapshot, not history) |
| F16 | Watchlist bell/alert column (REFERENCE `features/universe-pit.js` 7680) | — | — | `tradinglib/research/screen/alerts.py` exists, but is scoped to screens | N/A (the block it lives in is behind `const INCLUDE_TL_LISTS = false` at REFERENCE 7508, so the bell column never renders in the composed reference app; TradingLib's alerting watches screen membership, not watchlists) |

### F.2 Screener

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| F17 | Saved screens list + select | `data.js:1127` | `GET /api/screens` | `tradinglib/research/screen/rules.py` | Wired |
| F18 | `+ NEW SCREEN` | `data.js:1626` | `POST /api/screens` | `tradinglib/api/routes/screens.py:134` | Wired |
| F19 | Base-universe picker | `data.js` | `GET /api/universes` | `tradinglib/data/universe.py` | Wired |
| F20 | CRITERIA rows (metric, comparator, value) | `data.js:1537` | `GET /api/screens/metrics` | `tradinglib/api/routes/screens.py:154` | Wired |
| F21 | `+ Add rule` | `data.js:1190` | client + `POST /api/screens/preview` | `tradinglib/research/screen/rules.py` | Wired |
| F22 | `RANK BY composite ▾` | `data.js:1191` | `PUT /api/screens/{id}` (rank spec) | `tradinglib/research/screen/rules.py` | Wired |
| F23 | PASS FUNNEL live counts per rule | `data.js:1547,1573` | `POST /api/screens/preview` per rule prefix | `tradinglib/research/screen/funnel.py` | Wired |
| F24 | `VIEW AS QUERY ⌄` toggle + query text | `data.js:1194,1272,1581` | `POST /api/screens/preview` returns the query string | `tradinglib/research/screen/rules.py` | Wired |
| F25 | `Run screen` | `data.js:1643` | `POST /api/screens/{id}/run` | `tradinglib/api/routes/screens.py:269` | Wired |
| F26 | Results table (rank, ticker, composite, metric columns) | `data.js` | `GET /api/screens/runs/{id}` | `tradinglib/research/screen/funnel.py` | Wired |
| F27 | RESULT PROFILE (sector mix, median mkt cap, turnover, overlap with live book) | `data.js` `#rp-overlap` | `GET /api/screens/runs/{id}` | `tradinglib/research/screen/funnel.py` `live_book_overlap()` — survivors held in `tradinglib.trade`'s `PortfolioStore`, read-only, with the source named or the reason overlap cannot be computed | Wired |
| F28 | `→ BACKTEST THIS SCREEN` → real queued backtest + toast | `data.js:1744,1215` | `POST /api/screens/{id}/queue-backtest` | `tradinglib/api/routes/screens.py:291`, `tradinglib/research/backtest.py` | Wired (SPEC §6 requirement) |
| F29 | `SAVE AS UNIVERSE` | `data.js:1216` | `POST /api/universes` from the run's members | `tradinglib/data/universe.py` | Wired |
| F30 | `PIN + ALERT ON CHANGES` | `data.js:1924` `pinScreen()` + `data.js:1955` `loadScreenAlerts()` / `#screenAlerts` | `PUT /api/screens/{id}` (tags `pinned`+`alert`), `GET /api/screens/alerts`, `POST /api/screens/alerts/check`, `POST /api/screens/alerts/{id}/acknowledge`, WS `notifications` | `tradinglib/research/screen/alerts.py` (`ScreenAlertMonitor`, started by the lifespan) | Wired (a pinned screen is re-run on a cadence, its survivors diffed against the previous stored run, and each entry/exit persisted as an alert and published on `notifications`; the panel lists them with the symbols that moved and an Acknowledge control) |
| F31 | Screen update / delete | `data.js:1618` | `PUT`/`DELETE /api/screens/{id}` | `tradinglib/api/routes/screens.py:236,248` | Wired |

### F.3 Source Catalog

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| F32 | Catalog source list with status pills | `data.js:1840` `renderCatalog` | `GET /api/ingest/sources` | `tradinglib/data/ingest/catalog.py` | Wired (real probe statuses, not a hard-coded six-adapter dictionary) |
| F33 | `+ Request new source` | `data.js:1868` | routes to the ingestion form | — | Wired |
| F34 | Protocol key legend | `data.js:206` | — | — | N/A (static legend) |
| F35 | Source detail head + status | `data.js:1883` | `GET /api/ingest/sources/{id}` | `tradinglib/data/ingest/catalog.py` | Wired |
| F36 | `Credentials` button | `data.js:1917` | reports `auth.configured` and routes to Settings | `tradinglib/config.py` | Wired |
| F37 | Protocols-supported pills | `data.js:1933` | `GET /api/ingest/sources/{id}` | `tradinglib/data/ingest/connectors/*` | Wired |
| F38 | Datasets grid → select to configure | `data.js:1954` — subscriptions of the source | `GET /api/ingest/subscriptions` | `tradinglib/data/ingest/catalog.py` | Wired |
| F39 | Observed state (rows ingested, last ingest, last error) | `data.js:1989` | `GET /api/ingest/sources/{id}` | `tradinglib/data/ingest/catalog.py` | Wired |
| F40 | Subscription `FEED MODE` (live / scheduled) | `data.js` subscription form | `GET/POST /api/ingest/subscriptions` | `tradinglib/data/ingest/catalog.py` `Subscription.feed_mode` persisted; the API returns `feed_mode_supported` / `feed_mode_detail` | Wired (a REST source asked for `live` is told it will be polled instead of being silently downgraded) |
| F41 | Subscription: `Channel / dataset` input | `data.js:2037` | `POST /api/ingest/subscriptions` | `tradinglib/data/ingest/catalog.py` | Wired (TradingLib uses the connector's real channel name) |
| F42 | Subscription: `HISTORICAL / BACKFILL` protocol chips (`data-hp`) + `FROM` / `TO` inputs (`#bf-from`, `#bf-to`) | `data.js:2396` `renderBackfillBlock()` + `runBackfill()`; live progress on the `ingest` channel | `POST /api/ingest/sources/{id}/backfill?range=&end=` | `tradinglib/data/ingest/scheduler.py` `backfill(range=, end=)` → `Connector.backfill(range, batch_size, end)` (rest / polling / database / file / library) | Wired (chips come from `Protocol.can_backfill`, so a WebSocket source says it has no history endpoint instead of offering a dead control; FROM takes a duration, `all`, an ISO date or a quarter, TO takes `now` or a date, and an unparseable TO is a 422 rather than a silent "now") |
| F43 | Subscription: `Poll interval` preset buttons (REFERENCE `features/poll-interval.js` 8383, `[data-poll]`) | `data.js` `renderPollBlock()` | `PUT /api/ingest/sources/{id}` (schedule) | `tradinglib/data/ingest/scheduler.py` | Wired |
| F43a | Subscription: `Poll interval` **custom** input (REFERENCE `#pollCustom`, 8387) — type an interval the presets do not offer | `data.js` `renderPollBlock()` renders `input#pollCustom.poll-custom` beside the `[data-poll]` presets; `input` re-runs `renderPollNote()` (request cost, and whether the cadence matches the selected bar frequency) without losing the caret, Enter/blur commits through `setSourceSchedule()`; `toSeconds()` refuses an unparseable token client-side with the reference's own wording | `POST /api/ingest/sources` (the source's `schedule`, the same path the presets use) → `GET /api/ingest/sources/{id}` | `tradinglib/data/ingest/source.py` `parse_schedule()` accepts any `<number><s|m|h|d|w>` token and `SourceConfig.__post_init__` validates it, so a bad cadence is a 400 and never reaches the database; `Schedule.label` names custom intervals in their own units (`Every 45s`, `Every 4h`) and `IngestScheduler.add_source()` re-registers the job at the new period — when the cadence really changed (`1d` → `24h` does not count) the *pending* fire is re-derived as `last_run_at + new period`, the same arithmetic every completed run uses, so shortening makes an overdue job due at once and lengthening pushes it back instead of firing on the old timer | Wired |
| F43b | Subscription: per-dataset `Frequency` chips (REFERENCE `source-catalog-v3.js` 4396, `[data-catalog-frequency]`) — the dataset's bar granularity (`1m`/`5m`/`1h`/`1d`, `Quarterly`/`Annual`) | `data.js` `renderFrequencyBlock()` renders `button.frequency-option[data-catalog-frequency]` chips with `aria-pressed`, from the source's own options; `renderFrequencyNote()` states whether the choice reaches the fetch or is advisory, and a chip click re-runs the poll-interval cross-check | `POST /api/ingest/subscriptions` / `GET /api/ingest/subscriptions` (`granularity`, `granularity_applied`, `granularity_detail`, `granularity_options`); `GET /api/ingest/sources` carries `granularity_options` / `granularity_default` | `tradinglib/data/ingest/catalog.py`: `Subscription.granularity` + the `ingest_subscriptions.granularity` column (migrated by `db.ensure_columns`), `GRANULARITY_OPTIONS` per `DataDomain`, `parse_granularity()` and a domain check in `save_subscription()` (a price feed cannot be subscribed `quarterly`). The value really reaches the fetch where it can: `scheduler.requested_interval()` reads the active subscriptions and passes `interval=` to `LibraryConnector.fetch()`, which uses it for the `bars` dataset. `granularity_applied` is the scheduler's own answer, not a second guess at it: the API's `_granularity_effect()` and `requested_interval()` share `catalog.agreed_bar_interval()` (which subscriptions vote) and `catalog.granularity_reaches_fetch()` (whether the source's real dataset, resolved by `connectors/library.effective_dataset()`, takes a bar size), so all three ways a value cannot reach the vendor call are reported `false` with the reason — the fetch path has no bar-size parameter (EDGAR filings, a reporting cadence), the subscription is paused, or two active subscriptions ask for different bars and neither is imposed | Wired |
| F44 | Subscription: `Universe / watchlist scope` select + custom symbols | `data.js:2054,2066` | `POST /api/ingest/subscriptions` | `tradinglib/data/ingest/catalog.py` | Wired |
| F45 | `PRESERVE POINT-IN-TIME` toggle | `data.js` ingestion form | `POST /api/ingest/sources` | `tradinglib/data/ingest/scheduler.py` `_drop_restated_bars()` / `_drop_restated_series()` — a vendor restatement of an already-stored timestamp is refused | Wired |
| F46 | Subscription estimate line | `data.js:2074` | `GET /api/ingest/sources/{id}` (real rows ingested / last ingest / backfill) | `tradinglib/data/ingest/catalog.py` | Wired |
| F47 | `+ Add subscription` | `data.js:2224` | `POST /api/ingest/subscriptions` | `tradinglib/api/routes/ingest.py:297` | Wired |
| F48 | `Dry run` | `data.js:2248` | `POST /api/ingest/sources/{id}/test` | `tradinglib/data/ingest/connectors/base.py:test()` | Wired (a real connection test, not a timed fake toast) |
| F49 | `Use custom configuration` → prefills the ingestion form | `data.js:2267` | — | — | Wired |
| F50 | Delete a subscription | `data.js:2324` | `DELETE /api/ingest/subscriptions/{id}` | `tradinglib/api/routes/ingest.py:312` | Wired |

### F.4 Data Ingestion form (SPEC §4 1:1 field mapping)

| # | REFERENCE field | TradingLib UI module | Endpoint | `SourceConfig` field | Status |
| --- | --- | --- | --- | --- | --- |
| F51 | `Source name` | `data.js:245` | `POST /api/ingest/sources` | `name` | Wired |
| F52 | `Data domain` | `data.js:246` | same | `data_domain` | Wired |
| F53 | `Protocol / connector` (+ protocol-dependent endpoint label/placeholder/hint/frequency) | `data.js:248,2513` | same | `protocol` → connector class | Wired |
| F54 | `Endpoint` | `data.js:251` | same | `endpoint` | Wired |
| F55 | `Authentication` | `data.js:253` | same | `auth` (`tradinglib/data/ingest/auth.py`) | Wired |
| F56 | `Format` | `data.js:253` | same | `format` (`tradinglib/data/ingest/formats.py`) | Wired |
| F57 | `Frequency / schedule` | `data.js:255` | same | `schedule` | Wired |
| F58 | `Scope` | `data.js:255` | same | `scope` / `scope_label` | Wired |
| F59 | `Source timezone` | `data.js:257` | same | `source_tz` | Wired |
| F60 | `Retry / reconnect policy` | `data.js:257` | same | `retry_policy` | Wired |
| F61 | `Enable historical backfill on first connection` | `data.js:253,2513`; `saveSource()` reports what the save actually did | `POST /api/ingest/sources` → `backfill` in the response | `tradinglib/api/routes/ingest.py` — `_start_first_connection_backfill()` starts `IngestScheduler.backfill()` in the background on the first save of an enabled source whose protocol can backfill; `tradinglib/data/ingest/catalog.py` `record_backfill()` stamps `SourceState.backfilled_at` so it fires once, and a later save answers "already backfilled" | Wired (a save that cannot start one says why — paused, protocol has no backfill, scheduler down, already run) |
| F62 | `Backfill range` (+ custom start date) | `data.js:255,258,2587` | same | `backfill.range` | Wired |
| F63 | `Backfill batch size` | `data.js:256` | same | `backfill.batch` | Wired |
| F64 | `Advanced configuration` textarea (headers, subscriptions, SQL, glob, pagination, schema mapping) | `data.js:259,2548` | same | `advanced` | Wired |
| F65 | `Test connection` | `data.js:2727` | `POST /api/ingest/sources/test` | `Connector.test()` (`tradinglib/data/ingest/connectors/base.py`) | Wired (real result: reachability, auth, latency, sample record, inferred schema) |
| F66 | `Save data source` | `data.js:2792` | `POST /api/ingest/sources` | `tradinglib/data/ingest/catalog.py:save` | Wired |
| F67 | Configured-sources table (Source, Domain, Protocol, Frequency, Backfill, Freshness, Coverage, Status) | `data.js:2400–2430` | `GET /api/ingest/sources` | `tradinglib/data/ingest/catalog.py` | Wired |
| F68 | Row action `Test` | `data.js:2445` | `POST /api/ingest/sources/{id}/test` | `tradinglib/data/ingest/connectors/*` | Wired |
| F69 | Row action `Edit` | `data.js:2185` | `PUT /api/ingest/sources/{id}` | `tradinglib/data/ingest/catalog.py` | Wired |
| F70 | Row action `Pause` | `data.js:2470` | `POST /api/ingest/sources/{id}/enabled` | `tradinglib/data/ingest/scheduler.py` | Wired |
| F71 | Source delete | `data.js:2486` | `DELETE /api/ingest/sources/{id}` | `tradinglib/api/routes/ingest.py:161` | Wired |
| F72 | ACTIVE SUBSCRIPTIONS table (Source, Datatype, Protocol, Freq, Range/Mode, Last ingest, Status) | `data.js:213,1810` | `GET /api/ingest/subscriptions`, WS `ingest` | `tradinglib/data/ingest/catalog.py`, `tradinglib/api/live.py` | Wired |
| F73 | Ingestion-lifecycle note | `data.js:269` | — | — | N/A (static prose) |

### F.5 Data Quality

| # | REFERENCE element | TradingLib UI module | Endpoint / channel | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| F74 | Stat cards OVERALL QUALITY / STALE SERIES / MISSING VALUES / FAILED CHECKS | `data.js:2827` `renderQuality` | `GET /api/quality/summary` | `tradinglib/data/quality.py` | Wired |
| F75 | CHECK SUITE table + `LAST RUN <time>` | `data.js:2900` `renderCheckSuite` | `GET /api/quality/series`, `/api/quality/datasets`, `/api/quality/summary` | `tradinglib/data/quality.py` | Wired (the table itself; the individual REFERENCE checks are F84–F90) |
| F76 | `Run checks` (whole suite) / `Check this dataset` | `data.js:2957,2988` | `POST /api/quality/run` | `tradinglib/data/quality.py` | Wired |
| F77 | Quality-incident table + `Resolve` | `data.js:2855,2879` | `GET /api/quality/incidents`, `POST /api/quality/incidents/{id}/resolve` | `tradinglib/data/quality.py` | Wired |
| F78 | Outlier queue with per-point QUARANTINE / ACCEPT VALUE | `data.js` quality tab | `GET /api/quality/outliers`, `POST /api/quality/outliers/{id}/decide` | `quality_outliers` table + decimal-shift heuristic; quarantine is enforced at the store level through `set_quarantine_filter()`, consulted by `read_bars()` / `read_series()` | Wired (verified on `fred/UNRATE`: 942 → 941 → 942 points) |
| F78b | Outlier queue third decision `Escalate` (REFERENCE `features/data-quality.js` 8497, `DECISIONS` = quarantine / accept / **human**) | `data.js` `outlierRow()` renders Quarantine / Accept value / **Escalate** (the last highlighted when the detector's own suggestion is `human`); an escalated row stays in the queue showing "escalated by … — value still in the data, awaiting a human" with a `clear escalation` control, and `renderQualityNote()` says the raw values are still in use | `POST /api/quality/outliers/{id}/decide` accepts `quarantine`, `accept`, `escalate`, `open` (a `Literal`, so anything else is a 422); `GET /api/quality/outliers` returns `needs_review` / `escalated` per row and `counts.awaiting_review` | `tradinglib/data/quality.py`: `OUTLIER_DECISIONS` maps `escalate` → the persisted `escalated` state, `OPEN_OUTLIER_STATES` keeps escalated rows in the `include_decided=false` queue, and `quarantined()` is untouched — escalation records who asked and when without removing the observation from any store read (verified on `series:fred:UNRATE`: 942 points before and after) | Wired |
| F78a | Outlier `undo` on a decided point (REFERENCE `features/data-quality.js`, `data-undo`) | `data.js:3305` — `undo` button re-opens the decision via `decideOutlier(row, "open", …)` | `POST /api/quality/outliers/{id}/decide` (`decision: "open"`) | `tradinglib/data/quality.py`; the quarantine filter is re-applied so readers see the point again | Wired |
| F79 | "Quarantined points are excluded from features" guarantee | `data.js` quality tab | `POST /api/quality/outliers/{id}/decide` | enforced in `tradinglib/data/store.py` via the quarantine filter hook, so every reader inherits it rather than each caller remembering | Wired |
| F80 | Live incident updates | `data.js:506` | WS `quality` | `tradinglib/data/quality.py` → `tradinglib/api/live.py` | Wired |
| F81 | Source SLA panel | `data.js` quality tab | `GET /api/quality/sla` | `QualityEngine.sla_report()` reusing `catalog.stale_sources()`, so SLA rows and the stale-source figures cannot disagree | Wired |
| F82 | Data Quality check · `Lookahead audit` | `data.js` check suite | `GET /api/quality/summary`, `POST /api/quality/run` | `tradinglib/data/quality.py` lookahead scan; a pass/fail integrity gate that opens an incident | Wired |
| F83 | Data Quality check · `Schema drift` | `data.js` check suite | `GET /api/quality/summary` | per-dataset baseline persisted in `quality_schemas`; six checks total | Wired |
| F84 | Check `Gap scan` (all price series) | `data.js:2936` | `GET /api/quality/series` | `tradinglib/data/quality.py:check_completeness` | Wired |
| F85 | Check `Outlier detection` (fundamentals-q) | `data.js:2936` | same | `tradinglib/data/quality.py:check_outliers` | Wired |
| F86 | Check `Stale tickers` | `data.js:2936` | same | `tradinglib/data/quality.py:check_staleness` | Wired |
| F87 | Data Quality check · `Dupe rows` | `data.js` check suite | `GET /api/quality/summary` | `tradinglib/data/quality.py` duplicate-row check; a pass/fail integrity gate that opens an incident | Wired |
| F88 | Check-suite per-row `REVIEW` / `LIST` actions | `data.js` check suite — named rows with per-row actions | `GET /api/quality/incidents`, `GET /api/quality/series` | `tradinglib/data/quality.py` | Wired |
| F89 | Per-row `LIST` action (list the stale tickers) | `data.js` check suite | `GET /api/quality/series` | `tradinglib/data/quality.py` | Wired |
| F90 | `TICKET` action on a failing check | `data.js` check suite | `GET/POST /api/quality/tickets` | `quality_tickets` table — honestly labelled *workspace* tickets; no external tracker is implied | Wired |

---

## G. Documentation (`#view-docs`)

REFERENCE line 990. SPEC §6: "can be static text — it is docs."

| # | REFERENCE element | TradingLib UI module | Endpoint | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| G1 | Methodology rows (Hypotheses and experiments / Backtest integrity / Validation gates) | `ui/js/pages/docs.js` | — | — | N/A (static prose, allowed by SPEC §7) |
| G2 | Product guide rows (Keyboard shortcuts / Agent autonomy) | `docs.js` | — | — | N/A |
| G3 | Build/health footer (TradingLib addition) | `docs.js:256` | `GET /api/health` | `tradinglib/api/app.py:575` | Wired |

---

## H. Settings (`#view-settings`)

REFERENCE line 992.

| # | REFERENCE element | TradingLib UI module | Endpoint | Backend | Status |
| --- | --- | --- | --- | --- | --- |
| H1 | `Toggle theme` button | `ui/js/pages/settings.js` (`data-theme` + real light palette) | `PUT /api/settings` | `tradinglib/api/routes/settings.py:421` | Wired (REFERENCE only flips an attribute; TradingLib actually restyles) |
| H2 | `Density` select | `settings.js` (`data-density`) | `PUT /api/settings` | `tradinglib/api/routes/settings.py` | Wired |
| H3 | `Timezone` select (drives the topbar clock) | `settings.js` (clock takeover) | `PUT /api/settings` | `tradinglib/api/routes/settings.py` | Wired |
| H4 | `Primary benchmark` select | `settings.js` | `PUT /api/settings` | `tradinglib/api/routes/settings.py` (used by `tradinglib/research/backtest.py`) | Wired |
| H5 | `Default universe` select | `settings.js` | `PUT /api/settings` | `tradinglib/data/universe.py` | Wired |
| H6 | API-keys entry, masked and write-only (SPEC §6 addition) | `settings.js` | `PUT /api/settings` | `tradinglib/config.py` (never returns a key) | Wired |
| H7 | Settings load on mount | `settings.js` | `GET /api/settings` | `tradinglib/api/routes/settings.py:413` | Wired |

---

## I. Backend surface with no REFERENCE element

Listed for completeness — SPEC §5 requires these, REFERENCE has no control for them.

| Endpoint | Backend | UI | Note |
| --- | --- | --- | --- |
| `GET/POST/DELETE /api/trade/*` | `tradinglib/trade/paper.py`, `alpaca_broker.py`, `ibkr_broker.py` | none | SPEC D4/§5: "Trade (no UI)". Correct by design. |
| `GET /api/ingest/backfills` | `tradinglib/api/routes/ingest.py` | none | Lists the backfills this process is running; the Source Catalog reads progress off the `ingest` channel instead, so there is no control for it. (`POST /api/ingest/sources/{id}/backfill` is no longer here: F42 wires it to the FROM/TO block.) |
| `POST /api/ingest/refresh`, `GET /api/ingest/scheduler`, `GET /api/ingest/summary` | `tradinglib/data/ingest/catalog.py`, `scheduler.py` | partly (`data.js` refreshes via `GET /api/ingest/sources`) | No dedicated control; not a REFERENCE element. |
| `GET /api/research/ranges` | `tradinglib/api/routes/research.py:38` | `stock.js` RANGES are hard-coded to the same list | Minor duplication, not a gap. |
| `GET /` and every `ui/` static asset | `tradinglib/api/app.py` `_NoCacheStaticFiles` | the SPA itself | Not a REFERENCE element. Served with `Cache-Control: no-cache, must-revalidate` so an edited module is picked up on the next reload instead of the browser re-running a cached copy; the conditional request still answers `304` when nothing changed. |

---

## J. REFERENCE surfaces outside the SPEC-locked page tree

`core/router.js` in REFERENCE registers five further TL pages that have **no rail
button** and are reachable only by typing a hash: `#tl/terminal`, `#tl/market`,
`#tl/rc`, `#tl/analytics`, `#tl/admin`. SPEC §3 locks the TradingLib page tree to the
seven workspaces and §6 defines the UI contract over those seven only, so these
were not built. They are listed here so the omission is explicit, not hidden.

| # | REFERENCE page | Content | Status |
| --- | --- | --- | --- |
| J1 | `#tl/terminal` (REFERENCE 998–1087) | An earlier Overview: tape, watchlist, system health, chart, fundamentals, market intelligence | N/A — superseded by `#overview` (section B), which covers every element |
| J2 | `#tl/market` (REFERENCE 1089–1291) | An earlier Market & Data: universe/PIT, screening, ingestion, quality | N/A — superseded by `#data` (section F), which covers every element |
| J3 | `#tl/rc` (REFERENCE 1493–1757) | Research Command: agent brief, cycle config, cycle timeline, cycle detail, hypotheses, experiments, validation, evaluation, promotion | N/A — its objects are covered by Strategy Lab (D) and Agent Center (E); the cycle-based agent brief / `▷ RUN AGENT · est $36` form is the same surface as E54–E60 |
| J4 | `#tl/analytics` (REFERENCE 2024–2077) | Correlation matrix, attribution, factor exposure, regime panel | **Not built** — no analytics workspace, no factor model, no attribution engine, no HMM regime model in `tradinglib/` |
| J5 | `#tl/admin` (REFERENCE 2079–2136) | Members & roles, audit log, data entitlements, compute & budgets, workspace policy | **Not built** — TradingLib is single-user: no members/roles/2FA, no entitlements model, no worker/queue telemetry. Budgets are covered by E107/E113/E114. |

---

## K. Result

Sections A–H enumerate **371** REFERENCE elements and dynamic values:

| Status | Rows |
| --- | --- |
| Wired | 360 |
| Partial | 0 |
| Missing | 0 |
| N/A (REFERENCE static prose, or scaffolding that never renders) | 11 |

**The matrix has no gaps left.** The five that remained after the build
workflow (D47, D47a, F43a, F43b, F78b) were closed on 2026-09-02:

* **D47 / D47a** — the Evaluation SCORECARD now renders all eight of REFERENCE's
  evidence rows (OOS Sharpe, Deflated Sharpe, Sweep stability, MC: P(Sharpe>1),
  MC: p95 max DD, WFA positive folds, Corr. to live book, Capacity est.; the
  GATES PASSED and VERDICT rows below them are frame, counted under D47). `Sweep stability`, `MC: P(Sharpe>1)`, `MC: p95 max DD` and
  `WFA positive folds` are joined from the newest *completed* sweep /
  Monte-Carlo / walk-forward analysis recorded against the candidate's backtest
  (`analyses.py`); `Corr. to live book` and `Capacity est.` are two new engines,
  `tradinglib/research/livebook.py` and `tradinglib/research/capacity.py`. A cell that
  cannot be computed renders `—` and carries the reason in its tooltip — "no
  sweep/MC/WFA analysis recorded", "no live book: …", "no usable volume history
  for …" — and every capacity figure carries its 5%-of-ADV participation
  assumption. No cell is ever filled with a placeholder number.
* **F43a** — `renderPollBlock()` renders `#pollCustom` beside the presets;
  intervals like `45s` / `2m` / `4h` / `1d` are validated in the browser and
  again by `parse_schedule()` before they can be stored, and they re-register
  the scheduler job on the source's existing schedule path — including the
  job's pending fire, which `add_source()` re-derives from the new cadence
  (`last_run_at` + new period) whenever the cadence actually changed.
* **F43b** — `Subscription.granularity` (migrated column), per-domain
  `GRANULARITY_OPTIONS`, `[data-catalog-frequency]` chips in `data.js`, and a
  fetch path that really uses the value for library `bars` sources; where it
  cannot be applied the API answers `granularity_applied: false` and says why.
  That answer is the scheduler's own rule, shared through
  `catalog.agreed_bar_interval()` / `granularity_reaches_fetch()`, so a paused
  subscription and two active subscriptions asking for different bars are both
  reported as not applied instead of being claimed.
* **F78b** — the outlier queue's third decision. `escalate` persists an
  `escalated` state that keeps the point *in* the queue and its value *in* the
  data, which is the whole difference between escalating and quarantining.

Section I lists three backend surfaces with no REFERENCE control; section J
lists five REFERENCE pages excluded by the SPEC-locked page tree, of which two
(`#tl/analytics`, `#tl/admin`) have no TradingLib equivalent at all — those two are
excluded from the totals above by the §3 page-tree argument, not because the
surfaces were built. J4's correlation matrix / attribution / factor exposure /
regime panel and J5's members-and-roles / entitlements / compute telemetry are
unbuilt, and a reader who counts REFERENCE surfaces rather than SPEC-§6
workspaces should count them as gaps.

### The eleven N/A rows, in full

| Row | Element | Why it is N/A |
| --- | --- | --- |
| A23 | Orphan shim `#ac-shim` controls | REFERENCE scaffolding for its verbatim vendor scripts; TradingLib has no vendor scripts to satisfy. |
| D50 | Evaluation "Add comment…" footer | Inert text in REFERENCE — no handler, no target. |
| D60 | Inert `#sl-*` subpages (REFERENCE 1482–1490) | REFERENCE scaffolding for `app-tl.js`; never rendered. |
| E35a | `views.runs` `Search runs` / `Status` toolbar | `control-room-merge.js` composes the Pipeline tab from `parts: ["pipeline"]` and aliases `#agents/runs` → `#agents/pipeline`, so that view never renders. |
| E91a | `views.agents` `Search agents` / `Status` toolbar | `agent-studio-merge.js` replaces the view's markup and nulls its `change`/`input` handlers. |
| E98b | `#ag-config` header `↺ RESET` / `▣ SAVE` (REFERENCE 1990) | The TL agent-policy subpage was culled by `features/agent-center-tl-tabs.js`; only `#cfgsel`/`#cfgform` are loaned into Agent Studio. |
| F16 | Watchlist bell/alert column | Behind `const INCLUDE_TL_LISTS = false` (REFERENCE 7508), so it never renders in the composed reference app. TradingLib's alerting (F30) watches screen membership, not watchlists. |
| F34 | Protocol key legend | Static legend. |
| F73 | Ingestion-lifecycle note | Static prose. |
| G1 | Methodology rows | Static prose, allowed by SPEC §7 ("Docs: can be static text — it is docs"). |
| G2 | Product guide rows | Same. |

### How to regenerate these totals

The table above is **derived**, not maintained by hand, and it goes stale the
moment a row's Status changes. Recount before hand-off:

```bash
.venv/bin/python - <<'EOF'
import collections, re, pathlib
head = pathlib.Path("COVERAGE.md").read_text().partition("## K. Result")[0]
counts, total = collections.Counter(), 0
for line in head.splitlines():
    if not re.match(r"^\|\s*[A-H]\d+[a-z]?\s*\|", line):
        continue
    total += 1
    status = line.rsplit("|", 2)[1].strip().lower().replace("*", "")
    key = ("wired" if status.startswith("wired")
           else "partial" if status.startswith("partial")
           else "n/a" if status.startswith("n/a")
           else "missing" if "missing" in status.split("(")[0]
           else "UNCLASSIFIED")
    counts[key] += 1
print(total, dict(counts))
EOF
```

### What "Wired" means here

Nothing in the matrix is marked Wired on the strength of an endpoint name alone:
every Wired row was traced from the DOM handler in `ui/js/**` through the route
in `tradinglib/api/routes/**` to the engine that computes the value. Where TradingLib
substitutes a real control for a REFERENCE control that was only a toast, the
row says so rather than claiming parity. Where REFERENCE shows editorialised
prose or a number no vendor can supply, the TradingLib row states the measurement
it renders instead (C10's rule-derived catalysts and risks, C24's hiring rate)
rather than generating text or inventing a figure — and a company for which the
measurement cannot be made renders `—` with the reason attached.

Rows whose REFERENCE original never renders in the composed prototype (the
`#ac-shim` scaffolding, the TL universe lists behind `INCLUDE_TL_LISTS = false`,
the culled `#ag-config` header) are marked N/A with the guard that disables them
named, so the omission is checkable rather than asserted.
