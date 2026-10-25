# TradingLib — Build Specification (BINDING)

This document is the signed-off spec agreed with the user. Every implementation
agent MUST follow it. Where this spec and convenience conflict, the spec wins.
The reference UI file is the reference UI design
(called REFERENCE below). REFERENCE is the spec for APPEARANCE and BEHAVIOR only —
**no application code may be copied from it. All JS behavior code is written from
scratch.** The ONE sanctioned exception (user decision D1a): the CSS `<style>`
blocks in REFERENCE are reused verbatim as the styling, and page markup may
mirror REFERENCE's DOM structure/classes (that structure IS the look).

## 1. Product definition

Two deliverables in this repository:

1. `tradinglib/` — a real Python trading-research library + FastAPI service.
   Everything the UI shows is genuinely implemented: real market data, real
   ingestion scheduling, real quality checks, real screener, real backtester,
   real LLM-powered agents, real approval gates, real audit log. NO stubs, NO
   hardcoded display data, NO fake numbers.
2. `ui/` — a vanilla-JS SPA that looks and behaves exactly like REFERENCE,
   fully wired to the backend via REST + WebSocket. Reference dead-ends are
   FIXED here: any-ticker symbol lookup works; palette "Open active backtest"
   opens the Strategy Lab backtesting tab; no console errors; live data
   everywhere.
3. `web/` — the same console in React + TypeScript, ported page by page and
   served at `/app` beside `ui/` (D1b, 2026-10-25). The rules above apply to it
   unchanged: no stubs, no hardcoded display data.

## 2. Locked decisions

- D1a: reuse REFERENCE CSS verbatim (ui/css/, one file per original style
  block, same order). All JS from scratch.
- D2: vanilla JS SPA, no build step. Live updates via WebSocket; charts via
  self-hosted TradingView lightweight-charts (candles/lines) + uPlot
  (sparklines) in ui/vendor/.
- D1b (2026-10-25, owner): a React + TypeScript console is built from `web/`
  and served at `/app`, page by page, alongside the vanilla console at `/`. It
  must meet §6 for every page it replaces. D1a (verbatim CSS) and D2 (no build
  step) apply to `ui/` only; `web/` has a Vite build and its own design tokens.
  When every page is ported, `/` switches to the React app and `ui/` is retired
  in a later decision. Plan: [UI_PORT.md](UI_PORT.md).
- D3: LLM providers = Anthropic via `claude-agent-sdk` + OpenAI via `openai`,
  behind `tradinglib/agents/providers.py`. Model picker lists models for whichever
  keys are configured. claude-agent-sdk may fall back to local Claude Code CLI
  auth when no ANTHROPIC_API_KEY is set (verify at runtime; report capability
  honestly in /api/agents/config).
- D4: hybrid storage — SQLite (SQLAlchemy) for app state at `tradinglib/db.py`;
  Parquet files + DuckDB query engine for time series at `tradinglib/data/store.py`.
- Data sources: yfinance, Alpaca (alpaca-py; REST + WebSocket stream), SEC
  EDGAR (direct REST, proper User-Agent), FRED (fredgraph CSV endpoints keyless;
  fredapi if key configured), Google Trends (pytrends).
- Trading: `tradinglib/trade/` = paper broker (simulated fills against live
  quotes) + Alpaca broker adapter + IBKR adapter via `ib_async` (successor of
  ib_insync; adapt if unavailable on Python 3.14). NO UI surface for trade yet.
  Agents/dev/test NEVER place live orders — paper/sandbox only.

## 3. Package tree (locked with user — do not restructure)

```
TradingLib/
├── pyproject.toml
├── README.md
├── SPEC.md                      (this file)
├── .venv/                       (Python 3.14 venv)
├── tradinglib/
│   ├── __init__.py
│   ├── config.py                # keys (env + config file), budgets, settings
│   ├── db.py                    # SQLite/SQLAlchemy app-state (cross-cutting)
│   ├── events.py                # EventBus (in-process pub/sub, async)
│   ├── data/
│   │   ├── base.py              # Quote/Bar/Series/Fundamentals models, Provider ABC
│   │   ├── providers/
│   │   │   ├── yfinance_provider.py
│   │   │   ├── alpaca_provider.py    # REST + AlpacaStream (WebSocket) -> EventBus
│   │   │   ├── edgar_provider.py
│   │   │   ├── fred_provider.py
│   │   │   └── trends_provider.py
│   │   ├── cache.py
│   │   ├── store.py             # Parquet + DuckDB time-series store
│   │   ├── universe.py          # built-ins (US Quality 50, S&P500, Nasdaq100), watchlists, PIT
│   │   ├── quality.py           # completeness/staleness/outliers, scores, incidents
│   │   └── ingest/
│   │       ├── source.py        # SourceConfig — one field per UI form control
│   │       ├── catalog.py       # SourceCatalog: saved sources + subscriptions, statuses
│   │       ├── auth.py          # ApiKeyAuth/OAuthAuth/BasicAuth strategies
│   │       ├── formats.py       # JSON/CSV parsers + schema mapping
│   │       ├── scheduler.py     # runs each source per its schedule (asyncio)
│   │       ├── gateway.py       # latency, sources-connected, delayed-series, health
│   │       └── connectors/
│   │           ├── base.py      # Connector ABC: connect/close/test/backfill/stream/fetch/status
│   │           ├── websocket.py # WebSocketConnector
│   │           ├── rest.py      # RestConnector
│   │           ├── polling.py   # PollingConnector
│   │           ├── database.py  # DatabaseConnector
│   │           ├── file.py      # FileConnector
│   │           └── library.py   # LibraryConnector (wraps python-lib providers e.g. yfinance)
│   ├── research/
│   │   ├── company.py           # Stock Research engine: composes data/ into page payloads
│   │   ├── screen/
│   │   │   ├── rules.py         # rule/expression engine
│   │   │   └── funnel.py        # funnel evaluation + stats
│   │   ├── hypotheses.py
│   │   ├── experiments.py
│   │   ├── backtest.py          # vectorized backtester: signals, costs, benchmark, OOS
│   │   ├── validation.py        # promotion gates, statistical tests
│   │   └── evaluation.py
│   ├── agents/
│   │   ├── providers.py         # AnthropicProvider(claude-agent-sdk) + OpenAIProvider(openai)
│   │   ├── registry.py          # agent definitions (Agent Studio backend)
│   │   ├── pipeline.py          # staged DAGs + input/output contracts
│   │   ├── runner.py            # runs: stage execution, artifacts, logs, token+cost accounting
│   │   ├── autopipeline.py      # scheduled autonomous runs
│   │   ├── findings.py
│   │   ├── knowledge.py
│   │   ├── approvals.py         # human-approval gates (block until approve/reject)
│   │   └── audit.py             # append-only audit log of every agent action
│   ├── trade/
│   │   ├── portfolio.py
│   │   ├── paper.py
│   │   ├── alpaca_broker.py
│   │   └── ibkr_broker.py
│   └── api/
│       ├── app.py               # FastAPI factory; serves ui/ at / and web/dist at /app; lifespan starts scheduler
│       ├── ws.py                # WsHub: /ws endpoint, channel subscriptions, EventBus fan-out
│       └── routes/              # one router per domain (see §5)
├── ui/
│   ├── index.html
│   ├── css/                     # REFERENCE css blocks, verbatim, original order
│   ├── js/
│   │   ├── api.js               # ApiClient
│   │   ├── stream.js            # LiveStream (WS client: reconnect/backoff, channels, heartbeat)
│   │   ├── router.js            # hash routing incl. #tl/lab style + launch hash
│   │   ├── palette.js           # ⌘K command palette
│   │   ├── components/          # shared render helpers (tables, panels, toasts, charts)
│   │   └── pages/               # one module per workspace (overview, stock, lab, agents, data, docs, settings)
│   └── vendor/                  # lightweight-charts.standalone.js, uplot.js/css (self-hosted)
├── web/                         # React + TypeScript console (D1b), built to web/dist, served at /app
└── tests/                       # pytest; core engines covered (see §8)
```

## 4. Architecture rules

- Domain modules NEVER import from `tradinglib.api`. They publish to
  `tradinglib.events.EventBus`; `api/ws.py` fans out to browsers.
- `research/*` and `agents/*` get ALL raw data via `tradinglib.data` (providers →
  cache → store). `research/company.py` derives page-shaped payloads (YoY
  growth, margins, EV/Sales, net cash, freshness) — it does not fetch vendors.
- Ingestion: UI form field ↔ `SourceConfig` field, 1:1 (name, data_domain,
  protocol, endpoint, auth, format, schedule, scope, source_tz, retry_policy,
  backfill{enabled,range,batch}, advanced{headers,subscriptions,sql,glob,
  pagination,schema_mapping}). Protocol selects connector class. "Test
  connection" → `Connector.test()` returns a real result. Built-in providers
  are pre-registered catalog sources (peers of user-created ones); "sources
  connected" counts are REAL statuses.
- Keyless-by-default operation must work: yfinance + EDGAR + FRED(csv) +
  Trends live; Alpaca/IBKR/OpenAI show honest "not configured" status until
  keys are set in Settings/Configuration. Nothing pretends.
- LLM agents: each pipeline stage calls its configured model with TOOLS that
  invoke the library itself (run screen, get data, run backtest, save finding).
  Runs record per-stage logs, artifacts, token usage and $ cost; budgets
  enforced (SPEND x/y in the datastrip is real). Human Approval stage blocks
  the run until approved in the UI; audit log records everything.
- All timestamps stored UTC; UI clock = real America/New_York via Intl.
- Async-first: FastAPI + asyncio scheduler; blocking provider SDK calls run
  in thread executors.

## 5. API surface (REST under /api, WS at /ws)

- `GET /api/status` — datastrip payload: data_live, gateway_ms, sources_connected,
  last_ingest, delayed_series, trials_logged, spend{used,budget}, engine_health.
- Universe/watchlists: `GET/POST /api/universes`, `GET /api/universes/{id}/members`,
  `POST /api/universes/{id}/snapshot`, CRUD `/api/watchlists`, active-universe select.
- Research: `GET /api/research/company/{symbol}` (+`/financials`, `/estimates`,
  `/valuation`, `/news`, `/altdata`, `/filings`, `/price?range=`) — any ticker.
- Screener: CRUD `/api/screens`, `POST /api/screens/{id}/run`,
  `POST /api/screens/{id}/queue-backtest`.
- Lab: CRUD `/api/lab/hypotheses`, `/api/lab/experiments` (+`/run`),
  `/api/lab/backtests` (+`POST run`, `GET {id}/results`), `/api/lab/validation`
  (+`/gates`, `POST {id}/promote`), `/api/lab/evaluation`.
- Ingestion: CRUD `/api/ingest/sources`, `POST /api/ingest/sources/test`
  (form payload → connector.test()), `POST /api/ingest/sources/{id}/backfill`,
  `GET /api/ingest/subscriptions`, `GET /api/ingest/gateway`.
- Quality: `GET /api/quality/summary`, `/api/quality/series`, `/api/quality/incidents`.
- Agents: `GET /api/agents/models` (available per configured keys); CRUD
  `/api/agents/definitions`; CRUD `/api/agents/pipelines` (+versions, duplicate);
  `POST /api/agents/pipelines/{id}/run`, `GET /api/agents/runs` (+`{id}`,
  `{id}/logs`, `{id}/artifacts`), inbox `GET /api/agents/inbox`;
  `GET/POST /api/agents/approvals/{id}` (approve/reject+comment);
  findings/knowledge CRUD + search; `GET /api/agents/audit`;
  autopipeline schedule CRUD; `GET/PUT /api/agents/config` (budgets, keys status).
- Trade (no UI): `/api/trade/portfolio`, `/api/trade/orders` (paper default).
- Search: `GET /api/search?q=` — palette results across pages/symbols/entities.
- Settings: `GET/PUT /api/settings` (theme, density, timezone, benchmark,
  default universe, keys via masked write-only fields).
- WS `/ws`: subscribe protocol `{"action":"subscribe","channel":...,"params":{...}}`.
  Channels: `tape` (quotes for active universe), `status` (datastrip deltas),
  `ingest` (source status/last-ingest), `quality` (incidents), `runs` (run/stage
  progress + logs), `approvals` (pending count), `notifications` (toasts).

## 6. UI contract

Pages and features must match REFERENCE exactly (layout, classes, panels,
tabs, interactions), populated from the API:
- Topbar: palette search (⌘K works), PICK mode + count + Export, universe
  selector (drives tape/screens/scanner scope), ENGINE HEALTHY (real health),
  live ET clock.
- Datastrip: every figure from `/api/status`, updated over WS.
- Overview: market strip, ticker tape (live), watchlist with filter + live
  prices, panels as in REFERENCE, agent inbox merge.
- Stock Research: symbol form loads ANY ticker (fixes reference dead path);
  tabs Overview / Financials / Earnings & Estimates / Valuation & Peers /
  News & Sentiment / Alternative Data / Research. Price chart
  (lightweight-charts, live-updating), fundamental snapshot (real derived
  metrics), revenue/EBITDA bars, news+sentiment, alt signals (Trends, news
  sentiment). Data freshness real.
- Strategy Lab: hypotheses list/detail, experiments (split + full views, run
  log), backtests (list + results incl. equity curve chart), validation gates,
  evaluation, pipeline tab; funnel/screening panels; tabbar switching without
  console errors (fixes reference TypeError).
- Agent Center: tabs Overview(+inbox merge), Pipeline (builder: saved
  pipelines w/ versions, stage cards, add/duplicate/remove stage, per-stage
  config incl. assigned agent, model picker, scope, objective, include-in-run,
  input/output contracts, handoff; Run inspector: Preview/Inputs/Outputs/
  Logs/Artifacts live), AutoPipeline, Findings, Knowledge, Approval Review
  (real gate: approve/reject resumes/kills run), Agent Studio (agent CRUD wired
  to registry), Audit Log (real), Configuration (budgets, provider keys status).
  "New run" actually runs; active/idle counts real.
- Market & Data: Universes & Watchlists (CRUD), Screener (rules → live counts,
  run, queue backtest → REAL queued backtest + toast), Source Catalog (catalog
  + detail + subscription config), Data Ingestion (the full configure form —
  §4 mapping, protocol-dependent fields, Test connection, Save), Data Quality
  (real checks).
- Docs: methodology/product guide content (can be static text — it is docs).
- Settings: working theme/density/timezone/benchmark/universe defaults; API
  keys entry (masked, write-only).
- Palette actions: navigation + "Open active backtest" → Strategy Lab
  backtesting tab (FIXED), symbol research jump.
- Toasts for action feedback; connection loss → visible degraded state;
  recovery → live again (acceptance test).

## 7. Coverage matrix (deliverable)

`TradingLib/COVERAGE.md`: table mapping every interactive element / dynamic value
in REFERENCE → UI module → endpoint/WS channel → backend implementation.
No gaps allowed. Verification reads this.

## 8. Tests & acceptance

- pytest in `tests/`: backtester correctness (known synthetic series →
  expected stats), screener rules, quality checks, paper broker fills,
  connector test()/backfill against local fixtures, ingest scheduler, API
  smoke (httpx AsyncClient), agents runner with a MockProvider (deterministic
  LLM stub — used ONLY in tests, clearly named).
- Acceptance: (1) side-by-side visual match per page; (2) coverage matrix
  complete; (3) kill backend → UI shows degraded, restart → recovers;
  (4) end-to-end pipeline run with real LLM if key/CLI auth available else
  MockProvider, with human approval gate exercised in UI; (5) `import tradinglib`
  usable headless; tests green.

## 9. Style

Python: type-hinted, dataclasses/Pydantic models, docstrings, no dead code.
JS: ES2022 modules-free (plain scripts, ordered), small focused files,
JSDoc-level comments only where non-obvious. No frameworks, no build step.
