# TradingLib — TODO

Remaining work as of 2026-09-02, after the coverage-gap pass (621 tests passed /
0 failed; the five gaps below are closed and verified against a running server
and a browser pass). Sources: COVERAGE.md (371 rows: 371 Wired/N-A, 0 Partial,
0 Missing) and the verification history.

## 1. Coverage gaps — CLOSED 2026-09-02

- [x] **D47a — Evaluation scorecard evidence rows.** All eight of REFERENCE's
      evidence rows now render (OOS Sharpe, Deflated Sharpe, Sweep stability,
      MC: P(Sharpe>1), MC: p95 max DD, WFA positive folds, Corr. to live book,
      Capacity est. — GATES PASSED and VERDICT are separate frame rows, D47);
      an uncomputable cell shows `—` with the reason in its tooltip.
  - [x] `Sweep stability` — joined from the newest completed
        `AnalysisEngine.run_sweep()` recorded against the candidate's backtest
        (`median_metric / best_metric`, with the `robust` flag shown as
        IN REGION ✓ / PEAK ONLY).
  - [x] `MC: P(Sharpe>1)` — joined from `run_monte_carlo()`'s
        `prob_above_one`.
  - [x] `MC: p95 max DD` — joined from `run_monte_carlo()`'s
        `p95_max_drawdown`.
  - [x] `WFA positive folds` — joined from `run_walk_forward()`'s
        `positive_folds / scored`.
  - [x] `Corr. to live book` — new engine `tradinglib/research/livebook.py`:
        rebuilds the paper book's daily equity from `PortfolioStore` fills and
        positions marked with stored closes (read-only use of `trade/`), then
        Pearson-correlates it with the candidate's stored return series. Under
        20 overlapping sessions it renders `—, no live book` — never 0.0.
  - [x] `Capacity est.` — new engine `tradinglib/research/capacity.py`:
        `min(participation × ADV_usd / weight)` over the held names at 5%
        participation on a 63-session ADV from stored volume. The assumption is
        in every cell's tooltip; missing volume history refuses the bound
        instead of computing it over part of the book.
- [x] **D47 — Evaluation scorecard.** Flipped to Wired with D47a.
- [x] **F43a — Custom poll interval.** `#pollCustom` sits beside the presets in
      `renderPollBlock()`; `45s`/`2m`/`4h`/`1d` validate in the browser and
      again in `parse_schedule()`, and commit through the existing source
      schedule path (the scheduler job is re-registered at the new period, and
      its pending fire is re-timed to `last_run_at` + the new period whenever
      the cadence actually changed).
      `Schedule.label` now names custom cadences in their own units.
- [x] **F43b — Per-dataset frequency chips.** `Subscription.granularity` plus a
      migrated `ingest_subscriptions.granularity` column, per-domain options,
      `[data-catalog-frequency]` chips in `data.js`, carried through
      POST/GET `/api/ingest/subscriptions`. The value reaches
      `LibraryConnector.fetch(interval=…)` for library `bars` sources via
      `IngestScheduler.requested_interval()`; where it cannot be applied the API
      answers `granularity_applied: false` with the reason. The API and the
      scheduler decide from one shared rule
      (`catalog.agreed_bar_interval()` / `granularity_reaches_fetch()`), so a
      paused subscription and two active subscriptions asking for different
      bars are both reported as not applied.
- [x] **F78b — Outlier queue `Escalate` decision.** `escalate` persists an
      `escalated` state in `tradinglib/data/quality.py`; the point stays in the
      queue (`OPEN_OUTLIER_STATES`, `counts.awaiting_review`) and its value
      stays in the data — only `quarantined` is filtered from store reads.

## 2. Verification not completed

- [ ] **Independent round-3 re-audit** (stopped on request mid-run): fresh
      coverage + backend + browser sweeps over the round-2 fixes. The fixes
      were verified by their authors; no independent pass confirmed them.
- [ ] **True pixel-level screenshot comparison vs the reference.** All UI
      fidelity checks so far were DOM-structure/computed-style based (the
      browser pane would not composite frames in the build environment). Do a
      side-by-side visual pass per page/tab on a machine with a display.

## 3. Deliberate deferrals

- [ ] **Trading UI.** `tradinglib.trade` (paper broker + Alpaca + IBKR via
      ib_async) is implemented and tested, but has no UI surface — deferred by
      decision until the architecture discussion. Design question to settle:
      where trading lives in the UI (new workspace vs. panel in existing ones).
- [ ] **Provider keys.** OpenAI, Alpaca, FRED api-key, IBKR gateway are
      unconfigured; the app reports "not configured" honestly. Enter keys in
      Settings → API keys (masked, write-only) to activate. Anthropic works via
      local Claude Code CLI auth and was proven end-to-end.

## 4. Housekeeping

- [ ] Decide on the **"FX Reference Rates"** ingestion source (created as real
      verification evidence; genuinely connected and polling daily). Keep it or
      delete it from Market & Data → Data Ingestion.
- [ ] Decide on the three **analyses left on `bt-ca227d137878`** (a 3-cell
      sweep `sw-3fa13e0f72`, a 400-path Monte Carlo `mc-9dc6685d35`, a 4-fold
      walk-forward `wf-89434be76e`), run on 2026-09-02 to verify the D47a
      joins against real data. They are genuine analyses and are what fills that
      candidate's scorecard column; they do not affect `trials_logged` or the
      deflated Sharpe (which count backtest runs, not analyses). Delete them
      from Strategy Lab → attached analyses if the workspace should be clean.
- [ ] **ruff config.** No `[tool.ruff]` in pyproject.toml; 311 default-rule
      findings project-wide (pre-existing style baseline, not regressions —
      bug-catching classes F/E9/B are clean). Configure a rule set and clean up.
- [ ] **UI cache headers.** Static assets are served with etag/last-modified
      only; a `cache-control: no-cache` (or versioned asset URLs) would stop
      browsers running stale JS after edits. (Flagged in round 2 — verify the
      fix actually landed; if not, add it in `tradinglib/api/app.py` statics.)

## 5. React port of the console

Planned 2026-10-25 in [UI_PORT.md](UI_PORT.md): a React + TypeScript console
built from `web/` and served at `/app`, beside the current one. First step is
the shell, Overview and Stock Research (slices P0–P5). SPEC amendment D1b
approved and applied.

- [x] P0 scaffold and serving (2026-10-25).
- [x] P1 data layer (2026-10-25).
- [x] P2 shell (2026-10-25).
- [x] P3 Overview (2026-10-25).
- [x] P4 Stock Research (2026-10-25).
- [ ] P5 verification.
