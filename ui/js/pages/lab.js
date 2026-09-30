/* TradingLib — Strategy Lab workspace (#page-lab).
 *
 * The reference's Strategy Lab, rebuilt from scratch against the real backend:
 * the same DOM (header, five-card funnel, the HYPOTHESES / EXPERIMENTS /
 * BACKTESTS / VALIDATION / EVALUATION / PIPELINE tabbar and their panels) with
 * every number coming from /api/lab/*, /api/screens*, /api/quality/* and
 * /api/universes*. Nothing here is mock data: an empty workspace renders empty
 * states, and a failed call renders the failure.
 *
 * This is the only workspace in the TL tree, so it speaks TL's class dialect:
 * .panel > .ptitle, .tbl, .chip, .pill, .kv, .rowitem, .crow, .fcard.
 *
 * Routing (see ui/js/router.js): "#tl/lab/<tab>[?id=<entity>]". `ctx.sub` is
 * the tab, `ctx.params.id` selects a hypothesis / experiment / backtest inside
 * it — honoured on mount AND in route(), which is what makes the palette's
 * "Open active backtest" land on a real run. Tab clicks navigate through the
 * router; they never rebuild the page, and they never throw (the reference's
 * tabbar TypeError is a bug, not a feature).
 *
 * Live updates arrive on the WebSocket `lab` channel: experiment run logs
 * stream into the run log as they happen, and backtest/validation/promotion
 * events refresh the affected lists.
 */
(function (global) {
  "use strict";

  const ui = global.AlgoUI;
  const el = ui.el;
  const fmt = ui.fmt;

  /** Tab keys, in tabbar order. */
  const TABS = ["hypotheses", "experiments", "backtests", "validation", "evaluation", "pipeline"];
  /** Tab key → the id of the section it shows (reference ids). */
  const SECTION = {
    hypotheses: "t-hypo",
    experiments: "t-exp",
    backtests: "t-bt",
    validation: "t-val",
    evaluation: "t-eval",
    pipeline: "t-pipe",
  };
  /** Funnel stages, in reference order. */
  const STAGES = ["research", "validated", "paper", "approved", "live"];
  /** Hypothesis lifecycle states offered as transitions. */
  const HYPOTHESIS_STATES = ["draft", "active", "testing", "validated", "rejected", "archived"];
  /** Stages a promotion may target. */
  const PROMOTION_STAGES = ["validated", "paper", "approved", "live"];
  /** Evidence kinds the backend accepts (tradinglib.research.hypotheses). */
  const EVIDENCE_KINDS = ["paper", "knowledge", "run", "backtest", "experiment", "finding", "screen", "note", "url"];

  /**
   * A fresh, complete detail cache. Every bucket the page reads must be
   * declared here — mount() and unmount() both go through this factory so the
   * shape can never drift between the two (a missing bucket used to blow up
   * the backtests tab on the second visit).
   * @returns {{experiment: Object, results: Object, fan: Object, analysis: Object,
   *            prices: Object, promotion: Object, artifact: Object, counts: (Object|null|undefined)}}
   */
  function emptyCache() {
    return {
      /** Experiment detail (runs, config) keyed by experiment id. */
      experiment: {},
      /** Backtest result payloads keyed by backtest id. */
      results: {},
      /** Monte-Carlo cones keyed by backtest id (GET .../fan). */
      fan: {},
      /** Full analysis payloads keyed by analysis id. */
      analysis: {},
      /** Per-symbol price series for the Price & signals panel. */
      prices: {},
      /** Promotion sign-off state keyed by backtest id. */
      promotion: {},
      /** Pinned cfg+data+code bundles keyed by backtest id (GET .../artifact). */
      artifact: {},
      /** Trial-ledger batch and cell counts (GET /analyses/counts). */
      counts: undefined,
    };
  }

  /** Everything the page keeps between renders. Rebuilt on every mount. */
  const state = {
    ctx: null,
    nodes: {},
    tab: "experiments",
    /** Data from the API. `errors` holds the failure for any key that failed. */
    data: {
      summary: null, hypotheses: [], experiments: [], backtests: [], gates: [],
      thresholds: null, reports: [], strategies: [], evaluation: null,
      screens: [], screenRuns: [], universes: [], activeUniverse: null, quality: null,
      /** Agent runs behind the hypotheses tab's merged "Agent activity" panel. */
      agentRuns: [],
      /** Agent definitions, so an activity step can name the agent not its id. */
      agentDefinitions: [],
      /** Attached analyses (sweep / walk-forward) for the selected experiment. */
      analyses: [],
      /** What a sweep axis may vary, from /api/lab/analyses/options. */
      analysisOptions: null,
    },
    errors: {},
    /** Selected entity per tab. */
    sel: { hypothesis: null, experiment: null, backtest: null },
    /** "view" | "new" | "edit" per editor. */
    mode: { hypo: "view", exp: "view", bt: "view" },
    /** Hypothesis to preselect in the next "new experiment" form. */
    newExperimentHypothesis: null,
    /** Experiment whose configuration seeds the next "new experiment" form. */
    cloneFrom: null,
    /** Detail payloads fetched lazily, keyed by id. Shape from emptyCache(). */
    cache: emptyCache(),
    /**
     * Which analysis is open in the experiment detail, replacing it in place:
     * `null` (none), `{kind:"mc"}` or `{kind:"sweep"|"walk_forward", id}`.
     */
    anView: null,
    /** Symbol shown in the backtests tab's Price & signals panel. */
    signalSymbol: null,
    filters: { stage: null, expQuery: "", expOrigin: "all", btQuery: "" },
    compare: new Set(),
    compareOpen: false,
    strategyView: "rules",
    /** Live experiment log: { runId, experimentId, lines: [] }. */
    log: { runId: null, experimentId: null, lines: [] },
    busy: {},
    chart: null,
    ddChart: null,
    unsubscribe: [],
    loaded: false,
  };

  // ---------------------------------------------------------------- helpers

  /** Was this failure caused by our own unmount? Then it is not an error. */
  function cancelled(err) {
    return !!(err && (err.cancelled || err.name === "AbortError"));
  }

  /**
   * Report a failed action honestly (toast + console), never silently.
   *
   * A 4xx is the backend refusing on purpose (a blocked promotion, an illegal
   * transition): that is a result, so it is logged as a warning. Anything else
   * is a genuine error.
   */
  function fail(what, err) {
    if (cancelled(err)) return;
    const message = err && err.message ? err.message : String(err);
    const refusal = err && err.status >= 400 && err.status < 500;
    if (refusal) console.warn("strategy lab: " + what, message);
    else console.error("strategy lab: " + what, err);
    if (global.toast) global.toast.error(what + " — " + message);
  }

  /** A `.btn` span in the reference's dialect, keyboard-operable. */
  function tbtn(label, onClick, options) {
    const opts = options || {};
    const node = el("span.btn", { role: "button", tabindex: "0" });
    ui.append(node, label);
    if (opts.primary) node.classList.add("primary");
    if (opts.small) node.classList.add("small");
    if (opts.newStyle) node.classList.add("new");
    if (opts.className) opts.className.split(/\s+/).filter(Boolean).forEach(function (c) { node.classList.add(c); });
    if (opts.style) node.setAttribute("style", opts.style);
    if (opts.id) node.id = opts.id;
    if (opts.disabled) {
      node.setAttribute("aria-disabled", "true");
      node.style.opacity = "0.5";
      node.style.cursor = "default";
      return node;
    }
    if (onClick) {
      node.addEventListener("click", onClick);
      node.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick(event);
        }
      });
    }
    return node;
  }

  /** `<div><label>L</label><span class="metricbig tone">V</span></div>` */
  function metric(label, value, tone) {
    const big = el("span.metricbig", { text: value === undefined || value === null ? "—" : String(value) });
    if (tone) big.classList.add(tone);
    return el("div", el("label", { text: label }), big);
  }

  /** A `.foot` line. */
  function foot(text, style) {
    return el("div.foot", { text: text, style: style || "" });
  }

  /** A `.stat` line: dim timestamp then text. */
  function stat(when, text, tone) {
    const left = el("span.dim", { text: when });
    const body = el("span", { text: " " + text });
    if (tone) body.classList.add(tone);
    return el("div.stat", left, body);
  }

  /** A `.crow` rule row (the strategy rule builder's line). */
  function crow(parts) {
    const row = el("div.crow");
    row.appendChild(el("span.drop", { text: parts[0] }));
    if (parts[1]) row.appendChild(el("span.drop", { text: String(parts[1]) }));
    if (parts[2]) row.appendChild(el("span.op", { text: String(parts[2]) }));
    if (parts[3] !== undefined && parts[3] !== null && parts[3] !== "") {
      row.appendChild(el("span.drop", { text: String(parts[3]) }));
    }
    return row;
  }

  /** The reference's `.empty` block: a headline, an explanation, an action. */
  function emptyBox(headline, detail, action) {
    const box = el("div.empty",
      el("div.hi", { style: "margin-bottom:6px", text: headline }),
      foot(detail, "margin-bottom:12px"));
    if (action) box.appendChild(action);
    return box;
  }

  /** An honest inline failure notice. */
  function errorNote(prefix, err) {
    const message = err && err.message ? err.message : String(err);
    return el("div.note.warn", { text: prefix + ": " + message });
  }

  /** A labelled form field. */
  function field(label, control, options) {
    const opts = options || {};
    return el("div", { style: opts.style || "margin-bottom:6px" },
      el("label", { text: label }), control);
  }

  /** `<input>` with a value. */
  function input(value, options) {
    const opts = options || {};
    const node = el("input", { type: opts.type || "text" });
    if (opts.placeholder) node.placeholder = opts.placeholder;
    if (opts.style) node.setAttribute("style", opts.style);
    node.value = value === undefined || value === null ? "" : String(value);
    return node;
  }

  /** `<select>` from [{value,label}] (or plain strings). */
  function select(items, value, options) {
    const opts = options || {};
    const node = el("select");
    if (opts.style) node.setAttribute("style", opts.style);
    (items || []).forEach(function (item) {
      const v = typeof item === "string" ? item : item.value;
      const label = typeof item === "string" ? item : item.label;
      const option = el("option", { value: String(v), text: String(label) });
      if (String(v) === String(value)) option.selected = true;
      node.appendChild(option);
    });
    return node;
  }

  /** Ratio with two decimals. */
  function ratio(value) {
    return fmt.num(value, 2);
  }

  /** Percentage from a fraction (0.184 → "18.4%"). */
  function percent(value, decimals) {
    return fmt.pct(value, { fraction: true, decimals: decimals === undefined ? 1 : decimals });
  }

  /** Signed percentage from a fraction. */
  function signedPercent(value, decimals) {
    return fmt.pct(value, { fraction: true, sign: true, decimals: decimals === undefined ? 1 : decimals });
  }

  /** Colour class for a signed number. */
  function signTone(value) {
    if (value === undefined || value === null || isNaN(Number(value))) return "";
    return Number(value) > 0 ? "ok" : (Number(value) < 0 ? "bad" : "");
  }

  /** Colour class for a gate status. */
  function gateTone(status) {
    if (status === "pass") return "ok";
    if (status === "fail") return "bad";
    if (status === "warn") return "warn";
    return "dim";
  }

  /** Colour class for a funnel stage / experiment stage. */
  function stageTone(stage) {
    if (stage === "live" || stage === "approved") return "blu";
    if (stage === "paper" || stage === "validated") return "grn";
    if (stage === "killed") return "red";
    return "";
  }

  /** Colour class for a backtest status. */
  function runTone(status) {
    if (status === "complete") return "ok";
    if (status === "failed" || status === "cancelled") return "bad";
    if (status === "running") return "blu";
    return "warn";
  }

  /** "Aug 12 14:03" in New York, from an ISO timestamp. */
  function when(value) {
    const date = fmt.toDate(value);
    if (!date) return "—";
    return fmt.etDate(value) + " " + fmt.etClock(date).slice(0, 5);
  }

  // ------------------------------------------------------------ data access

  /** Newest completed backtest per experiment id. */
  function newestByExperiment() {
    const out = {};
    state.data.backtests.forEach(function (record) {
      if (!record.experiment_id) return;
      if (!out[record.experiment_id] && record.status === "complete") out[record.experiment_id] = record;
    });
    return out;
  }

  /** Newest validation report per backtest id. */
  function reportsByBacktest() {
    const out = {};
    state.data.reports.forEach(function (report) {
      if (!out[report.backtest_id]) out[report.backtest_id] = report;
    });
    return out;
  }

  /** The deflated Sharpe a report measured, or null. */
  function deflatedOf(report) {
    if (!report || !report.tests) return null;
    const test = report.tests.deflated_sharpe;
    return test && test.deflated_sharpe !== undefined ? test.deflated_sharpe : null;
  }

  /** The window a gate judges: out-of-sample stats when present. */
  function judgedWindow(record) {
    if (!record) return null;
    return record.out_of_sample || record.stats || null;
  }

  /** Experiment lookup by id. */
  function experimentById(id) {
    return state.data.experiments.filter(function (e) { return e.id === id; })[0] || null;
  }

  /** Hypothesis lookup by id. */
  function hypothesisById(id) {
    return state.data.hypotheses.filter(function (h) { return h.id === id; })[0] || null;
  }

  /** Backtest lookup by id. */
  function backtestById(id) {
    return state.data.backtests.filter(function (b) { return b.id === id; })[0] || null;
  }

  /** Where an experiment came from, derived from its runs — never invented. */
  function originOf(experiment) {
    const runs = state.data.backtests.filter(function (b) { return b.experiment_id === experiment.id; });
    if (runs.some(function (b) { return !!b.screen_id; })) return "screen";
    if ((experiment.owner || "").toLowerCase() === "agent") return "agent";
    if (runs.some(function (b) { return b.source === "agent"; })) return "agent";
    return "manual";
  }

  /** The active universe's display name. */
  function universeName(id) {
    if (!id) return "—";
    const match = state.data.universes.filter(function (u) { return u.id === id; })[0];
    return match ? match.name : id;
  }

  /** Options for a universe `<select>`. */
  function universeOptions() {
    return state.data.universes.map(function (u) {
      return { value: u.id, label: u.name };
    });
  }

  global.AlgoPages.register("lab", {
    mount: function (ctx) {
      state.ctx = ctx;
      state.chart = null;
      state.ddChart = null;
      state.loaded = false;
      state.tab = ctx.sub && TABS.indexOf(ctx.sub) >= 0 ? ctx.sub : "experiments";
      applySelection(ctx.params && ctx.params.id);
      buildSkeleton(ctx.root);
      showTab(state.tab);
      subscribeLive(ctx);
      loadAll();
    },
    route: function (ctx) {
      state.ctx = ctx;
      const tab = ctx.sub && TABS.indexOf(ctx.sub) >= 0 ? ctx.sub : state.tab;
      const changedTab = tab !== state.tab;
      state.tab = tab;
      const changedSelection = applySelection(ctx.params && ctx.params.id);
      showTab(tab);
      if (state.loaded && (changedTab || changedSelection)) renderTab(tab);
    },
    unmount: function () {
      destroyCharts();
      state.unsubscribe.forEach(function (off) {
        try { off(); } catch (err) { console.warn("lab: unsubscribe failed", err); }
      });
      state.unsubscribe = [];
      state.nodes = {};
      state.cache = emptyCache();
      state.compare = new Set();
      state.compareOpen = false;
      state.loaded = false;
    },
  });

  /**
   * Point the current tab's selection at `id`.
   * @returns {boolean} whether anything changed
   */
  function applySelection(id) {
    if (!id) return false;
    const key = state.tab === "hypotheses" ? "hypothesis"
      : (state.tab === "experiments" ? "experiment" : "backtest");
    if (state.sel[key] === id) return false;
    state.sel[key] = id;
    if (key === "experiment") state.mode.exp = "view";
    if (key === "hypothesis") state.mode.hypo = "view";
    if (key === "backtest") state.mode.bt = "view";
    return true;
  }

  /** Navigate to a tab (and optionally an entity) through the router. */
  function goTab(tab, id) {
    const params = id ? { id: id } : {};
    state.ctx.router.go("lab", { sub: tab, params: params });
  }

  function destroyCharts() {
    if (state.chart) {
      try { state.chart.destroy(); } catch (err) { console.warn("lab: chart destroy", err); }
      state.chart = null;
    }
    if (state.ddChart) {
      try { state.ddChart.destroy(); } catch (err) { console.warn("lab: drawdown chart destroy", err); }
      state.ddChart = null;
    }
  }

  // --------------------------------------------------------------- skeleton

  /**
   * Build the page once: the reference's header, funnel, tabbar and the six
   * `.sub` sections. Every dynamic region is kept in `state.nodes` so a render
   * replaces content without rebuilding the page (and without re-binding).
   */
  function buildSkeleton(root) {
    const n = state.nodes = {};
    ui.clear(root);

    n.crumb = el("span.dim", { id: "crumb", text: "/ all experiments" });
    root.appendChild(el("div", { style: "display:flex;align-items:center;gap:10px;flex-wrap:wrap" },
      el("span.logo", { style: "font-size:14px", text: "> STRATEGY LAB" }),
      n.crumb,
      tbtn("+ NEW EXPERIMENT", function () {
        state.anView = null;
        state.mode.exp = "new";
        state.compareOpen = false;
        clearExperimentSeed();
        goTab("experiments");
        renderExperiments();
      }, { small: true, id: "btn-newexp", style: "margin-left:auto" }),
      tbtn("+ NEW HYPOTHESIS", function () {
        state.mode.hypo = "new";
        goTab("hypotheses");
        renderHypotheses();
      }, { small: true, id: "btn-newhypo" })));

    n.funnel = el("div.grid.g5", { id: "funnel", style: "gap:8px;margin-top:12px" });
    root.appendChild(n.funnel);

    n.tabs = el("div.tabbar", { id: "tabs" });
    root.appendChild(n.tabs);

    // ---- HYPOTHESES -------------------------------------------------------
    n.hypolist = el("div", { id: "hypolist" });
    n.hypodetail = el("div", { id: "hypodetail" });
    n.sections = {};
    // The merged agent-center panel. Its classes are styled only beneath `.ac`
    // (07-ac-in-tl.css), so it carries its own `.ac` host exactly as the
    // reference does — `data-from` records where the panel came from.
    n.agentActivity = el("aside.panel");
    n.sections["t-hypo"] = el("section.sub", { id: "t-hypo" },
      el("div.grid.g32",
        el("div", n.hypolist, tbtn("+ NEW HYPOTHESIS", function () {
          state.mode.hypo = "new";
          renderHypotheses();
        }, { newStyle: true, id: "hypo-new" })),
        n.hypodetail),
      el("div.ac", { dataset: { from: "agent-center · agent activity" } }, n.agentActivity));
    root.appendChild(n.sections["t-hypo"]);

    // ---- EXPERIMENTS ------------------------------------------------------
    n.expCount = el("span", { id: "exp-count", style: "margin-left:auto" });
    n.expFilter = el("div", { id: "exp-filter" });
    n.expQuery = input("", { placeholder: "Filter by name, hypothesis, universe…" });
    n.expQuery.id = "exp-q";
    n.expQuery.addEventListener("input", function () {
      state.filters.expQuery = n.expQuery.value;
      renderExpList();
    });
    n.expOrigin = select(
      [{ value: "all", label: "All origins" }, { value: "manual", label: "Manual" },
        { value: "agent", label: "Agent" }, { value: "screen", label: "Screen" }],
      "all", { style: "width:118px" });
    n.expOrigin.id = "exp-origin";
    n.expOrigin.addEventListener("change", function () {
      state.filters.expOrigin = n.expOrigin.value;
      renderExpList();
    });
    n.explist = el("div", { id: "explist" });
    n.cmpn = el("span", { id: "cmpn", text: "0" });
    n.expDetail = el("div.panel", { id: "sl6-expdetail" });
    n.expFull = el("div", { id: "exp-full", style: "display:none" });
    n.expSplit = el("div.grid.g23", { id: "exp-split" },
      el("div.panel.tight",
        el("div.ptitle", { style: "display:flex" }, el("span", { text: "EXPERIMENTS" }), n.expCount),
        n.expFilter,
        el("div", { style: "display:flex;gap:8px;margin-bottom:8px" }, n.expQuery, n.expOrigin),
        n.explist,
        el("div", { style: "display:flex;gap:6px;align-items:center;margin-top:8px" },
          tbtn(["COMPARE SELECTED (", n.cmpn, ")"], openCompare, { small: true, id: "btn-compare" }),
          foot("select 2–4", "margin-left:auto")),
        tbtn("+ NEW EXPERIMENT", function () {
          state.anView = null;
          state.mode.exp = "new";
          state.compareOpen = false;
          clearExperimentSeed();
          renderExperiments();
        }, { newStyle: true, id: "exp-new" })),
      n.expDetail);
    n.screens = el("div.panel.tight", { style: "margin-top:12px" });
    n.sections["t-exp"] = el("section.sub", { id: "t-exp" }, n.expSplit, n.expFull, n.screens);
    root.appendChild(n.sections["t-exp"]);

    // ---- BACKTESTS --------------------------------------------------------
    n.btQuery = input("", { placeholder: "Filter runs…", style: "margin-bottom:8px" });
    n.btQuery.addEventListener("input", function () {
      state.filters.btQuery = n.btQuery.value;
      renderBtList();
    });
    n.btlist = el("div", { id: "btlist" });
    n.btFoot = foot("", "border-top:1px solid var(--line);margin-top:8px;padding-top:6px");
    n.btmain = el("div", { id: "btmain" });
    n.sections["t-bt"] = el("section.sub", { id: "t-bt" },
      el("div.grid", { style: "grid-template-columns:196px minmax(0,1fr)" },
        el("div.panel.tight", { style: "align-self:start" },
          el("div.ptitle", { text: "BACKTEST LOG" }),
          n.btQuery,
          n.btlist,
          tbtn("+ NEW BACKTEST", function () {
            state.mode.bt = "new";
            renderBtList();
            renderBtMain();
          }, { newStyle: true, id: "bt-new" }),
          n.btFoot),
        n.btmain));
    root.appendChild(n.sections["t-bt"]);

    // ---- VALIDATION -------------------------------------------------------
    n.valGates = el("div.panel.tight");
    n.valLedger = el("div.panel.tight");
    n.valQuality = el("div.panel.tight");
    n.valCandidate = el("div", { style: "margin-top:12px" });
    n.sections["t-val"] = el("section.sub", { id: "t-val" },
      el("div.grid.g32",
        n.valGates,
        el("div", { style: "display:flex;flex-direction:column;gap:12px" }, n.valLedger, n.valQuality)),
      n.valCandidate);
    root.appendChild(n.sections["t-val"]);

    // ---- EVALUATION -------------------------------------------------------
    n.evalBody = el("div", { id: "eval-body" });
    n.sections["t-eval"] = el("section.sub", { id: "t-eval" }, n.evalBody);
    root.appendChild(n.sections["t-eval"]);

    // ---- PIPELINE ---------------------------------------------------------
    n.pipeBody = el("div", { id: "pipe-body" });
    n.sections["t-pipe"] = el("section.sub", { id: "t-pipe" }, n.pipeBody);
    root.appendChild(n.sections["t-pipe"]);

    renderTabs();
    renderFunnel();
  }

  /** Show one `.sub` section and mark its tab active. */
  function showTab(tab) {
    const sections = state.nodes.sections || {};
    Object.keys(sections).forEach(function (id) {
      sections[id].style.display = id === SECTION[tab] ? "block" : "none";
    });
    const bar = state.nodes.tabs;
    if (bar) {
      bar.querySelectorAll(".tab").forEach(function (node) {
        node.classList.toggle("active", node.dataset.tab === tab);
      });
    }
    renderCrumb();
  }

  /** The tabbar, with the real counts the backend reports. */
  function renderTabs() {
    const bar = state.nodes.tabs;
    if (!bar) return;
    const summary = state.data.summary;
    const counts = {
      hypotheses: summary ? summary.hypotheses.total : state.data.hypotheses.length,
      experiments: summary ? summary.experiments : state.data.experiments.length,
      backtests: summary ? summary.backtests : state.data.backtests.length,
      validation: state.data.reports.length,
      evaluation: null,
      pipeline: state.data.strategies.length,
    };
    const labels = {
      hypotheses: "HYPOTHESES", experiments: "EXPERIMENTS", backtests: "BACKTESTS",
      validation: "VALIDATION", evaluation: "EVALUATION", pipeline: "PIPELINE",
    };
    ui.clear(bar);
    TABS.forEach(function (tab) {
      const node = el("span.tab", { dataset: { tab: tab } }, labels[tab] + " ");
      if (counts[tab] !== null && counts[tab] !== undefined) {
        node.appendChild(el("span.n", { text: String(counts[tab]) }));
      }
      if (tab === state.tab) node.classList.add("active");
      node.addEventListener("click", function () { goTab(tab); });
      bar.appendChild(node);
    });
  }

  /** The header breadcrumb: which tab, which selection, which universe. */
  function renderCrumb() {
    const crumb = state.nodes.crumb;
    if (!crumb) return;
    const active = state.data.activeUniverse;
    const parts = [];
    if (state.tab === "experiments") {
      const experiment = experimentById(state.sel.experiment);
      parts.push(state.mode.exp === "new" ? "new experiment"
        : (state.compareOpen ? "comparing " + state.compare.size + " experiments"
          : (experiment ? experiment.name : "all experiments")));
    } else if (state.tab === "hypotheses") {
      const hypothesis = hypothesisById(state.sel.hypothesis);
      parts.push(state.mode.hypo === "new" ? "new hypothesis"
        : (hypothesis ? hypothesis.id + " · " + hypothesis.title : "all hypotheses"));
    } else if (state.tab === "backtests") {
      parts.push(state.mode.bt === "new" ? "new backtest" : (state.sel.backtest || "backtest log"));
    } else {
      parts.push(state.tab);
    }
    if (active) parts.push(active.name || active.id);
    crumb.textContent = "/ " + parts.join(" · ");
  }

  // ------------------------------------------------------------ data loading

  /**
   * Load everything the workspace shows, in parallel. Each call is settled
   * independently: one dead endpoint degrades one panel, and the panel says so
   * instead of showing a plausible-looking zero.
   */
  function loadAll() {
    const ctx = state.ctx;
    const api = ctx.api;
    const opts = { signal: ctx.signal };
    const jobs = [
      ["summary", api.lab.summary(opts)],
      ["hypotheses", api.lab.hypotheses({ include_evidence: true }, opts)],
      ["experiments", api.lab.experiments(null, opts)],
      ["backtests", api.lab.backtests({ limit: 200 }, opts)],
      ["gates", api.lab.gates(opts)],
      ["reports", api.lab.validation({ limit: 200 }, opts)],
      ["strategies", api.lab.strategies(null, opts)],
      ["evaluation", api.lab.evaluation(null, opts)],
      ["screens", api.screens.list(null, opts)],
      ["screenRuns", api.get("/api/screens/runs", { limit: 50 }, opts)],
      ["universes", api.universes.list(null, opts)],
      ["activeUniverse", api.universes.active(opts)],
      ["quality", api.quality.summary(null, opts)],
      ["agentRuns", api.get("/api/agents/runs", { limit: 12 }, opts)],
      ["agentDefinitions", api.get("/api/agents/definitions", { include_stats: false }, opts)],
      ["analyses", api.lab.analyses({ limit: 100 }, opts)],
    ];
    return Promise.all(jobs.map(function (job) {
      return job[1].then(
        function (payload) { return { key: job[0], payload: payload }; },
        function (err) { return { key: job[0], error: err }; }
      );
    })).then(function (results) {
      if (ctx.signal.aborted) return;
      results.forEach(function (result) { absorb(result); });
      state.loaded = true;
      defaultSelections();
      renderTabs();
      renderFunnel();
      renderCrumb();
      renderTab(state.tab);
    });
  }

  /** Fold one settled result into `state.data` / `state.errors`. */
  function absorb(result) {
    const key = result.key;
    if (result.error) {
      if (!cancelled(result.error)) {
        state.errors[key] = result.error;
        console.warn("strategy lab: " + key + " failed", result.error);
      }
      return;
    }
    delete state.errors[key];
    const payload = result.payload;
    switch (key) {
      case "summary": state.data.summary = payload; break;
      case "hypotheses": state.data.hypotheses = payload.hypotheses || []; break;
      case "experiments": state.data.experiments = payload.experiments || []; break;
      case "backtests": state.data.backtests = payload.backtests || []; break;
      case "gates":
        state.data.gates = payload.gates || [];
        state.data.thresholds = payload.thresholds || null;
        break;
      case "reports": state.data.reports = payload.reports || []; break;
      case "strategies": state.data.strategies = payload.strategies || []; break;
      case "evaluation": state.data.evaluation = payload; break;
      case "screens": state.data.screens = payload.screens || []; break;
      case "screenRuns": state.data.screenRuns = payload.runs || []; break;
      case "universes": state.data.universes = payload.universes || []; break;
      case "activeUniverse": state.data.activeUniverse = payload ? payload.universe : null; break;
      case "quality": state.data.quality = payload; break;
      case "agentRuns": state.data.agentRuns = payload.runs || []; break;
      case "agentDefinitions": state.data.agentDefinitions = payload.agents || payload.definitions || []; break;
      case "analyses": state.data.analyses = payload.analyses || []; break;
      default: break;
    }
  }

  /** Reload one slice of the data and re-render the current tab. */
  function reload(keys) {
    const ctx = state.ctx;
    const api = ctx.api;
    const opts = { signal: ctx.signal };
    const builders = {
      summary: function () { return api.lab.summary(opts); },
      hypotheses: function () { return api.lab.hypotheses({ include_evidence: true }, opts); },
      experiments: function () { return api.lab.experiments(null, opts); },
      backtests: function () { return api.lab.backtests({ limit: 200 }, opts); },
      reports: function () { return api.lab.validation({ limit: 200 }, opts); },
      strategies: function () { return api.lab.strategies(null, opts); },
      evaluation: function () { return api.lab.evaluation(null, opts); },
      screens: function () { return api.screens.list(null, opts); },
      screenRuns: function () { return api.get("/api/screens/runs", { limit: 50 }, opts); },
      quality: function () { return api.quality.summary(null, opts); },
      agentRuns: function () { return api.get("/api/agents/runs", { limit: 12 }, opts); },
      analyses: function () { return api.lab.analyses({ limit: 100 }, opts); },
    };
    const wanted = (keys || []).filter(function (key) { return !!builders[key]; });
    return Promise.all(wanted.map(function (key) {
      return builders[key]().then(
        function (payload) { return { key: key, payload: payload }; },
        function (err) { return { key: key, error: err }; }
      );
    })).then(function (results) {
      if (state.ctx.signal.aborted) return;
      results.forEach(absorb);
      renderTabs();
      renderFunnel();
      renderCrumb();
      renderTab(state.tab);
    });
  }

  /** Pick sensible selections once the data is in, without inventing any. */
  function defaultSelections() {
    if (!state.sel.hypothesis && state.data.hypotheses.length) {
      state.sel.hypothesis = state.data.hypotheses[0].id;
    }
    if (!state.sel.experiment && state.data.experiments.length) {
      state.anView = null;
      state.sel.experiment = state.data.experiments[0].id;
    }
    if (!state.sel.backtest && state.data.backtests.length) {
      const complete = state.data.backtests.filter(function (b) { return b.status === "complete"; })[0];
      state.sel.backtest = (complete || state.data.backtests[0]).id;
    }
  }

  // -------------------------------------------------------------- live feed

  /** Subscribe to the `lab` channel and fold its events into the page. */
  function subscribeLive(ctx) {
    if (!ctx.stream) return;
    state.unsubscribe.push(ctx.stream.subscribe("lab"));
    state.unsubscribe.push(ctx.stream.on("lab", onLabEvent));
  }

  /**
   * One `lab` event: an experiment log line, a backtest phase, a validation
   * verdict or a promotion. Log lines stream straight into the run log; the
   * rest refresh only the lists they touch.
   */
  function onLabEvent(payload) {
    if (!payload || !state.loaded) return;
    const kind = payload.kind;
    if (kind === "experiment") {
      if (payload.phase === "started") {
        state.log = { runId: payload.id, experimentId: payload.experiment_id, lines: [] };
        appendLog({ ts: payload.ts, level: "info", message: "run " + payload.id + " started" });
      } else if (payload.phase === "log") {
        if (state.log.runId === null) state.log.runId = payload.id;
        if (!state.log.experimentId) state.log.experimentId = payload.experiment_id;
        appendLog(payload);
      } else if (payload.phase === "complete") {
        appendLog({ ts: payload.ts, level: "info", message: "run complete · acceptance " + (payload.passed_gates ? "passed" : "not met") });
        reload(["summary", "experiments", "backtests", "reports", "evaluation"]);
      } else if (payload.phase === "failed") {
        appendLog({ ts: payload.ts, level: "error", message: payload.error || "run failed" });
        reload(["summary", "experiments", "backtests"]);
      }
      return;
    }
    if (kind === "backtest") {
      if (payload.phase === "complete" || payload.phase === "failed") {
        reload(["summary", "backtests", "evaluation"]);
      }
      return;
    }
    if (kind === "validation") {
      reload(["reports", "evaluation"]);
      return;
    }
    if (kind === "promotion") {
      // A sign-off changes the two-key state, so the cached one is now stale.
      if (payload.backtest_id) delete state.cache.promotion[payload.backtest_id];
      reload(["strategies", "experiments", "reports", "evaluation", "summary"]);
    }
    if (kind === "analysis" && (payload.phase === "complete" || payload.phase === "failed")) {
      if (payload.id) delete state.cache.analysis[payload.id];
      reload(["analyses"]);
    }
  }

  /**
   * Append a line to the live run log and repaint just that box.
   *
   * The finished run's stored log and the streamed lines describe the same
   * events, and the HTTP response can land before the last frames arrive, so a
   * line already on screen (same timestamp and text) is dropped rather than
   * shown twice.
   */
  function appendLog(entry) {
    const line = {
      ts: entry.ts || new Date().toISOString(),
      level: entry.level || "info",
      message: entry.message || "",
    };
    const duplicate = state.log.lines.some(function (existing) {
      return existing.ts === line.ts && existing.message === line.message;
    });
    if (duplicate) return;
    state.log.lines.push(line);
    if (state.log.lines.length > 400) state.log.lines.splice(0, state.log.lines.length - 400);
    paintLog();
  }

  /** Repaint the run-log box in place (it is the only live region). */
  function paintLog() {
    const box = state.nodes.logBox;
    if (!box) return;
    ui.render(box, logLines());
    box.scrollTop = box.scrollHeight;
  }

  /** The log lines, as DOM. */
  function logLines() {
    if (!state.log.lines.length) {
      return el("span.dim", { text: "No run log yet. Running the experiment streams its log here live." });
    }
    return state.log.lines.map(function (line) {
      const time = fmt.toDate(line.ts);
      const tone = line.level === "error" ? "bad" : (line.level === "warn" ? "warn" : "dim");
      return el("div",
        el("span", { class: tone, text: (time ? fmt.etClock(time) : "--:--:--") + " " }),
        el("span", { text: line.message }));
    });
  }

  // ---------------------------------------------------------------- routing

  /** Render whichever tab is showing (the others render when they open). */
  function renderTab(tab) {
    if (!state.loaded) return;
    try {
      if (tab === "hypotheses") renderHypotheses();
      else if (tab === "experiments") renderExperiments();
      else if (tab === "backtests") renderBacktests();
      else if (tab === "validation") renderValidation();
      else if (tab === "evaluation") renderEvaluation();
      else if (tab === "pipeline") renderPipeline();
      renderCrumb();
    } catch (err) {
      console.error("strategy lab: rendering " + tab + " failed", err);
      if (global.toast) global.toast.error("Strategy Lab could not render " + tab + " — " + (err && err.message ? err.message : err));
    }
  }

  // ----------------------------------------------------------------- funnel

  /**
   * The five funnel cards.
   *
   * A funnel counts how far each experiment *got*, so every card shows the
   * number that reached its stage or went past it: RESEARCH is therefore every
   * experiment logged (which is what the EXPERIMENTS tab badge counts too),
   * and each later card is a subset of the one before it. Counting only the
   * experiments parked *at* a stage produced a non-monotone strip whose
   * captions contradicted their own numbers.
   *
   * The per-stage counts come from /api/lab/evaluation's funnel; the client
   * falls back to counting the loaded experiments when that call failed.
   */
  function renderFunnel() {
    const host = state.nodes.funnel;
    if (!host) return;
    ui.clear(host);
    const evaluation = state.data.evaluation;
    const at = {};
    STAGES.concat(["killed"]).forEach(function (stage) { at[stage] = 0; });
    if (evaluation && evaluation.funnel) {
      (evaluation.funnel.stages || []).forEach(function (row) { at[row.stage] = row.count; });
    } else {
      state.data.experiments.forEach(function (exp) {
        if (at[exp.stage] === undefined) at[exp.stage] = 0;
        at[exp.stage] += 1;
      });
    }
    // reached[stage] = at[stage] + everything further down the funnel.
    const counts = {};
    let carried = 0;
    for (let i = STAGES.length - 1; i >= 0; i -= 1) {
      carried += at[STAGES[i]] || 0;
      counts[STAGES[i]] = carried;
    }
    // An experiment that was killed was still logged, so it belongs to the
    // RESEARCH count even though it reached no later stage.
    counts.research += at.killed || 0;
    const total = counts.research;
    const captions = {
      research: "experiments logged",
      validated: total
        ? percent((counts.validated || 0) / total, 1) + " of research"
        : "gates passed",
      paper: counts.validated
        ? percent((counts.paper || 0) / counts.validated, 1) + " of validated"
        : "promoted to paper",
      approved: "awaiting deploy",
      live: (counts.approved || 0) + " approved, "
        + ((counts.live || 0) ? counts.live + " deployed" : "none deployed"),
    };
    STAGES.forEach(function (stage) {
      const value = counts[stage] || 0;
      const card = el("div.fcard", { dataset: { stage: stage } },
        el("label", { text: stage.toUpperCase() }),
        el("span.metricbig", { class: value ? "hi" : "mid", text: String(value) }),
        el("div.dim.mini", { text: captions[stage] }));
      if (state.filters.stage === stage) card.classList.add("on");
      card.addEventListener("click", function () {
        state.filters.stage = state.filters.stage === stage ? null : stage;
        state.compareOpen = false;
        state.mode.exp = "view";
        renderFunnel();
        goTab("experiments");
        renderExperiments();
      });
      host.appendChild(card);
    });
  }

  // ------------------------------------------------------------- hypotheses

  function renderHypotheses() {
    renderHypoList();
    renderHypoDetail();
    renderAgentActivity();
  }

  /** The stage of a run that explains its current state. */
  function tellingStage(run) {
    const stages = run.stages || [];
    const byStatus = function (status) {
      return stages.filter(function (stage) { return stage.status === status; });
    };
    return byStatus("running")[0]
      || byStatus("awaiting_approval")[0]
      || byStatus("failed")[0]
      || stages.filter(function (stage) { return stage.stage_id === run.current_stage_id; })[0]
      || byStatus("succeeded").slice(-1)[0]
      || stages[0]
      || null;
  }

  /** Title line of one activity step: who is (or was last) doing the work. */
  function agentActivityActor(run) {
    const stage = tellingStage(run);
    if (stage && stage.agent_id) {
      const definition = (state.data.agentDefinitions || []).filter(function (item) {
        return item.id === stage.agent_id;
      })[0];
      if (definition) return definition.name;
    }
    if (stage && stage.name) return stage.name;
    return run.name || run.pipeline_id;
  }

  /** Body line of one activity step: what that run is actually doing. */
  function agentActivitySentence(run) {
    const stage = tellingStage(run);
    const label = run.name || run.pipeline_id;
    const progress = run.progress || {};
    const done = progress.total ? progress.completed + " of " + progress.total + " stages" : "";
    const cost = run.cost_usd ? " · $" + fmt.num(run.cost_usd, 2) : "";
    const stamp = " · " + when(run.finished_at || run.started_at || run.created_at);
    if (run.status === "running") {
      return "Running " + (stage ? stage.name : "a stage") + " in " + label
        + (done ? " · " + done : "") + cost + stamp;
    }
    if (run.status === "awaiting_approval") {
      return "Blocked on a human decision at " + (stage ? stage.name : "the approval gate")
        + " in " + label + stamp;
    }
    if (run.status === "paused") {
      return "Paused before " + (stage ? stage.name : "the next stage") + " in " + label + stamp;
    }
    if (run.status === "queued") {
      return "Queued: " + label + (done ? " · " + done : "") + stamp;
    }
    if (run.status === "failed") {
      return "Failed at " + (stage ? stage.name : "an unknown stage") + " in " + label
        + (run.error ? " — " + run.error : "") + cost + stamp;
    }
    if (run.status === "killed") {
      return "Cancelled at " + (stage ? stage.name : "an unknown stage") + " in " + label
        + (run.error ? " — " + run.error : "") + stamp;
    }
    return "Completed " + label + (done ? " · " + done : "") + cost + stamp;
  }

  /**
   * The merged agent-center "Agent activity" panel (reference line 1329).
   *
   * One `.activity-step` per recent pipeline run from `GET /api/agents/runs`:
   * the agent that owns the stage the run is standing on, and what that stage
   * is actually doing right now. Nothing is narrated that the runner has not
   * recorded — a workspace with no runs says so.
   */
  function renderAgentActivity() {
    const panel = state.nodes.agentActivity;
    if (!panel) return;
    ui.clear(panel);
    const runs = state.data.agentRuns || [];
    const autonomous = runs.filter(function (run) { return run.trigger === "schedule"; }).length;
    const live = runs.filter(function (run) {
      return run.status === "running" || run.status === "awaiting_approval" || run.status === "queued";
    }).length;
    const badgeText = live
      ? live + " live"
      : (autonomous ? autonomous + " autonomous" : (runs.length ? "idle" : "none"));
    panel.appendChild(el("div.panel-title", "Agent activity ",
      ui.badge(badgeText, { tone: live ? "yellow" : "" })));

    if (state.errors.agentRuns) {
      panel.appendChild(el("div.activity-step",
        el("b", { text: "Agent Center unreachable" }),
        el("span", { text: String(state.errors.agentRuns.message || state.errors.agentRuns) })));
      return;
    }
    if (!runs.length) {
      panel.appendChild(el("div.activity-step",
        el("b", { text: "No agent runs yet" }),
        el("span", { text: "Start a pipeline in the Agent Center and its stages appear here." })));
      return;
    }
    runs.slice(0, 6).forEach(function (run) {
      const step = el("div.activity-step",
        el("b", { text: agentActivityActor(run) }),
        el("span", { text: agentActivitySentence(run) }));
      step.style.cursor = "pointer";
      step.addEventListener("click", function () {
        state.ctx.router.go("agents", { sub: "runs", params: { run: run.id } });
      });
      panel.appendChild(step);
    });
    panel.appendChild(el("div.activity-step",
      el("b", { text: "Where this comes from" }),
      el("span", { text: "GET /api/agents/runs — the same ledger the Agent Center's run table reads." })));
  }

  /** The hypothesis list: one `.rowitem` per hypothesis, real counts on it. */
  function renderHypoList() {
    const host = state.nodes.hypolist;
    if (!host) return;
    ui.clear(host);
    if (state.errors.hypotheses) {
      host.appendChild(errorNote("Hypotheses could not be loaded", state.errors.hypotheses));
      return;
    }
    const items = state.data.hypotheses;
    if (!items.length) {
      host.appendChild(emptyBox(
        "No hypotheses yet",
        "A hypothesis is a falsifiable statement; every experiment hangs off one.",
        tbtn("+ NEW HYPOTHESIS", function () {
          state.mode.hypo = "new";
          renderHypotheses();
        }, { primary: true, small: true })));
      return;
    }
    items.forEach(function (hypothesis) {
      const experiments = state.data.experiments.filter(function (exp) {
        return exp.hypothesis_id === hypothesis.id;
      });
      const row = el("div.rowitem");
      if (hypothesis.id === state.sel.hypothesis && state.mode.hypo === "view") row.classList.add("sel");
      const pill = el("span.pill", {
        style: "margin-left:auto",
        text: hypothesis.owner || hypothesis.origin || "unassigned",
      });
      if ((hypothesis.owner || "").toLowerCase() === "agent") pill.classList.add("blu");
      row.appendChild(el("div", { style: "display:flex;gap:8px;align-items:center" },
        el("span.hi", { text: hypothesis.id + " · " + hypothesis.title }), pill));
      row.appendChild(el("div.foot", { style: "margin-top:3px" },
        el("span", { text: experiments.length + " experiments · " + hypothesis.evidence_count + " evidence · " }),
        el("span", { class: statusToneFor(hypothesis.status), text: String(hypothesis.status).toUpperCase() })));
      row.addEventListener("click", function () {
        state.sel.hypothesis = hypothesis.id;
        state.mode.hypo = "view";
        goTab("hypotheses", hypothesis.id);
        renderHypotheses();
      });
      host.appendChild(row);
    });
  }

  /** Colour class for a hypothesis status. */
  function statusToneFor(status) {
    if (status === "validated") return "ok";
    if (status === "rejected") return "bad";
    if (status === "archived") return "dim";
    if (status === "testing") return "blu";
    return "";
  }

  /** The hypothesis detail panel, or the create/edit form. */
  function renderHypoDetail() {
    const host = state.nodes.hypodetail;
    if (!host) return;
    // Never overwrite a form the user is filling in (see renderExpDetail).
    if (state.mode.hypo !== "view" && host.querySelector("[data-form='hypothesis']")) return;
    ui.clear(host);
    if (state.mode.hypo === "new" || state.mode.hypo === "edit") {
      host.appendChild(hypothesisForm(state.mode.hypo === "edit" ? hypothesisById(state.sel.hypothesis) : null));
      return;
    }
    const hypothesis = hypothesisById(state.sel.hypothesis);
    if (!hypothesis) {
      host.appendChild(el("div.panel", el("div.ptitle", { text: "HYPOTHESIS" }),
        foot("Select a hypothesis on the left, or create one.")));
      return;
    }
    const experiments = state.data.experiments.filter(function (exp) {
      return exp.hypothesis_id === hypothesis.id;
    });
    const newest = newestByExperiment();
    const reports = reportsByBacktest();
    const panel = el("div.panel");

    panel.appendChild(el("div", { style: "display:flex;align-items:center;gap:10px;flex-wrap:wrap" },
      el("span.hi", { style: "font-size:14px;font-weight:600", text: hypothesis.id }),
      el("span.dim", { text: hypothesis.title }),
      el("span", { style: "margin-left:auto", class: statusToneFor(hypothesis.status),
        text: String(hypothesis.status).toUpperCase() })));

    panel.appendChild(el("div.note.info", { style: "margin:10px 0" },
      el("div.ptitle", { style: "margin-bottom:4px;color:var(--blu)", text: "WOULD FALSIFY THIS" }),
      el("span.hi", { text: hypothesis.falsifier })));

    panel.appendChild(el("div.panel.tight", { style: "margin-bottom:10px" },
      el("label", { text: "STATEMENT" }),
      el("div", { style: "font-family:system-ui;font-size:13px;margin-top:3px", text: hypothesis.statement })));

    if (hypothesis.rationale) {
      panel.appendChild(el("div.panel.tight", { style: "margin-bottom:10px" },
        el("label", { text: "RATIONALE" }),
        el("div", { style: "font-family:system-ui;font-size:13px;margin-top:3px", text: hypothesis.rationale })));
    }

    const provenance = el("div.panel.tight", el("label", { text: "PROVENANCE" }),
      ui.kv("Origin", hypothesis.origin || "—"),
      ui.kv("Owner", hypothesis.owner || "—"),
      ui.kv("Universe", universeName(hypothesis.universe_id)),
      ui.kv("Experiments", String(experiments.length)),
      ui.kv("Created", when(hypothesis.created_at)),
      ui.kv("Updated", when(hypothesis.updated_at)));
    if (hypothesis.tags && hypothesis.tags.length) {
      provenance.appendChild(el("div", { style: "display:flex;gap:6px;flex-wrap:wrap;margin-top:6px" },
        hypothesis.tags.map(function (tag) { return el("span.chip.mini", { text: tag }); })));
    }

    const evidence = el("div.panel.tight", el("label", { text: "LINKED EVIDENCE" }));
    if (!hypothesis.evidence || !hypothesis.evidence.length) {
      evidence.appendChild(foot("Nothing linked yet."));
    } else {
      hypothesis.evidence.forEach(function (link) {
        const right = el("span", { style: "text-align:right" });
        const label = link.label || link.ref;
        if (link.url) {
          right.appendChild(el("a", { href: link.url, target: "_blank", rel: "noreferrer noopener", text: label }));
        } else {
          right.appendChild(el("span.ref", { text: label }));
        }
        const row = el("div.kv", el("span.dim", { text: link.kind }), right);
        const remove = el("span.x", { text: "✕", style: "cursor:pointer;margin-left:8px", title: "detach" });
        remove.addEventListener("click", function () { detachEvidence(hypothesis.id, link.id); });
        right.appendChild(remove);
        evidence.appendChild(row);
      });
    }
    evidence.appendChild(evidenceForm(hypothesis));
    panel.appendChild(el("div.grid.g2", provenance, evidence));

    const list = el("div.panel.tight", { style: "margin-top:10px" },
      el("label", { text: "EXPERIMENTS UNDER THIS HYPOTHESIS" }));
    if (!experiments.length) {
      list.appendChild(foot("No experiments yet. This hypothesis has never been tested.", "padding:6px 0"));
    } else {
      experiments.forEach(function (exp) {
        const record = newest[exp.id];
        const window_ = judgedWindow(record);
        const report = record ? reports[record.id] : null;
        const right = el("span", { class: record ? "" : "dim" },
          record
            ? String(exp.stage).toUpperCase() + " · Sharpe " + ratio(window_ ? window_.sharpe : null)
              + (report ? " · " + report.gates_passed + "/" + report.gates_total + " gates" : "")
            : "no completed run");
        const ref = el("span.ref", { text: exp.name });
        ref.addEventListener("click", function () {
        state.anView = null;
        state.sel.experiment = exp.id;
          state.mode.exp = "view";
          goTab("experiments", exp.id);
          renderExperiments();
        });
        list.appendChild(el("div.kv", { style: "padding:5px 0" }, ref, right));
      });
    }

    const actions = el("div", { style: "display:flex;gap:6px;margin-top:8px;flex-wrap:wrap" },
      tbtn("+ NEW EXPERIMENT", function () {
        state.anView = null;
        state.mode.exp = "new";
        state.cloneFrom = null;
        state.newExperimentHypothesis = hypothesis.id;
        // Drop any form already open so the new hypothesis link takes effect.
        if (state.nodes.expDetail) ui.clear(state.nodes.expDetail);
        goTab("experiments");
        renderExperiments();
      }, { primary: true, small: true }),
      tbtn("EDIT", function () {
        state.mode.hypo = "edit";
        renderHypotheses();
      }, { small: true }));
    HYPOTHESIS_STATES.filter(function (status) { return status !== hypothesis.status; })
      .forEach(function (status) {
        actions.appendChild(tbtn("MARK " + status.toUpperCase(), function () {
          setHypothesisStatus(hypothesis.id, status);
        }, { small: true }));
      });
    actions.appendChild(tbtn("DELETE", function () {
      if (!global.confirm("Delete " + hypothesis.id + "? Its experiments keep their reference.")) return;
      deleteHypothesis(hypothesis.id);
    }, { small: true }));
    list.appendChild(actions);
    panel.appendChild(list);

    if (hypothesis.status_history && hypothesis.status_history.length) {
      const history = el("div.panel.tight", { style: "margin-top:10px" },
        el("label", { text: "STATUS HISTORY" }));
      hypothesis.status_history.slice().reverse().forEach(function (entry) {
        history.appendChild(stat(when(entry.at || entry.ts),
          (entry.actor ? entry.actor + " " : "") + "→ " + String(entry.to || entry.status || "").toUpperCase()
          + (entry.note ? " · " + entry.note : "")));
      });
      panel.appendChild(history);
    }
    host.appendChild(panel);
  }

  /** The add-evidence mini form. */
  function evidenceForm(hypothesis) {
    const kind = select(EVIDENCE_KINDS, "run", { style: "width:110px" });
    const ref = input("", { placeholder: "bt-… / k-… / url" });
    const label = input("", { placeholder: "label (optional)" });
    const row = el("div", { style: "display:flex;gap:6px;margin-top:8px;flex-wrap:wrap" },
      kind, ref, label,
      tbtn("+ LINK", function () {
        if (!ref.value.trim()) {
          global.toast.warn("Evidence needs a reference.");
          return;
        }
        attachEvidence(hypothesis.id, {
          kind: kind.value,
          ref: ref.value.trim(),
          label: label.value.trim() || null,
        });
      }, { small: true }));
    return row;
  }

  /** Create/edit form for a hypothesis. Falsifier is required by the domain. */
  function hypothesisForm(existing) {
    const editing = !!existing;
    const title = input(editing ? existing.title : "", { placeholder: "Momentum decay is crowding-conditioned" });
    const statement = el("textarea", { rows: "2", placeholder: "Conditioning 12-1 momentum on low crowding slows alpha decay." });
    statement.value = editing ? existing.statement : "";
    const falsifier = input(editing ? existing.falsifier : "", { placeholder: "crowding percentile adds no incremental IC over plain momentum" });
    const rationale = el("textarea", { rows: "2", placeholder: "Why this is worth testing (optional)." });
    rationale.value = editing && existing.rationale ? existing.rationale : "";
    const owner = input(editing ? (existing.owner || "") : "", { placeholder: "owner" });
    const origin = input(editing ? (existing.origin || "") : "", { placeholder: "manual · lit scan · agent" });
    const universe = select([{ value: "", label: "— none —" }].concat(universeOptions()),
      editing ? existing.universe_id : (state.data.activeUniverse ? state.data.activeUniverse.id : ""));
    const tags = input(editing && existing.tags ? existing.tags.join(", ") : "", { placeholder: "momentum, crowding" });
    const status = select(HYPOTHESIS_STATES, editing ? existing.status : "active");

    const panel = el("div.panel", { dataset: { form: "hypothesis" } });
    panel.appendChild(el("div", { style: "display:flex;align-items:center;gap:10px" },
      el("span.hi", { style: "font-size:14px;font-weight:600", text: editing ? "Edit hypothesis" : "New hypothesis" }),
      el("span.dim", { text: editing ? existing.id : "draft" }),
      tbtn("CANCEL", function () {
        state.mode.hypo = "view";
        renderHypotheses();
      }, { small: true, style: "margin-left:auto" })));

    panel.appendChild(field("TITLE", title, { style: "margin-top:12px" }));
    panel.appendChild(field("STATEMENT", statement));
    panel.appendChild(el("div.note.info", { style: "margin:10px 0" },
      el("label", { style: "color:var(--blu)", text: "FALSIFIER · REQUIRED" }),
      falsifier,
      foot("What evidence would kill this? The backend refuses a hypothesis without one.", "margin-top:5px")));
    panel.appendChild(field("RATIONALE", rationale));
    panel.appendChild(el("div.grid.g2", { style: "gap:8px" },
      field("OWNER", owner), field("ORIGIN", origin),
      field("UNIVERSE SCOPE", universe), field("TAGS", tags)));
    if (!editing) panel.appendChild(field("STATUS", status));

    const save = tbtn(editing ? "SAVE CHANGES" : "SAVE AND OPEN", function () {
      const body = {
        title: title.value.trim(),
        statement: statement.value.trim(),
        falsifier: falsifier.value.trim(),
        rationale: rationale.value.trim() || null,
        owner: owner.value.trim() || null,
        origin: origin.value.trim() || null,
        universe_id: universe.value || null,
        tags: tags.value.split(",").map(function (t) { return t.trim(); }).filter(Boolean),
      };
      if (!body.title || !body.statement || !body.falsifier) {
        global.toast.warn("Title, statement and falsifier are all required.");
        return;
      }
      if (editing) {
        updateHypothesis(existing.id, body);
      } else {
        body.status = status.value;
        createHypothesis(body);
      }
    }, { primary: true });
    panel.appendChild(el("div", { style: "display:flex;gap:8px;margin-top:10px" }, save));
    return panel;
  }

  // --- hypothesis actions ---------------------------------------------------

  function createHypothesis(body) {
    state.ctx.api.lab.createHypothesis(body, { signal: state.ctx.signal }).then(function (created) {
      global.toast.ok("Created " + created.id);
      state.sel.hypothesis = created.id;
      state.mode.hypo = "view";
      return reload(["hypotheses", "summary"]);
    }).catch(function (err) { fail("Could not create the hypothesis", err); });
  }

  function updateHypothesis(id, body) {
    state.ctx.api.lab.updateHypothesis(id, body, { signal: state.ctx.signal }).then(function () {
      global.toast.ok("Saved " + id);
      state.mode.hypo = "view";
      return reload(["hypotheses"]);
    }).catch(function (err) { fail("Could not save the hypothesis", err); });
  }

  function setHypothesisStatus(id, status) {
    state.ctx.api.lab.setHypothesisStatus(id, { status: status }, { signal: state.ctx.signal })
      .then(function () {
        global.toast.ok(id + " → " + status.toUpperCase());
        return reload(["hypotheses", "summary"]);
      })
      .catch(function (err) { fail("Transition refused", err); });
  }

  function deleteHypothesis(id) {
    state.ctx.api.lab.deleteHypothesis(id, { signal: state.ctx.signal }).then(function () {
      global.toast.ok("Deleted " + id);
      if (state.sel.hypothesis === id) state.sel.hypothesis = null;
      return reload(["hypotheses", "summary"]);
    }).catch(function (err) { fail("Could not delete the hypothesis", err); });
  }

  function attachEvidence(id, body) {
    state.ctx.api.lab.addEvidence(id, body, { signal: state.ctx.signal }).then(function () {
      global.toast.ok("Evidence linked");
      return reload(["hypotheses"]);
    }).catch(function (err) { fail("Could not link evidence", err); });
  }

  function detachEvidence(hypothesisId, evidenceId) {
    state.ctx.api.del("/api/lab/hypotheses/" + encodeURIComponent(hypothesisId)
      + "/evidence/" + encodeURIComponent(evidenceId), { signal: state.ctx.signal })
      .then(function () {
        global.toast.ok("Evidence detached");
        return reload(["hypotheses"]);
      })
      .catch(function (err) { fail("Could not detach evidence", err); });
  }

  // ------------------------------------------------------------ experiments

  function renderExperiments() {
    const nodes = state.nodes;
    if (state.compareOpen) {
      nodes.expSplit.style.display = "none";
      nodes.expFull.style.display = "block";
    } else {
      nodes.expSplit.style.display = "grid";
      nodes.expFull.style.display = "none";
      ui.clear(nodes.expFull);
    }
    renderExpList();
    renderExpDetail();
    renderScreens();
  }

  /**
   * Whether an experiment reached `stage` — i.e. it sits there or further
   * down the funnel. This is what the funnel cards count, so clicking a card
   * has to select the same population its number came from. "killed" is only
   * ever reached by RESEARCH, which every logged experiment reaches.
   */
  function reachedStage(exp, stage) {
    const wanted = STAGES.indexOf(stage);
    if (wanted < 0) return exp.stage === stage;
    const actual = STAGES.indexOf(exp.stage);
    if (actual < 0) return wanted === 0; // killed / unknown: research only
    return actual >= wanted;
  }

  /** Experiments that survive the funnel-stage, text and origin filters. */
  function filteredExperiments() {
    const query = state.filters.expQuery.trim().toLowerCase();
    return state.data.experiments.filter(function (exp) {
      if (state.filters.stage && !reachedStage(exp, state.filters.stage)) return false;
      if (state.filters.expOrigin !== "all" && originOf(exp) !== state.filters.expOrigin) return false;
      if (!query) return true;
      const haystack = [exp.name, exp.hypothesis_id, exp.universe_id, exp.signal, exp.owner]
        .filter(Boolean).join(" ").toLowerCase();
      return haystack.indexOf(query) >= 0;
    });
  }

  function renderExpList() {
    const host = state.nodes.explist;
    if (!host) return;
    ui.clear(host);
    ui.clear(state.nodes.expFilter);
    if (state.filters.stage) {
      const chip = el("span.chip.on", {
        text: "reached " + state.filters.stage + " ✕", style: "cursor:pointer",
      });
      chip.addEventListener("click", function () {
        state.filters.stage = null;
        renderFunnel();
        renderExpList();
      });
      state.nodes.expFilter.appendChild(el("div.kv", { style: "margin-bottom:8px" },
        chip, foot("click again to clear")));
    }
    if (state.errors.experiments) {
      host.appendChild(errorNote("Experiments could not be loaded", state.errors.experiments));
      state.nodes.expCount.textContent = "—";
      return;
    }
    const rows = filteredExperiments();
    state.nodes.expCount.textContent = rows.length + " of " + state.data.experiments.length;
    if (!rows.length) {
      host.appendChild(emptyBox(
        state.data.experiments.length ? "Nothing at this filter" : "No experiments yet",
        state.data.experiments.length
          ? "Experiments arrive here once they exist at this stage."
          : "An experiment pins a reproducible backtest configuration to a hypothesis.",
        tbtn("+ NEW EXPERIMENT", function () {
          state.anView = null;
          state.mode.exp = "new";
          clearExperimentSeed();
          renderExperiments();
        }, { primary: true, small: true })));
      return;
    }
    const newest = newestByExperiment();
    const reports = reportsByBacktest();
    rows.forEach(function (exp) {
      const record = newest[exp.id];
      const window_ = judgedWindow(record);
      const report = record ? reports[record.id] : null;
      const row = el("div.rowitem");
      if (exp.id === state.sel.experiment && state.mode.exp === "view" && !state.compareOpen) row.classList.add("sel");
      const check = el("input", { type: "checkbox", dataset: { cmp: exp.id } });
      check.checked = state.compare.has(exp.id);
      check.addEventListener("change", function () {
        if (check.checked) state.compare.add(exp.id);
        else state.compare.delete(exp.id);
        state.nodes.cmpn.textContent = String(state.compare.size);
      });
      check.addEventListener("click", function (event) { event.stopPropagation(); });
      const pill = el("span.pill", { style: "margin-left:auto", text: String(exp.stage).toUpperCase() });
      const tone = stageTone(exp.stage);
      if (tone) pill.classList.add(tone);
      row.appendChild(el("div", { style: "display:flex;gap:8px;align-items:center" },
        check, el("span.hi", { text: exp.name }), pill));
      const origin = el("span.pill.mini", { text: originOf(exp).toUpperCase() });
      if (originOf(exp) === "agent") origin.classList.add("blu");
      row.appendChild(el("div.foot", { style: "margin-top:3px" }, origin,
        el("span", {
          text: " " + (exp.hypothesis_id || "no hypothesis")
            + " · Sharpe " + ratio(window_ ? window_.sharpe : null)
            + " · gates " + (report ? report.gates_passed + "/" + report.gates_total : "—"),
        })));
      row.addEventListener("click", function () {
        state.anView = null;
        state.sel.experiment = exp.id;
        state.mode.exp = "view";
        state.compareOpen = false;
        goTab("experiments", exp.id);
        renderExperiments();
      });
      host.appendChild(row);
    });
    state.nodes.cmpn.textContent = String(state.compare.size);
  }

  /** Detail for the selected experiment, or the create form. */
  function renderExpDetail() {
    const host = state.nodes.expDetail;
    if (!host) return;
    // A half-filled form is user input: a live event that refreshes the page
    // must not wipe it, so an open form is left exactly as it is.
    if (state.mode.exp === "new" && host.querySelector("[data-form='experiment']")) return;
    ui.clear(host);
    state.nodes.logBox = null;
    if (state.mode.exp === "new") {
      host.appendChild(experimentForm());
      return;
    }
    const exp = experimentById(state.sel.experiment);
    if (!exp) {
      host.appendChild(el("div.ptitle", { text: "EXPERIMENT" }));
      host.appendChild(foot("Select an experiment on the left, or create one."));
      return;
    }
    // An open analysis replaces the detail in place, exactly as the reference
    // does; `#an-back` is the only way out, so the two can never be confused.
    if (state.anView && state.anView.id) {
      renderAnalysisView(host, exp);
      return;
    }
    const newest = newestByExperiment();
    const record = newest[exp.id];
    const reports = reportsByBacktest();
    const report = record ? reports[record.id] : null;
    const window_ = judgedWindow(record);
    const stats = record ? record.stats : null;

    const hypRef = el("span.ref", { text: exp.hypothesis_id || "none" });
    if (exp.hypothesis_id) {
      hypRef.addEventListener("click", function () {
        state.sel.hypothesis = exp.hypothesis_id;
        state.mode.hypo = "view";
        goTab("hypotheses", exp.hypothesis_id);
        renderHypotheses();
      });
    }
    const stagePill = el("span.pill", { text: String(exp.stage).toUpperCase() });
    const tone = stageTone(exp.stage);
    if (tone) stagePill.classList.add(tone);
    host.appendChild(el("div", { style: "display:flex;align-items:center;gap:10px;flex-wrap:wrap" },
      el("span.hi", { style: "font-family:system-ui;font-size:15px;font-weight:600", text: exp.name }),
      stagePill,
      el("span.foot", { style: "margin-left:auto" }, "hypothesis ", hypRef)));
    host.appendChild(foot(exp.id + " · " + (exp.signal || "no signal")
      + " · " + (exp.universe_id ? universeName(exp.universe_id) : ((exp.config && exp.config.symbols.length) + " symbols"))
      + " · created " + when(exp.created_at), "margin:2px 0 10px"));

    host.appendChild(el("div", { style: "display:flex;gap:22px;margin-bottom:10px;flex-wrap:wrap" },
      metric("OOS SHARPE", window_ ? ratio(window_.sharpe) : "—", window_ ? "ok" : "dim"),
      metric("DEFLATED", report ? ratio(deflatedOf(report)) : "—", report ? "hi" : "dim"),
      metric("MAX DD", stats ? percent(stats.max_drawdown) : "—", stats ? "bad" : "dim"),
      metric("TRADES", stats ? fmt.int(stats.trades) : "—", stats ? "blu" : "dim"),
      metric("UNIVERSE", exp.universe_id ? universeName(exp.universe_id) : "explicit", "hi")));
    if (!record) {
      host.appendChild(el("div.note", { style: "margin-bottom:10px",
        text: "No completed backtest yet — every figure above stays empty until this experiment runs." }));
    }

    host.appendChild(strategyPanel(exp));
    host.appendChild(acceptancePanel(exp));
    host.appendChild(backtestsPanel(exp));
    host.appendChild(analysesPanel(exp, record));
    host.appendChild(runsPanel(exp));

    host.appendChild(el("div", { style: "display:flex;gap:8px;flex-wrap:wrap" },
      tbtn("⑂ SEND TO PIPELINE", function () {
        if (record) state.sel.backtest = record.id;
        goTab("pipeline");
        renderPipeline();
      }, { primary: true, small: true, disabled: !record }),
      tbtn("CLONE & EDIT", function () {
        state.anView = null;
        state.mode.exp = "new";
        state.cloneFrom = exp;
        renderExperiments();
      }, { small: true }),
      tbtn("COMPARE WITH…", function () {
        state.compare.add(exp.id);
        state.nodes.cmpn.textContent = String(state.compare.size);
        if (state.compare.size < 2) {
          global.toast.warn("Tick a second experiment in the list to compare.");
          return;
        }
        openCompare();
      }, { small: true }),
      tbtn("DELETE", function () {
        if (!global.confirm("Delete experiment " + exp.name + " and its runs?")) return;
        state.ctx.api.lab.deleteExperiment(exp.id, { signal: state.ctx.signal }).then(function () {
          global.toast.ok("Deleted " + exp.name);
        state.anView = null;
        state.sel.experiment = null;
          return reload(["experiments", "summary", "backtests", "evaluation"]);
        }).catch(function (err) { fail("Could not delete the experiment", err); });
      }, { small: true })));
  }

  /** STRATEGY panel: the persisted config as rules, or as the stored JSON. */
  function strategyPanel(exp) {
    const config = exp.config || {};
    const signal = config.signal || {};
    const body = el("div", { id: "stratbody" });

    function paintRules() {
      ui.clear(body);
      const rules = [];
      rules.push(["signal", signal.kind, "lookback", signal.lookback + "d"]);
      if (signal.skip) rules.push(["signal", "skip", "=", signal.skip + "d"]);
      if (signal.kind === "ma_cross") rules.push(["signal", "sma", "=", signal.fast + "/" + signal.slow]);
      if (signal.expression) rules.push(["expression", signal.expression, "", ""]);
      if (signal.filter_expression) rules.push(["filter", signal.filter_expression, "", ""]);
      if (signal.top_n) rules.push(["rank", "top n", "=", String(signal.top_n)]);
      if (signal.min_signal !== null && signal.min_signal !== undefined) {
        rules.push(["gate", "min signal", ">=", String(signal.min_signal)]);
      }
      rules.push(["weight", "", "", signal.weighting]);
      rules.push(["rebalance", "", "", config.rebalance]);
      rules.push(["universe", "", "", config.universe_id || ((config.symbols || []).length + " symbols")]);
      rules.push(["benchmark", "", "", config.benchmark || "none"]);
      rules.push(["costs", "bps", "=", ((config.costs || {}).commission_bps + (config.costs || {}).slippage_bps) + " one-way"]);
      rules.push(["split", "OOS", "=", config.oos_fraction ? percent(config.oos_fraction, 0) : (config.oos_start || "none")]);
      rules.forEach(function (rule) { body.appendChild(crow(rule)); });
      body.appendChild(foot("Rules are read from the persisted configuration — this is exactly what the run executed."));
    }

    function paintCode() {
      ui.clear(body);
      body.appendChild(el("div.code", { text: JSON.stringify(config, null, 2) }));
      body.appendChild(foot("The stored BacktestConfig, verbatim. Re-running this JSON reproduces the run.", "margin-top:6px"));
    }

    const rulesChip = el("span.chip.mini", { text: "RULES", style: "cursor:pointer" });
    const codeChip = el("span.chip.mini", { text: "CODE", style: "cursor:pointer" });
    if (state.strategyView === "code") codeChip.classList.add("on"); else rulesChip.classList.add("on");
    rulesChip.addEventListener("click", function () {
      state.strategyView = "rules";
      rulesChip.classList.add("on");
      codeChip.classList.remove("on");
      paintRules();
    });
    codeChip.addEventListener("click", function () {
      state.strategyView = "code";
      codeChip.classList.add("on");
      rulesChip.classList.remove("on");
      paintCode();
    });
    if (state.strategyView === "code") paintCode(); else paintRules();

    return el("div.panel.tight", { style: "margin-bottom:10px" },
      el("div.ptitle", { style: "display:flex" },
        el("span", { text: "STRATEGY" }),
        el("span", { style: "margin-left:auto" }, rulesChip, " ", codeChip)),
      body);
  }

  /** The experiment's own acceptance criteria (what "passed" means for it). */
  function acceptancePanel(exp) {
    const criteria = exp.criteria || {};
    return el("div.panel.tight", { style: "margin-bottom:10px" },
      el("label", { text: "ACCEPTANCE CRITERIA · APPLIED TO EVERY RUN" }),
      ui.kv("Min OOS Sharpe", ratio(criteria.min_oos_sharpe)),
      ui.kv("Max drawdown", percent(criteria.max_drawdown)),
      ui.kv("Min trades", fmt.int(criteria.min_trades)),
      ui.kv("IC horizon", exp.horizon + " bars"),
      ui.kv("Kind", String(exp.kind).toUpperCase()));
  }

  /** BACKTESTS table for this experiment + "run with current config". */
  function backtestsPanel(exp) {
    const runs = state.data.backtests.filter(function (b) { return b.experiment_id === exp.id; });
    const reports = reportsByBacktest();
    const panel = el("div.panel.tight", { style: "margin-bottom:10px" },
      el("label", { text: "BACKTESTS" }));
    if (!runs.length) {
      panel.appendChild(foot("No backtest has been run for this experiment yet.", "padding:6px 0"));
    } else {
      panel.appendChild(ui.table({
        tree: "tl",
        columns: [
          { label: "RUN", render: function (row) {
            const ref = el("span.ref", { text: row.id });
            ref.addEventListener("click", function () {
              state.sel.backtest = row.id;
              state.mode.bt = "view";
              goTab("backtests", row.id);
              renderBacktests();
            });
            return ref;
          } },
          { label: "DATE", render: function (row) { return el("span.dim", { text: when(row.finished_at || row.created_at) }); } },
          { label: "SHARPE", num: true, render: function (row) {
            const w = judgedWindow(row);
            return w ? ratio(w.sharpe) : "—";
          } },
          { label: "DEFL", num: true, render: function (row) {
            return ratio(deflatedOf(reports[row.id]));
          } },
          { label: "MAX DD", num: true, render: function (row) {
            return row.stats ? percent(row.stats.max_drawdown) : "—";
          } },
          { label: "STATUS", num: true, render: function (row) {
            return el("span", { class: runTone(row.status), text: row.status });
          } },
        ],
        rows: runs,
      }));
    }
    const runBtn = tbtn("▷ RUN BACKTEST WITH CURRENT CONFIG", function () {
      runBacktestForExperiment(exp, runBtn);
    }, { newStyle: true, style: "margin-top:6px" });
    panel.appendChild(runBtn);
    return panel;
  }

  // ------------------------------------------------------------- analyses

  /** The analyses attached to one experiment, newest first. */
  function analysesFor(exp) {
    return (state.data.analyses || []).filter(function (row) {
      return row.experiment_id === exp.id
        || (row.backtest_id && state.data.backtests.some(function (b) {
          return b.id === row.backtest_id && b.experiment_id === exp.id;
        }));
    });
  }

  /** Human label for an analysis kind. */
  function analysisLabel(kind) {
    if (kind === "sweep") return "SWEEP";
    if (kind === "walk_forward") return "WALK-FORWARD";
    if (kind === "monte_carlo") return "MONTE CARLO";
    return String(kind).toUpperCase();
  }

  /**
   * ANALYSES panel (reference line 3508): the chip row of everything attached
   * to this experiment plus the three "+" controls that create one.
   *
   * A single backtest is one observation; each of these turns it into a
   * distribution. Every chip opens the analysis in place, replacing the detail
   * with its own view and an `#an-back` control.
   */
  function analysesPanel(exp, record) {
    const panel = el("div.panel.tight", { style: "margin-bottom:10px" },
      el("label", { text: "ANALYSES" }));
    if (state.errors.analyses) {
      panel.appendChild(errorNote("Analyses could not be loaded", state.errors.analyses));
      return panel;
    }
    const rows = analysesFor(exp);
    const chips = el("div", { style: "display:flex;gap:6px;flex-wrap:wrap;margin-top:4px" });
    if (!rows.length) {
      chips.appendChild(foot("No analyses yet — a single run is one observation."));
    }
    rows.forEach(function (row) {
      const chip = el("span.chip", { style: "cursor:pointer", dataset: { an: row.id },
        text: analysisLabel(row.kind) + " " + row.chip });
      if (row.tone) chip.classList.add(row.tone);
      chip.addEventListener("click", function () { openAnalysis(row.id); });
      chips.appendChild(chip);
    });
    panel.appendChild(chips);

    if (!record) {
      panel.appendChild(el("div.note", { style: "margin-top:8px",
        text: "Run a backtest first — every analysis re-uses the run's configuration and its price panel." }));
      return panel;
    }
    const actions = el("div", { style: "display:flex;gap:6px;margin-top:8px;flex-wrap:wrap" });
    const sweepBtn = tbtn("+ SWEEP", function () {
      startAnalysis("sweep", exp, record, sweepBtn);
    }, { small: true });
    const mcBtn = tbtn("+ MONTE CARLO", function () {
      startAnalysis("monte_carlo", exp, record, mcBtn);
    }, { small: true });
    const wfaBtn = tbtn("+ WALK-FORWARD", function () {
      startAnalysis("walk_forward", exp, record, wfaBtn);
    }, { small: true });
    actions.appendChild(sweepBtn);
    actions.appendChild(mcBtn);
    actions.appendChild(wfaBtn);
    panel.appendChild(actions);
    panel.appendChild(foot("Sweep and walk-forward re-run real backtests over "
      + record.id + "'s price panel; Monte Carlo block-bootstraps its return series. "
      + "Sweep cells count as trials in the deflation ledger — resamples do not.",
      "margin-top:6px"));
    return panel;
  }

  /** Which signal parameter this configuration can meaningfully be swept on. */
  function sweepAxesFor(config) {
    const signal = (config && config.signal) || {};
    const kind = signal.kind;
    if (kind === "ma_cross") {
      return [
        { path: "signal.fast", values: scaleAround(signal.fast || 50, [0.4, 0.7, 1, 1.4, 2]) },
        { path: "signal.slow", values: scaleAround(signal.slow || 200, [0.5, 0.75, 1, 1.5, 2]) },
      ];
    }
    const lookbacks = scaleAround(signal.lookback || 252, [0.25, 0.5, 0.75, 1, 1.5, 2]);
    if (kind === "momentum") {
      return [
        { path: "signal.lookback", values: lookbacks },
        { path: "signal.skip", values: [3, 6, 12, 21, 42].filter(function (v) { return v > 0; }) },
      ];
    }
    return [{ path: "signal.lookback", values: lookbacks }];
  }

  /** Scale a base parameter by factors, de-duplicated and kept positive. */
  function scaleAround(base, factors) {
    const out = [];
    factors.forEach(function (factor) {
      const value = Math.max(2, Math.round(Number(base) * factor));
      if (out.indexOf(value) < 0) out.push(value);
    });
    return out;
  }

  /** Kick off one analysis against the experiment's newest completed run. */
  function startAnalysis(kind, exp, record, button) {
    if (state.busy.analysis) return;
    state.busy.analysis = true;
    const original = button.textContent;
    button.textContent = "RUNNING…";
    const api = state.ctx.api;
    const opts = { signal: state.ctx.signal, timeoutMs: 600000 };
    const base = {
      backtest_id: record.id,
      experiment_id: exp.id,
      hypothesis_id: exp.hypothesis_id || null,
    };
    let request;
    if (kind === "sweep") {
      request = api.lab.runSweep(Object.assign({
        spec: { axes: sweepAxesFor(record.config), metric: "sharpe", window: "out_of_sample" },
      }, base), opts);
    } else if (kind === "walk_forward") {
      request = api.lab.runWalkForward(Object.assign({
        spec: { folds: 5, train_fraction: 0.6, anchored: true, metric: "sharpe",
          axes: sweepAxesFor(record.config).slice(0, 1) },
      }, base), opts);
    } else {
      request = api.post("/api/lab/analyses/monte-carlo",
        Object.assign({ paths: 5000, shown: 200 }, base), opts);
    }
    request.then(function (created) {
      global.toast.ok(analysisLabel(kind) + " complete — " + (created.chip || created.id));
      state.anView = { kind: kind, id: created.id };
      return reload(["analyses"]);
    }).catch(function (err) {
      button.textContent = original;
      fail(analysisLabel(kind) + " failed", err);
    }).finally(function () {
      state.busy.analysis = false;
    });
  }

  /** Open one stored analysis in place, fetching its payload if needed. */
  function openAnalysis(analysisId) {
    const cached = state.cache.analysis[analysisId];
    state.anView = { id: analysisId, kind: cached ? cached.kind : null };
    renderExpDetail();
  }

  /** Fetch one analysis payload once, then repaint the view showing it. */
  function fetchAnalysis(analysisId) {
    if (state.busy["analysis:" + analysisId]) return;
    state.busy["analysis:" + analysisId] = true;
    state.ctx.api.lab.analysis(analysisId, { signal: state.ctx.signal })
      .then(function (payload) {
        state.cache.analysis[analysisId] = payload;
      })
      .catch(function (err) {
        if (cancelled(err)) return;
        state.cache.analysis[analysisId] = { error: err };
      })
      .finally(function () {
        delete state.busy["analysis:" + analysisId];
        if (state.anView && state.anView.id === analysisId) renderExpDetail();
      });
  }

  /**
   * Render one analysis in place of the experiment detail (reference line
   * 3618): a back control, the analysis' own title, and its view.
   */
  function renderAnalysisView(host, exp) {
    const analysisId = state.anView.id;
    const payload = state.cache.analysis[analysisId];
    const head = el("div", { style: "display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap" });
    head.appendChild(tbtn("← " + exp.name, function () {
      state.anView = null;
      renderExpDetail();
    }, { small: true, id: "an-back" }));
    // The stored name usually already starts with the kind ("Monte Carlo · …"),
    // so prefixing it again would read "MONTE CARLO · Monte Carlo · …".
    let title = "ANALYSIS";
    if (payload && !payload.error) {
      const kindLabel = analysisLabel(payload.kind);
      const name = String(payload.name || "");
      title = name.toLowerCase().indexOf(kindLabel.toLowerCase().replace("-", " ")) === 0
        ? name
        : kindLabel + (name ? " · " + name : "");
    }
    head.appendChild(el("span.hi", {
      style: "font-family:system-ui;font-size:14px;font-weight:600", text: title }));
    if (payload && !payload.error) {
      head.appendChild(el("span.foot", { style: "margin-left:auto",
        text: payload.status + " · " + when(payload.created_at)
          + (payload.duration_ms ? " · " + fmt.num(payload.duration_ms / 1000, 1) + "s" : "") }));
    }
    host.appendChild(head);

    if (!payload) {
      // Reached both by clicking a chip and by finishing a "+" run, so the
      // fetch is kicked off here rather than only in the click handler.
      host.appendChild(foot("Loading the analysis…"));
      fetchAnalysis(analysisId);
      return;
    }
    if (payload.error) {
      host.appendChild(errorNote("The analysis could not be loaded", payload.error));
      return;
    }
    if (payload.status === "failed") {
      host.appendChild(el("div.note.warn", { text: "⚠ " + (payload.error || "the analysis failed") }));
      return;
    }
    (payload.warnings || []).forEach(function (warning) {
      host.appendChild(el("div.note.warn", { style: "margin-bottom:8px", text: "⚠ " + warning }));
    });
    if (payload.kind === "sweep") host.appendChild(sweepView(payload));
    else if (payload.kind === "walk_forward") host.appendChild(walkForwardView(payload));
    else host.appendChild(monteCarloView(payload));

    host.appendChild(el("div", { style: "display:flex;gap:8px;margin-top:10px" },
      tbtn("DELETE ANALYSIS", function () {
        if (!global.confirm("Delete this analysis?")) return;
        state.ctx.api.lab.deleteAnalysis(analysisId, { signal: state.ctx.signal })
          .then(function () {
            delete state.cache.analysis[analysisId];
            state.anView = null;
            global.toast.ok("Analysis deleted");
            return reload(["analyses"]);
          })
          .catch(function (err) { fail("Could not delete the analysis", err); })
      }, { small: true })));
  }

  /** Background/foreground colours for one sweep cell, by metric value. */
  function sweepShade(value, best) {
    if (value === null || value === undefined || isNaN(Number(value))) {
      return ["#0d1622", "#3a4a63"];
    }
    const share = best > 0 ? Number(value) / best : 0;
    if (share >= 0.95) return ["#2ee6a8", "#04120c"];
    if (share >= 0.8) return ["#267a50", "#d9f5ea"];
    if (share >= 0.6) return ["#1f5c3e", "#d9f5ea"];
    if (share >= 0.35) return ["#1a4732", "#9fd8c0"];
    if (share > 0) return ["#12261f", "#5c8a78"];
    return ["#0d1622", "#3a4a63"];
  }

  /** SWEEP view: the metric surface as a heat map, plus its robustness verdict. */
  function sweepView(payload) {
    const wrap = el("div");
    const spec = payload.spec || {};
    const axes = spec.axes || [];
    const cells = payload.cells || [];
    const summary = payload.summary || {};
    const best = Number(summary.best_metric || 0);
    const metric = summary.metric || "sharpe";

    const grid = el("div.panel.tight", { style: "margin-bottom:10px" },
      el("label", { text: (metric + " · " + (spec.window || "full") + " window · "
        + axes.map(function (a) { return a.path; }).join(" × ")).toUpperCase() }));

    if (axes.length === 2) {
      const cols = axes[0].values;
      const rows = axes[1].values;
      const table = el("div", { style: "display:grid;grid-template-columns:60px repeat("
        + cols.length + ",1fr);gap:2px;text-align:center;font-size:10px;margin-top:6px" });
      table.appendChild(el("div"));
      cols.forEach(function (value) { table.appendChild(el("div.dim", { text: String(value) })); });
      rows.forEach(function (rowValue) {
        table.appendChild(el("div.dim", { style: "text-align:right;padding-right:5px",
          text: String(rowValue) }));
        cols.forEach(function (colValue) {
          const cell = cells.filter(function (c) {
            return c.params && c.params[axes[0].path] === colValue
              && c.params[axes[1].path] === rowValue;
          })[0];
          const value = cell && cell.metric !== null && cell.metric !== undefined ? cell.metric : null;
          const shade = sweepShade(value, best);
          const isBest = cell && summary.best_params
            && JSON.stringify(cell.params) === JSON.stringify(summary.best_params);
          const box = el("div", {
            style: "background:" + shade[0] + ";color:" + shade[1] + ";border:"
              + (isBest ? "2px solid #e8edf7" : "1px solid transparent")
              + ";border-radius:3px;padding:6px 0",
            text: value === null ? "—" : fmt.num(value, 2),
          });
          if (cell && cell.status === "failed") box.title = cell.error || "cell failed";
          table.appendChild(box);
        });
      });
      grid.appendChild(table);
    } else {
      grid.appendChild(ui.table({
        tree: "tl",
        columns: [
          { label: "CELL", render: function (row) { return row.label || "base"; } },
          { label: metric.toUpperCase(), num: true, render: function (row) {
            return row.metric === null || row.metric === undefined ? "—" : fmt.num(row.metric, 3);
          } },
          { label: "MAX DD", num: true, render: function (row) {
            return row.stats ? percent(row.stats.max_drawdown) : "—";
          } },
          { label: "TRADES", num: true, render: function (row) {
            return row.trades === undefined ? "—" : fmt.int(row.trades);
          } },
        ],
        rows: cells,
        empty: "No cells were evaluated.",
      }));
    }
    grid.appendChild(foot("Each cell is a real backtest over the same price panel, "
      + "scored on its " + (spec.window || "full") + " window. "
      + summary.evaluated + " of " + summary.cells + " cells scored"
      + (summary.failed ? ", " + summary.failed + " failed" : "") + ".", "margin-top:6px"));
    wrap.appendChild(grid);

    const left = el("div.panel.tight", el("label", { text: "BEST CELL" }));
    left.appendChild(ui.kv("Parameters", summary.best_label || "base configuration", { tone: "hi" }));
    left.appendChild(ui.kv("Best " + metric,
      summary.best_metric === null || summary.best_metric === undefined
        ? "—" : fmt.num(summary.best_metric, 3), { tone: "hi" }));
    left.appendChild(ui.kv("Median cell",
      summary.median_metric === undefined ? "—" : fmt.num(summary.median_metric, 3)));
    left.appendChild(ui.kv("Worst cell",
      summary.worst_metric === undefined ? "—" : fmt.num(summary.worst_metric, 3), { tone: "bad" }));
    left.appendChild(ui.kv("Positive cells",
      summary.positive_share === undefined ? "—" : percent(summary.positive_share, 0)));
    left.appendChild(ui.kv("Plateau", summary.robust ? "YES ✓" : "NO",
      { tone: summary.robust ? "ok" : "warn" }));

    const note = el("div.note" + (summary.robust ? ".info" : ".warn"), {
      text: summary.robust
        ? "The median cell keeps most of the best cell's edge and most cells are positive — "
          + "the result does not hinge on the exact parameter."
        : "Only part of the grid works: the median cell is far below the peak, which is the "
          + "classic overfit signature. Treat the best cell as a candidate, not a finding.",
    });
    wrap.appendChild(el("div.grid.g2", { style: "gap:10px" }, left, note));
    return wrap;
  }

  /** WALK-FORWARD view: the fold scheme, per-fold OOS bars and consistency. */
  function walkForwardView(payload) {
    const wrap = el("div");
    const summary = payload.summary || {};
    const folds = (payload.cells || []).filter(function (f) { return f.status === "complete"; });
    const metric = summary.metric || "sharpe";

    const scheme = el("div.panel.tight", { style: "margin-bottom:10px" },
      el("label", { text: "WINDOW SCHEME · ▨ TRAIN · ▨ TEST" }));
    const timeline = el("div.wfa-timeline");
    const first = folds.length ? Date.parse(folds[0].train.start) : 0;
    const last = folds.length ? Date.parse(folds[folds.length - 1].test.end) : 1;
    const span = Math.max(1, last - first);
    folds.forEach(function (fold) {
      const track = el("div.track");
      const trainStart = (Date.parse(fold.train.start) - first) / span;
      const trainEnd = (Date.parse(fold.train.end) - first) / span;
      const testStart = (Date.parse(fold.test.start) - first) / span;
      const testEnd = (Date.parse(fold.test.end) - first) / span;
      track.appendChild(el("div.segment.is", {
        style: "left:" + (trainStart * 100) + "%;width:" + ((trainEnd - trainStart) * 100) + "%",
        text: "train" }));
      const failed = Number(fold.test_metric) <= 0;
      const test = el("div.segment.oos", {
        style: "left:" + (testStart * 100) + "%;width:" + Math.max(2, (testEnd - testStart) * 100) + "%",
        text: fmt.num(fold.test_metric, 2) });
      if (failed) test.classList.add("failed");
      track.appendChild(test);
      timeline.appendChild(el("div.fold", el("label", { text: "f" + fold.fold }), track));
    });
    if (!folds.length) timeline.appendChild(foot("No fold could be scored on this history."));
    scheme.appendChild(el("div.ac", { dataset: { from: "agent-center · walk-forward timeline" } }, timeline));
    scheme.appendChild(foot((summary.anchored ? "Anchored" : "Rolling") + " train windows · "
      + summary.folds + " folds · the parameter is chosen on the train window only and scored "
      + "on the test window that follows.", "margin-top:6px"));
    wrap.appendChild(scheme);

    const table = el("div.panel.tight", { style: "margin-bottom:10px" },
      el("label", { text: "PER-FOLD SELECTION AND OUT-OF-SAMPLE SCORE" }));
    table.appendChild(ui.table({
      tree: "tl",
      columns: [
        { label: "FOLD", render: function (row) { return "f" + row.fold; } },
        { label: "TRAIN", render: function (row) {
          return el("span.dim", { text: row.train.start + " → " + row.train.end }); } },
        { label: "TEST", render: function (row) {
          return el("span.dim", { text: row.test.start + " → " + row.test.end }); } },
        { label: "CHOSEN", render: function (row) { return row.label || "base"; } },
        { label: "TRAIN " + metric.toUpperCase(), num: true, render: function (row) {
          return row.train_metric === null ? "—" : fmt.num(row.train_metric, 2); } },
        { label: "TEST " + metric.toUpperCase(), num: true, render: function (row) {
          const value = row.test_metric;
          return el("span", { class: signTone(value), text: value === null ? "—" : fmt.num(value, 2) });
        } },
      ],
      rows: payload.cells || [],
      empty: "No folds.",
    }));
    wrap.appendChild(table);

    const consistency = el("div.panel.tight", el("label", { text: "CONSISTENCY" }));
    consistency.appendChild(ui.kv("Positive folds",
      summary.positive_folds + " / " + summary.scored, { tone: "hi" }));
    consistency.appendChild(ui.kv("Mean test " + metric,
      summary.mean_test_metric === null || summary.mean_test_metric === undefined
        ? "—" : fmt.num(summary.mean_test_metric, 2), { tone: "hi" }));
    consistency.appendChild(ui.kv("Worst fold",
      summary.worst_test_metric === null || summary.worst_test_metric === undefined
        ? "—" : fmt.num(summary.worst_test_metric, 2), { tone: "bad" }));
    consistency.appendChild(ui.kv("Walk-forward efficiency",
      summary.efficiency === null || summary.efficiency === undefined
        ? "not measurable" : fmt.num(summary.efficiency, 2),
      { tone: summary.efficiency !== null && summary.efficiency >= 0.5 ? "ok" : "warn" }));
    const stitched = summary.stitched;
    if (stitched) {
      consistency.appendChild(ui.kv("Stitched OOS return", signedPercent(stitched.total_return),
        { tone: signTone(stitched.total_return) }));
      consistency.appendChild(ui.kv("Stitched OOS max DD", percent(stitched.max_drawdown),
        { tone: "bad" }));
    }
    const verdict = el("div.note" + (summary.efficiency !== null && summary.efficiency >= 0.5 ? ".info" : ".warn"), {
      text: summary.efficiency === null || summary.efficiency === undefined
        ? "Efficiency needs both a train and a test score; this run produced only one of them."
        : (summary.efficiency >= 0.5
          ? "Most of the in-sample edge survived out of sample: the per-fold selection is not "
            + "just fitting noise."
          : "Less than half the in-sample edge survived. The selection step is fitting noise — "
            + "the grid, not the strategy, is what is being measured."),
    });
    wrap.appendChild(el("div.grid.g2", { style: "gap:10px" }, consistency, verdict));
    return wrap;
  }

  /** MONTE CARLO view: the equity cone plus Sharpe and drawdown distributions. */
  function monteCarloView(payload) {
    const cone = ((payload.cells || [])[0] || {}).cone || {};
    const summary = payload.summary || {};
    const wrap = el("div");
    const panel = el("div.panel.tight", { style: "margin-bottom:10px" },
      el("label", {}, "EQUITY CONE · ", el("span.ok", { text: "— realised" }),
        " · bands p5–p95 / p25–p75"));
    panel.appendChild(coneSvg(cone, { height: 200 }));
    panel.appendChild(el("div.note" + (summary.favourable_sequencing ? ".warn" : ".info"), {
      style: "margin-top:6px",
      text: summary.favourable_sequencing
        ? "⚠ The realised path sits at p" + fmt.num(summary.realised_percentile, 0)
          + " of the resample cone — favourable sequencing, not only edge. Median-path Sharpe "
          + fmt.num(summary.median_sharpe, 2) + " against a realised " + fmt.num(summary.realised_sharpe, 2) + "."
        : "The realised path sits at p" + fmt.num(summary.realised_percentile, 0)
          + " of the resample cone — an ordinary draw from this return distribution. "
          + "Median-path Sharpe " + fmt.num(summary.median_sharpe, 2)
          + " against a realised " + fmt.num(summary.realised_sharpe, 2) + ".",
    }));
    wrap.appendChild(panel);

    const sharpe = el("div.panel.tight", el("label", { text: "SHARPE DISTRIBUTION" }));
    sharpe.appendChild(ui.kv("P(Sharpe > 0)", percent(summary.prob_positive, 1),
      { tone: summary.prob_positive >= 0.9 ? "ok" : "warn" }));
    sharpe.appendChild(ui.kv("P(Sharpe > 1)", percent(summary.prob_above_one, 1), { tone: "hi" }));
    sharpe.appendChild(ui.kv("Median Sharpe", fmt.num(summary.median_sharpe, 2), { tone: "hi" }));
    sharpe.appendChild(ui.kv("Realised Sharpe", fmt.num(summary.realised_sharpe, 2), { tone: "hi" }));

    const risk = el("div.panel.tight", el("label", { text: "DRAWDOWN RISK" }));
    risk.appendChild(ui.kv("Median max DD", percent(summary.median_max_drawdown)));
    risk.appendChild(ui.kv("p95 max DD", percent(summary.p95_max_drawdown), { tone: "bad" }));
    risk.appendChild(ui.kv("P(DD > 20%)", percent(summary.prob_drawdown_over_20pct, 1),
      { tone: summary.prob_drawdown_over_20pct > 0.2 ? "warn" : "" }));
    risk.appendChild(ui.kv("Block length", (cone.block || "—") + " periods"));

    wrap.appendChild(el("div.grid.g2", { style: "gap:10px" }, sharpe, risk));
    wrap.appendChild(foot(fmt.int(summary.paths) + " circular-block bootstrap resamples of "
      + fmt.int(summary.observations) + " return observations, seed " + summary.seed
      + " — the same run always produces the same cone.", "margin-top:8px"));
    return wrap;
  }

  /**
   * Draw a resample cone: the p5–p95 and p25–p75 bands, the median and the
   * realised path, as one inline SVG (no chart library needed for a cone).
   */
  function coneSvg(cone, options) {
    const opts = options || {};
    const height = opts.height || 200;
    const bands = cone.bands || {};
    const series = [bands.p5, bands.p25, bands.p50, bands.p75, bands.p95, cone.realised]
      .filter(Boolean);
    const box = el("div.chart-box", { style: "height:" + height + "px;min-height:" + height + "px" },
      el("div.chart-grid"));
    if (!series.length) {
      box.appendChild(foot("The cone has no points."));
      return el("div.ac", { dataset: { from: "agent-center · fan chart" } }, box);
    }
    const n = series[0].length;
    let low = Infinity;
    let high = -Infinity;
    series.forEach(function (row) {
      row.forEach(function (value) {
        if (value < low) low = value;
        if (value > high) high = value;
      });
    });
    const span = (high - low) || 1;
    const W = 1000;
    const H = 300;
    function x(i) { return (i / Math.max(1, n - 1)) * W; }
    function y(v) { return H - ((v - low) / span) * H; }
    function path(row) {
      return row.map(function (value, i) {
        return (i ? "L" : "M") + x(i).toFixed(1) + "," + y(value).toFixed(1);
      }).join(" ");
    }
    function bandPath(upper, lower) {
      const up = upper.map(function (value, i) {
        return (i ? "L" : "M") + x(i).toFixed(1) + "," + y(value).toFixed(1);
      }).join(" ");
      const down = lower.slice().reverse().map(function (value, i) {
        return "L" + x(n - 1 - i).toFixed(1) + "," + y(value).toFixed(1);
      }).join(" ");
      return up + " " + down + " Z";
    }
    const parts = [];
    if (bands.p5 && bands.p95) {
      parts.push('<path d="' + bandPath(bands.p95, bands.p5) + '" fill="#2ee6a8" opacity="0.07"/>');
    }
    if (bands.p25 && bands.p75) {
      parts.push('<path d="' + bandPath(bands.p75, bands.p25) + '" fill="#2ee6a8" opacity="0.12"/>');
    }
    (cone.curves || []).slice(0, opts.paths || 60).forEach(function (row) {
      parts.push('<path d="' + path(row) + '" fill="none" stroke="#1f5878" stroke-width="0.6" opacity="0.35"/>');
    });
    if (bands.p50) {
      parts.push('<path d="' + path(bands.p50)
        + '" fill="none" stroke="#5c6b84" stroke-width="1.4" stroke-dasharray="5 4"/>');
    }
    if (cone.realised) {
      parts.push('<path d="' + path(cone.realised) + '" fill="none" stroke="#2ee6a8" stroke-width="2"/>');
    }
    const svg = el("div");
    svg.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" '
      + 'aria-label="Monte-Carlo equity cone">' + parts.join("") + "</svg>";
    box.appendChild(svg.firstChild);
    return el("div.ac", { dataset: { from: "agent-center · fan chart" } }, box);
  }

  /** Experiment runs (backtest + IC study) with the live log. */
  function runsPanel(exp) {
    const panel = el("div.panel.tight", { style: "margin-bottom:10px" },
      el("div.ptitle", { style: "display:flex" },
        el("span", { text: "EXPERIMENT RUNS" }),
        el("span.foot", { style: "margin-left:auto", text: "backtest + information coefficient" })));

    const detail = state.cache.experiment[exp.id];
    if (!detail) {
      panel.appendChild(foot("Loading runs…"));
      state.ctx.api.lab.experiment(exp.id, { signal: state.ctx.signal }).then(function (payload) {
        state.cache.experiment[exp.id] = payload;
        if (state.tab === "experiments" && state.sel.experiment === exp.id) renderExpDetail();
      }).catch(function (err) {
        if (cancelled(err)) return;
        state.cache.experiment[exp.id] = { runs: [], error: err };
        if (state.tab === "experiments" && state.sel.experiment === exp.id) renderExpDetail();
      });
    } else if (detail.error) {
      panel.appendChild(errorNote("Runs could not be loaded", detail.error));
    } else if (!detail.runs || !detail.runs.length) {
      panel.appendChild(foot("Never run.", "padding:4px 0"));
    } else {
      panel.appendChild(ui.table({
        tree: "tl",
        columns: [
          { label: "RUN", render: function (row) { return el("span.hi", { text: row.id }); } },
          { label: "STARTED", render: function (row) { return el("span.dim", { text: when(row.started_at) }); } },
          { label: "IC", num: true, render: function (row) {
            const ic = row.results && row.results.ic ? row.results.ic.mean_ic : null;
            return ic === null || ic === undefined ? "—" : fmt.num(ic, 4);
          } },
          { label: "SECONDS", num: true, render: function (row) {
            return row.duration_ms ? fmt.num(row.duration_ms / 1000, 2) : "—";
          } },
          { label: "ACCEPTED", num: true, render: function (row) {
            if (!row.results || row.results.passed_gates === undefined || row.results.passed_gates === null) return "—";
            return el("span", { class: row.results.passed_gates ? "ok" : "warn",
              text: row.results.passed_gates ? "PASS" : "NOT MET" });
          } },
          { label: "STATUS", num: true, render: function (row) {
            return el("span", { class: runTone(row.status), text: row.status });
          } },
        ],
        rows: detail.runs,
      }));
    }

    const runBtn = tbtn(state.busy.run ? "▷ RUNNING…" : "▷ RUN EXPERIMENT", function () {
      runExperiment(exp, runBtn);
    }, { primary: true, small: true });
    if (state.busy.run) runBtn.style.opacity = "0.6";
    state.nodes.runBtn = runBtn;
    panel.appendChild(el("div", { style: "display:flex;gap:8px;margin:8px 0 6px;align-items:center" },
      runBtn,
      foot("Runs a real backtest over stored bars and a real IC study; the log streams below.", "margin-left:auto")));

    const box = el("div.code", { style: "max-height:190px;overflow:auto;white-space:pre-wrap" });
    state.nodes.logBox = box;
    panel.appendChild(el("label", { text: "RUN LOG · LIVE" }));
    panel.appendChild(box);
    paintLog();
    return panel;
  }

  // --- experiment actions ---------------------------------------------------

  /** POST /api/lab/experiments/{id}/run — the real thing, with the live log. */
  function runExperiment(exp, button) {
    if (state.busy.run) return;
    state.busy.run = true;
    button.textContent = "▷ RUNNING…";
    button.style.opacity = "0.6";
    state.log = { runId: null, experimentId: exp.id, lines: [] };
    appendLog({ level: "info", message: "requesting run of " + exp.name + "…" });
    state.ctx.api.lab.runExperiment(exp.id, {}, { signal: state.ctx.signal, timeoutMs: 600000 })
      .then(function (run) {
        state.busy.run = false;
        delete state.cache.experiment[exp.id];
        if (run.status === "complete") {
          global.toast.ok("Experiment run " + run.id + " complete"
            + (run.results && run.results.passed_gates !== undefined
              ? (run.results.passed_gates ? " · acceptance passed" : " · acceptance not met") : ""));
        } else {
          global.toast.error("Experiment run " + run.id + " " + run.status + (run.error ? " — " + run.error : ""));
        }
        if (run.log && run.log.length) {
          state.log = { runId: run.id, experimentId: exp.id, lines: run.log.map(function (line) {
            return { ts: line.ts, level: line.level, message: line.message };
          }) };
        }
        return reload(["summary", "experiments", "backtests", "reports", "evaluation", "strategies"]);
      })
      .catch(function (err) {
        state.busy.run = false;
        appendLog({ level: "error", message: (err && err.message) || String(err) });
        fail("The experiment run failed", err);
        renderExpDetail();
      });
  }

  /** POST /api/lab/backtests/run with the experiment's stored configuration. */
  function runBacktestForExperiment(exp, button) {
    if (state.busy.bt) return;
    state.busy.bt = true;
    button.textContent = "▷ RUNNING BACKTEST…";
    state.ctx.api.lab.runBacktest({
      config: exp.config,
      experiment_id: exp.id,
      hypothesis_id: exp.hypothesis_id,
      source: "manual",
    }, { signal: state.ctx.signal, timeoutMs: 600000 })
      .then(function (record) {
        state.busy.bt = false;
        global.toast.ok("Backtest " + record.id + " " + record.status);
        state.sel.backtest = record.id;
        return reload(["summary", "backtests", "evaluation"]);
      })
      .catch(function (err) {
        state.busy.bt = false;
        fail("The backtest failed", err);
        renderExpDetail();
      });
  }

  // -------------------------------------------------- backtest configuration

  /** Signal kinds the backtester implements (tradinglib.research.backtest). */
  const SIGNAL_KINDS = ["momentum", "mean_reversion", "ma_cross", "low_volatility", "expression", "static"];
  const WEIGHTINGS = ["equal", "signal", "inverse_vol"];
  const REBALANCES = ["daily", "weekly", "monthly", "quarterly"];

  /**
   * The BacktestConfig editor, shared by the experiment and backtest forms.
   * Every control maps 1:1 onto a field of the persisted configuration.
   * @param {object|null} initial an existing config to seed from
   * @returns {{node: HTMLElement, read: () => object}}
   */
  function configFields(initial) {
    const config = initial || {};
    const signal = config.signal || {};
    const costs = config.costs || {};
    const activeId = state.data.activeUniverse ? state.data.activeUniverse.id : "";

    const name = input(config.name || "", { placeholder: "mom_12_1_top10" });
    const universe = select([{ value: "", label: "— explicit symbols —" }].concat(universeOptions()),
      config.universe_id !== undefined && config.universe_id !== null ? config.universe_id : activeId);
    const symbols = input((config.symbols || []).join(", "), { placeholder: "AAPL, MSFT, JPM" });
    const kind = select(SIGNAL_KINDS, signal.kind || "momentum");
    const lookback = input(signal.lookback === undefined ? 252 : signal.lookback, { type: "number" });
    const skip = input(signal.skip === undefined ? 21 : signal.skip, { type: "number" });
    const fast = input(signal.fast === undefined ? 50 : signal.fast, { type: "number" });
    const slow = input(signal.slow === undefined ? 200 : signal.slow, { type: "number" });
    const topN = input(signal.top_n === undefined || signal.top_n === null ? 10 : signal.top_n, { type: "number" });
    const weighting = select(WEIGHTINGS, signal.weighting || "equal");
    const expression = input(signal.expression || "", { placeholder: "rank(mom_12_1) (expression signals only)" });
    const filterExpression = input(signal.filter_expression || "", { placeholder: "optional filter expression" });
    const start = input(config.start || "", { type: "date" });
    const end = input(config.end || "", { type: "date" });
    const benchmark = input(config.benchmark === undefined ? "SPY" : (config.benchmark || ""), { placeholder: "SPY" });
    const rebalance = select(REBALANCES, config.rebalance || "monthly");
    const capital = input(config.initial_capital === undefined ? 1000000 : config.initial_capital, { type: "number" });
    const maxPositions = input(config.max_positions === undefined || config.max_positions === null ? "" : config.max_positions,
      { type: "number", placeholder: "no cap" });
    const longOnly = el("input", { type: "checkbox" });
    longOnly.checked = config.long_only === undefined ? true : !!config.long_only;
    const commission = input(costs.commission_bps === undefined ? 1 : costs.commission_bps, { type: "number" });
    const slippage = input(costs.slippage_bps === undefined ? 5 : costs.slippage_bps, { type: "number" });
    const borrow = input(costs.borrow_bps_annual === undefined ? 0 : costs.borrow_bps_annual, { type: "number" });
    const oosFraction = input(config.oos_fraction === undefined || config.oos_fraction === null ? 0.3 : config.oos_fraction,
      { type: "number", step: "0.05" });

    const node = el("div.grid.g3", { style: "gap:12px" },
      el("div.panel.tight",
        el("label", { text: "PERIOD & DATA" }),
        field("NAME", name),
        field("START", start),
        field("END", end),
        field("OOS FRACTION", oosFraction),
        foot("Empty dates mean the whole stored history. The store's coverage decides what is loadable.")),
      el("div.panel.tight",
        el("label", { text: "PORTFOLIO" }),
        field("UNIVERSE", universe),
        field("SYMBOLS (when no universe)", symbols),
        field("BENCHMARK", benchmark),
        field("REBALANCE", rebalance),
        field("INITIAL CAPITAL", capital),
        field("MAX POSITIONS", maxPositions),
        el("div", { style: "margin:6px 0" }, longOnly, el("span", { text: " long only" }))),
      el("div.panel.tight",
        el("label", { text: "SIGNAL & COSTS" }),
        field("SIGNAL", kind),
        field("LOOKBACK (bars)", lookback),
        field("SKIP (bars)", skip),
        field("SMA FAST / SLOW", el("div", { style: "display:flex;gap:6px" }, fast, slow)),
        field("TOP N", topN),
        field("WEIGHTING", weighting),
        field("EXPRESSION", expression),
        field("FILTER EXPRESSION", filterExpression),
        field("COMMISSION / SLIPPAGE / BORROW BPS",
          el("div", { style: "display:flex;gap:6px" }, commission, slippage, borrow))));

    function number(control, fallback) {
      const value = control.value === "" ? null : Number(control.value);
      if (value === null || isNaN(value)) return fallback;
      return value;
    }

    return {
      node: node,
      read: function () {
        return {
          name: name.value.trim() || "backtest",
          universe_id: universe.value || null,
          symbols: symbols.value.split(",").map(function (s) { return s.trim().toUpperCase(); }).filter(Boolean),
          signal: {
            kind: kind.value,
            lookback: number(lookback, 252),
            skip: number(skip, 21),
            fast: number(fast, 50),
            slow: number(slow, 200),
            top_n: number(topN, null),
            weighting: weighting.value,
            expression: expression.value.trim() || null,
            filter_expression: filterExpression.value.trim() || null,
          },
          start: start.value || null,
          end: end.value || null,
          benchmark: benchmark.value.trim() || null,
          rebalance: rebalance.value,
          initial_capital: number(capital, 1000000),
          max_positions: number(maxPositions, null),
          long_only: longOnly.checked,
          costs: {
            commission_bps: number(commission, 1),
            slippage_bps: number(slippage, 5),
            borrow_bps_annual: number(borrow, 0),
          },
          oos_fraction: number(oosFraction, 0.3),
        };
      },
    };
  }

  /** What the run will be judged by, and on what data — both read live. */
  function judgementNote() {
    const gates = state.data.gates || [];
    const quality = state.data.quality;
    const note = el("div.note.info", { style: "margin:10px 0" },
      el("div.ptitle", { style: "margin-bottom:4px;color:var(--blu)", text: "THIS RUN WILL BE JUDGED BY" }),
      el("span", { text: gates.length ? gates.map(function (gate) { return gate.name; }).join(" · ") : "the gate suite could not be loaded" }));
    const active = state.data.activeUniverse;
    const parts = [];
    if (active) parts.push(active.name + " · " + (active.member_count || active.symbol_count || 0) + " members");
    if (quality) {
      parts.push("store quality " + (quality.overall_quality_pct === null || quality.overall_quality_pct === undefined
        ? "not measured" : fmt.num(quality.overall_quality_pct, 1) + "%"));
      parts.push(quality.stale_series + " stale series");
      parts.push(quality.open_incidents + " open incidents");
      parts.push(quality.datasets_in_store + " datasets in store");
    }
    note.appendChild(foot(parts.join(" · ") || "no data-quality figures available", "margin-top:5px"));
    return note;
  }

  /** Forget whatever seeded the new-experiment form (clone / hypothesis). */
  function clearExperimentSeed() {
    state.cloneFrom = null;
    state.newExperimentHypothesis = null;
  }

  /** The new-experiment form: config + hypothesis link + acceptance criteria. */
  function experimentForm() {
    const seed = state.cloneFrom;
    const fields = configFields(seed ? seed.config : null);
    const name = input(seed ? seed.name + " (clone)" : "", { placeholder: "mom_crowd_cond_v2" });
    const hypothesis = select(
      [{ value: "", label: "— none —" }].concat(state.data.hypotheses.map(function (h) {
        return { value: h.id, label: h.id + " · " + h.title };
      })),
      state.newExperimentHypothesis || (seed ? seed.hypothesis_id : "") || "");
    const kind = select(["combined", "backtest", "signal"], seed ? seed.kind : "combined");
    const horizon = input(seed ? seed.horizon : 21, { type: "number" });
    const owner = input(seed ? (seed.owner || "") : "", { placeholder: "owner" });
    const minSharpe = input(seed && seed.criteria ? seed.criteria.min_oos_sharpe : 0.8, { type: "number", step: "0.1" });
    const maxDd = input(seed && seed.criteria ? seed.criteria.max_drawdown : 0.15, { type: "number", step: "0.01" });
    const minTrades = input(seed && seed.criteria ? seed.criteria.min_trades : 30, { type: "number" });
    const notes = el("textarea", { rows: "2", placeholder: "What is this experiment testing?" });
    notes.value = seed && seed.notes ? seed.notes : "";

    const panel = el("div", { dataset: { form: "experiment" } });
    panel.appendChild(el("div", { style: "display:flex;align-items:center;gap:10px" },
      el("span.hi", { style: "font-family:system-ui;font-size:15px;font-weight:600", text: "New experiment" }),
      el("span.dim", { text: "draft" }),
      tbtn("CANCEL", function () {
        state.mode.exp = "view";
        clearExperimentSeed();
        renderExperiments();
      }, { small: true, style: "margin-left:auto" })));
    panel.appendChild(el("div.grid.g2", { style: "gap:8px;margin-top:12px" },
      field("NAME · REQUIRED", name),
      field("HYPOTHESIS", hypothesis),
      field("KIND", kind),
      field("IC HORIZON (bars)", horizon),
      field("OWNER", owner)));
    panel.appendChild(field("NOTES", notes));
    panel.appendChild(el("div.ptitle", { style: "margin-top:10px", text: "CONFIGURATION · PINNED WITH THE EXPERIMENT" }));
    panel.appendChild(fields.node);
    panel.appendChild(el("div.grid.g3", { style: "gap:8px;margin-top:10px" },
      field("ACCEPT · MIN OOS SHARPE", minSharpe),
      field("ACCEPT · MAX DRAWDOWN", maxDd),
      field("ACCEPT · MIN TRADES", minTrades)));
    panel.appendChild(judgementNote());

    const create = tbtn("SAVE EXPERIMENT", function () {
      submitExperiment(false);
    }, { small: true });
    const createRun = tbtn("▷ SAVE AND RUN", function () {
      submitExperiment(true);
    }, { primary: true });
    panel.appendChild(el("div", { style: "display:flex;gap:8px;margin-top:10px" }, createRun, create));
    panel.appendChild(foot("Running executes a real backtest over stored bars plus an information-coefficient study; nothing is simulated.", "margin-top:6px"));

    function submitExperiment(runAfter) {
      const config = fields.read();
      const experimentName = name.value.trim() || config.name;
      config.name = experimentName;
      if (!experimentName) {
        global.toast.warn("The experiment needs a name.");
        return;
      }
      if (!config.universe_id && !config.symbols.length) {
        global.toast.warn("Choose a universe or list explicit symbols.");
        return;
      }
      const body = {
        name: experimentName,
        config: config,
        hypothesis_id: hypothesis.value || null,
        kind: kind.value,
        horizon: Number(horizon.value) || 21,
        owner: owner.value.trim() || null,
        notes: notes.value.trim() || null,
        criteria: {
          min_oos_sharpe: Number(minSharpe.value),
          max_drawdown: Number(maxDd.value),
          min_trades: Number(minTrades.value),
        },
      };
      createRun.textContent = "▷ SAVING…";
      state.ctx.api.lab.createExperiment(body, { signal: state.ctx.signal }).then(function (created) {
        global.toast.ok("Created experiment " + created.name);
        state.anView = null;
        state.sel.experiment = created.id;
        state.mode.exp = "view";
        clearExperimentSeed();
        return reload(["experiments", "summary"]).then(function () {
          if (!runAfter) return null;
          const fresh = experimentById(created.id);
          if (!fresh) return null;
          goTab("experiments", created.id);
          return runExperiment(fresh, state.nodes.runBtn || el("span"));
        });
      }).catch(function (err) {
        createRun.textContent = "▷ SAVE AND RUN";
        fail("Could not create the experiment", err);
      });
    }

    return panel;
  }

  // ---------------------------------------------------------------- compare

  /** Compare 2–4 selected experiments on their newest completed runs. */
  function openCompare() {
    const keys = Array.from(state.compare).slice(0, 4);
    if (keys.length < 2) {
      global.toast.warn("Select 2–4 experiments to compare.");
      return;
    }
    const newest = newestByExperiment();
    const reports = reportsByBacktest();
    const items = keys.map(function (id) {
      const exp = experimentById(id);
      const record = exp ? newest[exp.id] : null;
      return {
        exp: exp,
        record: record,
        report: record ? reports[record.id] : null,
        window: judgedWindow(record),
      };
    }).filter(function (item) { return !!item.exp; });

    const rows = [
      { label: "OOS Sharpe", better: "up", value: function (i) { return i.window ? i.window.sharpe : null; }, format: ratio },
      { label: "Deflated Sharpe", better: "up", value: function (i) { return deflatedOf(i.report); }, format: ratio },
      { label: "Max drawdown", better: "up", value: function (i) { return i.record && i.record.stats ? i.record.stats.max_drawdown : null; }, format: percent },
      { label: "Total return", better: "up", value: function (i) { return i.record && i.record.stats ? i.record.stats.total_return : null; }, format: signedPercent },
      { label: "Excess vs benchmark", better: "up", value: function (i) { return i.window ? i.window.excess_return : null; }, format: signedPercent },
      { label: "Trades", better: "flat", value: function (i) { return i.record && i.record.stats ? i.record.stats.trades : null; }, format: fmt.int },
      { label: "Turnover", better: "flat", value: function (i) { return i.record && i.record.stats ? i.record.stats.turnover : null; }, format: ratio },
    ];

    const table = el("table.tbl");
    const head = el("tr", el("th", { style: "width:22%", text: "METRIC" }));
    items.forEach(function (item) {
      head.appendChild(el("th.r", { text: item.exp.name }));
    });
    table.appendChild(head);
    rows.forEach(function (row) {
      const values = items.map(row.value);
      let best = null;
      if (row.better === "up") {
        values.forEach(function (v) {
          if (v === null || v === undefined || isNaN(Number(v))) return;
          if (best === null || Number(v) > best) best = Number(v);
        });
      }
      const tr = el("tr", el("td.dim", { text: row.label }));
      values.forEach(function (value) {
        const cell = el("td.r", { text: value === null || value === undefined ? "—" : row.format(value) });
        if (best !== null && Number(value) === best) cell.classList.add("ok");
        tr.appendChild(cell);
      });
      table.appendChild(tr);
    });

    // Configuration diff: only the fields that actually differ.
    const flat = items.map(function (item) { return flattenConfig(item.exp.config || {}); });
    const allKeys = [];
    flat.forEach(function (map) {
      Object.keys(map).forEach(function (key) { if (allKeys.indexOf(key) < 0) allKeys.push(key); });
    });
    const differing = allKeys.filter(function (key) {
      const distinct = {};
      flat.forEach(function (map) { distinct[String(map[key])] = true; });
      return Object.keys(distinct).length > 1;
    });
    const diff = el("table.tbl");
    const diffHead = el("tr", el("th", { style: "width:22%", text: "PARAM" }));
    items.forEach(function (item) { diffHead.appendChild(el("th.r", { text: item.exp.name })); });
    diff.appendChild(diffHead);
    differing.forEach(function (key) {
      const tr = el("tr", el("td.dim", { text: key }));
      flat.forEach(function (map) {
        tr.appendChild(el("td.r.hi", { text: map[key] === undefined ? "—" : String(map[key]) }));
      });
      diff.appendChild(tr);
    });

    const full = state.nodes.expFull;
    ui.clear(full);
    full.appendChild(el("div", { style: "display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap" },
      tbtn("← BACK TO EXPERIMENTS", function () {
        state.compareOpen = false;
        renderExperiments();
      }, { small: true }),
      el("span.hi", { style: "font-family:system-ui;font-size:15px;font-weight:600",
        text: "Comparing " + items.length + " experiments" }),
      foot("values are each experiment's newest completed backtest", "margin-left:auto")));
    full.appendChild(el("div.grid.g32", { style: "margin-bottom:12px" },
      el("div.panel.tight", el("div.ptitle", { text: "METRICS" }), table),
      el("div.panel.tight", el("div.ptitle", { text: "CONFIG DIFF" }), diff,
        foot((allKeys.length - differing.length) + " identical fields hidden.", "margin-top:6px"))));
    full.appendChild(el("div.panel.tight",
      el("div.ptitle", { text: "VERDICT · GATES PASSED" }),
      items.map(function (item) {
        const right = item.report
          ? el("span", { class: item.report.passed ? "ok" : "warn",
            text: item.report.gates_passed + " / " + item.report.gates_total + " · " + item.report.verdict.toUpperCase() })
          : el("span.dim", { text: "never validated" });
        return el("div.kv", { style: "padding:5px 0" }, el("span.hi", { text: item.exp.name }), right);
      }),
      foot("Gate counts come from each run's stored validation report.", "border-top:1px solid var(--line);margin-top:6px;padding-top:6px")));

    state.compareOpen = true;
    renderExperiments();
    global.scrollTo(0, 0);
  }

  /** Flatten a BacktestConfig into "a.b" → scalar, for the diff table. */
  function flattenConfig(config) {
    const out = {};
    Object.keys(config).forEach(function (key) {
      const value = config[key];
      if (value && typeof value === "object" && !Array.isArray(value)) {
        Object.keys(value).forEach(function (inner) {
          out[key + "." + inner] = value[inner];
        });
      } else if (Array.isArray(value)) {
        out[key] = value.length + " items";
      } else {
        out[key] = value;
      }
    });
    return out;
  }

  // --------------------------------------------------------------- screening

  /**
   * SCREENING panel: the saved screens that feed this lab, with the survivor
   * counts their last real run produced. Running a screen here runs it for
   * real, and queueing a backtest creates a real lab run from the survivors.
   */
  function renderScreens() {
    const host = state.nodes.screens;
    if (!host) return;
    ui.clear(host);
    host.appendChild(el("div.ptitle", { style: "display:flex" },
      el("span", { text: "SCREENING · SURVIVORS FEEDING THE LAB" }),
      el("span.foot", { style: "margin-left:auto",
        text: "universe " + (state.data.activeUniverse ? state.data.activeUniverse.name : "—") })));
    if (state.errors.screens) {
      host.appendChild(errorNote("Screens could not be loaded", state.errors.screens));
      return;
    }
    if (!state.data.screens.length) {
      host.appendChild(foot("No screens saved yet. Build one in Market & Data → Screener; its survivors can be queued as a lab backtest."));
      return;
    }
    const latest = {};
    state.data.screenRuns.forEach(function (run) {
      if (run.screen_id && !latest[run.screen_id]) latest[run.screen_id] = run;
    });
    host.appendChild(ui.table({
      tree: "tl",
      columns: [
        { label: "SCREEN", render: function (row) { return el("span.hi", { text: row.name }); } },
        { label: "UNIVERSE", render: function (row) { return el("span.dim", { text: universeName(row.universe_id) }); } },
        { label: "RULES", num: true, render: function (row) { return String(row.active_rule_count); } },
        { label: "LAST RUN", render: function (row) {
          const run = latest[row.id];
          return run ? el("span.dim", { text: when(run.ran_at) }) : el("span.dim", { text: "never" });
        } },
        { label: "SURVIVORS", num: true, render: function (row) {
          const run = latest[row.id];
          if (!run) return "—";
          return el("span.hi", { text: run.pass_count + " of " + run.base_count });
        } },
        { label: "", render: function (row) {
          return el("span", { style: "display:flex;gap:6px;justify-content:flex-end" },
            tbtn("RUN", function () { runScreen(row); }, { small: true }),
            tbtn("→ BACKTEST", function () { queueScreenBacktest(row); }, { small: true }));
        } },
      ],
      rows: state.data.screens,
    }));
    host.appendChild(foot("Counts are the survivor counts of the screen's last persisted run — never an estimate.", "margin-top:6px"));
  }

  function runScreen(screen) {
    state.ctx.api.screens.run(screen.id, { persist: true }, { signal: state.ctx.signal, timeoutMs: 300000 })
      .then(function (run) {
        global.toast.ok(screen.name + ": " + run.pass_count + " of " + run.base_count + " names passed");
        return reload(["screens", "screenRuns"]);
      })
      .catch(function (err) { fail("Could not run the screen", err); });
  }

  function queueScreenBacktest(screen) {
    state.ctx.api.screens.queueBacktest(screen.id, {}, { signal: state.ctx.signal, timeoutMs: 300000 })
      .then(function (request) {
        const id = request.backtest_id;
        if (!id) {
          global.toast.warn(screen.name + ": " + (request.detail || "the lab did not accept the request"));
          return null;
        }
        global.toast.ok("Queued backtest " + id + " from " + screen.name);
        return reload(["backtests", "summary"]).then(function () {
          state.sel.backtest = id;
          goTab("backtests", id);
          renderBacktests();
        });
      })
      .catch(function (err) { fail("Could not queue a backtest from the screen", err); });
  }

  // -------------------------------------------------------------- backtests

  function renderBacktests() {
    renderBtList();
    renderBtMain();
  }

  function renderBtList() {
    const host = state.nodes.btlist;
    if (!host) return;
    ui.clear(host);
    state.nodes.btFoot.textContent = state.data.backtests.length
      + " runs in the ledger · every one appended, none edited";
    if (state.errors.backtests) {
      host.appendChild(errorNote("Backtests could not be loaded", state.errors.backtests));
      return;
    }
    const query = state.filters.btQuery.trim().toLowerCase();
    const rows = state.data.backtests.filter(function (record) {
      if (!query) return true;
      return [record.id, record.name, record.experiment_id, record.hypothesis_id, record.status]
        .filter(Boolean).join(" ").toLowerCase().indexOf(query) >= 0;
    });
    if (!rows.length) {
      host.appendChild(foot(state.data.backtests.length ? "No run matches this filter." : "No backtests yet."));
      return;
    }
    rows.forEach(function (record) {
      const row = el("div.rowitem");
      if (record.id === state.sel.backtest && state.mode.bt === "view") row.classList.add("sel");
      row.appendChild(el("div", { style: "display:flex;gap:6px" },
        el("span.hi", { text: record.id }),
        el("span", { style: "margin-left:auto", class: runTone(record.status) + " mini", text: record.status.toUpperCase() })));
      row.appendChild(el("div.foot", { text: record.name }));
      row.appendChild(el("div.foot", {
        text: when(record.finished_at || record.created_at)
          + " · " + (record.hypothesis_id || record.source),
      }));
      row.addEventListener("click", function () {
        state.sel.backtest = record.id;
        state.mode.bt = "view";
        goTab("backtests", record.id);
        renderBacktests();
      });
      host.appendChild(row);
    });
  }

  /** The tearsheet for the selected run, or the new-backtest form. */
  function renderBtMain() {
    const host = state.nodes.btmain;
    if (!host) return;
    // Never overwrite a form the user is filling in (see renderExpDetail).
    if (state.mode.bt === "new" && host.querySelector("[data-form='backtest']")) return;
    destroyCharts();
    ui.clear(host);
    if (state.mode.bt === "new") {
      host.appendChild(backtestForm());
      return;
    }
    const record = backtestById(state.sel.backtest);
    if (!record) {
      host.appendChild(el("div.panel",
        el("div.ptitle", { text: "BACKTEST" }),
        foot("Select a run on the left, or start a new one.")));
      return;
    }
    const head = el("div.panel", { style: "margin-bottom:12px" });
    const statusPill = el("span.pill", { text: record.status.toUpperCase() });
    const tone = record.status === "complete" ? "grn" : (record.status === "failed" ? "red" : "amb");
    statusPill.classList.add(tone);
    head.appendChild(el("div", { style: "display:flex;align-items:center;gap:10px;flex-wrap:wrap" },
      el("span.hi", { style: "font-family:system-ui;font-size:15px;font-weight:600", text: record.id + " · " + record.name }),
      statusPill,
      el("span.foot", { style: "margin-left:auto",
        text: (record.hypothesis_id ? "hypothesis " + record.hypothesis_id + " · " : "")
          + "source " + record.source + " · " + when(record.finished_at || record.created_at) })));
    head.appendChild(foot(record.signal + " · " + record.symbol_count + " symbols"
      + (record.config.universe_id ? " · " + universeName(record.config.universe_id) : "")
      + (record.duration_ms ? " · ran in " + fmt.num(record.duration_ms / 1000, 2) + "s" : ""), "margin:4px 0 8px"));

    if (record.error) {
      head.appendChild(el("div.note.warn", { text: "⚠ " + record.error }));
    }
    (record.warnings || []).forEach(function (warning) {
      head.appendChild(el("div.note.warn", { style: "margin-top:6px", text: "⚠ " + warning }));
    });

    if (record.status === "queued") {
      const runBtn = tbtn("▷ RUN THIS BACKTEST", function () {
        runQueuedBacktest(record, runBtn);
      }, { primary: true, style: "margin-top:10px" });
      head.appendChild(el("div.note", { style: "margin-top:10px",
        text: "Queued: the configuration is pinned but nothing has been computed yet." }));
      head.appendChild(runBtn);
      host.appendChild(head);
      host.appendChild(configPanel(record.config));
      return;
    }
    if (record.status === "running") {
      head.appendChild(el("div.note", { style: "margin-top:10px",
        text: "Running since " + when(record.started_at) + ". Partial results are withheld until every window closes; "
          + "the ledger row updates when it finishes." }));
      host.appendChild(head);
      host.appendChild(configPanel(record.config));
      return;
    }
    if (record.status !== "complete") {
      host.appendChild(head);
      host.appendChild(configPanel(record.config));
      return;
    }

    const stats = record.stats || {};
    head.appendChild(el("div", { style: "display:flex;gap:20px;flex-wrap:wrap;margin-top:12px" },
      metric("TOTAL RETURN", signedPercent(stats.total_return), signTone(stats.total_return)),
      metric("CAGR", signedPercent(stats.cagr), signTone(stats.cagr)),
      metric("VOLATILITY", percent(stats.volatility), "hi"),
      metric("SHARPE", ratio(stats.sharpe), stats.sharpe >= 1 ? "ok" : "hi"),
      metric("SORTINO", ratio(stats.sortino), "hi"),
      metric("MAX DD", percent(stats.max_drawdown), "bad"),
      metric("HIT RATE", percent(stats.hit_rate), "hi"),
      metric("TRADES", fmt.int(stats.trades), "blu")));
    head.appendChild(foot("Statistics are computed from the net-of-cost return series over "
      + fmt.num(stats.years, 2) + " years (" + fmt.int(stats.periods) + " periods at "
      + fmt.int(record.periods_per_year) + "/year).", "margin-top:6px"));

    // A real download link, not a scripted save: the browser fetches the PDF
    // the API renders from the same stored run this page is showing.
    const exportLink = el("a.btn.primary.small", {
      href: state.ctx.api.lab.tearsheetUrl(record.id),
      download: "tradinglib-tearsheet-" + record.id + ".pdf",
      style: "text-decoration:none",
      text: "EXPORT TEARSHEET PDF",
    });
    // Same idea for the raw series: a link the browser downloads, so the
    // numbers behind every statistic above can be checked in a spreadsheet.
    const csvLink = el("a.btn.small", {
      href: state.ctx.api.lab.returnsCsvUrl(record.id),
      download: "tradinglib-returns-" + record.id + ".csv",
      style: "text-decoration:none",
      title: "equity, return, drawdown and benchmark columns, one row per bar",
      text: "DOWNLOAD RETURNS CSV",
    });
    head.appendChild(el("div", { style: "display:flex;gap:8px;margin-top:10px;flex-wrap:wrap" },
      exportLink,
      csvLink,
      tbtn("↻ REFRESH RESAMPLE", function () {
        delete state.cache.fan[record.id];
        state.ctx.api.lab.fan(record.id, { paths: 5000, shown: 200, refresh: true },
          { signal: state.ctx.signal, timeoutMs: 300000 })
          .then(function (payload) {
            state.cache.fan[record.id] = payload;
            global.toast.ok("Recomputed " + fmt.int(payload.paths) + " resampled paths");
            if (state.tab === "backtests" && state.sel.backtest === record.id) renderBtMain();
          })
          .catch(function (err) { fail("The resample failed", err); });
      }, { small: true })));
    host.appendChild(head);

    host.appendChild(equityPanel(record));
    host.appendChild(signalsPanel(record));
    host.appendChild(tradeLogPanel(record));
    host.appendChild(fanChartPanel(record));
    host.appendChild(windowsPanel(record));
    host.appendChild(configPanel(record.config));
    host.appendChild(artifactPanel(record));
    host.appendChild(gatesPanel(record));
  }

  /**
   * The trade ledger of a completed run.
   *
   * Returns `undefined` while the results payload is still in flight and
   * `null` once it has arrived without one — a run recorded before the ledger
   * existed has no round trips to show, and saying "loading" forever would be
   * a lie.
   */
  function tradeLogOf(record) {
    const results = state.cache.results[record.id];
    if (results && results.trade_log) return results.trade_log;
    if (results) return null;
    return undefined;
  }

  /**
   * The shortest *daily* chart range that still covers a run's window.
   *
   * ``/api/research/ranges`` only serves daily bars up to ``5y``; ``max`` is
   * monthly, which would put every entry marker on the wrong bar. Asking for
   * the narrowest daily range that covers the run keeps the markers aligned
   * with the closes the backtest actually traded.
   */
  function dailyRangeFor(record) {
    const stats = record.stats || {};
    const from = fmt.toDate(stats.start || (record.config && record.config.start));
    const to = fmt.toDate(stats.end || (record.config && record.config.end)) || new Date();
    if (!from) return "5y";
    const days = Math.ceil((to.getTime() - from.getTime()) / 86400000) + 30;
    const ladder = [["1m", 31], ["3m", 92], ["6m", 183], ["1y", 365], ["2y", 730], ["5y", 1826]];
    for (let i = 0; i < ladder.length; i += 1) {
      if (days <= ladder[i][1]) return ladder[i][0];
    }
    return "5y";
  }

  /** The note shown where a trade ledger would be, when the run has none. */
  function noLedgerNote() {
    return el("div.note", { style: "margin-top:8px",
      text: "This run was recorded before the trade ledger existed, so it has no per-trade "
        + "entries. Re-run it and the round trips are stored with the result." });
  }

  /**
   * Price & signals (reference line 1367): one symbol's close, its entries and
   * exits as ▲/▼ markers, and the bars it was held shaded.
   *
   * The backtester holds a portfolio, not one instrument, so this shows one
   * symbol at a time — the one it traded most, changeable — rather than
   * pretending a 50-name book has a single price line.
   */
  function signalsPanel(record) {
    const panel = el("section.panel", { style: "margin-bottom:12px" });
    const log = tradeLogOf(record);
    const rows = (log && log.rows) || [];
    const bySymbol = {};
    rows.forEach(function (trade) {
      bySymbol[trade.symbol] = (bySymbol[trade.symbol] || 0) + 1;
    });
    const symbols = Object.keys(bySymbol).sort(function (a, b) {
      return bySymbol[b] - bySymbol[a] || a.localeCompare(b);
    });
    if (!state.signalSymbol || symbols.indexOf(state.signalSymbol) < 0) {
      state.signalSymbol = symbols[0] || null;
    }
    const symbol = state.signalSymbol;

    const head = el("div.panel-title", "Price & signals ",
      ui.badge("▲ buy · ▼ sell · shaded in-position"));
    if (symbols.length > 1) {
      const picker = select(symbols.map(function (s) {
        return { value: s, label: s + " · " + bySymbol[s] + (bySymbol[s] === 1 ? " trade" : " trades") };
      }), symbol, { style: "margin-left:auto;width:170px" });
      picker.addEventListener("change", function () {
        state.signalSymbol = picker.value;
        renderBtMain();
      });
      head.appendChild(picker);
    }
    panel.appendChild(head);

    if (log === undefined) {
      panel.appendChild(foot("Loading the trade ledger…", "padding:8px 0"));
      return el("div.ac", { dataset: { from: "agent-center · price & signals" } }, panel);
    }
    if (log === null) {
      panel.appendChild(noLedgerNote());
      return el("div.ac", { dataset: { from: "agent-center · price & signals" } }, panel);
    }
    if (!symbol) {
      panel.appendChild(foot("This run recorded no round trips, so there is nothing to mark. "
        + "A signal that never went flat holds a position from the first bar to the last.",
        "padding:8px 0"));
      return el("div.ac", { dataset: { from: "agent-center · price & signals" } }, panel);
    }

    const box = el("div.chart-box", { style: "height:280px;min-height:280px" }, el("div.chart-grid"));
    panel.appendChild(box);
    const trades = rows.filter(function (trade) { return trade.symbol === symbol; });
    const note = el("div.foot", { style: "margin-top:8px",
      text: trades.length + (trades.length === 1 ? " position in " : " positions in ")
        + symbol + " · shading marks the bars it was held · prices are the same stored closes "
        + "the backtest traded against." });
    panel.appendChild(note);

    const range = dailyRangeFor(record);
    const key = symbol + "@" + range;
    const cached = state.cache.prices[key];
    if (cached && cached.error) {
      box.appendChild(errorNote("The price series could not be loaded", cached.error));
    } else if (cached) {
      const clipped = paintSignals(box, cached, trades);
      if (clipped) {
        note.textContent += " The daily chart range tops out at five years, so "
          + clipped + " earlier position(s) fall outside this window.";
      }
    } else {
      box.appendChild(foot("Loading " + symbol + "…"));
      state.ctx.api.research.price(symbol, range, { signal: state.ctx.signal })
        .then(function (payload) {
          state.cache.prices[key] = payload;
          if (state.tab === "backtests" && state.signalSymbol === symbol) renderBtMain();
        })
        .catch(function (err) {
          if (cancelled(err)) return;
          state.cache.prices[key] = { error: err };
          if (state.tab === "backtests" && state.signalSymbol === symbol) renderBtMain();
        });
    }
    return el("div.ac", { dataset: { from: "agent-center · price & signals" } }, panel);
  }

  /** Draw the close line, the in-position shading and the entry/exit markers. */
  function paintSignals(box, payload, trades) {
    const points = (payload.points || []).filter(function (point) {
      return point && point.close !== null && point.close !== undefined;
    });
    if (points.length < 2) {
      box.appendChild(foot("Fewer than two stored closes for this symbol."));
      return 0;
    }
    // Clip the window to the trades, with a little air either side, so the
    // markers are not squashed into a corner of a decade of history.
    const stamps = points.map(function (p) { return Date.parse(p.time); });
    const dates = [];
    trades.forEach(function (trade) {
      if (trade.entry) dates.push(Date.parse(trade.entry));
      if (trade.exit) dates.push(Date.parse(trade.exit));
    });
    let lo = 0;
    let hi = points.length - 1;
    if (dates.length) {
      const first = Math.min.apply(null, dates);
      const last = Math.max.apply(null, dates);
      const pad = Math.max(1, Math.round(points.length * 0.04));
      lo = Math.max(0, stamps.findIndex(function (ts) { return ts >= first; }) - pad);
      let end = stamps.length - 1;
      for (let i = stamps.length - 1; i >= 0; i -= 1) {
        if (stamps[i] <= last) { end = i; break; }
      }
      hi = Math.min(points.length - 1, end + pad);
      if (hi - lo < 8) { lo = 0; hi = points.length - 1; }
    }
    const window_ = points.slice(lo, hi + 1);
    const windowStamps = stamps.slice(lo, hi + 1);
    const closes = window_.map(function (p) { return Number(p.close); });
    const low = Math.min.apply(null, closes);
    const high = Math.max.apply(null, closes);
    const span = (high - low) || 1;
    const W = 800;
    const H = 230;
    const path = closes.map(function (value, i) {
      const x = (i / Math.max(1, closes.length - 1)) * W;
      const y = H - ((value - low) / span) * H;
      return (i ? "L" : "M") + x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    const svg = el("div");
    svg.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" '
      + 'aria-label="close price with entry and exit markers">'
      + '<path d="' + path + '" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>';
    box.appendChild(svg.firstChild);

    /**
     * Fractional horizontal position of a timestamp inside the window, or
     * `null` when it falls outside it — a marker placed at the edge for a
     * date the chart does not cover would be a lie about where it happened.
     */
    function fraction(iso) {
      const ts = Date.parse(iso);
      if (isNaN(ts)) return null;
      if (ts < windowStamps[0] || ts > windowStamps[windowStamps.length - 1]) return null;
      let index = windowStamps.findIndex(function (value) { return value >= ts; });
      if (index < 0) index = windowStamps.length - 1;
      return index / Math.max(1, windowStamps.length - 1);
    }
    let outside = 0;
    trades.slice(0, 40).forEach(function (trade) {
      const start = fraction(trade.entry);
      const exitAt = trade.exit ? fraction(trade.exit) : 1;
      // An exit after the chart's last bar still ends the shading at the edge;
      // an *entry* nobody can place is dropped and counted instead.
      const end = exitAt === null ? 1 : exitAt;
      if (start === null) {
        outside += 1;
        return;
      }
      const zone = el("div.position-zone.trade-overlay", {
        style: "left:" + (start * 100).toFixed(2) + "%;width:"
          + Math.max(0.4, (end - start) * 100).toFixed(2) + "%",
        title: trade.symbol + " " + trade.entry + " → " + (trade.exit || "open"),
      });
      box.appendChild(zone);
      const entryPrice = Number(trade.entry_price);
      const entryY = isNaN(entryPrice) ? 0.5 : 1 - (entryPrice - low) / span;
      box.appendChild(el("span.signal-marker.buy.trade-overlay", {
        style: "left:" + (start * 100).toFixed(2) + "%;top:"
          + Math.min(92, Math.max(2, entryY * 100)).toFixed(2) + "%",
        title: "BUY " + trade.symbol + " " + trade.entry,
        text: "▲",
      }));
      if (trade.exit && exitAt !== null) {
        const exitPrice = Number(trade.exit_price);
        const exitY = isNaN(exitPrice) ? 0.5 : 1 - (exitPrice - low) / span;
        box.appendChild(el("span.signal-marker.sell.trade-overlay", {
          style: "left:" + (end * 100).toFixed(2) + "%;top:"
            + Math.min(92, Math.max(2, exitY * 100)).toFixed(2) + "%",
          title: "SELL " + trade.symbol + " " + trade.exit,
          text: "▼",
        }));
      }
    });
    return outside;
  }

  /** Trade log (reference line 1368): the run's round trips, newest first. */
  function tradeLogPanel(record) {
    const panel = el("section.panel", { style: "margin-bottom:12px" });
    const log = tradeLogOf(record);
    panel.appendChild(el("div.panel-title", "Trade log ",
      ui.badge(log ? log.total + " trades" : (log === null ? "not recorded" : "loading"))));
    if (log === undefined) {
      panel.appendChild(foot("Loading the trade ledger…", "padding:8px 0"));
      return el("div.ac", { dataset: { from: "agent-center · trade log" } }, panel);
    }
    if (log === null) {
      panel.appendChild(noLedgerNote());
      return el("div.ac", { dataset: { from: "agent-center · trade log" } }, panel);
    }
    const rows = log.rows || [];
    if (!rows.length) {
      panel.appendChild(foot("No round trip closed in this run.", "padding:8px 0"));
      return el("div.ac", { dataset: { from: "agent-center · trade log" } }, panel);
    }
    panel.appendChild(ui.table({
      columns: [
        { label: "#", render: function (row) { return String(row.number); } },
        { label: "Symbol", render: function (row) { return row.symbol; } },
        { label: "Side", render: function (row) { return row.side; } },
        { label: "Entry", render: function (row) { return row.entry; } },
        { label: "Exit", render: function (row) { return row.exit || "open"; } },
        { label: "Bars", num: true, render: function (row) { return fmt.int(row.bars); } },
        { label: "Weight", num: true, render: function (row) { return percent(row.weight, 1); } },
        { label: "Return", num: true, render: function (row) {
          if (row.return_pct === null || row.return_pct === undefined) {
            return el("span.dim", { text: "—" });
          }
          // An open position's return is marked to the last close, not realised;
          // the "~" says so rather than implying the money is booked.
          return el("span", {
            class: signTone(row.return_pct),
            title: row.open ? "unrealised - marked to the last stored close" : "realised at the exit close",
            text: (row.open ? "~" : "") + signedPercent(row.return_pct),
          });
        } },
        { label: "Why", render: function (row) { return row.reason; } },
      ],
      rows: rows,
    }));
    panel.appendChild(foot(log.closed + " closed · " + log.open + " still open"
      + (log.win_rate === null || log.win_rate === undefined
        ? "" : " · win rate " + percent(log.win_rate, 1))
      + (log.average_return === null || log.average_return === undefined
        ? "" : " · average " + signedPercent(log.average_return)),
      "margin-top:6px"));
    // The run persists at most the newest 500 round trips, so a very busy
    // strategy has a ledger the store itself cannot show in full. Saying which
    // trades are missing beats a row count that quietly disagrees with the
    // TRADES statistic above.
    if (rows.length < log.total) {
      panel.appendChild(el("div.note", { style: "margin-top:8px",
        text: "Showing the newest " + rows.length + " of " + log.total + " round trips. The "
          + "ledger keeps the most recent 500 with the run, so the earliest "
          + (log.total - rows.length) + " are counted in the statistics above but not stored "
          + "individually." }));
    }
    return el("div.ac", { dataset: { from: "agent-center · trade log" } }, panel);
  }

  /**
   * A run's pinned reproduction bundle: the digests of the config, the price
   * panel and the code that produced the result.
   *
   * This is what makes a stored number checkable a year later — the hashes say
   * whether a re-run saw the same inputs, rather than trusting that it did.
   */
  function artifactPanel(record) {
    const panel = el("section.panel", { style: "margin-bottom:12px" });
    const bundle = state.cache.artifact[record.id];
    panel.appendChild(el("div.panel-title", "Artifact bundle ",
      ui.badge(bundle === undefined ? "loading"
        : (bundle && bundle.available ? "pinned" : "not pinned"))));
    if (bundle === undefined) {
      fetchArtifact(record.id);
      panel.appendChild(foot("Loading the pinned bundle…", "padding:8px 0"));
      return el("div.ac", { dataset: { from: "agent-center · artifact bundle" } }, panel);
    }
    if (bundle && bundle.error) {
      panel.appendChild(errorNote("The artifact bundle could not be loaded", bundle.error));
      return el("div.ac", { dataset: { from: "agent-center · artifact bundle" } }, panel);
    }
    if (!bundle.available) {
      panel.appendChild(el("div.note", { style: "margin-top:8px",
        text: bundle.reason || "This run has no pinned artifact bundle." }));
      return el("div.ac", { dataset: { from: "agent-center · artifact bundle" } }, panel);
    }
    const cfg = bundle.config || {};
    const data = bundle.data || {};
    const code = bundle.code || {};
    const rows = [
      { part: "BUNDLE", digest: bundle.sha256, detail: "pinned " + when(bundle.pinned_at) },
      { part: "CFG", digest: cfg.sha256, detail: fmt.int(cfg.bytes) + " bytes of configuration" },
      { part: "DATA", digest: data.sha256,
        detail: fmt.int(data.rows) + " rows × " + fmt.int(data.columns) + " symbols"
          + (data.first_bar ? " · " + String(data.first_bar).slice(0, 10)
            + " → " + String(data.last_bar).slice(0, 10) : "") },
      { part: "CODE", digest: code.sha256,
        detail: (code.tradinglib_version ? "tradinglib " + code.tradinglib_version : "engine source")
          + ((code.modules || []).length ? " · " + code.modules.length + " research modules" : "") },
    ].filter(function (row) { return row.digest; });
    panel.appendChild(ui.table({
      columns: [
        { label: "PART", render: function (row) { return el("span.hi", { text: row.part }); } },
        { label: "SHA-256", render: function (row) {
          return el("code", { title: row.digest, text: String(row.digest).slice(0, 16) + "…" });
        } },
        { label: "WHAT WAS PINNED", render: function (row) {
          return el("span.dim", { text: row.detail });
        } },
      ],
      rows: rows,
    }));
    panel.appendChild(foot("Bundle " + bundle.bundle_id + ". A re-run that reports these same "
      + "digests saw the same configuration, the same price panel and the same code.",
      "margin-top:6px"));
    return el("div.ac", { dataset: { from: "agent-center · artifact bundle" } }, panel);
  }

  /** Fetch one run's artifact bundle once, then repaint the detail showing it. */
  function fetchArtifact(backtestId) {
    if (state.busy["artifact:" + backtestId]) return;
    state.busy["artifact:" + backtestId] = true;
    state.ctx.api.lab.artifact(backtestId, { signal: state.ctx.signal })
      .then(function (payload) { state.cache.artifact[backtestId] = payload; })
      .catch(function (err) {
        if (cancelled(err)) return;
        state.cache.artifact[backtestId] = { error: err };
      })
      .finally(function () {
        delete state.busy["artifact:" + backtestId];
        if (state.tab === "backtests" && state.sel.backtest === backtestId) renderBtMain();
      });
  }

  /**
   * Equity path fan chart (reference line 1369): the block-bootstrap cone the
   * realised curve is one draw from.
   */
  function fanChartPanel(record) {
    const panel = el("section.panel", { style: "margin-bottom:12px" });
    const cached = state.cache.fan[record.id];
    const badgeText = cached && !cached.error
      ? (cached.shown + " shown · " + fmt.int(cached.paths) + " calculated")
      : "block bootstrap";
    panel.appendChild(el("div.panel-title", "Equity path fan chart ", ui.badge(badgeText)));
    if (cached && cached.error) {
      panel.appendChild(errorNote("The resample could not be computed", cached.error));
      return el("div.ac", { dataset: { from: "agent-center · fan chart" } }, panel);
    }
    if (!cached) {
      const box = el("div.chart-box.fan-chart", el("div.chart-grid"));
      box.appendChild(foot("Resampling " + record.id + "…"));
      panel.appendChild(box);
      panel.appendChild(foot("5,000 circular-block bootstrap paths of this run's own return "
        + "series; the resample is seeded, so the cone is reproducible.", "margin-top:6px"));
      state.ctx.api.lab.fan(record.id, { paths: 5000, shown: 200 },
        { signal: state.ctx.signal, timeoutMs: 300000 })
        .then(function (payload) {
          state.cache.fan[record.id] = payload;
          if (state.tab === "backtests" && state.sel.backtest === record.id) renderBtMain();
        })
        .catch(function (err) {
          if (cancelled(err)) return;
          state.cache.fan[record.id] = { error: err };
          if (state.tab === "backtests" && state.sel.backtest === record.id) renderBtMain();
        });
      return el("div.ac", { dataset: { from: "agent-center · fan chart" } }, panel);
    }
    const cone = coneSvg(cached, { height: 300, paths: 200 });
    cone.firstChild.classList.add("fan-chart");
    panel.appendChild(cone.firstChild);
    const summary = cached.summary || {};
    panel.appendChild(el("div.metric-grid", { style: "margin-top:8px" },
      el("div.metric", el("small", { text: "REALISED PERCENTILE" }),
        el("b", { text: summary.realised_percentile === undefined
          ? "—" : "p" + fmt.num(summary.realised_percentile, 0) })),
      el("div.metric", el("small", { text: "P(SHARPE > 0)" }),
        el("b", { text: percent(summary.prob_positive, 1) })),
      el("div.metric", el("small", { text: "MEDIAN MAX DD" }),
        el("b", { text: percent(summary.median_max_drawdown) })),
      el("div.metric", el("small", { text: "P95 MAX DD" }),
        el("b", { text: percent(summary.p95_max_drawdown) }))));
    panel.appendChild(foot("The realised path is one ordering of these returns; the cone is the "
      + "rest of the distribution the same returns could have produced. "
      + (summary.favourable_sequencing
        ? "This one sat high in the cone — favourable sequencing, not only edge."
        : "This one is an ordinary draw from it."), "margin-top:6px"));
    return el("div.ac", { dataset: { from: "agent-center · fan chart" } }, panel);
  }

  /** The equity curve (lightweight-charts) plus its drawdown, from results. */
  function equityPanel(record) {
    const panel = el("div.panel.tight", { style: "margin-bottom:12px" });
    panel.appendChild(el("div.ptitle", { style: "display:flex" },
      el("span", {}, "EQUITY · ", el("span.ok", { text: "— strategy" }), " · ",
        el("span.dim", { text: "-- benchmark " + (record.benchmark || "none") })),
      el("span.foot", { style: "margin-left:auto", text: "lightweight-charts · real curve" })));
    const chartBox = el("div", { style: "height:260px" });
    const ddBox = el("div", { style: "height:110px;margin-top:8px" });
    panel.appendChild(chartBox);
    panel.appendChild(el("label", { style: "margin-top:8px", text: "UNDERWATER · DRAWDOWN FROM PEAK" }));
    panel.appendChild(ddBox);
    const stats = record.stats || {};
    panel.appendChild(el("div.grid.g3", { style: "gap:8px;margin-top:8px" },
      ui.kv("Max drawdown", percent(stats.max_drawdown), { tone: "bad" }),
      ui.kv("Best period", signedPercent(stats.best_period), { tone: "ok" }),
      ui.kv("Worst period", signedPercent(stats.worst_period), { tone: "bad" })));

    const cached = state.cache.results[record.id];
    if (cached && cached.equity_curve) {
      drawCurve(chartBox, ddBox, cached);
    } else if (cached && cached.error) {
      panel.appendChild(errorNote("The equity curve could not be loaded", cached.error));
    } else {
      chartBox.appendChild(foot("Loading the equity curve…"));
      state.ctx.api.lab.backtestResults(record.id, { signal: state.ctx.signal }).then(function (payload) {
        state.cache.results[record.id] = payload;
        if (state.tab === "backtests" && state.sel.backtest === record.id) renderBtMain();
      }).catch(function (err) {
        if (cancelled(err)) return;
        state.cache.results[record.id] = { error: err };
        if (state.tab === "backtests" && state.sel.backtest === record.id) renderBtMain();
      });
    }
    return panel;
  }

  /** Draw the equity and drawdown series with lightweight-charts. */
  function drawCurve(chartBox, ddBox, results) {
    const curve = results.equity_curve;
    const dates = curve.dates || [];
    const equity = [];
    const benchmark = [];
    const drawdown = [];
    dates.forEach(function (date, index) {
      const value = curve.equity[index];
      if (value !== null && value !== undefined) equity.push({ time: date, value: value });
      if (curve.benchmark && curve.benchmark[index] !== null && curve.benchmark[index] !== undefined) {
        benchmark.push({ time: date, value: curve.benchmark[index] });
      }
      if (curve.drawdown && curve.drawdown[index] !== null && curve.drawdown[index] !== undefined) {
        drawdown.push({ time: date, value: curve.drawdown[index] * 100 });
      }
    });
    if (!equity.length) {
      ui.render(chartBox, foot("The stored run has no equity points."));
      return;
    }
    try {
      state.chart = global.AlgoCharts.equityChart(chartBox, { height: 260 });
      state.chart.setLine(equity);
      if (benchmark.length) state.chart.setBenchmark(benchmark);
      state.chart.fit();
      if (drawdown.length) {
        state.ddChart = global.AlgoCharts.priceChart(ddBox, { type: "area", height: 110, color: "#ff5c5c" });
        state.ddChart.setLine(drawdown);
        state.ddChart.fit();
      }
    } catch (err) {
      ui.render(chartBox, errorNote("The chart library failed", err));
    }
  }

  /** In-sample / out-of-sample / benchmark breakdown, all from the record. */
  function windowsPanel(record) {
    const rows = [
      ["Total return", "total_return", signedPercent],
      ["CAGR", "cagr", signedPercent],
      ["Volatility", "volatility", percent],
      ["Sharpe", "sharpe", ratio],
      ["Sortino", "sortino", ratio],
      ["Max drawdown", "max_drawdown", percent],
      ["Hit rate", "hit_rate", percent],
      ["Turnover", "turnover", ratio],
      ["Trades", "trades", fmt.int],
      ["Benchmark return", "benchmark_return", signedPercent],
      ["Excess return", "excess_return", signedPercent],
      ["Information ratio", "information_ratio", ratio],
      ["Alpha vs benchmark", "alpha_vs_benchmark", signedPercent],
      ["Beta vs benchmark", "beta_vs_benchmark", ratio],
    ];
    const windows = [
      { label: "FULL", stats: record.stats },
      { label: "IN SAMPLE", stats: record.in_sample },
      { label: "OUT OF SAMPLE", stats: record.out_of_sample },
    ];
    const table = el("table.tbl");
    const head = el("tr", el("th", { style: "width:34%", text: "METRIC" }));
    windows.forEach(function (w) { head.appendChild(el("th.r", { text: w.label })); });
    table.appendChild(head);
    rows.forEach(function (row) {
      const tr = el("tr", el("td.dim", { text: row[0] }));
      windows.forEach(function (w) {
        const value = w.stats ? w.stats[row[1]] : null;
        tr.appendChild(el("td.r", { text: value === null || value === undefined ? "—" : row[2](value) }));
      });
      table.appendChild(tr);
    });
    const oos = record.out_of_sample;
    return el("div.panel.tight", { style: "margin-bottom:12px" },
      el("div.ptitle", { style: "display:flex" },
        el("span", { text: "WINDOWS · STRATEGY vs BENCHMARK " + (record.benchmark || "none") }),
        el("span.foot", { style: "margin-left:auto",
          text: oos ? "OOS from " + fmt.etDate(String(oos.start || "").slice(0, 10)) : "no out-of-sample window" })),
      table);
  }

  /** The pinned configuration, collapsed by default like the reference. */
  function configPanel(config) {
    const body = el("div", { style: "display:none" });
    const flat = flattenConfig(config || {});
    const keys = Object.keys(flat);
    const columns = [[], [], []];
    keys.forEach(function (key, index) { columns[index % 3].push(key); });
    body.appendChild(el("div.grid.g3", { style: "gap:12px" },
      columns.map(function (group) {
        return el("div.panel.tight", group.map(function (key) {
          return ui.kv(key, flat[key] === null || flat[key] === "" ? "—" : String(flat[key]), { tone: "hi" });
        }));
      })));
    body.appendChild(foot("Every field is pinned with the run. Changing one produces a new run, never an edit.", "margin-top:8px"));
    const toggle = el("div.foot", {
      style: "letter-spacing:1px;margin:10px 0 6px;cursor:pointer",
      text: "▾ CONFIGURATION · " + keys.length + " PINNED FIELDS",
    });
    toggle.addEventListener("click", function () {
      body.style.display = body.style.display === "none" ? "block" : "none";
    });
    return el("div.panel.tight", { style: "margin-bottom:12px" }, toggle, body);
  }

  /** The gate summary of one run, with the actions that change it. */
  function gatesPanel(record) {
    const report = reportsByBacktest()[record.id];
    const panel = el("div.panel.tight", { style: "margin-bottom:12px" });
    panel.appendChild(el("div.ptitle", { style: "display:flex" },
      el("span", { text: "PROMOTION GATES" }),
      el("span.foot", { style: "margin-left:auto",
        text: report ? "report " + report.id + " · " + when(report.evaluated_at) : "never validated" })));
    if (report) {
      panel.appendChild(gateTable(report.gates));
      panel.appendChild(el("div.kv", { style: "margin-top:6px" },
        el("span", { text: "Verdict" }),
        el("span", { class: report.passed ? "ok" : (report.verdict === "fail" ? "bad" : "warn"),
          text: report.verdict.toUpperCase() + " · " + report.gates_passed + "/" + report.gates_total
            + " · score " + fmt.num(report.score, 0) })));
    } else {
      panel.appendChild(foot("This run has never been put through the gate suite.", "padding:6px 0"));
    }
    const validateBtn = tbtn(report ? "RE-EVALUATE GATES" : "▷ EVALUATE GATES", function () {
      validateBacktest(record.id, validateBtn);
    }, { primary: !report, small: true });
    panel.appendChild(el("div", { style: "display:flex;gap:8px;margin-top:8px;flex-wrap:wrap" },
      validateBtn,
      tbtn("OPEN IN VALIDATION →", function () {
        state.sel.backtest = record.id;
        goTab("validation");
        renderValidation();
      }, { small: true }),
      record.experiment_id ? tbtn("OPEN EXPERIMENT →", function () {
        state.anView = null;
        state.sel.experiment = record.experiment_id;
        state.mode.exp = "view";
        goTab("experiments", record.experiment_id);
        renderExperiments();
      }, { small: true }) : null));
    return panel;
  }

  /** A gate table: name, threshold, observed, outcome — all measured values. */
  function gateTable(gates) {
    return ui.table({
      tree: "tl",
      columns: [
        { label: "GATE", render: function (gate) { return el("span.hi", { text: gate.name }); } },
        { label: "THRESHOLD", render: function (gate) {
          return el("span.dim", { text: gate.comparator + " " + formatGateValue(gate.metric, gate.threshold) });
        } },
        { label: "OBSERVED", num: true, render: function (gate) {
          return el("span", { class: gateTone(gate.status),
            text: gate.observed === null || gate.observed === undefined ? "—" : formatGateValue(gate.metric, gate.observed) });
        } },
        { label: "WINDOW", render: function (gate) {
          return el("span.dim", { text: gate.window + (gate.hard ? " · hard" : "") });
        } },
        { label: "OUTCOME", num: true, render: function (gate) {
          return el("span", { class: gateTone(gate.status), text: String(gate.status).toUpperCase() });
        } },
      ],
      rows: gates || [],
      empty: "No gates.",
    });
  }

  /** Format a gate value in the unit its metric is measured in. */
  function formatGateValue(metric, value) {
    if (value === null || value === undefined) return "—";
    if (metric === "max_drawdown" || metric === "excess_return") return percent(value);
    if (metric === "sharpe_retention") return percent(value, 0);
    if (metric === "trades" || metric === "years") return fmt.num(value, metric === "years" ? 2 : 0);
    if (metric === "p_value") return fmt.num(value, 3);
    return fmt.num(value, 2);
  }

  /** The new-backtest form. */
  function backtestForm() {
    const fields = configFields(null);
    const experiment = select(
      [{ value: "", label: "— none —" }].concat(state.data.experiments.map(function (exp) {
        return { value: exp.id, label: exp.name + (exp.hypothesis_id ? " · " + exp.hypothesis_id : "") };
      })), state.sel.experiment || "");
    const panel = el("div.panel", { dataset: { form: "backtest" } });
    panel.appendChild(el("div", { style: "display:flex;align-items:center;gap:10px" },
      el("span.hi", { style: "font-family:system-ui;font-size:15px;font-weight:600", text: "New backtest" }),
      tbtn("CANCEL", function () {
        state.mode.bt = "view";
        renderBacktests();
      }, { small: true, style: "margin-left:auto" })));
    panel.appendChild(el("div.grid.g2", { style: "gap:8px;margin-top:12px" },
      field("EXPERIMENT · SETS THE LEDGER FAMILY", experiment)));
    panel.appendChild(fields.node);
    panel.appendChild(judgementNote());
    const runBtn = tbtn("▷ RUN BACKTEST", function () {
      const config = fields.read();
      if (!config.universe_id && !config.symbols.length) {
        global.toast.warn("Choose a universe or list explicit symbols.");
        return;
      }
      const exp = experimentById(experiment.value);
      runBtn.textContent = "▷ RUNNING…";
      state.ctx.api.lab.runBacktest({
        config: config,
        experiment_id: experiment.value || null,
        hypothesis_id: exp ? exp.hypothesis_id : null,
        source: "manual",
      }, { signal: state.ctx.signal, timeoutMs: 600000 }).then(function (record) {
        global.toast.ok("Backtest " + record.id + " " + record.status);
        state.sel.backtest = record.id;
        state.mode.bt = "view";
        return reload(["backtests", "summary", "evaluation"]);
      }).catch(function (err) {
        runBtn.textContent = "▷ RUN BACKTEST";
        fail("The backtest failed", err);
      });
    }, { primary: true });
    const queueBtn = tbtn("QUEUE WITHOUT RUNNING", function () {
      const config = fields.read();
      const exp = experimentById(experiment.value);
      state.ctx.api.lab.queueBacktest({
        config: config,
        experiment_id: experiment.value || null,
        hypothesis_id: exp ? exp.hypothesis_id : null,
        source: "manual",
      }, { signal: state.ctx.signal }).then(function (record) {
        global.toast.ok("Queued " + record.id);
        state.sel.backtest = record.id;
        state.mode.bt = "view";
        return reload(["backtests", "summary"]);
      }).catch(function (err) { fail("Could not queue the backtest", err); });
    });
    // The dry run answers "would this config even load?" before a long run is
    // spent finding out. It writes no ledger row, so it costs nothing to ask.
    const dryBox = el("div", { style: "margin-top:10px" });
    // tbtn renders a span, which has no `disabled`; a flag is what actually
    // stops a second click landing while the first is still in flight.
    let drying = false;
    const dryBtn = tbtn("DRY RUN · VALIDATE CONFIG ONLY", function () {
      if (drying) return;
      drying = true;
      const config = fields.read();
      dryBtn.textContent = "VALIDATING…";
      state.ctx.api.lab.validateConfig({ config: config },
        { signal: state.ctx.signal, timeoutMs: 300000 })
        .then(function (report) { paintDryRun(dryBox, report); })
        .catch(function (err) {
          if (cancelled(err)) return;
          ui.clear(dryBox);
          dryBox.appendChild(errorNote("The dry run could not be completed", err));
        })
        .finally(function () {
          drying = false;
          dryBtn.textContent = "DRY RUN · VALIDATE CONFIG ONLY";
        });
    });
    panel.appendChild(el("div", { style: "display:flex;gap:8px;margin-top:10px;flex-wrap:wrap" },
      runBtn, queueBtn, dryBtn));
    panel.appendChild(dryBox);
    return panel;
  }

  /**
   * Render one dry-run report: what the configuration resolves to, and every
   * reason the real run would refuse — before any compute is spent on it.
   */
  function paintDryRun(host, report) {
    ui.clear(host);
    const ok = report.ok && report.would_run;
    const box = el("div.note" + (ok ? "" : ".warn"), { style: "margin-top:10px" });
    box.appendChild(el("div.hi", {
      text: ok ? "✓ This configuration would run." : "⚠ This configuration would not run.",
    }));
    if (report.stage === "config") {
      // The config never parsed, so there is nothing else to report about it.
      (report.issues || []).forEach(function (issue) {
        box.appendChild(el("div.red", { style: "margin-top:4px", text: issue }));
      });
      host.appendChild(box);
      return;
    }
    const covered = report.covered || {};
    const bench = report.benchmark || {};
    box.appendChild(foot(
      fmt.int(report.symbol_count) + " symbols · " + fmt.int(report.bars) + " bars"
      + (covered.start ? " · " + covered.start + " → " + covered.end : "")
      + " · benchmark " + (bench.symbol || "none") + (bench.available
        ? " (" + fmt.int(bench.bars) + " bars)" : " (no stored history)"),
      "margin-top:4px"));
    if (report.signal) {
      box.appendChild(foot("Signal: " + report.signal, "margin-top:2px"));
    }
    (report.issues || []).forEach(function (issue) {
      box.appendChild(el("div.red", { style: "margin-top:4px", text: "· " + issue }));
    });
    (report.warnings || []).forEach(function (warning) {
      box.appendChild(el("div.amb", { style: "margin-top:4px", text: "⚠ " + warning }));
    });
    const symbols = report.symbols || [];
    if (symbols.length) {
      box.appendChild(foot("Resolved: " + symbols.slice(0, 24).join(", ")
        + (symbols.length > 24 ? " … and " + (symbols.length - 24) + " more" : ""),
        "margin-top:4px"));
    }
    box.appendChild(foot("Nothing was written to the ledger — this is a dry run.", "margin-top:6px"));
    host.appendChild(box);
  }

  function runQueuedBacktest(record, button) {
    button.textContent = "▷ RUNNING…";
    state.ctx.api.lab.executeBacktest(record.id, {}, { signal: state.ctx.signal, timeoutMs: 600000 })
      .then(function (finished) {
        global.toast.ok("Backtest " + finished.id + " " + finished.status);
        // A re-run replaces the result, the ledger and the pinned digests, so
        // none of the three may survive as a stale cache entry.
        delete state.cache.results[record.id];
        delete state.cache.artifact[record.id];
        return reload(["backtests", "summary", "evaluation"]);
      })
      .catch(function (err) {
        button.textContent = "▷ RUN THIS BACKTEST";
        fail("The backtest failed", err);
      });
  }

  function validateBacktest(backtestId, button) {
    button.textContent = "EVALUATING…";
    state.ctx.api.lab.validate(backtestId, {}, { signal: state.ctx.signal, timeoutMs: 600000 })
      .then(function (report) {
        global.toast.ok("Gates: " + report.gates_passed + "/" + report.gates_total + " · " + report.verdict);
        return reload(["reports", "evaluation", "experiments"]);
      })
      .catch(function (err) {
        button.textContent = "▷ EVALUATE GATES";
        fail("Gate evaluation failed", err);
      });
  }

  // ------------------------------------------------------------- validation

  function renderValidation() {
    renderGateSuite();
    renderTrialLedger();
    renderDataQuality();
    renderCandidateVerdict();
  }

  /** GATE SUITE · WORKSPACE DEFAULTS — the real suite the backend applies. */
  function renderGateSuite() {
    const host = state.nodes.valGates;
    ui.clear(host);
    host.appendChild(el("div.ptitle", { text: "GATE SUITE · WORKSPACE DEFAULTS" }));
    if (state.errors.gates) {
      host.appendChild(errorNote("The gate suite could not be loaded", state.errors.gates));
      return;
    }
    host.appendChild(ui.table({
      tree: "tl",
      columns: [
        { label: "GATE", render: function (gate) { return el("span.hi", { text: gate.name }); } },
        { label: "THRESHOLD", render: function (gate) {
          return gate.comparator + " " + formatGateValue(gate.metric, gate.threshold);
        } },
        { label: "APPLIES", render: function (gate) {
          return el("span.dim", { text: gate.window + (gate.hard ? " · hard, never overridable" : " · overridable") });
        } },
      ],
      rows: state.data.gates,
    }));
    host.appendChild(foot("Every report stores the thresholds that judged it, so a later change never rewrites an old verdict.", "margin-top:8px"));
  }

  /** TRIAL LEDGER — counts read from the tables, not stored anywhere. */
  function renderTrialLedger() {
    const host = state.nodes.valLedger;
    ui.clear(host);
    const summary = state.data.summary;
    const reports = state.data.reports;
    const passed = reports.filter(function (r) { return r.passed; }).length;
    host.appendChild(el("div.ptitle", { text: "TRIAL LEDGER" }));
    host.appendChild(ui.kv("Backtest runs (all-time)", summary ? fmt.int(summary.backtests) : fmt.int(state.data.backtests.length), { tone: "hi" }));
    host.appendChild(ui.kv("· experiment runs", summary ? fmt.int(summary.experiment_runs) : "—"));
    host.appendChild(ui.kv("· from screens", fmt.int(state.data.backtests.filter(function (b) { return !!b.screen_id; }).length)));
    host.appendChild(ui.kv("· failed runs", fmt.int(state.data.backtests.filter(function (b) { return b.status === "failed"; }).length)));
    // Every sweep cell is a trial too: a 16-cell sweep is 16 looks at the data,
    // not one. Showing batches with the cell count behind them keeps the
    // multiple-comparison cost of a sweep visible in the ledger it inflates.
    const counts = state.cache.counts;
    if (counts === undefined) fetchAnalysisCounts();
    // `undefined` is still in flight; `null` means the request failed, and
    // saying so beats an ellipsis that never resolves.
    const num = function (key) {
      if (counts === undefined) return "…";
      if (counts === null) return "unavailable";
      return fmt.int(counts[key]);
    };
    host.appendChild(ui.kv("Sweep batches", num("sweeps"), {
      tone: "hi",
      title: counts
        ? fmt.int(counts.sweep_cells) + " sweep cells - each one is a separate look at the data"
        : "the analysis counts could not be loaded",
    }));
    host.appendChild(ui.kv("· sweep cells", num("sweep_cells")));
    host.appendChild(ui.kv("Walk-forward batches", num("walk_forwards"), {
      title: counts ? fmt.int(counts.walk_forward_folds) + " folds in total" : "",
    }));
    host.appendChild(ui.kv("· folds scored", num("walk_forward_folds")));
    host.appendChild(ui.kv("Validation reports", fmt.int(reports.length)));
    host.appendChild(ui.kv("· all gates passed", fmt.int(passed), { tone: passed ? "ok" : "" }));
    host.appendChild(ui.kv("Promoted strategies", fmt.int(state.data.strategies.length)));
    host.appendChild(ui.kv("Ledger mode", "APPEND-ONLY", { tone: "ok" }));
    host.appendChild(foot("Deflated Sharpe on every report is computed against this trial count for the hypothesis family.",
      "border-top:1px solid var(--line);margin-top:6px;padding-top:6px"));
  }

  /** Fetch the trial-ledger analysis counts once, then repaint the ledger. */
  function fetchAnalysisCounts() {
    if (state.busy.counts) return;
    state.busy.counts = true;
    state.ctx.api.lab.analysisCounts({ signal: state.ctx.signal })
      .then(function (payload) { state.cache.counts = payload; })
      .catch(function (err) {
        if (cancelled(err)) return;
        state.cache.counts = null;
      })
      .finally(function () {
        delete state.busy.counts;
        if (state.tab === "validation") renderTrialLedger();
      });
  }

  /** The data the gates judged, straight from /api/quality/summary. */
  function renderDataQuality() {
    const host = state.nodes.valQuality;
    ui.clear(host);
    host.appendChild(el("div.ptitle", { text: "DATA UNDER THE GATES" }));
    if (state.errors.quality) {
      host.appendChild(errorNote("Quality could not be loaded", state.errors.quality));
      return;
    }
    const quality = state.data.quality;
    if (!quality) {
      host.appendChild(foot("No quality summary available."));
      return;
    }
    const active = state.data.activeUniverse;
    host.appendChild(ui.kv("Active universe", active ? active.name : "—", { tone: "hi" }));
    host.appendChild(ui.kv("Universe members", active ? fmt.int(active.member_count || active.symbol_count) : "—"));
    host.appendChild(ui.kv("Datasets in store", fmt.int(quality.datasets_in_store)));
    host.appendChild(ui.kv("Checked datasets", fmt.int(quality.datasets)));
    host.appendChild(ui.kv("Overall quality",
      quality.overall_quality_pct === null || quality.overall_quality_pct === undefined
        ? "not measured yet" : fmt.num(quality.overall_quality_pct, 1) + "%",
      { tone: quality.overall_quality_pct === null ? "dim" : "ok" }));
    host.appendChild(ui.kv("Stale series", fmt.int(quality.stale_series), { tone: quality.stale_series ? "warn" : "" }));
    host.appendChild(ui.kv("Open incidents", fmt.int(quality.open_incidents), { tone: quality.open_incidents ? "bad" : "ok" }));
    host.appendChild(ui.kv("Last check run", quality.last_run ? when(quality.last_run) : "never"));
    const note = quality.open_incidents
      ? el("div.note.warn", { style: "margin-top:8px", text: "⚠ " + quality.open_incidents + " open data incident(s) — gate verdicts inherit whatever the data says." })
      : el("div.note.good", { style: "margin-top:8px", text: "✓ No open data incidents against the series these runs read." });
    host.appendChild(note);
  }

  /** The selected run's real gate outcomes, plus the promote action. */
  function renderCandidateVerdict() {
    const host = state.nodes.valCandidate;
    ui.clear(host);
    const record = backtestById(state.sel.backtest);
    const panel = el("div.panel");
    panel.appendChild(el("div.ptitle", { style: "display:flex" },
      el("span", { text: "CANDIDATE VERDICT" }),
      el("span.foot", { style: "margin-left:auto", text: record ? record.id + " · " + record.name : "no run selected" })));
    if (!record) {
      panel.appendChild(foot("Select a run in the BACKTESTS tab to see its gate outcomes."));
      host.appendChild(panel);
      return;
    }
    const chooser = select(state.data.backtests.map(function (b) {
      return { value: b.id, label: b.id + " · " + b.name + " · " + b.status };
    }), record.id, { style: "max-width:420px" });
    chooser.addEventListener("change", function () {
      state.sel.backtest = chooser.value;
      renderValidation();
    });
    panel.appendChild(field("RUN", chooser, { style: "margin:6px 0 10px" }));

    const report = reportsByBacktest()[record.id];
    if (!report) {
      panel.appendChild(el("div.note", { text: "This run has not been through the gate suite yet — the gates below are the definitions, with nothing observed." }));
      panel.appendChild(gateTable(state.data.gates.map(function (gate) {
        return Object.assign({}, gate, { observed: null, status: "pending" });
      })));
    } else {
      const ring = el("div", { style: "display:flex;align-items:center;gap:14px;margin:8px 0" },
        el("span.metricbig", { class: report.passed ? "ok" : (report.verdict === "fail" ? "bad" : "warn"),
          text: fmt.num(report.score, 0) }),
        el("div",
          el("div.hi", { text: report.verdict.toUpperCase().replace("CONDITIONAL", "CONDITIONAL PASS") }),
          foot(report.gates_passed + " of " + report.gates_total + " gates passed · "
            + report.trials + " trial(s) in the deflation ledger · evaluated " + when(report.evaluated_at))));
      panel.appendChild(ring);
      panel.appendChild(gateTable(report.gates));
      const failures = report.gates.filter(function (gate) { return !gate.passed; });
      if (failures.length) {
        const blocking = el("div.panel.tight", { style: "margin-top:10px" },
          el("label", { text: "BLOCKING ISSUES" }));
        failures.forEach(function (gate) {
          blocking.appendChild(el("div.kv",
            el("span", { class: gate.hard ? "bad" : "warn", text: (gate.hard ? "hard · " : "soft · ") + gate.name }),
            el("span", { text: gate.detail || (gate.observed === null ? "not measurable on this run"
              : "observed " + formatGateValue(gate.metric, gate.observed)) })));
        });
        panel.appendChild(blocking);
      }
      if (report.tests) panel.appendChild(testsPanel(report.tests));
    }

    const actions = el("div", { style: "display:flex;gap:8px;margin-top:10px;flex-wrap:wrap" });
    const evaluateBtn = tbtn(report ? "RE-EVALUATE GATES" : "▷ EVALUATE GATES", function () {
      validateBacktest(record.id, evaluateBtn);
    }, { primary: !report, small: true });
    actions.appendChild(evaluateBtn);
    actions.appendChild(tbtn("PROMOTE →", function () {
      goTab("pipeline");
      renderPipeline();
    }, { small: true, disabled: !report }));
    panel.appendChild(actions);
    host.appendChild(panel);
  }

  /** The statistical tests a report actually ran. */
  function testsPanel(tests) {
    const panel = el("div.panel.tight", { style: "margin-top:10px" }, el("label", { text: "STATISTICAL TESTS · COMPUTED FROM THE RETURN SERIES" }));
    const t = tests.t_test;
    if (t) {
      panel.appendChild(ui.kv("Excess-return t-test",
        "t " + fmt.num(t.t_stat, 2) + " · p " + fmt.num(t.p_value, 3) + " · " + t.observations + " obs",
        { tone: t.significant ? "ok" : "warn" }));
      panel.appendChild(ui.kv("· basis", t.basis));
    }
    const d = tests.deflated_sharpe;
    if (d) {
      panel.appendChild(ui.kv("Deflated Sharpe",
        ratio(d.deflated_sharpe) + " from " + ratio(d.annualised_sharpe) + " over " + d.trials + " trial(s)", { tone: "hi" }));
      panel.appendChild(ui.kv("· basis", d.basis));
    }
    const b = tests.bootstrap_sharpe;
    if (b) {
      panel.appendChild(ui.kv("Bootstrap Sharpe",
        "p05 " + ratio(b.p05) + " · median " + ratio(b.p50) + " · p95 " + ratio(b.p95)));
      panel.appendChild(ui.kv("· P(Sharpe > 0)", percent(b.prob_positive) + " over " + fmt.int(b.iterations) + " block resamples"));
    }
    const v = tests.variance_ratio;
    if (v) {
      Object.keys(v).forEach(function (key) {
        const value = v[key];
        if (value === null || typeof value === "object") return;
        const rendered = typeof value !== "number" ? String(value)
          : (Number.isInteger(value) ? fmt.int(value) : fmt.num(value, 3));
        panel.appendChild(ui.kv("Variance ratio · " + key, rendered));
      });
    }
    return panel;
  }

  // ------------------------------------------------------------- evaluation

  function renderEvaluation() {
    const host = state.nodes.evalBody;
    ui.clear(host);
    if (state.errors.evaluation) {
      host.appendChild(errorNote("The evaluation payload could not be loaded", state.errors.evaluation));
      return;
    }
    const evaluation = state.data.evaluation;
    if (!evaluation) {
      host.appendChild(el("div.panel", foot("No evaluation payload.")));
      return;
    }
    host.appendChild(scorecardPanel(evaluation.scorecard));
    host.appendChild(el("div.grid.g32", { style: "margin-bottom:12px" },
      candidatesPanel(evaluation.candidates || []),
      gateOutcomesPanel(evaluation.gate_outcomes)));
    host.appendChild(el("div.grid.g32",
      decisionLogPanel(),
      trackingPanel(evaluation.tracking || [])));
  }

  /** SCORECARD · the candidate comparison the backend computes. */
  function scorecardPanel(scorecard) {
    const panel = el("div.panel.tight", { style: "margin-bottom:12px" });
    const exportBtn = tbtn("EXPORT REPORT", function () { exportScorecard(scorecard); }, { small: true, style: "margin-left:auto" });
    panel.appendChild(el("div.ptitle", { style: "display:flex" },
      el("span", { text: "SCORECARD · CANDIDATES BY OUT-OF-SAMPLE SHARPE" }), exportBtn));
    const candidates = (scorecard && scorecard.candidates) || [];
    if (!candidates.length) {
      panel.appendChild(foot("No candidate has a completed backtest yet — run an experiment and the scorecard fills itself."));
      return panel;
    }
    const table = el("table.tbl");
    const head = el("tr", el("th", { style: "width:28%", text: "EVIDENCE" }));
    candidates.forEach(function (candidate) {
      const th = el("th");
      const ref = el("span.ref", { text: candidate.name });
      ref.addEventListener("click", function () {
        state.anView = null;
        state.sel.experiment = candidate.experiment_id;
        state.mode.exp = "view";
        goTab("experiments", candidate.experiment_id);
        renderExperiments();
      });
      th.appendChild(ref);
      head.appendChild(th);
    });
    table.appendChild(head);
    (scorecard.rows || []).forEach(function (row) {
      const tr = el("tr", el("td.dim", { text: row.label }));
      (row.values || []).forEach(function (value, index) {
        const display = (row.displays || [])[index];
        const note = (row.notes || [])[index];
        const tone = (row.tones || [])[index];
        const shown = value === null || value === undefined
          ? "—" : (display || formatScorecard(row.format, value));
        const td = el("td", { text: shown });
        // A cell with no number states why in its tooltip; a cell with one
        // carries the assumption it was computed under. Neither is decoration.
        if (note) td.title = note;
        if (value === null || value === undefined) td.classList.add("dim");
        else if (tone === "ok") td.classList.add("ok");
        else if (tone === "warn") td.classList.add("warn");
        tr.appendChild(td);
      });
      table.appendChild(tr);
    });
    const gatesRow = el("tr", { style: "background:#0b1120" }, el("td.dim", { text: "GATES PASSED" }));
    candidates.forEach(function (candidate) {
      gatesRow.appendChild(el("td", { class: candidate.gates ? "" : "dim", text: candidate.gates || "not validated" }));
    });
    table.appendChild(gatesRow);
    const verdictRow = el("tr", el("td.dim", { text: "VERDICT" }));
    candidates.forEach(function (candidate) {
      const td = el("td");
      const pill = el("span.pill", { text: (candidate.verdict || "not validated").toUpperCase() });
      if (candidate.verdict === "pass") pill.classList.add("grn");
      else if (candidate.verdict === "fail") pill.classList.add("red");
      else if (candidate.verdict === "conditional") pill.classList.add("amb");
      td.appendChild(pill);
      verdictRow.appendChild(td);
    });
    table.appendChild(verdictRow);
    const stageRow = el("tr", el("td.dim", { text: "STAGE" }));
    candidates.forEach(function (candidate) {
      stageRow.appendChild(el("td", { text: String(candidate.stage).toUpperCase() }));
    });
    table.appendChild(stageRow);
    panel.appendChild(table);
    return panel;
  }

  /** Format one scorecard cell in the unit the backend declared. */
  function formatScorecard(format, value) {
    if (value === null || value === undefined) return "—";
    if (format === "percent") return percent(value);
    if (format === "count") return fmt.int(value);
    if (format === "usd") return usdShort(value);
    return fmt.num(value, 2);
  }

  /** "$120M" / "$95.4M" — the scale a capacity figure is actually known to. */
  function usdShort(value) {
    const magnitude = Math.abs(value);
    if (magnitude >= 1e12) return "$" + fmt.num(value / 1e12, 1) + "T";
    if (magnitude >= 1e9) return "$" + fmt.num(value / 1e9, 1) + "B";
    if (magnitude >= 1e6) return "$" + fmt.num(value / 1e6, 1) + "M";
    if (magnitude >= 1e3) return "$" + fmt.num(value / 1e3, 1) + "K";
    return "$" + fmt.num(value, 0);
  }

  /** Download the scorecard as JSON — the same numbers, nothing added. */
  function exportScorecard(scorecard) {
    try {
      const payload = JSON.stringify({
        exported_at: new Date().toISOString(),
        universe: state.data.activeUniverse ? state.data.activeUniverse.id : null,
        scorecard: scorecard,
        candidates: state.data.evaluation ? state.data.evaluation.candidates : [],
      }, null, 2);
      const blob = new Blob([payload], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = el("a", { href: url, download: "tradinglib-scorecard.json" });
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      global.toast.ok("Scorecard exported");
    } catch (err) {
      fail("Export failed", err);
    }
  }

  /** Every experiment with its newest run and gate outcome. */
  function candidatesPanel(candidates) {
    const panel = el("div.panel.tight", el("div.ptitle", { text: "CANDIDATES · ONE ROW PER EXPERIMENT" }));
    if (!candidates.length) {
      panel.appendChild(foot("No experiments yet."));
      return panel;
    }
    panel.appendChild(ui.table({
      tree: "tl",
      columns: [
        { label: "EXPERIMENT", render: function (row) {
          const ref = el("span.ref", { text: row.name });
          ref.addEventListener("click", function () {
        state.anView = null;
        state.sel.experiment = row.experiment_id;
            state.mode.exp = "view";
            goTab("experiments", row.experiment_id);
            renderExperiments();
          });
          return ref;
        } },
        { label: "STAGE", render: function (row) { return el("span.dim", { text: String(row.stage).toUpperCase() }); } },
        { label: "OOS SHARPE", num: true, render: function (row) { return ratio(row.oos_sharpe); } },
        { label: "DEFL", num: true, render: function (row) { return ratio(row.deflated_sharpe); } },
        { label: "MAX DD", num: true, render: function (row) { return row.max_drawdown === null ? "—" : percent(row.max_drawdown); } },
        { label: "TRADES", num: true, render: function (row) { return row.trades === null ? "—" : fmt.int(row.trades); } },
        { label: "GATES", num: true, render: function (row) {
          if (!row.gates_total) return el("span.dim", { text: "—" });
          return el("span", { class: row.gates_passed === row.gates_total ? "ok" : "warn",
            text: row.gates_passed + "/" + row.gates_total });
        } },
      ],
      rows: candidates,
    }));
    return panel;
  }

  /** Gate pass rates over the reporting window. */
  function gateOutcomesPanel(outcomes) {
    const panel = el("div.panel.tight", el("div.ptitle", {
      text: "GATE OUTCOMES · LAST " + ((outcomes && outcomes.window_days) || 0) + " DAYS",
    }));
    if (!outcomes || !outcomes.reports) {
      panel.appendChild(foot("No validation reports in this window."));
      return panel;
    }
    panel.appendChild(ui.kv("Reports evaluated", fmt.int(outcomes.reports), { tone: "hi" }));
    (outcomes.by_gate || []).forEach(function (row) {
      const total = (row.pass || 0) + (row.fail || 0) + (row.warn || 0) + (row.pending || 0);
      const rate = total ? (row.pass || 0) / total : 0;
      panel.appendChild(ui.kv(row.name || row.gate, (row.pass || 0) + " / " + total + " passed"));
      const bar = el("div.bar", el("i", { style: "width:" + Math.round(rate * 100) + "%" }));
      if (rate < 0.5) bar.firstChild.classList.add("red");
      else if (rate < 0.8) bar.firstChild.classList.add("amb");
      panel.appendChild(bar);
    });
    return panel;
  }

  /** DECISION LOG · the real validation and promotion history. */
  function decisionLogPanel() {
    const panel = el("div.panel.tight", el("div.ptitle", { text: "DECISION LOG · VALIDATIONS AND PROMOTIONS" }));
    const entries = [];
    state.data.reports.forEach(function (report) {
      entries.push({
        at: report.evaluated_at,
        text: "gates " + report.gates_passed + "/" + report.gates_total + " · " + report.verdict
          + " on " + report.backtest_id + (report.comment ? " · " + report.comment : ""),
        tone: report.passed ? "ok" : (report.verdict === "fail" ? "bad" : "warn"),
      });
    });
    state.data.strategies.forEach(function (row) {
      entries.push({
        at: row.promoted_at,
        text: "promoted " + row.name + " → " + String(row.stage).toUpperCase()
          + (row.overrides && row.overrides.length ? " · " + row.overrides.length + " override(s)" : "")
          + (row.promoted_by ? " by " + row.promoted_by : ""),
        tone: "blu",
      });
    });
    entries.sort(function (a, b) { return String(b.at).localeCompare(String(a.at)); });
    if (!entries.length) {
      panel.appendChild(foot("Nothing decided yet. Evaluating gates or promoting a run writes here."));
      return panel;
    }
    entries.slice(0, 25).forEach(function (entry) {
      panel.appendChild(stat(when(entry.at), entry.text, entry.tone));
    });
    panel.appendChild(foot("Every line is a stored record; nothing here is derived for display.",
      "border-top:1px solid var(--line);margin-top:6px;padding-top:6px"));
    return panel;
  }

  /** Out-of-sample tracking of promoted strategies (the "paper" panel). */
  function trackingPanel(tracking) {
    const panel = el("div.panel.tight", el("div.ptitle", { text: "TRACKING · PROMOTED STRATEGIES OUT OF SAMPLE" }));
    if (!tracking.length) {
      panel.appendChild(foot("Nothing promoted yet. Promote a validated run in the PIPELINE tab and its live measurement appears here."));
      return panel;
    }
    tracking.forEach(function (record) {
      panel.appendChild(el("div.kv", { style: "padding:4px 0" },
        el("span.hi", { text: record.name }),
        el("span", { class: record.status === "killed" ? "bad" : "ok",
          text: String(record.stage).toUpperCase() + " · day " + record.days_live })));
      panel.appendChild(ui.kv("· live Sharpe (expected)",
        ratio(record.live_sharpe) + " (" + ratio(record.expected_sharpe) + ")"));
      panel.appendChild(ui.kv("· live return", signedPercent(record.live_return)));
      panel.appendChild(ui.kv("· max drawdown", percent(record.live_max_drawdown)));
      panel.appendChild(ui.kv("· decay score", record.decay_score === null || record.decay_score === undefined
        ? "—" : fmt.num(record.decay_score, 2),
        { tone: record.in_band === false ? "warn" : "" }));
      if (record.band && record.band.low !== undefined) {
        const low = Number(record.band.low);
        const high = Number(record.band.high);
        const live = Number(record.live_sharpe);
        if (isFinite(low) && isFinite(high) && high > low && isFinite(live)) {
          const position = Math.max(0, Math.min(1, (live - low) / (high - low)));
          const track = el("div.ktrack", el("i", { style: "width:" + Math.round(position * 100) + "%" }), el("u"));
          if (record.in_band === false) track.firstChild.classList.add("red");
          panel.appendChild(track);
          panel.appendChild(foot("band " + ratio(low) + " – " + ratio(high) + " · the marker is the kill line"));
        }
      }
      (record.notes || []).forEach(function (note) { panel.appendChild(foot("· " + note)); });
      panel.appendChild(foot("measured " + when(record.updated_at),
        "border-top:1px solid var(--line);margin:6px 0;padding-top:6px"));
    });
    const refresh = tbtn("RE-MEASURE ALL", function () {
      refresh.textContent = "MEASURING…";
      state.ctx.api.post("/api/lab/evaluation/refresh", {}, { signal: state.ctx.signal, timeoutMs: 600000 })
        .then(function () {
          global.toast.ok("Tracking re-measured");
          return reload(["evaluation", "strategies"]);
        })
        .catch(function (err) {
          refresh.textContent = "RE-MEASURE ALL";
          fail("Could not re-measure tracking", err);
        });
    }, { small: true });
    panel.appendChild(refresh);
    return panel;
  }

  // --------------------------------------------------------------- pipeline

  function renderPipeline() {
    const host = state.nodes.pipeBody;
    ui.clear(host);
    host.appendChild(el("div.grid.g23",
      advancePanel(),
      el("div", { style: "display:flex;flex-direction:column;gap:12px" },
        promotionQueuePanel(),
        stageHistoryPanel())));
  }

  /** ADVANCE STRATEGY: real gate checks, real overrides, real promotion. */
  function advancePanel() {
    const panel = el("div.panel");
    panel.appendChild(el("div.ptitle", { text: "ADVANCE STRATEGY" }));
    const runs = state.data.backtests.filter(function (b) { return b.status === "complete"; });
    if (!runs.length) {
      panel.appendChild(foot("Nothing to advance: no completed backtest exists yet."));
      return panel;
    }
    if (!state.sel.backtest || !backtestById(state.sel.backtest)) state.sel.backtest = runs[0].id;
    const chooser = select(runs.map(function (b) {
      const exp = experimentById(b.experiment_id);
      return { value: b.id, label: (exp ? exp.name : b.name) + " · " + b.id };
    }), state.sel.backtest);
    chooser.addEventListener("change", function () {
      state.sel.backtest = chooser.value;
      renderPipeline();
    });
    panel.appendChild(field("BACKTEST", chooser, { style: "margin-bottom:8px" }));

    const record = backtestById(state.sel.backtest);
    const experiment = record ? experimentById(record.experiment_id) : null;
    const stage = select(PROMOTION_STAGES, "paper");
    panel.appendChild(field("TARGET STAGE", stage, { style: "margin-bottom:8px" }));
    panel.appendChild(el("label", { text: "GATE CHECKS · COMPUTED FROM RUN ARTIFACTS" }));

    const report = reportsByBacktest()[state.sel.backtest];
    const overrideBoxes = [];
    if (!report) {
      const evaluateBtn = tbtn("▷ EVALUATE GATES FIRST", function () {
        validateBacktest(state.sel.backtest, evaluateBtn);
      }, { primary: true, small: true, style: "margin-top:8px" });
      panel.appendChild(el("div.note.warn", { style: "margin:6px 0",
        text: "This run has no validation report. Promotion is refused until the gates have been computed." }));
      panel.appendChild(evaluateBtn);
      return panel;
    }
    report.gates.forEach(function (gate) {
      const mark = el("span", { class: gateTone(gate.status), text: gate.passed ? "✓" : (gate.status === "warn" ? "◐" : "✗") });
      const right = el("span", { class: gateTone(gate.status),
        text: gate.observed === null || gate.observed === undefined
          ? "not measurable" : formatGateValue(gate.metric, gate.observed) });
      panel.appendChild(el("div.kv", { style: "border-bottom:1px solid #141b2b" },
        el("span", {}, mark, " ", gate.name), right));
      if (!gate.passed && !gate.hard) {
        const box = el("input", { type: "checkbox", dataset: { gate: gate.id } });
        overrideBoxes.push({ box: box, gate: gate });
      }
    });
    if (overrideBoxes.length) {
      const overrides = el("div.panel.tight", { style: "margin-top:8px" },
        el("label", { text: "OVERRIDES · SOFT GATES ONLY" }));
      overrideBoxes.forEach(function (item) {
        overrides.appendChild(el("div", { style: "padding:2px 0" },
          item.box, el("span", { text: " " + item.gate.name })));
      });
      overrides.appendChild(foot("A hard gate can never be overridden; the backend refuses it."));
      panel.appendChild(overrides);
    }
    const notes = el("textarea", { rows: "2", placeholder: "Why this advances despite the outstanding gate(s)." });
    panel.appendChild(field("NOTES · REQUIRED FOR AN OVERRIDE", notes, { style: "margin-top:8px" }));
    const actor = input("", { placeholder: "who is approving" });
    panel.appendChild(field("APPROVING AS · REQUIRED", actor));
    panel.appendChild(approvalsPanel(state.sel.backtest, stage.value));

    const advance = tbtn("▷ SIGN AND ADVANCE", function () {
      const signer = actor.value.trim();
      if (!signer) {
        global.toast.warn("Name the approver — a promotion nobody is accountable for is refused.");
        actor.focus();
        return;
      }
      const chosen = overrideBoxes.filter(function (item) { return item.box.checked; })
        .map(function (item) { return item.gate.id; });
      advance.textContent = "▷ SIGNING…";
      state.ctx.api.lab.promote(state.sel.backtest, {
        stage: stage.value,
        comment: notes.value.trim() || null,
        overrides: chosen,
        actor: signer,
      }, { signal: state.ctx.signal, timeoutMs: 300000 }).then(function (result) {
        const promotion = result.promotion || {};
        delete state.cache.promotion[state.sel.backtest];
        if (result.promoted) {
          global.toast.ok((experiment ? experiment.name : state.sel.backtest)
            + " → " + stage.value.toUpperCase() + " · signed by " + promotion.signed.join(" + "));
        } else {
          global.toast.ok(signer + " signed · " + (promotion.label || "one more approval needed"));
        }
        return reload(["strategies", "experiments", "reports", "evaluation", "summary"]);
      }).catch(function (err) {
        advance.textContent = "▷ SIGN AND ADVANCE";
        fail("Promotion refused", err);
      });
    }, { primary: true, style: "margin-top:10px" });
    panel.appendChild(advance);
    panel.appendChild(foot((report.passed
      ? "Every gate passed; this promotion needs no override."
      : "Outstanding gates: " + report.gates.filter(function (g) { return !g.passed; }).length
        + ". Promotion without an override is refused with a 409.")
      + " Two different named approvers are required: the first signature parks the "
      + "promotion, the second releases it.", "margin-top:6px"));
    return panel;
  }

  /**
   * The two-key state of one backtest's promotion (D58): who has signed, who is
   * still needed, and a way to withdraw a signature that has not released yet.
   */
  function approvalsPanel(backtestId, stage) {
    const panel = el("div.panel.tight", { style: "margin-top:8px" },
      el("label", { text: "APPROVALS · TWO DIFFERENT PEOPLE" }));
    const cached = state.cache.promotion[backtestId];
    if (cached && cached.error) {
      panel.appendChild(errorNote("The sign-offs could not be read", cached.error));
      return panel;
    }
    if (!cached) {
      panel.appendChild(foot("Reading the sign-offs…"));
      state.ctx.api.lab.promotionApprovals(backtestId, { stage: stage }, { signal: state.ctx.signal })
        .then(function (payload) {
          state.cache.promotion[backtestId] = payload;
          if (state.tab === "pipeline") renderPipeline();
        })
        .catch(function (err) {
          if (cancelled(err)) return;
          state.cache.promotion[backtestId] = { error: err };
          if (state.tab === "pipeline") renderPipeline();
        });
      return panel;
    }
    const signed = cached.signed || [];
    const line = el("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0" });
    line.appendChild(el("span", {
      class: cached.complete ? "ok" : "warn",
      text: cached.complete ? "approved" : cached.label,
    }));
    signed.forEach(function (name) {
      line.appendChild(el("span.chip.grn.mini", { text: name + " ✓" }));
    });
    for (let i = signed.length; i < cached.required; i += 1) {
      line.appendChild(el("span.chip.mini", { text: "pending" }));
    }
    panel.appendChild(line);
    (cached.approvals || []).forEach(function (entry) {
      const row = el("div.kv");
      row.appendChild(el("span", { text: entry.actor + " · " + when(entry.approved_at)
        + (entry.comment ? " · " + entry.comment : "") }));
      if (entry.strategy_id) {
        row.appendChild(el("span.dim", { text: "released " + entry.strategy_id }));
      } else {
        row.appendChild(tbtn("WITHDRAW", function () {
          state.ctx.api.lab.withdrawApproval(backtestId, entry.actor, { signal: state.ctx.signal })
            .then(function () {
              delete state.cache.promotion[backtestId];
              global.toast.ok("Withdrew " + entry.actor + "'s sign-off");
              renderPipeline();
            })
            .catch(function (err) { fail("Could not withdraw the sign-off", err); });
        }, { small: true }));
      }
      panel.appendChild(row);
    });
    if (!signed.length) {
      panel.appendChild(foot("Nobody has signed this promotion yet."));
    }
    return panel;
  }

  /** PROMOTION QUEUE: everything promoted, plus anything awaiting a co-signer. */
  function promotionQueuePanel() {
    const panel = el("div.panel.tight", el("div.ptitle", { text: "PROMOTION QUEUE" }));
    if (state.errors.strategies) {
      panel.appendChild(errorNote("Promoted strategies could not be loaded", state.errors.strategies));
      return panel;
    }
    // Anything one person has signed but nobody has co-signed sits here first:
    // it is the state the reference calls "needs 2nd approval".
    const pending = Object.keys(state.cache.promotion)
      .map(function (id) { return state.cache.promotion[id]; })
      .filter(function (entry) { return entry && !entry.error && !entry.complete && (entry.signed || []).length; });
    pending.forEach(function (entry) {
      const record = backtestById(entry.backtest_id);
      const item = el("div.rowitem");
      item.appendChild(el("div", { style: "display:flex;gap:8px" },
        el("span.hi", { text: record ? record.name : entry.backtest_id }),
        el("span.warn", { style: "margin-left:auto", text: entry.label.toUpperCase() })));
      item.appendChild(el("span.foot", {
        text: entry.signed.map(function (name) { return name + " ✓"; }).join(" · ")
          + " · " + entry.outstanding + " pending · target " + String(entry.stage).toUpperCase(),
      }));
      panel.appendChild(item);
    });
    if (!state.data.strategies.length) {
      panel.appendChild(foot(pending.length
        ? "Nothing released yet — the promotions above still need a second signature."
        : "Nothing promoted yet."));
      return panel;
    }
    state.data.strategies.forEach(function (row) {
      const item = el("div.rowitem");
      const tone = row.status === "killed" ? "bad" : (row.status === "held" ? "dim" : "ok");
      item.appendChild(el("div", { style: "display:flex;gap:8px" },
        el("span.hi", { text: row.name }),
        el("span", { style: "margin-left:auto", class: tone,
          text: String(row.stage).toUpperCase() + " · " + String(row.status).toUpperCase() })));
      item.appendChild(el("span.foot", {
        text: "promoted " + when(row.promoted_at)
          + (row.promoted_by ? " by " + row.promoted_by : "")
          + (row.overrides && row.overrides.length ? " · " + row.overrides.length + " override(s)" : "")
          + (row.comment ? " · " + row.comment : ""),
      }));
      const actions = el("div", { style: "display:flex;gap:6px;margin-top:6px" });
      ["active", "held", "killed"].filter(function (status) { return status !== row.status; })
        .forEach(function (status) {
          actions.appendChild(tbtn(status.toUpperCase(), function () {
            state.ctx.api.post("/api/lab/validation/strategies/" + encodeURIComponent(row.id) + "/status",
              { status: status }, { signal: state.ctx.signal })
              .then(function () {
                global.toast.ok(row.name + " → " + status.toUpperCase());
                return reload(["strategies", "evaluation"]);
              })
              .catch(function (err) { fail("Could not change the status", err); });
          }, { small: true }));
        });
      actions.appendChild(tbtn("OPEN RUN →", function () {
        state.sel.backtest = row.backtest_id;
        goTab("backtests", row.backtest_id);
        renderBacktests();
      }, { small: true }));
      item.appendChild(actions);
      panel.appendChild(item);
    });
    return panel;
  }

  /** STAGE HISTORY for the selected run: reports, promotion, hypothesis moves. */
  function stageHistoryPanel() {
    const record = backtestById(state.sel.backtest);
    const panel = el("div.panel.tight", el("div.ptitle", {
      text: "STAGE HISTORY · " + (record ? record.name : "no run selected"),
    }));
    if (!record) {
      panel.appendChild(foot("Select a run above."));
      return panel;
    }
    const entries = [];
    entries.push({ at: record.created_at, text: "run queued (" + record.source + ")" });
    if (record.started_at) entries.push({ at: record.started_at, text: "run started" });
    if (record.finished_at) entries.push({ at: record.finished_at, text: "run " + record.status });
    state.data.reports.filter(function (report) { return report.backtest_id === record.id; })
      .forEach(function (report) {
        entries.push({
          at: report.evaluated_at,
          text: "gates evaluated · " + report.gates_passed + "/" + report.gates_total + " · " + report.verdict,
          tone: report.passed ? "ok" : "warn",
        });
      });
    state.data.strategies.filter(function (row) { return row.backtest_id === record.id; })
      .forEach(function (row) {
        entries.push({ at: row.promoted_at, text: "promoted to " + String(row.stage).toUpperCase(), tone: "blu" });
      });
    const hypothesis = hypothesisById(record.hypothesis_id);
    if (hypothesis && hypothesis.status_history) {
      hypothesis.status_history.forEach(function (change) {
        entries.push({
          at: change.at || change.ts,
          text: hypothesis.id + " → " + String(change.to || change.status || "").toUpperCase()
            + (change.note ? " · " + change.note : ""),
        });
      });
    }
    entries.sort(function (a, b) { return String(a.at).localeCompare(String(b.at)); });
    entries.forEach(function (entry) { panel.appendChild(stat(when(entry.at), entry.text, entry.tone)); });
    panel.appendChild(foot("Every transition is stored; this panel only reads them.",
      "border-top:1px solid var(--line);margin-top:5px;padding-top:5px"));
    return panel;
  }

  global.__algoLabState = state; // inspection hook for manual verification
})(window);
