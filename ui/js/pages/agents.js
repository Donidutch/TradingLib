/* TradingLib — Agent Center workspace.
 *
 * Replicates REFERENCE's Research Control Room (the v5 shell inside
 * #view-agents) and drives every part of it from /api/agents/*:
 *
 *   Overview        pending approvals + unacknowledged findings, live counters
 *   Pipeline        the Pipeline Builder: saved pipelines, versions, stage
 *                   cards, stage configuration, the run inspector and the
 *                   run ledger — all persisted, all executable
 *   AutoPipeline    real schedules with their guardrails and their real runs
 *   Findings        the findings registry with search
 *   Knowledge       the knowledge base with full-text search
 *   Approval Review the human gate: approving resumes the run, rejecting
 *                   kills it
 *   Agent Studio    the agent registry with each agent's real run record
 *   Audit Log       the immutable log, filterable
 *   Configuration   budgets (writable), provider status, spend
 *
 * Live updates arrive on the `runs` and `approvals` WebSocket channels, which
 * tradinglib.agents.runner publishes onto the event bus: `run`/`stage`/`log`
 * frames repaint the stage cards, the run table and the inspector's Logs tab
 * while a run executes.
 *
 * Routing: "#agents/<sub>" selects a view. The reference's own keys work
 * ("inbox", "tl-pipeline", "tl-knowledge", "agents") and so do the readable
 * aliases ("overview", "autopipeline", "knowledge", "studio").
 *
 * Honesty rules kept here:
 *   - nothing is rendered that the API did not return. Where the reference
 *     shows a control the engine has no counterpart for (single-stage
 *     execution, "pause"), the control is either absent or disabled with the
 *     reason on it, never wired to a fake action;
 *   - a failed call renders the error it got, and the page keeps working.
 */
(function (global) {
  "use strict";

  /** The nine workspaces of the control room, in the reference's order. */
  const VIEWS = [
    { key: "inbox", label: "Overview", title: "Overview",
      blurb: "Research needing a decision, and the state of the workspace" },
    { key: "pipeline", label: "Pipeline", title: "Pipeline Builder",
      blurb: "Compose and version agent research workflows" },
    { key: "tl-pipeline", label: "AutoPipeline", title: "AutoPipeline",
      blurb: "Scheduled autonomous runs and the guardrails that bound them" },
    { key: "findings", label: "Findings", title: "Findings",
      blurb: "Registry of agent-produced research claims" },
    // Evidence Explorer is a REFERENCE view but NOT a REFERENCE nav tab: the
    // reference's final nav row is the seven ORDER entries plus Knowledge and
    // AutoPipeline, and the explorer is reached from a finding's or a run's
    // "Trace" link. `nav:false` keeps it routable (#tl/agents/evidence) and
    // titled without adding a tenth tab the reference does not have.
    { key: "evidence", label: "Evidence", title: "Evidence Explorer", nav: false,
      blurb: "Trace a decision back through validation, experiments, hypotheses and sources" },
    { key: "tl-knowledge", label: "Knowledge", title: "Knowledge",
      blurb: "What the agents have learned, and where it came from" },
    { key: "approval", label: "Approval Review", title: "Approval Review",
      blurb: "Make a documented human research decision" },
    { key: "agents", label: "Agent Studio", title: "Agent Studio",
      blurb: "Configure, evaluate, and version research agents" },
    { key: "audit", label: "Audit Log", title: "Audit Log",
      blurb: "Immutable actions across agents and reviewers" },
    { key: "configuration", label: "Configuration", title: "Configuration",
      blurb: "Research autonomy, budgets, providers and spend" },
  ];

  /** The subset of VIEWS the nav row shows — the reference's nine tabs. */
  const NAV_VIEWS = VIEWS.filter(function (view) { return view.nav !== false; });

  /** Readable sub-route aliases for the reference's internal view keys. */
  const ALIASES = {
    overview: "inbox",
    runs: "pipeline",
    builder: "pipeline",
    autopipeline: "tl-pipeline",
    schedules: "tl-pipeline",
    knowledge: "tl-knowledge",
    lineage: "evidence",
    trace: "evidence",
    studio: "agents",
    approvals: "approval",
    config: "configuration",
    audit: "audit",
  };

  /** Run/stage status → the reference's pipeline-progress class. */
  const PROGRESS_CLASS = {
    pending: "queued",
    running: "running",
    awaiting_approval: "awaiting-approval",
    succeeded: "completed",
    failed: "failed",
    skipped: "skipped",
    killed: "cancelled",
  };

  /** Run status → the reference's pipeline-status-badge class and label. */
  const RUN_BADGE = {
    queued: ["state-queued", "Queued"],
    running: ["state-running", "Running"],
    awaiting_approval: ["state-awaiting-approval", "Awaiting approval"],
    succeeded: ["state-completed", "Completed"],
    failed: ["state-failed", "Failed"],
    killed: ["state-cancelled", "Cancelled"],
  };

  /** The five inspector tabs, in the reference's order. */
  const INSPECTOR_TABS = ["preview", "inputs", "outputs", "logs", "artifacts"];

  // ---------------------------------------------------------------- state

  /** @type {any} the router context of the current mount */
  let ctx = null;
  /** @type {any} window.AlgoUI */
  let ui = null;
  /** Shorthand for AlgoUI.el, bound at mount. */
  let el = null;
  /** @type {HTMLElement|null} #v5Workspace */
  let workspace = null;
  /** @type {Array<() => void>} teardown callbacks run by unmount() */
  let teardown = [];

  /** Everything the page has loaded. Rebuilt on every mount. */
  let state = null;

  /** A fresh, empty page state. */
  function blankState() {
    return {
      view: "inbox",
      loading: true,
      error: null,
      // shared
      config: null,          // GET /config
      models: null,          // GET /models
      definitions: [],       // GET /definitions
      agentCounts: null,
      // overview
      inbox: null,
      inboxItemId: null,
      inboxQueue: "Needs attention",
      inboxType: "All types",
      inboxQuery: "",
      overviewRuns: [],
      overviewFindings: [],
      overviewKnowledge: [],
      guardrailBlocks: null,
      scheduleStatus: null,
      // pipeline builder
      pipelines: [],
      pipelineId: null,
      pipeline: null,
      stageId: null,
      catalogOpen: false,
      preview: null,
      runs: [],
      runCounts: null,
      runId: null,
      run: null,
      inspectorTab: "preview",
      liveLine: "",
      estimate: null,       // GET /pipelines/{id}/estimate
      compareA: null,       // "RUN A" picker
      compareB: null,       // "RUN B" picker
      comparison: null,     // GET /runs/compare
      // autopipeline
      schedules: [],
      scheduleId: null,
      universes: [],        // GET /api/universes, for the brief's UNIVERSE pick
      // evidence explorer
      evidence: null,          // GET /evidence
      evidenceType: "All types",
      evidenceFocus: "All relationships",
      evidenceNodeId: null,
      evidenceCentered: null,  // node the graph has been centred on
      // findings
      findings: null,
      findingQuery: "",
      findingId: null,
      // knowledge
      knowledge: null,
      knowledgeQuery: "",
      knowledgeId: null,
      knowledgeEntry: null,
      // approval
      approvals: null,
      approvalId: null,
      approvalRationale: "",
      approvalConditions: "",
      validationTargets: null,   // GET /approvals/validation-targets
      validationTarget: null,
      // studio
      studioKind: "all",
      studioAgentId: null,
      studioTab: "overview",
      /** Name of the Config tab's selected PRESET chip, or null. */
      studioPreset: null,
      agentVersions: null,       // GET /definitions/{id}/versions
      agentVersionsFor: null,    // which agent agentVersions belongs to
      // audit
      audit: null,
      auditActor: "",
      auditAction: "",
      auditQuery: "",
      auditRunId: null,
    };
  }

  // ------------------------------------------------------------- utilities

  /** GET/POST/PUT/DELETE against /api/agents, carrying the route's signal. */
  function req(method, path, payload) {
    const options = { signal: ctx.signal };
    const url = "/api/agents" + path;
    if (method === "GET") return ctx.api.get(url, payload || null, options);
    if (method === "POST") return ctx.api.post(url, payload || {}, options);
    if (method === "PUT") return ctx.api.put(url, payload || {}, options);
    return ctx.api.del(url, options);
  }

  /** True when a rejection is just this route being torn down. */
  function aborted(err) {
    return !!err && (err.name === "AbortError" || (err.cause && err.cause.name === "AbortError"));
  }

  /** Report a failed action honestly: a toast plus the console detail. */
  function fail(what, err) {
    if (aborted(err)) return;
    const message = err && err.message ? err.message : String(err);
    console.error("agent center: " + what + " failed", err);
    ctx.toast(what + " failed — " + message, "red");
  }

  /** "14:53:08" in New York for an ISO timestamp, "—" when absent. */
  function clockOf(value) {
    const date = ui.fmt.toDate(value);
    return date ? ui.fmt.etClock(date) : "—";
  }

  /** "Today 14:22" / "Aug 05 16:31" in New York. */
  function stampOf(value) {
    const date = ui.fmt.toDate(value);
    if (!date) return "—";
    const sameDay = ui.fmt.etDate(date) === ui.fmt.etDate(new Date());
    return (sameDay ? "Today" : ui.fmt.etDate(date)) + " " + ui.fmt.etClock(date).slice(0, 5);
  }

  /**
   * Money at a precision that does not round real spend away: agent runs cost
   * fractions of a cent, and "$0.00" next to a run that really did charge
   * something would be a lie.
   */
  function money(value) {
    const n = Number(value);
    if (value === undefined || value === null || value === "" || isNaN(n)) return "—";
    if (n !== 0 && Math.abs(n) < 1) return ui.fmt.usd(n, { decimals: 4 });
    return ui.fmt.usd(n, { decimals: 2 });
  }

  /** Milliseconds → "31m 08s", "—" when unknown. */
  function durationOf(ms) {
    if (ms === undefined || ms === null || isNaN(Number(ms))) return "—";
    const total = Math.max(0, Math.round(Number(ms) / 1000));
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return minutes + "m " + String(seconds).padStart(2, "0") + "s";
  }

  /**
   * A duration difference, which can be negative — `durationOf` clamps at zero
   * because a run cannot last less than no time, but a B−A delta certainly can.
   */
  function signedDuration(ms) {
    if (ms === undefined || ms === null || isNaN(Number(ms))) return "—";
    const value = Number(ms);
    if (value === 0) return "0m 00s";
    return (value < 0 ? "−" : "+") + durationOf(Math.abs(value));
  }

  /** Seconds → "18m" / "2h" / "4d", the reference's inbox age format. */
  function ageOf(seconds) {
    if (seconds === undefined || seconds === null || isNaN(Number(seconds))) return "—";
    return ui.fmt.ago(Date.now() - Number(seconds) * 1000);
  }

  /** Find a loaded agent definition by id. */
  function definitionOf(agentId) {
    if (!agentId) return null;
    for (let i = 0; i < state.definitions.length; i += 1) {
      if (state.definitions[i].id === agentId) return state.definitions[i];
    }
    return null;
  }

  /** Human label for an agent id, falling back to the id itself. */
  function agentLabel(agentId) {
    const definition = definitionOf(agentId);
    return definition ? definition.name : (agentId || "—");
  }

  /** The stage currently selected in the builder, or the first one. */
  function selectedStage() {
    if (!state.pipeline || !state.pipeline.stages || !state.pipeline.stages.length) return null;
    const stages = state.pipeline.stages;
    for (let i = 0; i < stages.length; i += 1) {
      if (stages[i].id === state.stageId) return stages[i];
    }
    return stages[0];
  }

  /** The stage record of the selected run that matches the selected stage. */
  function selectedStageRun() {
    const stage = selectedStage();
    if (!state.run || !stage || !state.run.stages) return null;
    for (let i = 0; i < state.run.stages.length; i += 1) {
      if (state.run.stages[i].stage_id === stage.id) return state.run.stages[i];
    }
    return null;
  }

  /** A `.v5-badge` span with the reference's state modifier class. */
  function stateBadge(text, modifier) {
    const node = el("span.v5-badge", { text: text });
    if (modifier) node.classList.add("v5-state-" + modifier);
    return node;
  }

  /** Turn a status string into the reference's modifier form. */
  function modifierOf(value) {
    return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-");
  }

  // ---------------------------------------------------------------- shell

  /** Build the page-head and the v5 shell; returns the workspace element. */
  function buildShell() {
    const activeBadge = el("span.badge.green", { id: "acAgentCounts", text: "counting…" });
    const newRun = ui.button("+ New run", {
      primary: true,
      onClick: function () { startRunFromHeader(); },
    });
    const head = el("div.page-head",
      el("div", el("small", { text: "AUTONOMOUS RESEARCH" }), el("h1", { text: "Agent Center" })),
      el("div.toolbar", activeBadge, newRun));

    const navButtons = el("div");
    NAV_VIEWS.forEach(function (view) {
      const button = el("button", {
        type: "button",
        "aria-selected": view.key === state.view ? "true" : "false",
        dataset: { v5View: view.key },
        text: view.label,
        on: { click: function () { go(view.key); } },
      });
      navButtons.appendChild(button);
    });
    const nav = el("nav.v5-nav", { "aria-label": "Agent Center workspaces" },
      el("div.v5-nav-group", el("span"), navButtons));

    const context = el("header.v5-context",
      el("div",
        el("small", { id: "acTrail", text: "Agent Center / Overview" }),
        el("h2", { id: "acTitle", text: "Overview" }),
        el("p", { id: "acBlurb", text: VIEWS[0].blurb })),
      el("div.v5-context-actions",
        el("span.v5-badge", { id: "acContextBadge", text: "Research only" }),
        ui.button("⌕ Command", {
          dataset: { v5Command: "" },
          onClick: function () {
            if (typeof global.openPalette === "function") global.openPalette();
            else ctx.toast("Press ⌘K to open the command palette", "cyan");
          },
        })));

    const live = el("div.v5-live", { id: "acLive", role: "status", "aria-live": "polite",
      text: "Research Control Room ready." });
    workspace = el("main.v5-workspace", { id: "v5Workspace", tabindex: "-1" });

    const shell = el("section.v5-shell", { id: "controlRoomV5" }, nav, context, live, workspace);
    ui.render(ctx.root, [head, shell]);
    return workspace;
  }

  /** Paint the nav selection, the breadcrumb and the attention counts. */
  function paintChrome() {
    const view = VIEWS.filter(function (v) { return v.key === state.view; })[0] || VIEWS[0];
    ctx.root.querySelectorAll(".v5-nav button").forEach(function (button) {
      button.setAttribute("aria-selected",
        button.dataset.v5View === state.view ? "true" : "false");
    });
    const trail = ctx.root.querySelector("#acTrail");
    const title = ctx.root.querySelector("#acTitle");
    const blurb = ctx.root.querySelector("#acBlurb");
    if (trail) trail.textContent = "Agent Center / " + view.title;
    if (title) title.textContent = view.title;
    if (blurb) blurb.textContent = view.blurb;

    const badge = ctx.root.querySelector("#acContextBadge");
    if (badge) {
      const pending = pendingApprovals();
      badge.className = "v5-badge";
      if (state.view === "approval" && pending) {
        badge.classList.add("v5-state-awaiting-approval");
        badge.textContent = "Human decision required";
      } else {
        badge.textContent = "Research only";
      }
    }
    paintCounts();
  }

  /** Number of approvals waiting on a person, from the loaded payloads. */
  function pendingApprovals() {
    if (state.approvals && typeof state.approvals.pending === "number") {
      return state.approvals.pending;
    }
    if (state.inbox && typeof state.inbox.pending_approvals === "number") {
      return state.inbox.pending_approvals;
    }
    if (state.config && state.config.runs) return state.config.runs.pending_approvals || 0;
    return 0;
  }

  /** Number of items in the attention queue, from the loaded payloads. */
  function attentionCount() {
    if (state.inbox && typeof state.inbox.count === "number") return state.inbox.count;
    return pendingApprovals();
  }

  /** Write the header badge and the two nav attention counts. */
  function paintCounts() {
    const counts = state.agentCounts;
    const header = ctx.root.querySelector("#acAgentCounts");
    if (header) {
      header.textContent = counts
        ? counts.active + " active · " + counts.idle + " idle"
        : "counting…";
      header.className = "badge" + (counts && counts.active ? " green" : "");
    }
    setNavCount("inbox", attentionCount());
    setNavCount("approval", pendingApprovals());
  }

  /** Show (or remove) the `.v5-count` bubble on one nav button. */
  function setNavCount(viewKey, value) {
    const button = ctx.root.querySelector('.v5-nav button[data-v5-view="' + viewKey + '"]');
    if (!button) return;
    const existing = button.querySelector(".v5-count");
    if (!value) {
      if (existing) existing.remove();
      return;
    }
    const bubble = existing || el("span.v5-count");
    bubble.textContent = String(value);
    bubble.setAttribute("aria-label", value + " needing attention");
    if (!existing) button.appendChild(bubble);
  }

  /** Announce something in the shell's live region. */
  function announce(message) {
    const node = ctx.root.querySelector("#acLive");
    if (node) node.textContent = message;
  }

  /** Navigate to a view through the router, so the hash stays truthful. */
  function go(viewKey, params) {
    ctx.router.go("agents", { sub: viewKey, params: params || {} });
  }

  // ----------------------------------------------------------- data loading

  /** Load what every view needs: counts, models and the agent roster. */
  async function loadShared() {
    const [config, models, definitions] = await Promise.all([
      req("GET", "/config"),
      req("GET", "/models"),
      req("GET", "/definitions"),
    ]);
    state.config = config;
    state.models = models;
    state.definitions = definitions.agents || [];
    state.agentCounts = definitions.counts || (config ? config.agents : null);
  }

  /** Load the data the current view renders, then draw it. */
  async function loadView() {
    const view = state.view;
    try {
      if (view === "inbox") await loadOverview();
      else if (view === "pipeline") await loadBuilder();
      else if (view === "tl-pipeline") await loadSchedules();
      else if (view === "findings") await loadFindings();
      else if (view === "evidence") await loadEvidence();
      else if (view === "tl-knowledge") await loadKnowledge();
      else if (view === "approval") await loadApprovals();
      else if (view === "agents") await loadStudio();
      else if (view === "audit") await loadAudit();
      else if (view === "configuration") await loadConfiguration();
    } catch (err) {
      if (aborted(err)) return;
      state.error = err;
      console.error("agent center: loading " + view + " failed", err);
    }
    render();
  }

  /** Repaint the workspace for the current view. */
  function render() {
    if (!workspace) return;
    paintChrome();
    if (state.error) {
      ui.render(workspace, ui.errorPanel("Agent Center", state.error));
      return;
    }
    if (state.loading) {
      ui.render(workspace, ui.emptyState("Agent Center", "Loading…", { badge: "working" }));
      return;
    }
    const view = state.view;
    if (view === "inbox") ui.render(workspace, renderOverview());
    else if (view === "pipeline") ui.render(workspace, renderBuilder());
    else if (view === "tl-pipeline") ui.render(workspace, renderSchedules());
    else if (view === "findings") ui.render(workspace, renderFindings());
    else if (view === "evidence") ui.render(workspace, renderEvidence());
    else if (view === "tl-knowledge") ui.render(workspace, renderKnowledge());
    else if (view === "approval") ui.render(workspace, renderApproval());
    else if (view === "agents") ui.render(workspace, renderStudio());
    else if (view === "audit") ui.render(workspace, renderAudit());
    else if (view === "configuration") ui.render(workspace, renderConfiguration());
  }

  // -------------------------------------------------------------- overview

  async function loadOverview() {
    const [inbox, runs, findings, knowledge, schedules, blocks] = await Promise.all([
      req("GET", "/inbox", { limit: 50 }),
      req("GET", "/runs", { limit: 8 }),
      req("GET", "/findings", { limit: 6 }),
      req("GET", "/knowledge", { limit: 6 }),
      req("GET", "/autopipeline"),
      req("GET", "/audit", { limit: 200, q: "block" }),
    ]);
    state.inbox = inbox;
    state.overviewRuns = runs.runs || [];
    state.runCounts = runs.counts || null;
    state.overviewFindings = findings.findings || [];
    state.overviewKnowledge = knowledge.recent || [];
    state.scheduleStatus = schedules;
    state.guardrailBlocks = (blocks.records || []).filter(function (record) {
      const date = ui.fmt.toDate(record.ts);
      return date && ui.fmt.etDate(date) === ui.fmt.etDate(new Date());
    }).length;
    if (!state.inboxItemId && (inbox.items || []).length) {
      state.inboxItemId = inbox.items[0].id;
    }
  }

  /** The attention queue after the toolbar's queue/type/search filters. */
  function filteredInbox() {
    const items = (state.inbox && state.inbox.items) || [];
    const needle = state.inboxQuery.trim().toLowerCase();
    return items.filter(function (item) {
      // "Assigned to me" is the approval queue: those are the items that
      // literally cannot proceed without this person's decision.
      if (state.inboxQueue === "Assigned to me" && item.type !== "Approval") return false;
      if (state.inboxType !== "All types" && item.type !== state.inboxType) return false;
      if (!needle) return true;
      return [item.title, item.reason, item.symbol, item.source, item.id]
        .filter(Boolean)
        .some(function (field) { return String(field).toLowerCase().indexOf(needle) >= 0; });
    });
  }

  /** The default value of each inbox filter, so "cleared" has one definition. */
  const INBOX_FILTER_DEFAULTS = {
    inboxQuery: "",
    inboxQueue: "Needs attention",
    inboxType: "All types",
  };

  /** Whether any inbox filter is narrowing the queue right now. */
  function inboxFiltered() {
    return Object.keys(INBOX_FILTER_DEFAULTS).some(function (key) {
      return state[key] !== INBOX_FILTER_DEFAULTS[key];
    });
  }

  /** Reset queue/type/search to their defaults and repaint. */
  function clearInboxFilters() {
    Object.keys(INBOX_FILTER_DEFAULTS).forEach(function (key) {
      state[key] = INBOX_FILTER_DEFAULTS[key];
    });
    // The previously selected row may have been filtered out; let the
    // restored queue pick its own head again.
    state.inboxItemId = null;
    render();
  }

  function renderOverview() {
    const counts = state.runCounts || {};
    const spend = (state.config && state.config.spend) || {};
    const findingCounts = (state.inbox && state.inbox.unacknowledged_findings) || 0;
    const strip = el("div.v5x-ov-strip", {
      style: "display:flex;gap:26px;margin-bottom:12px;flex-wrap:wrap",
    },
      metric("ACTIVE", counts.running || 0, "ok"),
      metric("SCHEDULED", (state.scheduleStatus && state.scheduleStatus.enabled) || 0, "hi"),
      metric("PAUSED", counts.awaiting_approval || 0, "bad"),
      metric("FINDINGS PENDING", findingCounts, "warn"),
      metric("GUARDRAIL BLOCKS TODAY", state.guardrailBlocks || 0, "hi"),
      el("div",
        el("label", { text: "AGENT SPEND" }),
        el("span.metricbig.hi", { text: money(spend.used) },
          el("span.dim", { style: "font-size:11px", text: " /" + money(spend.budget) }))));

    const items = filteredInbox();
    const toolbar = el("div.v5-toolbar",
      el("label",
        el("span", { text: "Search" }),
        el("input.input", {
          type: "search", value: state.inboxQuery, placeholder: "Symbol, hypothesis, run…",
          dataset: { inboxQuery: "" },
          on: { input: function (e) { state.inboxQuery = e.target.value; render(); } },
        })),
      segmented("Inbox queue", ["Needs attention", "Assigned to me", "All research"],
        state.inboxQueue, function (value) { state.inboxQueue = value; render(); }),
      el("label",
        el("span", { text: "Type" }),
        select(["All types"].concat((state.inbox && state.inbox.types)
          || ["Approval", "Validation", "Experiment", "Data", "Agent"]), state.inboxType,
        function (value) { state.inboxType = value; render(); }, { inboxType: "" })),
      el("span.v5-toolbar-count", { text: items.length + " items" }));

    const list = el("div.v5-inbox-list");
    if (!items.length && inboxFiltered()) {
      // A filter hid everything. Say so, and offer the way back — otherwise
      // this is indistinguishable from a genuinely empty queue.
      list.appendChild(el("div.v5-empty",
        el("b", { text: "No research matches these filters." }),
        el("span", { text: "Clear filters to restore the queue." }),
        el("button.btn", {
          type: "button",
          dataset: { clearInbox: "" },
          on: { click: clearInboxFilters },
        }, "Clear filters")));
    } else if (!items.length) {
      const unavailable = Object.keys((state.inbox && state.inbox.sources_available) || {})
        .filter(function (kind) { return !state.inbox.sources_available[kind]; });
      list.appendChild(el("div.list-row",
        el("span", {
          text: "Nothing is waiting. Approvals, failed gates, failed experiment runs, "
            + "data-quality incidents and agent findings appear here."
            + (unavailable.length
              ? " " + unavailable.join(", ") + " could not be read in this process."
              : ""),
        })));
    }
    items.forEach(function (item) {
      const selected = item.id === state.inboxItemId;
      list.appendChild(el("button.v5-inbox-item", {
        type: "button", "aria-pressed": selected ? "true" : "false",
        dataset: { inboxItem: item.id },
        on: { click: function () { state.inboxItemId = item.id; render(); } },
      },
        el("span.v5-item-top",
          el("b", { text: item.symbol || "—" }),
          stateBadge(item.urgency || "Normal", modifierOf(item.urgency || "normal"))),
        el("strong", { text: item.title }),
        el("span", { text: item.reason || "" }),
        el("small", {
          text: [item.type, item.source || "—", ageOf(item.age_seconds)].join(" · "),
        })));
    });

    const detail = renderInboxDetail(items);
    const layout = el("div.v5-master-detail.v5-inbox-layout",
      ui.panel({
        className: "v5-list-panel",
        title: "Attention queue",
        badge: "ordered by urgency",
        body: list,
      }),
      detail);

    return [strip, toolbar, layout, renderOverviewPanels()];
  }

  /** One `<div><label>…<span class="metricbig">` of the overview strip. */
  function metric(label, value, tone) {
    return el("div",
      el("label", { text: label }),
      el("span.metricbig" + (tone ? "." + tone : ""), { text: String(value) }));
  }

  function renderInboxDetail(items) {
    const item = items.filter(function (i) { return i.id === state.inboxItemId; })[0] || items[0];
    if (!item && inboxFiltered()) {
      // Hidden by a filter, not absent. Name the filters and undo them.
      const total = (state.inbox && state.inbox.items ? state.inbox.items.length : 0);
      return ui.panel({
        className: "v5-detail-panel",
        title: "No match",
        badge: total + " in queue",
        body: el("div.v5-empty",
          el("b", { text: "No research matches these filters." }),
          el("span", { text: "Clear filters to restore the queue." }),
          el("button.btn", {
            type: "button",
            dataset: { clearInbox: "" },
            on: { click: clearInboxFilters },
          }, "Clear filters")),
      });
    }
    if (!item) {
      return ui.panel({
        className: "v5-detail-panel",
        title: "No item selected",
        body: el("div.v5-detail-pad", el("p.v5-lead", {
          text: "The queue is empty: no approval is pending and every finding has been reviewed.",
        })),
      });
    }
    const confidence = item.confidence_pct;
    const body = el("div.v5-detail-pad",
      el("p.v5-eyebrow", { text: item.type + " · " + (item.symbol || "—") }),
      el("h3", { text: item.title }),
      el("p.v5-lead", { text: item.reason || "" }),
      el("dl.v5-kv",
        kvPair("Origin", item.source || "—"),
        kvPair("Stage", item.stage_id || "—"),
        kvPair("Agent", agentLabel(item.agent_id)),
        kvPair("Age", ageOf(item.age_seconds))));
    if (typeof confidence === "number") {
      body.appendChild(el("div.v5-confidence",
        el("i", { style: "width:" + Math.max(0, Math.min(100, confidence)) + "%" })));
      body.appendChild(el("p.v5-lead", { text: "Evidence confidence " + ui.fmt.pct(confidence) }));
    }

    const actions = el("div.v5-detail-actions");
    if (item.type === "Approval") {
      actions.appendChild(ui.button("Review candidate", {
        primary: true,
        onClick: function () {
          state.approvalId = (item.context && item.context.approvalId) || item.id;
          go("approval", { id: state.approvalId });
        },
      }));
      actions.appendChild(ui.button("Open run", {
        onClick: function () {
          state.runId = item.source;
          go("pipeline", { run: item.source });
        },
      }));
    } else if (item.kind === "finding") {
      actions.appendChild(ui.button("Open finding", {
        primary: true,
        onClick: function () {
          state.findingId = item.id;
          go("findings", { id: item.id });
        },
      }));
      actions.appendChild(ui.button("Mark reviewed", {
        onClick: function () { acknowledgeFinding(item.id); },
      }));
    } else {
      // Validation / Experiment / Data / failed-run items each name the page
      // that owns them, so no queue row is a dead end.
      const context = item.context || {};
      actions.appendChild(ui.button(item.action || "Open", {
        primary: true,
        onClick: function () {
          if (item.route === "pipeline" && context.runId) {
            state.runId = context.runId;
            go("pipeline", { run: context.runId });
            return;
          }
          if (item.route === "lab") {
            const sub = context.backtestId ? "backtests" : "experiments";
            const params = context.backtestId
              ? { id: context.backtestId } : { id: context.experimentId || "" };
            ctx.router.go("lab", { sub: sub, params: params });
            return;
          }
          if (item.route === "data") {
            ctx.router.go("data", { sub: "quality", params: { dataset: context.dataset || "" } });
            return;
          }
          ctx.toast(item.type + " item " + item.id + " has no page of its own", "cyan");
        },
      }));
      if (item.source && item.route !== "pipeline") {
        actions.appendChild(el("span.dim.mini", {
          style: "align-self:center", text: "source " + item.source,
        }));
      }
    }
    body.appendChild(actions);

    return ui.panel({
      className: "v5-detail-panel",
      title: item.id,
      badge: item.status || "Needs attention",
      body: body,
    });
  }

  function renderOverviewPanels() {
    const now = el("div.panel.tight", el("div.ptitle", { text: "AGENTS NOW" }));
    if (!state.definitions.length) {
      now.appendChild(ui.kv("No agents", "the registry is empty", { tone: "dim" }));
    }
    state.definitions.forEach(function (definition) {
      const stats = definition.stats || {};
      let text = "IDLE";
      let tone = "dim";
      if (definition.active) {
        text = "● RUNNING";
        tone = "ok";
      } else if (!definition.enabled) {
        text = "DISABLED";
        tone = "bad";
      } else if (stats.last_run_at) {
        text = "IDLE · last " + ui.fmt.ago(stats.last_run_at) + " ago";
      } else {
        text = "IDLE · never run";
      }
      if (stats.cost_usd) text += " · " + money(stats.cost_usd);
      now.appendChild(ui.kv(definition.name, text, { tone: tone }));
    });

    const runs = el("div.panel.tight", el("div.ptitle", { text: "RECENT RUNS" }));
    if (!state.overviewRuns.length) {
      runs.appendChild(ui.kv("No runs yet", "start one from the Pipeline tab", { tone: "dim" }));
    }
    state.overviewRuns.slice(0, 5).forEach(function (run) {
      const progress = run.progress || {};
      runs.appendChild(ui.kv(
        run.id + " · " + run.name,
        run.status.replace(/_/g, " ") + " · " + progress.completed + "/" + progress.total
          + " · " + money(run.cost_usd),
        { tone: run.status === "failed" || run.status === "killed" ? "bad"
          : (run.status === "succeeded" ? "ok" : "warn") }));
    });

    const findings = el("div.panel.tight", el("div.ptitle", { text: "LATEST FINDINGS" }));
    if (!state.overviewFindings.length) {
      findings.appendChild(el("div.stat", el("span.dim", { text: "No findings recorded yet." })));
    }
    state.overviewFindings.slice(0, 4).forEach(function (finding) {
      findings.appendChild(el("div.stat",
        el("span.pill.blu.mini", { text: String(finding.status).toUpperCase() }),
        " " + finding.id + " · " + finding.title));
    });
    findings.appendChild(el("div.dim.mini", {
      style: "border-top:1px solid var(--line);margin-top:6px;padding-top:6px",
    }, ui.button("Review all → FINDINGS tab", {
      small: true, onClick: function () { go("findings"); },
    })));

    const knowledge = el("div.panel.tight",
      el("div.ptitle", { text: "KNOWLEDGE ADDED RECENTLY" }));
    if (!state.overviewKnowledge.length) {
      knowledge.appendChild(el("div.stat", el("span.dim", { text: "The knowledge base is empty." })));
    }
    state.overviewKnowledge.slice(0, 4).forEach(function (entry) {
      knowledge.appendChild(el("div.stat", { text: entry.id + " · " + entry.title }));
    });

    return el("div.grid.g2.v5x-ov-panels",
      el("div", { style: "display:flex;flex-direction:column;gap:12px" }, now, runs),
      el("div", { style: "display:flex;flex-direction:column;gap:12px" }, findings, knowledge));
  }

  async function acknowledgeFinding(findingId) {
    try {
      await req("POST", "/findings/" + encodeURIComponent(findingId) + "/acknowledge");
      ctx.toast("Finding " + findingId + " marked reviewed", "green");
      await loadView();
    } catch (err) {
      fail("Acknowledging the finding", err);
    }
  }

  // ------------------------------------------------------- pipeline builder

  async function loadBuilder() {
    const list = await req("GET", "/pipelines");
    state.pipelines = list.pipelines || [];
    if (!state.pipelineId && state.pipelines.length) state.pipelineId = state.pipelines[0].id;
    if (state.pipelineId) {
      state.pipeline = await req("GET", "/pipelines/" + encodeURIComponent(state.pipelineId));
      const stages = state.pipeline.stages || [];
      if (!stages.some(function (s) { return s.id === state.stageId; })) {
        state.stageId = stages.length ? stages[0].id : null;
      }
    } else {
      state.pipeline = null;
      state.stageId = null;
    }
    const runs = await req("GET", "/runs", { limit: 25 });
    state.runs = runs.runs || [];
    state.runCounts = runs.counts || null;
    if (state.runId && !state.runs.some(function (r) { return r.id === state.runId; })) {
      state.runId = null;
    }
    if (!state.runId && state.runs.length) state.runId = state.runs[0].id;
    state.run = state.runId
      ? await req("GET", "/runs/" + encodeURIComponent(state.runId))
      : null;
    state.estimate = state.pipelineId
      ? await ctx.api.optional(req("GET",
        "/pipelines/" + encodeURIComponent(state.pipelineId) + "/estimate"))
      : null;
    await loadRunComparison();
  }

  /**
   * Load the two runs the comparison pickers point at.
   *
   * `GET /runs/compare` does the joining server-side so the two runs are read
   * from one consistent snapshot of the ledger rather than two round trips that
   * could straddle a status change.
   */
  async function loadRunComparison() {
    if (!state.compareA || !state.compareB || state.compareA === state.compareB) {
      state.comparison = null;
      return;
    }
    state.comparison = await ctx.api.optional(
      req("GET", "/runs/compare", { a: state.compareA, b: state.compareB })
    );
  }

  function renderBuilder() {
    if (!state.pipelines.length) {
      return ui.panel({
        title: "Pipeline Builder",
        badge: "no pipelines",
        badgeTone: "yellow",
        body: [
          el("div.list-row", el("span", {
            text: "No pipeline is saved yet. Create one to compose a research workflow.",
          })),
          el("div", { style: "padding:11px" }, ui.button("+ New pipeline", {
            primary: true, onClick: newPipeline,
          })),
        ],
      });
    }
    return el("section.v5-merged", { dataset: { v5Part: "pipeline" } },
      el("div",
        renderBuilderToolbar(),
        el("div.pipeline-live", { id: "v5xLive", text: state.liveLine }),
        renderFlowPanel(),
        el("div.pipeline-workspace", renderStageConfig(), renderInspector()),
        renderRunTable(),
        renderRunComparison(),
        renderRunDetail(),
        el("div.v5-preview-box", { id: "v5PipelinePreview" }, renderPreviewBox())));
  }

  function renderBuilderToolbar() {
    const pipeline = state.pipeline;
    const options = state.pipelines.map(function (item) {
      return { value: item.id, label: item.name + " · v" + item.version };
    });
    const picker = el("select.select", {
      dataset: { pipelineSelect: "" },
      on: {
        change: function (e) {
          state.pipelineId = e.target.value;
          state.stageId = null;
          reloadView();
        },
      },
    });
    options.forEach(function (option) {
      picker.appendChild(el("option", {
        value: option.value, text: option.label,
        selected: option.value === state.pipelineId,
      }));
    });

    const status = pipeline
      ? [pipeline.status, "v" + pipeline.version,
        pipeline.dirty ? "unsaved changes" : "clean",
        pipeline.locked ? "locked" : null].filter(Boolean).join(" · ")
      : "—";

    return el("div.v5-toolbar.v5-pipeline-toolbar",
      el("label", el("span", { text: "Saved pipeline" }), picker),
      ui.button("+ New pipeline", { dataset: { pipelineNew: "" }, onClick: newPipeline }),
      ui.button("Duplicate pipeline", {
        dataset: { pipelineDuplicate: "" }, onClick: duplicatePipeline,
      }),
      ui.button("Save new version", {
        dataset: { pipelineVersion: "" }, onClick: saveVersion,
      }),
      ui.button("Delete pipeline", { onClick: deletePipeline }),
      el("span.v5-toolbar-count", { text: status }));
  }

  /**
   * What a stage card should print for its model.
   *
   * Once a run exists, the model that actually executed is the truth. Before
   * that it is the stage override, else the assigned agent's model, else the
   * provider default the backend resolved (`effective_model`) - the same chain
   * the runner walks. "no model" is reserved for the case where nothing could
   * be resolved at all, which is a real, reportable configuration problem.
   */
  function stageModel(stage, record) {
    if (stage.kind === "human_approval") {
      return { label: "Required", detail: "A person decides; no model is called." };
    }
    if (record && record.model) {
      return { label: record.model, detail: "The model this run executed on." };
    }
    if (stage.model) {
      return { label: stage.model, detail: "Set on this stage." };
    }
    if (stage.effective_model) {
      const source = stage.effective_source === "agent"
        ? "Inherited from " + agentLabel(stage.agent_id)
        : "Resolved at run time from the configured provider"
          + (stage.effective_provider ? " (" + stage.effective_provider + ")" : "");
      return { label: stage.effective_model, detail: source + "." };
    }
    return {
      label: "no model",
      detail: "No model is set on the stage or its agent, and no provider is "
        + "configured to resolve a default from.",
    };
  }

  function renderFlowPanel() {
    const pipeline = state.pipeline;
    const stages = (pipeline && pipeline.stages) || [];
    const runStages = {};
    if (state.run) {
      (state.run.stages || []).forEach(function (record) {
        runStages[record.stage_id] = record;
      });
    }

    const flow = el("div.pipeline-flow", {
      style: "grid-template-columns:repeat(" + Math.max(stages.length, 1)
        + ",minmax(145px,1fr))",
    });
    stages.forEach(function (stage, index) {
      const record = runStages[stage.id];
      let label = stage.locked ? "Locked" : (stage.include_in_run ? "Ready" : "Excluded");
      let modifier = stage.locked ? "locked" : (stage.include_in_run ? "ready" : "skipped");
      if (record) {
        label = String(record.status).replace(/_/g, " ");
        label = label.charAt(0).toUpperCase() + label.slice(1);
        modifier = PROGRESS_CLASS[record.status] || modifierOf(record.status);
      }
      const cost = record && record.cost_usd
        ? money(record.cost_usd) : "—";
      const model = stageModel(stage, record);
      const button = el("button.pipeline-stage", {
        type: "button",
        "aria-pressed": stage.id === state.stageId ? "true" : "false",
        dataset: { stageSelect: stage.id },
        on: { click: function () { state.stageId = stage.id; render(); } },
      },
        el("span.pipeline-stage-head",
          el("span.pipeline-stage-name", { text: (index + 1) + ". " + stage.name }),
          el("span.pipeline-stage-state", { text: label, class: "state-" + modifier })),
        el("span.pipeline-stage-agent", { text: agentLabel(stage.agent_id) }),
        el("span.pipeline-stage-foot",
          el("span", { text: model.label, title: model.detail }),
          el("span", { text: cost })));
      button.classList.add("state-" + modifier);
      flow.appendChild(button);
    });

    const actions = el("div.v5x-strip-actions",
      ui.button("+ Add stage", {
        dataset: { v4Catalog: "" },
        onClick: function () { state.catalogOpen = !state.catalogOpen; render(); },
      }),
      ui.button("Preview graph", {
        dataset: { pipelinePreview: "" }, onClick: previewGraph,
      }),
      ui.button("▶ Run full pipeline", {
        primary: true, dataset: { pipelineRun: "" }, onClick: runPipeline,
      }));
    actions.querySelector("[data-v4-catalog]")
      .setAttribute("aria-expanded", state.catalogOpen ? "true" : "false");

    const estimate = state.estimate;
    if (estimate) {
      // REFERENCE: "6 stages · est $9.40 · ~12 min". The figures come from
      // GET /pipelines/{id}/estimate, which prices each included stage from its
      // own run history where it has one and a documented default budget where
      // it does not — and says which, so an estimate is never mistaken for a bill.
      const minutes = Math.max(1, Math.round((estimate.seconds || 0) / 60));
      const cost = estimate.priced ? "est " + money(estimate.total_usd) : "cost not priced";
      const note = el("div.note", {
        style: "margin-top:8px",
        text: estimate.stage_count + " stages · " + cost + " · ~" + minutes + " min"
          + (estimate.blocks_on_approval
            ? " · stops at " + estimate.blocks_on_approval + " approval boundary" : "")
          + " · all runs → trial ledger",
      });
      note.title = estimate.basis
        + (estimate.priced
          ? ""
          : " · at least one stage has no published price, so it is left out of the total");
      actions.appendChild(note);
    }

    const approval = stages.filter(function (s) { return s.kind === "human_approval"; }).length;
    // Built by hand rather than through ui.panel(): the reference's flow title
    // is itself the .panel-title element (it carries a second class), and
    // ui.panel() would nest one inside another.
    const wrapper = el("section.panel.pipeline-flow-panel",
      el("div.panel-title.pipeline-flow-title",
        el("span", { text: "Pipeline flow · select a stage to configure it" }),
        el("span.badge", {
          text: stages.length + " stages · "
            + (approval ? "approval required" : "no approval boundary"),
        })),
      el("div.pipeline-flow-scroll", flow),
      actions);
    if (state.catalogOpen) wrapper.appendChild(renderCatalog());
    return wrapper;
  }

  /** The "+ Add stage" catalog: one entry per agent the registry holds. */
  function renderCatalog() {
    const catalog = el("div.v5x-catalog");
    if (!state.definitions.length) {
      catalog.appendChild(el("div.list-row",
        el("span", { text: "No agents are registered, so no stage can be added." })));
      return catalog;
    }
    state.definitions.forEach(function (definition) {
      catalog.appendChild(el("button", {
        type: "button", dataset: { catalogAdd: definition.id },
        on: { click: function () { addStage(definition); } },
      },
        el("b", { text: definition.name }),
        el("small", { text: definition.role + " · " + (definition.model || "no model") }),
        el("strong", { text: "+ Add" })));
    });
    return catalog;
  }

  function renderStageConfig() {
    const stage = selectedStage();
    if (!stage) {
      return ui.panel({
        className: "pipeline-config",
        title: "Stage configuration",
        body: el("div.pipeline-config-body",
          el("div.pipeline-config-note", {
            text: "This pipeline has no stages. Use “+ Add stage” to add one.",
          })),
      });
    }

    const stages = state.pipeline.stages;
    const index = stages.indexOf(stage);
    const ops = el("span.v5x-stage-ops",
      opButton("←", "Move " + stage.name + " left", index === 0, function () {
        moveStage(index, index - 1);
      }, { stageMove: "left", stageId: stage.id }),
      opButton("→", "Move " + stage.name + " right", index === stages.length - 1, function () {
        moveStage(index, index + 1);
      }, { stageMove: "right", stageId: stage.id }),
      opButton("Duplicate", "Duplicate " + stage.name, false, function () {
        duplicateStage(stage.id);
      }, { stageDuplicate: stage.id }),
      opButton("Remove", "Remove " + stage.name, !!stage.locked, function () {
        removeStage(stage.id);
      }, { stageRemove: stage.id }));

    const form = {};
    const body = el("div.pipeline-config-body",
      el("div.pipeline-config-note", {
        text: "Configure this agent stage, its resources, and the contract used for the "
          + "downstream handoff.",
      }),
      el("label.pipeline-switch",
        el("span", { text: "Include stage in pipeline" }),
        (form.include = el("input", {
          type: "checkbox", dataset: { stageToggle: stage.id },
          checked: !!stage.include_in_run,
          disabled: !!stage.locked,
        }))),
      el("div.pipeline-field-grid",
        field("Assigned agent", (form.agent = agentSelect(stage))),
        field("Stage name", (form.name = el("input", {
          value: stage.name, dataset: { stageField: "name" },
        })))),
      el("div.pipeline-field-grid",
        field("Model", (form.model = modelSelect(stage))),
        field("Scope", (form.scope = el("input", {
          value: scopeText(stage.scope),
          placeholder: "universe id, symbols, or key=value pairs",
          dataset: { stageField: "scope" },
        })))),
      field("Objective / instructions", (form.objective = el("textarea", {
        text: stage.objective || "", dataset: { stageField: "objective" },
      }))),
      field("Tools and data sources", (form.tools = el("textarea", {
        text: (stage.tools || []).join(", "), dataset: { stageField: "tools" },
      }))),
      el("div.pipeline-field-grid",
        field("Budget (USD)", (form.budget = el("input", {
          value: stage.budget_usd === null || stage.budget_usd === undefined
            ? "" : String(stage.budget_usd),
          placeholder: "unbounded",
          dataset: { stageField: "budget" },
        }))),
        field("Timeout (seconds)", (form.timeout = el("input", {
          value: stage.timeout_s === null || stage.timeout_s === undefined
            ? "" : String(stage.timeout_s),
          placeholder: "none",
          dataset: { stageField: "timeout" },
        })))),
      el("details.pipeline-advanced",
        el("summary", { text: "Advanced contracts and controls" }),
        field("Retries", (form.retries = el("input", {
          value: String(stage.retries || 0), dataset: { stageField: "retries" },
        }))),
        field("Input contract", (form.inputs = el("textarea", {
          text: (stage.inputs && stage.inputs.text) || "", dataset: { stageField: "input" },
        }))),
        field("Required input keys", (form.inputKeys = el("input", {
          value: ((stage.inputs && stage.inputs.required) || []).join(", "),
          placeholder: "comma-separated keys the upstream stage must produce",
          dataset: { stageField: "input_required" },
        }))),
        field("Output contract", (form.outputs = el("textarea", {
          text: (stage.outputs && stage.outputs.text) || "", dataset: { stageField: "output" },
        }))),
        field("Required output keys", (form.outputKeys = el("input", {
          value: ((stage.outputs && stage.outputs.required) || []).join(", "),
          dataset: { stageField: "output_required" },
        }))),
        field("Handoff condition", (form.handoff = el("input", {
          value: stage.handoff || "", dataset: { stageField: "handoff" },
        }))),
        field("Pass condition", (form.pass = el("input", {
          value: stage.pass_condition || "", dataset: { stageField: "pass" },
        }))),
        field("Fail condition", (form.fail = el("input", {
          value: stage.fail_condition || "", dataset: { stageField: "fail" },
        }))),
        field("Stop condition", (form.stop = el("input", {
          value: stage.stop_condition || "", dataset: { stageField: "stop" },
        })))),
      el("div.pipeline-config-actions",
        ui.button("Reset stage", {
          dataset: { v4ResetStage: "" },
          onClick: function () { render(); },
        }),
        ui.button("Save stage", {
          dataset: { stageSave: "" },
          onClick: function () { saveStage(stage, form); },
        }),
        singleStageButton()));

    return el("section.panel.pipeline-config", { id: "v5xConfig" },
      el("div.panel-title.v5x-config-title",
        el("span", { text: stage.name + " configuration" }),
        el("span.badge", { text: agentLabel(stage.agent_id) }),
        ops),
      body);
  }

  /**
   * "Run this stage" — a real run limited to the selected stage.
   *
   * `POST /pipelines/{id}/stages/{stage}/run` switches every other stage off
   * *for that run only*, so the saved pipeline is untouched. Because the
   * upstream stages did not execute, the stage is given the inputs typed into
   * the dialog in place of upstream output.
   */
  function singleStageButton() {
    const stage = currentStage();
    const button = ui.button("Run this stage", {
      dataset: { stageRun: "" },
      onClick: function () { runSingleStage(); },
    });
    if (!stage) {
      button.disabled = true;
      button.title = "select a stage first";
      return button;
    }
    if (stage.kind === "human_approval") {
      button.disabled = true;
      button.title = "the human-approval boundary has nothing to execute: it waits for a person";
      return button;
    }
    button.title = "Runs " + stage.name + " on its own; every other stage is switched off for "
      + "this run only, and the saved pipeline is not modified.";
    return button;
  }

  /** The stage the builder currently has selected, or null. */
  function currentStage() {
    const pipeline = state.pipeline;
    if (!pipeline) return null;
    return (pipeline.stages || []).filter(function (item) {
      return item.id === state.stageId;
    })[0] || null;
  }

  /** Ask for the stage's stand-in inputs, then run just that stage. */
  async function runSingleStage() {
    const stage = currentStage();
    if (!stage) return;
    const answer = await askForm("Run " + stage.name + " on its own", [{
      name: "inputs",
      label: "INPUTS (JSON) — stands in for the upstream stages, which will not run",
      value: "{}",
      multiline: true,
    }]);
    if (answer === null) return;
    let inputs;
    try {
      inputs = answer.inputs.trim() ? JSON.parse(answer.inputs) : {};
    } catch (err) {
      ctx.toast("Inputs must be a JSON object — " + err.message, "red");
      return;
    }
    if (!inputs || typeof inputs !== "object" || Array.isArray(inputs)) {
      ctx.toast("Inputs must be a JSON object", "red");
      return;
    }
    try {
      const run = await req("POST",
        "/pipelines/" + encodeURIComponent(state.pipelineId)
        + "/stages/" + encodeURIComponent(stage.id) + "/run",
        { inputs: inputs, wait: false });
      state.runId = run.id;
      ctx.toast("Started " + run.id + " for the " + stage.name + " stage alone", "green");
      await reloadView();
    } catch (err) {
      fail("Running the " + stage.name + " stage", err);
    }
  }

  function opButton(label, aria, disabled, onClick, dataset) {
    const button = el("button", {
      type: "button", "aria-label": aria, text: label,
      on: { click: onClick },
    });
    Object.keys(dataset || {}).forEach(function (key) {
      button.dataset[key] = String(dataset[key]);
    });
    if (disabled) button.disabled = true;
    return button;
  }

  /** One `.pipeline-field` label wrapping its control, as the reference does. */
  function field(label, control) {
    return el("label.pipeline-field", el("span", { text: label }), control);
  }

  function agentSelect(stage) {
    const node = el("select", { dataset: { stageField: "agent" } });
    node.appendChild(el("option", { value: "", text: "— unassigned —",
      selected: !stage.agent_id }));
    state.definitions.forEach(function (definition) {
      node.appendChild(el("option", {
        value: definition.id,
        text: definition.name,
        selected: definition.id === stage.agent_id,
      }));
    });
    if (stage.locked) node.disabled = true;
    return node;
  }

  /**
   * The model picker: only models a provider actually verified as available.
   * A model the stage refers to that is no longer offered is still listed, so
   * the configuration is shown as it really is rather than silently changed.
   */
  function modelSelect(stage) {
    const node = el("select", { dataset: { stageField: "model" } });
    const models = (state.models && state.models.models) || [];
    node.appendChild(el("option", { value: "", text: "No model", selected: !stage.model }));
    const seen = {};
    models.forEach(function (model) {
      seen[model.id] = true;
      const price = model.priced
        ? " · $" + model.input_cost_per_mtok + "/$" + model.output_cost_per_mtok + " per Mtok"
        : " · price unknown";
      node.appendChild(el("option", {
        value: model.provider + "/" + model.id,
        text: model.provider + " · " + model.label + price,
        selected: model.id === stage.model,
      }));
    });
    if (stage.model && !seen[stage.model]) {
      node.appendChild(el("option", {
        value: (stage.provider || "") + "/" + stage.model,
        text: stage.model + " · not offered by any configured provider",
        selected: true,
      }));
    }
    if (stage.locked) node.disabled = true;
    return node;
  }

  /** Render a stage scope object as the single-line form the builder edits. */
  function scopeText(scope) {
    if (!scope) return "";
    const keys = Object.keys(scope);
    if (!keys.length) return "";
    return keys.map(function (key) {
      const value = scope[key];
      return key + "=" + (Array.isArray(value) ? value.join("|") : String(value));
    }).join(", ");
  }

  /** Parse the scope line back into an object. A bare word is the universe. */
  function parseScope(text) {
    const trimmed = String(text || "").trim();
    if (!trimmed) return {};
    const out = {};
    trimmed.split(",").forEach(function (part) {
      const piece = part.trim();
      if (!piece) return;
      const eq = piece.indexOf("=");
      if (eq < 0) {
        out.universe = piece;
        return;
      }
      const key = piece.slice(0, eq).trim();
      const value = piece.slice(eq + 1).trim();
      out[key] = value.indexOf("|") >= 0 ? value.split("|") : value;
    });
    return out;
  }

  function renderInspector() {
    const stage = selectedStage();
    const record = selectedStageRun();
    const head = el("div.pipeline-inspector-head",
      el("div",
        el("b", { text: (stage ? stage.name : "Stage") + " · Run inspector" }),
        el("span", {
          text: record
            ? state.run.id + " · " + String(record.status).replace(/_/g, " ")
            : "Configuration preview",
        })),
      el("span", { text: stage ? agentLabel(stage.agent_id) : "—" }));

    const tabs = el("div.pipeline-inspector-tabs", { role: "tablist" });
    INSPECTOR_TABS.forEach(function (name) {
      tabs.appendChild(el("button.pipeline-inspector-tab", {
        type: "button", role: "tab",
        "aria-selected": name === state.inspectorTab ? "true" : "false",
        dataset: { v4Tab: name },
        text: name.charAt(0).toUpperCase() + name.slice(1),
        on: { click: function () { state.inspectorTab = name; render(); } },
      }));
    });

    return el("section.panel.pipeline-inspector", head, tabs,
      el("div.pipeline-inspector-content", { id: "acInspectorContent" },
        renderInspectorContent(stage, record)));
  }

  function renderInspectorContent(stage, record) {
    if (!stage) return el("div.pipeline-detail-list", el("div.pipeline-detail-item", {
      text: "No stage is selected.",
    }));
    const tab = state.inspectorTab;
    if (tab === "preview") {
      return [
        el("div.pipeline-preview-summary", {
          text: stage.objective || "This stage has no objective yet.",
        }),
        el("div.pipeline-preview-grid",
          previewCard("INPUT CONTRACT", (stage.inputs && stage.inputs.text) || "not declared"),
          previewCard("OUTPUT CONTRACT", (stage.outputs && stage.outputs.text) || "not declared"),
          previewCard("HANDOFF", stage.handoff || "next stage in order")),
      ];
    }
    if (tab === "inputs") return detailList(payloadLines(record ? record.inputs : null,
      (stage.inputs && stage.inputs.required) || [],
      "This stage has not run yet; the list shows its declared required inputs."));
    if (tab === "outputs") return detailList(payloadLines(record ? record.outputs : null,
      (stage.outputs && stage.outputs.required) || [],
      "This stage has not run yet; the list shows its declared required outputs."));
    if (tab === "logs") return renderLogs(record);
    return renderArtifacts(record);
  }

  function previewCard(label, value) {
    return el("div.pipeline-preview-card",
      el("small", { text: label }), el("b", { text: value }));
  }

  /** Real payload entries when the stage ran, the declared contract when not. */
  function payloadLines(payload, required, note) {
    if (payload && Object.keys(payload).length) {
      return Object.keys(payload).map(function (key) {
        const value = payload[key];
        const rendered = typeof value === "object" && value !== null
          ? JSON.stringify(value)
          : String(value);
        return key + ": " + (rendered.length > 220 ? rendered.slice(0, 220) + "…" : rendered);
      });
    }
    if (required && required.length) return required.map(function (key) { return key + " (required)"; });
    return [note];
  }

  function detailList(lines) {
    const list = el("div.pipeline-detail-list");
    lines.forEach(function (line) {
      list.appendChild(el("div.pipeline-detail-item", { text: line }));
    });
    return list;
  }

  function renderLogs(record) {
    const box = el("div", { id: "acLogBox" });
    const lines = (record && record.logs) || [];
    if (!lines.length) {
      box.appendChild(el("div.pipeline-log",
        el("time", { text: "--:--" }),
        el("span", {
          text: state.run
            ? "No log lines recorded for this stage yet."
            : "No run selected. Start a run to stream its log here.",
        })));
      return box;
    }
    lines.forEach(function (line) { box.appendChild(logRow(line)); });
    return box;
  }

  function logRow(line) {
    const row = el("div.pipeline-log",
      el("time", { text: clockOf(line.ts) }),
      el("span", { text: line.message }));
    if (line.level === "error") row.classList.add("red");
    else if (line.level === "warning") row.classList.add("warn");
    return row;
  }

  function renderArtifacts(record) {
    const artifacts = (record && record.artifacts) || [];
    if (!artifacts.length) {
      return detailList([state.run
        ? "This stage produced no artifacts."
        : "No run selected."]);
    }
    const list = el("div.pipeline-detail-list");
    artifacts.forEach(function (artifact) {
      list.appendChild(el("div.pipeline-detail-item", {
        on: { click: function () { openArtifact(artifact.id); } },
        style: "cursor:pointer",
        text: artifact.name + " · " + artifact.kind + " · "
          + (artifact.size_bytes ? ui.fmt.int(artifact.size_bytes) + " bytes" : "inline"),
      }));
    });
    return list;
  }

  async function openArtifact(artifactId) {
    try {
      const artifact = await req("GET", "/artifacts/" + encodeURIComponent(artifactId));
      download(artifact.name + ".json", JSON.stringify(artifact.data, null, 2));
      ctx.toast("Artifact " + artifact.name + " downloaded", "green");
    } catch (err) {
      fail("Reading the artifact", err);
    }
  }

  /** Hand the browser a file built entirely in the page. */
  function download(filename, text, mime) {
    const blob = new Blob([text], { type: mime || "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function renderRunTable() {
    const table = el("table.pipeline-run-table",
      el("thead", el("tr",
        el("th"), el("th", { text: "Run" }), el("th", { text: "Pipeline" }),
        el("th", { text: "Objective" }), el("th", { text: "Status" }),
        el("th", { text: "Started" }), el("th", { text: "Duration" }),
        el("th", { text: "Stage progress" }), el("th", { text: "Cost" }),
        el("th", { text: "Action" }))));
    const body = el("tbody");
    if (!state.runs.length) {
      body.appendChild(el("tr", el("td", {
        colspan: "10", class: "muted",
        text: "No runs recorded yet. “▶ Run full pipeline” starts one.",
      })));
    }
    state.runs.forEach(function (run) {
      const badge = RUN_BADGE[run.status] || ["", run.status];
      const progress = el("span.pipeline-progress");
      ((run.progress && run.progress.stages) || []).forEach(function (status) {
        const cls = PROGRESS_CLASS[status] || modifierOf(status);
        progress.appendChild(el("i", { class: cls, title: status.replace(/_/g, " ") }));
      });
      const row = el("tr.pipeline-run-row", { dataset: { v4Run: run.id } },
        el("td", { style: "width:26px" }, el("input", {
          type: "checkbox", "aria-label": "Select " + run.id,
          checked: run.id === state.runId,
          on: { change: function () { selectRun(run.id); } },
        })),
        el("td", { text: run.id }),
        el("td", { text: run.pipeline_id + " v" + run.pipeline_version }),
        el("td", { text: run.objective || run.name || "—" }),
        el("td", el("span.pipeline-status-badge", { class: badge[0], text: badge[1] })),
        el("td", { text: stampOf(run.started_at || run.created_at) }),
        el("td", { text: durationOf(run.duration_ms) }),
        el("td", progress),
        el("td.pipeline-run-counts", { text: money(run.cost_usd) }),
        el("td", ui.button("View", {
          dataset: { v4Run: run.id }, onClick: function () { selectRun(run.id); },
        })));
      if (run.id === state.runId) row.classList.add("selected");
      body.appendChild(row);
    });
    table.appendChild(body);

    const counts = state.runCounts || {};
    return el("section.panel.pipeline-runs",
      el("div.panel-title",
        el("span", { text: "Previous pipeline runs" }),
        el("span.badge", { text: (counts.total || 0) + " runs · one store" }),
        el("span.v5x-cmp-hint", {
          text: (counts.active || 0) + " active · " + (counts.pending_approvals || 0)
            + " awaiting approval",
        })),
      el("div.pipeline-table-scroll", table));
  }

  /**
   * "Run comparison" — two run pickers and the stage-by-stage diff between
   * them, served by GET /api/agents/runs/compare so both sides come from one
   * consistent read of the ledger.
   */
  function renderRunComparison() {
    const runs = state.runs || [];
    const options = [{ value: "", label: "— pick a run —" }].concat(runs.map(function (run) {
      return {
        value: run.id,
        label: run.id + " · " + run.pipeline_id + " · " + String(run.status).replace(/_/g, " "),
      };
    }));
    const picker = function (side, current) {
      const node = el("select.select", {
        dataset: { runPick: side },
        on: {
          change: function (event) {
            if (side === "a") state.compareA = event.target.value || null;
            else state.compareB = event.target.value || null;
            loadRunComparison().then(render).catch(function (err) {
              fail("Comparing the runs", err);
            });
          },
        },
      });
      options.forEach(function (option) {
        node.appendChild(el("option", {
          value: option.value, text: option.label, selected: option.value === (current || ""),
        }));
      });
      return node;
    };

    // REFERENCE's `data-cmp-swap` sits between the two sides and flips them.
    // Every delta in the payload is signed (B−A), so the swap re-reads the
    // comparison rather than transposing the stale one client-side.
    const swap = ui.button("⇄ Swap", {
      dataset: { cmpSwap: "" },
      disabled: !(state.compareA && state.compareB && state.compareA !== state.compareB),
      onClick: function () {
        const a = state.compareA;
        state.compareA = state.compareB;
        state.compareB = a;
        state.comparison = null;
        render();
        loadRunComparison().then(render).catch(function (err) {
          fail("Comparing the runs", err);
        });
      },
    });
    swap.title = "Exchange RUN A and RUN B";

    const toolbar = el("div.v5-toolbar",
      el("label", el("span", { text: "RUN A" }), picker("a", state.compareA)),
      swap,
      el("label", el("span", { text: "RUN B" }), picker("b", state.compareB)),
      state.compareA || state.compareB
        ? ui.button("Clear", {
          onClick: function () {
            state.compareA = null; state.compareB = null; state.comparison = null; render();
          },
        })
        : null);

    const body = el("div", toolbar);
    const comparison = state.comparison;
    if (!runs.length) {
      body.appendChild(el("div.list-row",
        el("span", { text: "No run has been recorded for this workspace yet." })));
    } else if (!state.compareA || !state.compareB) {
      body.appendChild(el("div.list-row",
        el("span", { text: "Pick two runs to see the stage-by-stage difference in status, "
          + "model, tokens, cost and duration." })));
    } else if (state.compareA === state.compareB) {
      body.appendChild(el("div.list-row",
        el("span.yellow", { text: "Pick two different runs." })));
    } else if (!comparison) {
      body.appendChild(el("div.list-row",
        el("span", { text: "Loading the comparison…" })));
    } else {
      const totals = comparison.totals || {};
      body.appendChild(el("div.pipeline-preview-grid", {
        style: "grid-template-columns:repeat(4,minmax(0,1fr));margin:0 11px 10px",
      },
        previewCard("SAME PIPELINE", comparison.same_pipeline ? "yes" : "no"),
        previewCard("COST Δ (B−A)", money(totals.cost_delta)),
        previewCard("TOKENS Δ", ui.fmt.int(totals.token_delta)),
        previewCard("DURATION Δ", signedDuration(totals.duration_delta_ms))));

      const table = el("table.data-table",
        el("thead", el("tr",
          el("th", { text: "Stage" }),
          el("th", { text: "A status" }), el("th", { text: "B status" }),
          el("th", { text: "A model" }), el("th", { text: "B model" }),
          el("th", { text: "A cost" }), el("th", { text: "B cost" }),
          el("th", { text: "Δ" }))));
      const tbody = el("tbody");
      (comparison.stages || []).forEach(function (row) {
        const a = row.a || {};
        const b = row.b || {};
        tbody.appendChild(el("tr",
          el("td", { text: row.name + (row.only_in ? " (only in " + row.only_in.toUpperCase() + ")" : "") }),
          el("td", { text: a.status ? String(a.status).replace(/_/g, " ") : "—" }),
          el("td", {
            class: row.status_changed ? "yellow" : "",
            text: b.status ? String(b.status).replace(/_/g, " ") : "—",
          }),
          el("td", { text: a.model || "—" }),
          el("td", { class: row.model_changed ? "yellow" : "", text: b.model || "—" }),
          el("td", { text: a.cost_usd === undefined ? "—" : money(a.cost_usd) }),
          el("td", { text: b.cost_usd === undefined ? "—" : money(b.cost_usd) }),
          el("td", {
            class: row.cost_delta > 0 ? "red" : (row.cost_delta < 0 ? "green" : ""),
            text: row.cost_delta === null || row.cost_delta === undefined
              ? "—" : money(row.cost_delta),
          })));
      });
      table.appendChild(tbody);
      body.appendChild(el("div.v5-table-scroll", table));
    }

    return el("section.panel.pipeline-runs",
      el("div.panel-title",
        el("span", { text: "Run comparison" }),
        el("span.badge", { text: runs.length + " runs available" })),
      body);
  }

  function renderRunDetail() {
    const run = state.run;
    if (!run) {
      return el("section.panel.pipeline-runs",
        el("div.panel-title", el("span", { text: "Run detail" })),
        el("div", { style: "padding:11px" },
          el("span.dim", { text: "Select a run to see its stages, cost and conclusion." })));
    }
    const progressCards = el("div.pipeline-preview-grid", {
      style: "grid-template-columns:repeat(" + Math.max((run.stages || []).length, 1)
        + ",minmax(0,1fr))",
    });
    (run.stages || []).forEach(function (record) {
      const label = String(record.status).replace(/_/g, " ");
      progressCards.appendChild(el("div.pipeline-preview-card",
        el("small", { text: record.name }),
        el("b", {
          class: "state-" + (PROGRESS_CLASS[record.status] || modifierOf(record.status)),
          text: label.charAt(0).toUpperCase() + label.slice(1),
        })));
    });

    const usage = run.usage || {};
    const stats = el("div.pipeline-preview-grid", {
      style: "grid-template-columns:repeat(6,minmax(0,1fr))",
    },
      previewCard("DURATION", durationOf(run.duration_ms)),
      previewCard("COST", money(run.cost_usd)),
      previewCard("INPUT TOKENS", ui.fmt.int(usage.input_tokens)),
      previewCard("OUTPUT TOKENS", ui.fmt.int(usage.output_tokens)),
      previewCard("STAGES", (run.progress && run.progress.completed) + "/"
        + (run.progress && run.progress.total)),
      previewCard("TRIGGER", run.trigger || "manual"));

    const actions = el("div", { style: "display:flex;gap:6px;flex-wrap:wrap;margin-top:11px" },
      ui.button("Open in builder", {
        primary: true,
        onClick: function () {
          state.pipelineId = run.pipeline_id;
          state.stageId = null;
          reloadView();
        },
      }));
    if (run.status === "awaiting_approval") {
      actions.appendChild(ui.button("Open approval review", {
        onClick: function () { go("approval", { run: run.id }); },
      }));
    }
    if (["queued", "running"].indexOf(run.status) >= 0) {
      actions.appendChild(ui.button("Pause", {
        onClick: function () { pauseRun(run.id); },
      }));
    }
    if (run.status === "paused") {
      actions.appendChild(ui.button("Resume", {
        primary: true,
        onClick: function () { resumeRun(run.id); },
      }));
    }
    if (["queued", "running", "awaiting_approval", "paused"].indexOf(run.status) >= 0) {
      actions.appendChild(ui.button("Cancel run", {
        onClick: function () { cancelRun(run.id); },
      }));
    }
    if (["failed", "killed"].indexOf(run.status) >= 0) {
      actions.appendChild(ui.button("Retry failed stage", {
        onClick: function () { retryRun(run.id); },
      }));
    }
    if (["succeeded", "failed", "killed"].indexOf(run.status) >= 0) {
      actions.appendChild(ui.button("Replay from stage…", {
        onClick: function () { replayRun(run); },
      }));
    }
    actions.appendChild(ui.button("View in audit log →", {
      onClick: function () { state.auditRunId = run.id; go("audit", { run: run.id }); },
    }));
    actions.appendChild(ui.button("Export run", {
      onClick: function () { download(run.id + ".json", JSON.stringify(run, null, 2)); },
    }));

    const outputs = run.outputs && Object.keys(run.outputs).length
      ? JSON.stringify(run.outputs, null, 2)
      : null;

    return el("section.panel.pipeline-runs",
      el("div.panel-title",
        el("span", { text: "Run detail" }),
        el("span.badge", { text: run.id })),
      el("div", { style: "padding:11px" },
        el("div.hi", { style: "font:600 13px/1.3 Inter,system-ui,sans-serif",
          text: run.objective || run.name }),
        el("div.dim.mini", { style: "margin:2px 0 11px",
          text: [run.trigger, run.pipeline_id + " v" + run.pipeline_version,
            stampOf(run.started_at || run.created_at), durationOf(run.duration_ms),
            String(run.status).replace(/_/g, " ")].join(" · ") }),
        stats,
        el("div.pipeline-preview-summary", { style: "margin-top:10px" },
          el("label.dim.mini", { style: "display:block;margin-bottom:6px", text: "STAGES" }),
          progressCards),
        run.error
          ? el("div.pipeline-preview-summary",
            el("label.dim.mini", { style: "display:block;margin-bottom:6px", text: "ERROR" }),
            el("div.red", { text: run.error }))
          : null,
        outputs
          ? el("div.pipeline-preview-summary",
            el("label.dim.mini", { style: "display:block;margin-bottom:6px", text: "RUN OUTPUTS" }),
            el("pre", { style: "white-space:pre-wrap;font-size:11px;margin:0", text: outputs }))
          : null,
        actions));
  }

  function renderPreviewBox() {
    if (!state.preview) {
      return [
        el("b", { text: "Graph preview" }),
        el("span", {
          text: "Select Preview graph to validate contracts, handoffs and the approval boundary.",
        }),
      ];
    }
    const preview = state.preview;
    const nodes = [el("b", {
      text: "Graph preview · " + (preview.valid ? "valid" : preview.problems.length + " problems"),
    })];
    if (preview.valid) {
      nodes.push(el("span", {
        text: (preview.stages || []).map(function (stage, i) {
          return (i + 1) + ". " + stage.name;
        }).join("  →  "),
      }));
    } else {
      preview.problems.forEach(function (problem) {
        nodes.push(el("span.red", { text: "• " + problem }));
      });
    }
    return nodes;
  }

  // --- builder actions ------------------------------------------------

  /** Reload the current view's data without tearing the page down. */
  async function reloadView() {
    await loadView();
  }

  async function newPipeline() {
    const name = await askText("New pipeline", "Pipeline name", "New research pipeline");
    if (!name) return;
    try {
      const created = await req("POST", "/pipelines", { name: name });
      state.pipelineId = created.id;
      state.stageId = null;
      ctx.toast("Pipeline " + created.name + " created", "green");
      await reloadView();
    } catch (err) {
      fail("Creating the pipeline", err);
    }
  }

  async function duplicatePipeline() {
    if (!state.pipelineId) return;
    try {
      const copy = await req("POST",
        "/pipelines/" + encodeURIComponent(state.pipelineId) + "/duplicate", {});
      state.pipelineId = copy.id;
      state.stageId = null;
      ctx.toast("Duplicated as " + copy.name, "green");
      await reloadView();
    } catch (err) {
      fail("Duplicating the pipeline", err);
    }
  }

  async function saveVersion() {
    if (!state.pipelineId) return;
    const note = await askText("Save new version",
      "Note for this version (optional)", "", "what changed");
    // askText resolves null both for "cancelled" and for "left empty"; a
    // version with no note is legitimate, so an empty note is not a refusal.
    try {
      const saved = await req("POST",
        "/pipelines/" + encodeURIComponent(state.pipelineId) + "/versions", { note: note });
      ctx.toast("Version saved; working document is now v" + saved.version, "green");
      await reloadView();
    } catch (err) {
      fail("Saving the version", err);
    }
  }

  async function deletePipeline() {
    if (!state.pipelineId) return;
    const ok = await askConfirm("Delete pipeline",
      "Delete pipeline " + state.pipelineId + "? Its frozen versions go with it; runs already "
      + "recorded keep their own snapshot.", "Delete pipeline");
    if (!ok) return;
    try {
      await req("DELETE", "/pipelines/" + encodeURIComponent(state.pipelineId));
      ctx.toast("Pipeline deleted", "green");
      state.pipelineId = null;
      state.pipeline = null;
      state.stageId = null;
      await reloadView();
    } catch (err) {
      fail("Deleting the pipeline", err);
    }
  }

  async function addStage(definition) {
    if (!state.pipelineId) return;
    try {
      const updated = await req("POST",
        "/pipelines/" + encodeURIComponent(state.pipelineId) + "/stages", {
          id: definition.id,
          name: definition.name,
          kind: definition.kind === "human" ? "human_approval" : "agent",
          agent_id: definition.id,
          provider: definition.provider,
          model: definition.model,
          objective: definition.role,
          tools: definition.tools || [],
          include_in_run: true,
        });
      state.catalogOpen = false;
      state.stageId = updated.stages[updated.stages.length - 1].id;
      ctx.toast("Stage " + definition.name + " added", "green");
      await reloadView();
    } catch (err) {
      fail("Adding the stage", err);
    }
  }

  async function duplicateStage(stageId) {
    try {
      await req("POST", "/pipelines/" + encodeURIComponent(state.pipelineId)
        + "/stages/" + encodeURIComponent(stageId) + "/duplicate", {});
      ctx.toast("Stage duplicated", "green");
      await reloadView();
    } catch (err) {
      fail("Duplicating the stage", err);
    }
  }

  async function removeStage(stageId) {
    const ok = await askConfirm("Remove stage",
      "Remove stage " + stageId + " from this pipeline?", "Remove stage");
    if (!ok) return;
    try {
      await req("DELETE", "/pipelines/" + encodeURIComponent(state.pipelineId)
        + "/stages/" + encodeURIComponent(stageId));
      state.stageId = null;
      ctx.toast("Stage removed", "green");
      await reloadView();
    } catch (err) {
      fail("Removing the stage", err);
    }
  }

  async function moveStage(from, to) {
    const stages = state.pipeline.stages.map(function (stage) { return stage.id; });
    if (to < 0 || to >= stages.length) return;
    const moved = stages.splice(from, 1)[0];
    stages.splice(to, 0, moved);
    try {
      await req("POST", "/pipelines/" + encodeURIComponent(state.pipelineId) + "/stages/order",
        { order: stages });
      await reloadView();
    } catch (err) {
      fail("Reordering the stages", err);
    }
  }

  async function saveStage(stage, form) {
    const modelValue = form.model.value;
    const slash = modelValue.indexOf("/");
    const body = {
      name: form.name.value.trim() || stage.name,
      agent_id: form.agent.value || null,
      provider: slash > 0 ? modelValue.slice(0, slash) : null,
      model: slash > 0 ? modelValue.slice(slash + 1) : null,
      objective: form.objective.value,
      scope: parseScope(form.scope.value),
      include_in_run: form.include.checked,
      tools: form.tools.value.split(",").map(function (t) { return t.trim(); }).filter(Boolean),
      handoff: form.handoff.value,
      pass_condition: form.pass.value,
      fail_condition: form.fail.value,
      stop_condition: form.stop.value,
      retries: Number(form.retries.value) || 0,
      inputs: {
        text: form.inputs.value,
        required: form.inputKeys.value.split(",").map(function (k) { return k.trim(); })
          .filter(Boolean),
      },
      outputs: {
        text: form.outputs.value,
        required: form.outputKeys.value.split(",").map(function (k) { return k.trim(); })
          .filter(Boolean),
      },
    };
    const budget = form.budget.value.trim();
    const timeout = form.timeout.value.trim();
    if (budget) body.budget_usd = Number(budget);
    if (timeout) body.timeout_s = Number(timeout);
    // A locked stage (the approval boundary) only accepts the contract fields;
    // sending the rest would be refused, so they are dropped here.
    if (stage.locked) {
      ["name", "agent_id", "provider", "model", "include_in_run", "tools", "retries",
        "pass_condition", "fail_condition", "stop_condition", "budget_usd", "timeout_s"]
        .forEach(function (key) { delete body[key]; });
    }
    try {
      await req("PUT", "/pipelines/" + encodeURIComponent(state.pipelineId)
        + "/stages/" + encodeURIComponent(stage.id), body);
      ctx.toast("Stage " + stage.name + " saved", "green");
      await reloadView();
    } catch (err) {
      fail("Saving the stage", err);
    }
  }

  async function previewGraph() {
    try {
      state.preview = await req("GET",
        "/pipelines/" + encodeURIComponent(state.pipelineId) + "/validate");
      render();
      ctx.toast(state.preview.valid
        ? "Pipeline validates"
        : state.preview.problems.length + " problems block this run",
        state.preview.valid ? "green" : "yellow");
    } catch (err) {
      fail("Validating the pipeline", err);
    }
  }

  async function runPipeline() {
    if (!state.pipelineId) return;
    try {
      const run = await req("POST",
        "/pipelines/" + encodeURIComponent(state.pipelineId) + "/run", { trigger: "manual" });
      state.runId = run.id;
      state.liveLine = "Run " + run.id + " queued.";
      ctx.toast("Run " + run.id + " started", "green");
      announce("Run " + run.id + " started.");
      await reloadView();
    } catch (err) {
      fail("Starting the run", err);
    }
  }

  /** "+ New run" in the page head: run whichever pipeline is selected. */
  async function startRunFromHeader() {
    try {
      if (!state.pipelines.length) {
        const list = await req("GET", "/pipelines");
        state.pipelines = list.pipelines || [];
        if (!state.pipelineId && state.pipelines.length) state.pipelineId = state.pipelines[0].id;
      }
      if (!state.pipelineId) {
        ctx.toast("No pipeline is saved yet — create one in the Pipeline tab", "yellow");
        go("pipeline");
        return;
      }
      const run = await req("POST",
        "/pipelines/" + encodeURIComponent(state.pipelineId) + "/run", { trigger: "manual" });
      state.runId = run.id;
      ctx.toast("Run " + run.id + " started on " + state.pipelineId, "green");
      go("pipeline", { run: run.id });
    } catch (err) {
      fail("Starting the run", err);
    }
  }

  async function selectRun(runId) {
    state.runId = runId;
    try {
      state.run = await req("GET", "/runs/" + encodeURIComponent(runId));
      render();
    } catch (err) {
      fail("Loading the run", err);
    }
  }

  async function cancelRun(runId) {
    const ok = await askConfirm("Cancel run",
      "Cancel run " + runId + "? Stages that have not executed never will, and the run is "
      + "recorded as killed.", "Cancel run");
    if (!ok) return;
    try {
      await req("POST", "/runs/" + encodeURIComponent(runId) + "/cancel",
        { reason: "cancelled from the Agent Center" });
      ctx.toast("Run " + runId + " cancelled", "yellow");
      await reloadView();
    } catch (err) {
      fail("Cancelling the run", err);
    }
  }

  /** Stop a run at its next stage boundary, keeping everything it has done. */
  async function pauseRun(runId) {
    try {
      await req("POST", "/runs/" + encodeURIComponent(runId) + "/pause",
        { reason: "paused from the Agent Center" });
      ctx.toast("Run " + runId + " will stop at the next stage boundary", "yellow");
      await reloadView();
    } catch (err) {
      fail("Pausing the run", err);
    }
  }

  /** Continue a paused run from the stage it stopped in front of. */
  async function resumeRun(runId) {
    try {
      await req("POST", "/runs/" + encodeURIComponent(runId) + "/resume", {});
      ctx.toast("Run " + runId + " resumed", "green");
      await reloadView();
    } catch (err) {
      fail("Resuming the run", err);
    }
  }

  /** Re-execute from the first stage that did not succeed. */
  async function retryRun(runId) {
    const ok = await askConfirm("Retry run",
      "Re-execute " + runId + " from its first unsuccessful stage? Stages that already "
      + "succeeded keep their output; the retried stage and everything after it are reset.",
      "Retry");
    if (!ok) return;
    try {
      await req("POST", "/runs/" + encodeURIComponent(runId) + "/retry", {});
      ctx.toast("Run " + runId + " restarted from its failed stage", "green");
      await reloadView();
    } catch (err) {
      fail("Retrying the run", err);
    }
  }

  /**
   * "Replay from stage…" — start a NEW run of the same pipeline version that
   * begins at a chosen stage, so a conclusion can be re-derived without
   * repeating the expensive stages before it. The original run is left intact.
   */
  async function replayRun(run) {
    const stages = run.stages || [];
    if (!stages.length) {
      ctx.toast("Run " + run.id + " recorded no stages to replay from", "yellow");
      return;
    }
    const chosen = await askChoice("Replay " + run.id + " from a stage",
      "The replay is a new run of the same pipeline version. Stages before the chosen one "
      + "are skipped and their recorded output is handed to it, so nothing before the "
      + "chosen stage is charged again.",
      stages.map(function (record) {
        return {
          value: record.stage_id || record.id,
          label: record.name + " · " + String(record.status).replace(/_/g, " "),
        };
      }));
    if (chosen === null) return;
    try {
      const replayed = await req("POST", "/runs/" + encodeURIComponent(run.id) + "/replay",
        { stage_id: chosen, wait: false });
      state.runId = replayed.id;
      ctx.toast("Replay started as " + replayed.id, "green");
      await reloadView();
    } catch (err) {
      fail("Replaying the run", err);
    }
  }

  // ----------------------------------------------------------- autopipeline

  async function loadSchedules() {
    // Stages are needed here, not just names: the per-run model overrides put
    // one select per stage of whichever pipeline the schedule targets.
    const [status, pipelines, universes] = await Promise.all([
      req("GET", "/autopipeline"),
      req("GET", "/pipelines", { include_stages: true }),
      // Real universes for the brief's UNIVERSE pick; the form degrades to
      // "pipeline default" only when the endpoint is unavailable.
      ctx.api.optional(ctx.api.get("/api/universes", null, { signal: ctx.signal })),
    ]);
    state.scheduleStatus = status;
    state.schedules = status.schedules || [];
    state.pipelines = pipelines.pipelines || [];
    state.universes = (universes && universes.universes) || [];
    if (!state.pipelineId && state.pipelines.length) state.pipelineId = state.pipelines[0].id;
    if (state.schedules.length
      && !state.schedules.some(function (s) { return s.id === state.scheduleId; })) {
      state.scheduleId = state.schedules[0].id;
    }
  }

  function renderSchedules() {
    const status = state.scheduleStatus || {};
    const header = el("div", { style: "margin-bottom:10px" },
      el("span.hi", {
        style: "font-family:system-ui;font-size:15px;font-weight:600", text: "AutoPipeline",
      }),
      el("span.dim", { text: " · scheduled autonomous runs · " },
        el("span", {
          class: status.running ? "ok" : "warn",
          text: status.running
            ? (status.enabled || 0) + " enabled · timers running"
            : "timers stopped",
        })));

    const form = {};
    const pipelinePicker = el("select");
    state.pipelines.forEach(function (pipeline) {
      pipelinePicker.appendChild(el("option", {
        value: pipeline.id, text: pipeline.name,
        selected: pipeline.id === state.pipelineId,
      }));
    });
    form.pipeline = pipelinePicker;

    const create = el("div.panel",
      el("div.ptitle", { style: "display:flex" },
        el("span", { text: "NEW SCHEDULE" }),
        el("span.dim", { style: "margin-left:auto",
          text: status.next_run_at ? "next fire " + stampOf(status.next_run_at) : "nothing scheduled" })),
      el("label", { text: "PIPELINE" }), pipelinePicker,
      el("label", { text: "NAME" }),
      (form.name = el("input", { placeholder: "Nightly quality scan" })),
      el("label", { text: "CADENCE (CRON OR @daily/@hourly)" }),
      (form.cron = el("input", { value: "@daily" })),
      el("div.grid.g2", { style: "gap:8px;margin:8px 0" },
        el("div", el("label", { text: "TIMEZONE" }),
          (form.timezone = el("input", { value: "America/New_York" }))),
        el("div", el("label", { text: "MAX RUNS / DAY" }),
          (form.maxRuns = el("input", { value: "1" })))),
      el("div.grid.g2", { style: "gap:8px;margin-bottom:8px" },
        el("div", el("label", { text: "MAX SPEND / DAY (USD)" }),
          (form.maxSpend = el("input", { placeholder: "unbounded" }))),
        el("div", el("label", { text: "MAX CONCURRENT RUNS" }),
          (form.maxConcurrent = el("input", { value: "1" })))),
      el("label", { text: "OBJECTIVE (OPTIONAL)" }),
      (form.objective = el("input", { placeholder: "overrides the pipeline objective" })),
      briefFields(form),
      briefToggles(form),
      advancedFields(form),
      modelOverrideFields(form, pipelinePicker),
      stageToggleChips(form, pipelinePicker),
      el("div.note", { style: "margin:10px 0",
        text: "Every scheduled run is a real pipeline run: it spends the same budget and is "
          + "recorded in the same ledger and audit log." }),
      ui.button("▷ CREATE SCHEDULE", {
        primary: true, onClick: function () { createSchedule(form); },
      }));
    // The pipeline the schedule targets decides which stages can be overridden
    // and toggled, so both stage-shaped rows are rebuilt when the picker moves.
    pipelinePicker.addEventListener("change", function () {
      if (form.overrideHost) {
        ui.render(form.overrideHost, modelOverrideRows(form, pipelinePicker.value));
      }
      if (form.stageHost) {
        ui.render(form.stageHost, stageToggleRow(form, pipelinePicker.value));
      }
    });

    const list = el("div.panel.tight",
      el("div.ptitle", { style: "display:flex" },
        el("span", { text: "SCHEDULES" }),
        el("span.chip.mini", { style: "margin-left:auto", text: (status.total || 0) + " TOTAL" })));
    if (!state.schedules.length) {
      list.appendChild(el("div.dim.mini", { style: "padding:6px 2px",
        text: "No schedules yet." }));
    }
    state.schedules.forEach(function (schedule) {
      const row = el("div.rowitem", {
        on: { click: function () { state.scheduleId = schedule.id; render(); } },
      },
        el("span.hi", { text: schedule.id }),
        " · " + (schedule.name || schedule.pipeline_id),
        el("span", {
          style: "float:right", class: "mini " + (schedule.enabled ? "ok" : "dim"),
          text: schedule.enabled ? "ENABLED" : "DISABLED",
        }),
        el("br"),
        el("span.dim.mini", {
          text: schedule.cron + " " + schedule.timezone + " · next "
            + (schedule.next_run_at ? stampOf(schedule.next_run_at) : "—")
            + " · " + schedule.run_count + " runs",
        }));
      if (schedule.id === state.scheduleId) row.classList.add("sel");
      list.appendChild(row);
    });

    return el("div",
      header,
      el("div.grid.g23", create,
        el("div", { style: "display:flex;flex-direction:column;gap:12px" },
          list, renderScheduleDetail())));
  }

  /* The per-run brief. These four fields ARE the run inputs the first stage
   * receives (POST /autopipeline stores them in `inputs`, the runner hands them
   * to the scanner stage), so they are named after what the pipeline reads
   * rather than after the control. */
  function briefFields(form) {
    form.query = el("input", { placeholder: "AI infrastructure momentum" });
    form.tickers = el("input", { placeholder: "NVDA, AMD, TSLA" });
    form.topN = el("input", { value: "5" });
    form.scanMode = el("select");
    ["quick", "deep", "full universe"].forEach(function (mode) {
      form.scanMode.appendChild(el("option", { value: mode, text: mode }));
    });
    return el("div",
      el("label", { text: "QUERY" }), form.query,
      el("label", { style: "margin-top:8px", text: "TICKERS (OPTIONAL)" }), form.tickers,
      el("div.dim.mini", { style: "margin-bottom:8px",
        text: "comma-separated · overrides ticker scan" }),
      el("div.grid.g2", { style: "gap:8px;margin-bottom:8px" },
        el("div", el("label", { text: "TOP N TICKERS" }), form.topN),
        el("div", el("label", { text: "SCAN MODE" }), form.scanMode)));
  }

  /* The two brief toggles (reference `Resolve tickers` / `Auto-advance on pass`).
   *
   * Resolve tickers is ON by default; switching it OFF records
   * `resolve_tickers=false` in the run brief, which the first stage receives
   * verbatim with the rest of the brief ("Run inputs") — the directive to take
   * the ticker list as given instead of resolving the query to names itself.
   *
   * Auto-advance: the backend policy only moves an experiment whose backtest
   * cleared every gate to VALIDATED; anything past that still needs the human
   * approvals, and the note says so rather than implying otherwise. */
  function briefToggles(form) {
    form.resolveTickers = true;
    const resolve = el("span.chip.mini.grn", { style: "cursor:pointer", text: "◉ ON" });
    resolve.addEventListener("click", function () {
      form.resolveTickers = !form.resolveTickers;
      resolve.textContent = form.resolveTickers ? "◉ ON" : "○ OFF";
      resolve.classList.toggle("grn", form.resolveTickers);
    });
    form.autoAdvance = false;
    const advance = el("span.chip.mini", { style: "cursor:pointer", text: "○ OFF" });
    advance.addEventListener("click", function () {
      form.autoAdvance = !form.autoAdvance;
      advance.textContent = form.autoAdvance ? "◉ ON" : "○ OFF";
      advance.classList.toggle("grn", form.autoAdvance);
    });
    return el("div",
      el("div", { style: "display:flex;gap:18px;margin:4px 0 8px;align-items:center;flex-wrap:wrap" },
        el("span", resolve, el("span.mid", { text: " Resolve tickers" })),
        el("span", advance, el("span.mid", { text: " Auto-advance on pass" }))),
      el("div.dim.mini", { style: "margin-bottom:10px",
        text: "Pass → the experiment the run produced is set to VALIDATED in Strategy Lab. "
          + "Only when a validation report for its backtest passed every gate; promotion "
          + "beyond validated always needs the approval stage." }));
  }

  /* ADVANCED CONFIGURATION (reference: sectors, catalyst window, universe).
   * All three land in the run brief exactly like the query does: sectors and
   * the catalyst window are directives the first stage reads out of its
   * "Run inputs"; the universe is a real universe id the stage can hand to
   * its resolve_universe tool. */
  function advancedFields(form) {
    form.sectors = el("input", { placeholder: "tech, semiconductors" });
    form.catalystWindow = el("input", { placeholder: "48", type: "number" });
    form.universe = el("select");
    form.universe.appendChild(el("option", { value: "", text: "— pipeline default —" }));
    (state.universes || []).forEach(function (universe) {
      form.universe.appendChild(el("option", {
        value: universe.id, text: universe.name || universe.id,
      }));
    });
    return el("div",
      el("div.dim.mini", { style: "letter-spacing:1px;margin-bottom:6px",
        text: "▾ ADVANCED CONFIGURATION" }),
      el("label", { text: "SECTORS (OPTIONAL)" }), form.sectors,
      el("div.grid.g2", { style: "gap:8px;margin:8px 0" },
        el("div", el("label", { text: "CATALYST WINDOW (H)" }), form.catalystWindow),
        el("div", el("label", { text: "UNIVERSE" }), form.universe)));
  }

  /* Per-run model overrides, one select per stage of the chosen pipeline. They
   * are overrides of each stage's own configuration for the runs this schedule
   * starts — the saved pipeline is never edited. */
  function modelOverrideFields(form, picker) {
    form.modelOverrides = {};
    form.overrideHost = el("div.grid.g2", { style: "gap:8px;margin-bottom:4px" });
    ui.render(form.overrideHost, modelOverrideRows(form, picker.value));
    return el("div",
      el("label", { style: "margin-top:6px", text: "PER-RUN MODEL OVERRIDES" }),
      form.overrideHost,
      el("div.dim.mini", { style: "margin-bottom:8px",
        text: "Model picks are per-run overrides of each stage's config; the pipeline is "
          + "left as saved." }));
  }

  function modelOverrideRows(form, pipelineId) {
    form.modelOverrides = {};
    const pipeline = state.pipelines.filter(function (item) {
      return item.id === pipelineId;
    })[0];
    const stages = (pipeline && pipeline.stages) || [];
    if (!stages.length) {
      return [el("span.dim.mini", {
        text: pipeline
          ? "This pipeline has no stages to override."
          : "Pick a pipeline to list its stages.",
      })];
    }
    return stages.filter(function (stage) {
      return stage.kind !== "human_approval";
    }).map(function (stage) {
      const picker = el("select");
      picker.appendChild(el("option", { value: "", text: "— stage default —" }));
      ((state.models && state.models.models) || []).forEach(function (model) {
        picker.appendChild(el("option", { value: model.id, text: model.label || model.id }));
      });
      picker.addEventListener("change", function () {
        if (picker.value) form.modelOverrides[stage.id] = picker.value;
        else delete form.modelOverrides[stage.id];
      });
      return el("div",
        el("label", { text: (stage.name || stage.id).toUpperCase() + " MODEL" }), picker);
    });
  }

  /* STAGES · CLICK TO TOGGLE (reference `#pl-stages`). Chips write
   * `stage_toggles` (stage id → included); when the schedule fires they become
   * include_in_run overrides on the run's frozen pipeline copy
   * (apply_stage_overrides), so the saved pipeline itself is never edited.
   * The locked human-approval boundary cannot be switched off per run — the
   * backend refuses it — so its chip says "required" instead of pretending. */
  function stageToggleChips(form, picker) {
    form.stageToggles = {};
    form.stageHost = el("div", { style: "display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 10px" });
    ui.render(form.stageHost, stageToggleRow(form, picker.value));
    return el("div",
      el("label", { style: "margin-top:6px", text: "STAGES · CLICK TO TOGGLE" }),
      form.stageHost,
      el("div.dim.mini", { style: "margin-bottom:8px",
        text: "Per-run inclusion for this schedule's runs — a dim chip is skipped. The saved "
          + "pipeline is left as saved; the approval boundary cannot be skipped." }));
  }

  function stageToggleRow(form, pipelineId) {
    form.stageToggles = {};
    const pipeline = state.pipelines.filter(function (item) {
      return item.id === pipelineId;
    })[0];
    const stages = (pipeline && pipeline.stages) || [];
    if (!stages.length) {
      return [el("span.dim.mini", {
        text: pipeline ? "This pipeline has no stages." : "Pick a pipeline to list its stages.",
      })];
    }
    return stages.map(function (stage) {
      const label = stage.name || stage.id;
      if (stage.locked || stage.kind === "human_approval") {
        return el("span.chip.mini", {
          title: "The human-approval boundary cannot be switched off for a run.",
          text: label + " · required",
        });
      }
      let included = stage.include_in_run !== false;
      const chip = el("span.chip.mini", { style: "cursor:pointer", text: label });
      chip.classList.toggle("blu", included);
      chip.classList.toggle("dim", !included);
      chip.addEventListener("click", function () {
        included = !included;
        chip.classList.toggle("blu", included);
        chip.classList.toggle("dim", !included);
        // Only differences from the pipeline's own setting are recorded, so a
        // stage left alone keeps following the saved pipeline.
        if (included === (stage.include_in_run !== false)) {
          delete form.stageToggles[stage.id];
        } else {
          form.stageToggles[stage.id] = included;
        }
      });
      return chip;
    });
  }

  function renderScheduleDetail() {
    const schedule = state.schedules.filter(function (s) {
      return s.id === state.scheduleId;
    })[0];
    if (!schedule) {
      return el("div.panel", el("span.dim", { text: "Select a schedule to see its runs." }));
    }
    const usage = schedule.usage || {};
    const detail = el("div.panel",
      el("div", { style: "display:flex;align-items:center;gap:10px" },
        el("span.hi", { style: "font-weight:600", text: schedule.id }),
        el("span.dim", { text: schedule.name || schedule.pipeline_id }),
        el("span", {
          style: "margin-left:auto",
          class: schedule.blocked ? "bad" : (schedule.enabled ? "ok" : "dim"),
          text: schedule.blocked ? "BLOCKED" : (schedule.enabled ? "ENABLED" : "DISABLED"),
        })),
      ui.kv("Cadence", schedule.cron + " (" + schedule.timezone + ")"),
      ui.kv("Next run", schedule.next_run_at ? stampOf(schedule.next_run_at) : "—"),
      ui.kv("Last run", schedule.last_run_at
        ? stampOf(schedule.last_run_at) + " · " + (schedule.last_status || "—") : "never"),
      ui.kv("Runs today", (usage.runs === undefined ? "—" : usage.runs)
        + " / " + (schedule.guardrails.max_runs_per_day || "∞")
        + (usage.active_runs ? " · " + usage.active_runs + " active" : "")),
      ui.kv("Spend today", money(usage.spend_usd === undefined ? 0 : usage.spend_usd)
        + " / " + (schedule.guardrails.max_spend_usd_per_day
          ? money(schedule.guardrails.max_spend_usd_per_day) : "∞")),
      ui.kv("Max concurrent", schedule.guardrails.max_concurrent_runs),
      ui.kv("Auto-advance", schedule.auto_advance ? "ON · validated on pass" : "off",
        { tone: schedule.auto_advance ? "ok" : "" }));
    const brief = schedule.inputs || {};
    const briefKeys = Object.keys(brief);
    detail.appendChild(ui.kv("Run brief", briefKeys.length
      ? briefKeys.map(function (key) {
        const value = brief[key];
        return key + "=" + (Array.isArray(value) ? value.join("/") : value);
      }).join(" · ")
      : "— none; the first stage runs on the pipeline objective alone"));
    const overrides = schedule.model_overrides || {};
    const overrideKeys = Object.keys(overrides);
    detail.appendChild(ui.kv("Model overrides", overrideKeys.length
      ? overrideKeys.map(function (key) { return key + " → " + overrides[key]; }).join(" · ")
      : "— none; every stage uses its own model"));
    const toggles = schedule.stage_toggles || {};
    const toggleKeys = Object.keys(toggles);
    detail.appendChild(ui.kv("Stage toggles", toggleKeys.length
      ? toggleKeys.map(function (key) {
        return key + (toggles[key] ? " → on" : " → skipped");
      }).join(" · ")
      : "— none; stages run as the pipeline saves them"));
    if (schedule.last_advance_note) {
      detail.appendChild(el("div.note", { style: "margin-top:8px" },
        el("span", {
          class: schedule.last_advanced_run_id ? "ok" : "dim",
          text: "Auto-advance: " + schedule.last_advance_note,
        })));
    }
    if (schedule.blocked) {
      detail.appendChild(el("div.note", { style: "margin-top:8px" },
        el("span.warn", { text: "Guardrail: " + schedule.blocked })));
    }
    if (schedule.last_error) {
      detail.appendChild(el("div.note", { style: "margin-top:8px" },
        el("span.bad", { text: "Last error: " + schedule.last_error })));
    }

    const runs = el("div", { style: "margin-top:10px" },
      el("label.dim.mini", { style: "display:block;margin-bottom:4px", text: "RUNS IT STARTED" }));
    if (!(schedule.runs || []).length) {
      runs.appendChild(el("span.dim.mini", { text: "It has not fired yet." }));
    }
    (schedule.runs || []).forEach(function (run) {
      runs.appendChild(ui.kv(run.run_id,
        run.status + " · " + money(run.cost_usd)
        + " · " + stampOf(run.created_at)));
    });
    detail.appendChild(runs);

    detail.appendChild(el("div", { style: "display:flex;gap:6px;margin-top:10px;flex-wrap:wrap" },
      ui.button(schedule.enabled ? "DISABLE" : "ENABLE", {
        small: true,
        onClick: function () { toggleSchedule(schedule); },
      }),
      ui.button("RUN NOW", {
        small: true, primary: true,
        onClick: function () { fireSchedule(schedule.id); },
      }),
      ui.button("DELETE", {
        small: true,
        onClick: function () { deleteSchedule(schedule.id); },
      })));
    return detail;
  }

  /** The run brief, with only the fields the operator actually filled in. */
  function scheduleBrief(form) {
    const inputs = {};
    const query = form.query.value.trim();
    if (query) inputs.query = query;
    const tickers = form.tickers.value.split(",").map(function (part) {
      return part.trim().toUpperCase();
    }).filter(Boolean);
    if (tickers.length) inputs.tickers = tickers;
    const topN = Number(form.topN.value);
    if (isFinite(topN) && topN > 0) inputs.top_n = topN;
    if (form.scanMode.value) inputs.scan_mode = form.scanMode.value;
    // ON is the default behaviour, so only the OFF directive is recorded.
    if (!form.resolveTickers) inputs.resolve_tickers = false;
    const sectors = form.sectors.value.split(",").map(function (part) {
      return part.trim();
    }).filter(Boolean);
    if (sectors.length) inputs.sectors = sectors;
    const windowH = Number(form.catalystWindow.value);
    if (form.catalystWindow.value.trim() && isFinite(windowH) && windowH > 0) {
      inputs.catalyst_window_hours = windowH;
    }
    if (form.universe.value) inputs.universe = form.universe.value;
    return inputs;
  }

  async function createSchedule(form) {
    const body = {
      pipeline_id: form.pipeline.value,
      cron: form.cron.value.trim(),
      name: form.name.value.trim(),
      timezone: form.timezone.value.trim() || "UTC",
      objective: form.objective.value.trim() || null,
      max_concurrent_runs: Number(form.maxConcurrent.value) || 1,
    };
    if (form.maxRuns.value.trim()) body.max_runs_per_day = Number(form.maxRuns.value);
    if (form.maxSpend.value.trim()) body.max_spend_usd_per_day = Number(form.maxSpend.value);
    body.inputs = scheduleBrief(form);
    body.auto_advance = !!form.autoAdvance;
    if (Object.keys(form.modelOverrides || {}).length) {
      body.model_overrides = Object.assign({}, form.modelOverrides);
    }
    if (Object.keys(form.stageToggles || {}).length) {
      body.stage_toggles = Object.assign({}, form.stageToggles);
    }
    try {
      const created = await req("POST", "/autopipeline", body);
      state.scheduleId = created.id;
      ctx.toast("Schedule " + created.id + " created", "green");
      await reloadView();
    } catch (err) {
      fail("Creating the schedule", err);
    }
  }

  async function toggleSchedule(schedule) {
    try {
      await req("PUT", "/autopipeline/" + encodeURIComponent(schedule.id),
        { enabled: !schedule.enabled });
      ctx.toast("Schedule " + (schedule.enabled ? "disabled" : "enabled"), "green");
      await reloadView();
    } catch (err) {
      fail("Updating the schedule", err);
    }
  }

  async function fireSchedule(scheduleId) {
    try {
      const result = await req("POST",
        "/autopipeline/" + encodeURIComponent(scheduleId) + "/run", {});
      ctx.toast("Schedule fired: run " + result.run_id, "green");
      await reloadView();
    } catch (err) {
      fail("Firing the schedule", err);
    }
  }

  async function deleteSchedule(scheduleId) {
    const ok = await askConfirm("Delete schedule",
      "Delete schedule " + scheduleId + "? Its timer is removed; the runs it already started "
      + "stay in the ledger.", "Delete schedule");
    if (!ok) return;
    try {
      await req("DELETE", "/autopipeline/" + encodeURIComponent(scheduleId));
      state.scheduleId = null;
      ctx.toast("Schedule deleted", "green");
      await reloadView();
    } catch (err) {
      fail("Deleting the schedule", err);
    }
  }

  // ------------------------------------------------------ evidence explorer

  /**
   * Load the lineage graph.
   *
   * `GET /api/agents/evidence` builds the whole spine server-side from the
   * references the records already hold, so the columns cannot drift apart the
   * way a client-side join would. `node` and `focus` are sent along because the
   * backend also computes which nodes are *related* to the selection.
   */
  async function loadEvidence() {
    const query = { focus: focusKey(state.evidenceFocus) };
    if (state.evidenceNodeId) query.node = state.evidenceNodeId;
    if (state.evidenceCentered) query.finding_id = state.evidenceCentered;
    if (ctx.params && ctx.params.finding && !state.evidenceNodeId) {
      query.finding_id = ctx.params.finding;
    }
    if (ctx.params && ctx.params.run) query.run_id = ctx.params.run;
    state.evidence = await req("GET", "/evidence", query);
    state.evidenceNodeId = state.evidence.selected || null;
  }

  /** Map the reference's focus labels onto the query values the API takes. */
  function focusKey(label) {
    if (label === "Upstream") return "upstream";
    if (label === "Downstream") return "downstream";
    return "all";
  }

  function renderEvidence() {
    const graph = state.evidence;
    if (!graph) {
      return ui.emptyState("Evidence Explorer", "Loading the lineage graph…",
        { badge: "working" });
    }
    const types = graph.types || [];
    const nodes = graph.nodes || [];
    const related = graph.related || [];
    const selected = nodes.filter(function (node) {
      return node.id === state.evidenceNodeId;
    })[0] || nodes[0] || null;
    const visible = state.evidenceType === "All types"
      ? nodes
      : nodes.filter(function (node) { return node.type === state.evidenceType; });

    const toolbar = el("div.v5-toolbar",
      el("label",
        el("span", { text: "Object type" }),
        select(["All types"].concat(types), state.evidenceType, function (value) {
          state.evidenceType = value; render();
        }, { evidenceType: "" })),
      (function () {
        const group = el("div.v5-segmented", { "aria-label": "Evidence focus" });
        ["All relationships", "Upstream", "Downstream"].forEach(function (option) {
          group.appendChild(el("button", {
            type: "button",
            "aria-pressed": option === state.evidenceFocus ? "true" : "false",
            dataset: { evidenceFocus: option },
            text: option,
            on: {
              click: function () {
                state.evidenceFocus = option;
                reloadView();
              },
            },
          }));
        });
        return group;
      })(),
      ui.button("Center selected node", {
        dataset: { evidenceCenter: "" },
        onClick: function () { centerEvidenceNode(selected); },
      }),
      el("span.v5-toolbar-count", { text: visible.length + " of " + nodes.length + " nodes" }));

    const columns = el("div.v5-evidence-columns");
    types.forEach(function (type) {
      const column = el("div.v5-evidence-column", el("h3", { text: type }));
      const inColumn = visible.filter(function (node) { return node.type === type; });
      if (!inColumn.length) {
        column.appendChild(el("span.v5-column-empty", {
          text: "No " + type.toLowerCase() + " nodes",
        }));
      }
      inColumn.forEach(function (node) {
        const focused = state.evidenceFocus === "All relationships"
          || !related.length || related.indexOf(node.id) >= 0;
        const button = el("button", {
          type: "button",
          dataset: { evidenceNode: node.id },
          "aria-pressed": selected && node.id === selected.id ? "true" : "false",
          class: focused ? "" : "dimmed",
          on: { click: function () { selectEvidenceNode(node.id); } },
        },
          stateBadge(node.relation || "recorded", modifierOf(node.relation || "recorded")),
          el("b", { text: node.label }),
          el("small", {
            text: [node.id,
              node.confidence === null || node.confidence === undefined
                ? "no score" : ui.fmt.num(node.confidence, 0) + "%",
              node.freshness || "—"].join(" · "),
          }));
        column.appendChild(button);
      });
      columns.appendChild(column);
    });

    const graphPanel = el("section.panel.v5-evidence-graph",
      el("div.panel-title",
        el("span", { text: types.join(" → ") }),
        el("span.badge", {
          text: (graph.counts && types.map(function (type) {
            return (graph.counts[type] || 0) + " " + type.toLowerCase();
          }).join(" · ")) || "lineage",
        })),
      columns);
    if ((graph.notes || []).length) {
      graph.notes.forEach(function (note) {
        graphPanel.appendChild(el("div.note", { text: note }));
      });
    }

    return [toolbar, el("div.v5-evidence-layout", graphPanel, renderEvidenceInspector(selected))];
  }

  /** The node inspector: confidence, freshness, provenance and its edges. */
  function renderEvidenceInspector(node) {
    if (!node) {
      return el("aside.panel.v5-evidence-inspector",
        el("div.panel-title", el("span", { text: "Node inspector" })),
        el("div.v5-detail-pad",
          el("p.v5-lead", {
            text: "Nothing has been recorded on the evidence spine yet. It fills in as agents "
              + "cite sources, record findings and reviewers decide.",
          })));
    }
    const edgeList = function (label, ids, empty) {
      const article = el("article", el("small", { text: label }));
      if (!ids.length) {
        article.appendChild(el("span", { text: empty }));
        return article;
      }
      ids.forEach(function (id) {
        article.appendChild(el("button", {
          type: "button", dataset: { evidenceNode: id }, text: id,
          on: { click: function () { selectEvidenceNode(id); } },
        }));
      });
      return article;
    };

    const actions = el("div.v5-inline-actions");
    if (node.run_id) {
      actions.appendChild(ui.button("Open originating run", {
        onClick: function () {
          state.runId = node.run_id;
          go("pipeline", { run: node.run_id });
        },
      }));
    }
    actions.appendChild(ui.button("Open audit trail", {
      onClick: function () {
        state.auditQuery = node.route && node.route.id ? node.route.id : node.id;
        state.auditRunId = node.run_id || null;
        go("audit", node.run_id ? { run: node.run_id } : {});
      },
    }));
    const route = node.route || {};
    if (route.page) {
      actions.appendChild(ui.button("Open the object", {
        onClick: function () { openEvidenceObject(node); },
      }));
    }

    return el("aside.panel.v5-evidence-inspector",
      el("div.panel-title",
        el("span", { text: "Node inspector" }),
        el("span.badge", { text: node.type })),
      el("div.v5-detail-pad",
        el("p.v5-eyebrow", { text: node.id + " · " + (node.relation || "recorded") }),
        el("h3", { text: node.label }),
        el("p.v5-lead", { text: node.detail || "" }),
        el("dl.v5-kv",
          kvPair("Confidence", node.confidence === null || node.confidence === undefined
            ? "not scored" : ui.fmt.num(node.confidence, 0) + "%"),
          kvPair("Freshness", node.freshness || "—"),
          kvPair("Produced by", node.agent || "—"),
          kvPair("Audit events", node.audit_events === undefined
            ? "—" : String(node.audit_events))),
        el("div.v5-provenance-list",
          edgeList("UPSTREAM INPUTS", node.upstream || [], "Primary source node"),
          edgeList("DOWNSTREAM OBJECTS", node.downstream || [], "Terminal node"),
          el("article", el("small", { text: "PROVENANCE" }),
            el("span", {
              text: "Every edge here is a reference the record itself holds — a finding's "
                + "hypothesis, an experiment's hypothesis, a report's backtest, an approval's "
                + "run. Nothing on this graph is drawn by hand.",
            }))),
        actions));
  }

  /** Select a node without refetching (the graph already carries every node). */
  function selectEvidenceNode(nodeId) {
    state.evidenceNodeId = nodeId;
    // The related set is computed server-side per selection, so a new selection
    // needs a refetch whenever a focus filter is active.
    if (state.evidenceFocus === "All relationships") {
      render();
      return;
    }
    reloadView();
  }

  /** Re-centre the graph on the selected node and refetch around it. */
  function centerEvidenceNode(node) {
    if (!node) {
      ctx.toast("Select a node first", "yellow");
      return;
    }
    state.evidenceNodeId = node.id;
    const route = node.route || {};
    state.evidenceCentered = route.id || null;
    announce(node.id + " centred in the evidence spine.");
    ctx.toast("Centred on " + node.id, "cyan");
    reloadView();
  }

  /** Open the page that owns the object a node stands for. */
  function openEvidenceObject(node) {
    const route = node.route || {};
    if (route.page === "agents") {
      if (route.sub === "findings" && route.id) state.findingId = route.id;
      go(route.sub || "findings", route.id ? { id: route.id } : {});
      return;
    }
    if (route.page === "data") {
      ctx.router.go("data", { sub: route.sub || "source-catalog" });
      return;
    }
    if (route.page === "lab") {
      ctx.router.go("lab", { sub: route.sub || "hypotheses",
        params: route.id ? { id: route.id } : {} });
      return;
    }
    ctx.toast(node.id + " has no page of its own", "cyan");
  }

  // -------------------------------------------------------------- findings

  async function loadFindings() {
    const query = { limit: 200 };
    if (state.findingQuery.trim()) query.q = state.findingQuery.trim();
    state.findings = await req("GET", "/findings", query);
  }

  function renderFindings() {
    const payload = state.findings || { findings: [], counts: {} };
    const rows = payload.findings || [];
    const toolbar = el("div.v5-toolbar",
      el("label",
        el("span", { text: "Search findings" }),
        el("input.input", {
          type: "search", value: state.findingQuery,
          placeholder: "Symbol, claim, hypothesis…",
          dataset: { findingQuery: "" },
          on: {
            change: function (e) { state.findingQuery = e.target.value; reloadView(); },
            keydown: function (e) {
              if (e.key === "Enter") { state.findingQuery = e.target.value; reloadView(); }
            },
          },
        })),
      el("span.v5-toolbar-count", {
        text: rows.length + " findings · " + (payload.counts.unacknowledged || 0) + " unreviewed",
      }));

    const table = el("table.data-table.v5-findings-table",
      el("thead", el("tr",
        el("th", { text: "Finding" }), el("th", { text: "Symbol" }),
        el("th", { text: "Status" }), el("th", { text: "Confidence" }),
        el("th", { text: "Run" }), el("th", { text: "Agent" }),
        el("th", { text: "Hypothesis" }), el("th", { text: "Reviewed" }),
        el("th", { text: "Evidence" }))));
    const body = el("tbody");
    if (!rows.length) {
      body.appendChild(el("tr", el("td", {
        colspan: "9", class: "muted",
        text: state.findingQuery
          ? "No finding matches that search."
          : "No findings recorded yet. Agents write here through their save_finding tool.",
      })));
    }
    rows.forEach(function (finding) {
      body.appendChild(el("tr",
        el("td", el("button.tl-finding-link", {
          type: "button", dataset: { tlFinding: finding.id },
          text: finding.id + " " + finding.title,
          on: { click: function () { state.findingId = finding.id; render(); } },
        })),
        el("td", { text: finding.symbol_label }),
        el("td", el("span.tl-status", {
          class: finding.status === "promoted" ? "tl-grn"
            : (finding.status === "dismissed" ? "tl-dim" : "tl-amb"),
          text: finding.status,
        })),
        el("td", el("span.tl-pct", {
          class: (finding.confidence_pct || 0) >= 75 ? "tl-grn" : "tl-amb",
          text: finding.confidence_pct === null || finding.confidence_pct === undefined
            ? finding.confidence : ui.fmt.pct(finding.confidence_pct),
        })),
        el("td", finding.run_id
          ? ui.button(finding.run_id, {
            small: true,
            onClick: function () { state.runId = finding.run_id; go("pipeline", { run: finding.run_id }); },
          })
          : "—"),
        el("td", { text: agentLabel(finding.agent_id) }),
        el("td", { text: finding.hypothesis_id || "—" }),
        el("td", finding.acknowledged
          ? stateBadge("Reviewed", "completed")
          : stateBadge("Unreviewed", "awaiting-approval")),
        el("td", el("span", { text: String(finding.evidence_count) + " " },
          ui.button("Trace", {
            small: true,
            dataset: { evidenceTrace: finding.id },
            onClick: function () {
              // REFERENCE's "Trace" opens the Evidence Explorer centred on this
              // finding, so the lineage behind the claim is one click away.
              state.evidenceCentered = finding.id;
              state.evidenceNodeId = null;
              state.evidenceType = "All types";
              go("evidence", { finding: finding.id });
            },
          })))));
    });
    table.appendChild(body);

    const nodes = [toolbar, ui.panel({
      title: "Findings registry",
      badge: "linked research objects",
      body: el("div.v5-table-scroll", table),
    })];
    const selected = rows.filter(function (f) { return f.id === state.findingId; })[0];
    if (selected) nodes.push(renderFindingDetail(selected));
    return el("section.v5-merged", { dataset: { v5Part: "findings" } }, el("div", nodes));
  }

  function renderFindingDetail(finding) {
    const evidence = el("div.v5-evidence-cards");
    if (!(finding.evidence || []).length) {
      evidence.appendChild(el("article", el("span", { text: "NO EVIDENCE ATTACHED" }),
        el("p", { text: "This finding carries no evidence references." })));
    }
    (finding.evidence || []).forEach(function (item) {
      const supports = item.supports === undefined ? true : !!item.supports;
      evidence.appendChild(el("article", { class: supports ? "support" : "contradiction" },
        el("span", { text: supports ? "SUPPORTS" : "CONTRADICTS" }),
        el("p", { text: item.label || item.ref || JSON.stringify(item) })));
    });

    const actions = el("div.v5-detail-actions");
    if (!finding.acknowledged) {
      actions.appendChild(ui.button("Mark reviewed", {
        primary: true, onClick: function () { acknowledgeFinding(finding.id); },
      }));
    }
    actions.appendChild(ui.button("Delete finding", {
      onClick: async function () {
        const ok = await askConfirm("Delete finding",
          "Delete finding " + finding.id + " and its evidence references?", "Delete finding");
        if (!ok) return;
        try {
          await req("DELETE", "/findings/" + encodeURIComponent(finding.id));
          state.findingId = null;
          ctx.toast("Finding deleted", "green");
          await reloadView();
        } catch (err) {
          fail("Deleting the finding", err);
        }
      },
    }));

    return ui.panel({
      className: "v5-detail-panel",
      title: finding.id,
      badge: finding.status,
      body: el("div.v5-detail-pad",
        el("p.v5-eyebrow", { text: finding.symbol_label + " · " + agentLabel(finding.agent_id) }),
        el("h3", { text: finding.title }),
        el("p.v5-lead", { text: finding.summary }),
        el("dl.v5-kv",
          kvPair("Confidence", finding.confidence_pct === null
            || finding.confidence_pct === undefined
            ? finding.confidence : ui.fmt.pct(finding.confidence_pct)),
          kvPair("Run", finding.run_id || "—"),
          kvPair("Stage", finding.stage_id || "—"),
          kvPair("Recorded", stampOf(finding.created_at))),
        evidence,
        actions),
    });
  }

  // ------------------------------------------------------------- knowledge

  async function loadKnowledge() {
    const query = { limit: 200 };
    if (state.knowledgeQuery.trim()) query.q = state.knowledgeQuery.trim();
    state.knowledge = await req("GET", "/knowledge", query);
    const entries = state.knowledge.entries || [];
    if (entries.length
      && !entries.some(function (e) { return e.id === state.knowledgeId; })) {
      state.knowledgeId = entries[0].id;
    }
    state.knowledgeEntry = state.knowledgeId
      ? await req("GET", "/knowledge/" + encodeURIComponent(state.knowledgeId))
      : null;
  }

  function renderKnowledge() {
    const payload = state.knowledge || { entries: [], collections: [], counts: {} };
    const entries = payload.entries || [];
    const list = el("div.panel.tight",
      el("div.ptitle", { text: "KNOWLEDGE BASE · feeds agent briefs" }),
      el("input", {
        placeholder: "Search knowledge…", style: "margin-bottom:8px",
        value: state.knowledgeQuery,
        on: {
          change: function (e) { state.knowledgeQuery = e.target.value; reloadView(); },
          keydown: function (e) {
            if (e.key === "Enter") { state.knowledgeQuery = e.target.value; reloadView(); }
          },
        },
      }));
    const listBody = el("div", { id: "knowlist" });
    if (!entries.length) {
      listBody.appendChild(el("div.dim.mini", { style: "padding:6px 2px",
        text: state.knowledgeQuery
          ? "Nothing matches that search."
          : "The knowledge base is empty. Agents write here through save_knowledge." }));
    }
    entries.forEach(function (entry) {
      const row = el("div.rowitem", {
        on: { click: function () { selectKnowledge(entry.id); } },
      },
        el("span.hi", { text: entry.id }), " · " + entry.title,
        el("br"),
        el("span.dim.mini", {
          text: [entry.kind, (entry.tags || []).join("/") || "untagged",
            entry.source_run_id ? "from " + entry.source_run_id : "manual",
            entry.matched ? "match: " + entry.matched : null,
          ].filter(Boolean).join(" · "),
        }));
      if (entry.id === state.knowledgeId) row.classList.add("sel");
      listBody.appendChild(row);
    });
    list.appendChild(listBody);

    const collections = el("div.panel.tight",
      el("div.ptitle", { text: "COLLECTIONS" }));
    if (!(payload.collections || []).length) {
      collections.appendChild(el("div.dim.mini", { style: "padding:6px 2px", text: "None yet." }));
    }
    (payload.collections || []).forEach(function (collection) {
      collections.appendChild(ui.kv(collection.tag,
        collection.entries + " entries · " + collection.symbol_count + " symbols · "
        + collection.run_count + " runs"));
    });

    return el("div.grid.g23",
      el("div", { style: "display:flex;flex-direction:column;gap:12px" }, list, collections),
      renderKnowledgeDetail());
  }

  function renderKnowledgeDetail() {
    const entry = state.knowledgeEntry;
    if (!entry) {
      return el("div.panel", { id: "knowdetail" },
        el("span.dim", { text: "Select an entry to read it." }));
    }
    const sources = el("div", { style: "margin-top:10px" });
    (entry.sources || []).forEach(function (source) {
      sources.appendChild(ui.kv(source.kind || "source",
        source.label || source.ref || source.url || JSON.stringify(source)));
    });
    return el("div.panel", { id: "knowdetail" },
      el("div", { style: "display:flex;align-items:center;gap:10px" },
        el("span.hi", { style: "font-family:system-ui;font-size:15px;font-weight:600",
          text: entry.title }),
        el("span", { style: "margin-left:auto", class: "ok", text: entry.kind.toUpperCase() })),
      el("div.dim.mini", { style: "margin:2px 0 10px",
        text: [entry.id, entry.author ? "by " + entry.author : null,
          entry.source_run_id ? "from " + entry.source_run_id : null,
          entry.word_count + " words",
          "updated " + stampOf(entry.updated_at)].filter(Boolean).join(" · ") }),
      el("div.panel.tight", { style: "margin-bottom:10px" },
        el("div", { style: "font-family:system-ui;font-size:13px;white-space:pre-wrap",
          text: entry.body })),
      (entry.finding_ids || []).length
        ? ui.kv("Linked findings", entry.finding_ids.join(", "))
        : null,
      sources,
      el("div", { style: "display:flex;gap:6px;margin-top:10px" },
        ui.button("DELETE", {
          small: true,
          onClick: async function () {
            const ok = await askConfirm("Delete knowledge entry",
              "Delete " + entry.id + " — “" + entry.title + "”? It also leaves the search index.",
              "Delete entry");
            if (!ok) return;
            try {
              await req("DELETE", "/knowledge/" + encodeURIComponent(entry.id));
              state.knowledgeId = null;
              state.knowledgeEntry = null;
              ctx.toast("Entry deleted", "green");
              await reloadView();
            } catch (err) {
              fail("Deleting the entry", err);
            }
          },
        })));
  }

  async function selectKnowledge(entryId) {
    state.knowledgeId = entryId;
    try {
      state.knowledgeEntry = await req("GET", "/knowledge/" + encodeURIComponent(entryId));
      render();
    } catch (err) {
      fail("Loading the entry", err);
    }
  }

  // -------------------------------------------------------- approval review

  async function loadApprovals() {
    state.approvals = await req("GET", "/approvals", { limit: 100 });
    if (!state.validationTargets) {
      const published = await ctx.api.optional(req("GET", "/approvals/validation-targets"));
      state.validationTargets = (published && published.targets) || [];
      if (!state.validationTarget && state.validationTargets.length) {
        state.validationTarget = state.validationTargets[0].value;
      }
    }
    const items = state.approvals.approvals || [];
    const pending = items.filter(function (item) { return item.pending; });
    const preferred = ctx.params && (ctx.params.id || ctx.params.approval);
    if (preferred) state.approvalId = preferred;
    const known = items.some(function (item) { return item.id === state.approvalId; });
    if (!known) state.approvalId = (pending[0] || items[0] || {}).id || null;
    if (ctx.params && ctx.params.run) {
      const forRun = items.filter(function (item) { return item.run_id === ctx.params.run; })[0];
      if (forRun) state.approvalId = forRun.id;
    }
    state.run = null;
    const current = items.filter(function (item) { return item.id === state.approvalId; })[0];
    if (current) {
      state.run = await req("GET", "/runs/" + encodeURIComponent(current.run_id));
    }
  }

  function renderApproval() {
    const payload = state.approvals || { approvals: [], counts: {} };
    const items = payload.approvals || [];
    const current = items.filter(function (item) { return item.id === state.approvalId; })[0];
    if (!current) {
      return ui.panel({
        title: "Approval Review",
        badge: "nothing pending",
        badgeTone: "green",
        body: el("div.list-row", el("span", {
          text: "No approval has been requested. A pipeline stops here only when it reaches a "
            + "human-approval stage.",
        })),
      });
    }

    const banner = el("div.v5-approval-banner",
      el("div",
        el("small", { text: "MANDATORY HUMAN CHECKPOINT · " + current.id }),
        el("h3", { text: current.title }),
        el("p", { text: current.summary })),
      stateBadge(current.pending ? "Awaiting approval"
        : (current.state === "approved" ? "Approved" : "Rejected"),
      current.pending ? "awaiting-approval"
        : (current.state === "approved" ? "completed" : "rejected")));

    const spine = el("aside.panel.v5-evidence-spine",
      el("div.panel-title", { text: "Evidence spine" },
        el("span.badge", { text: current.run_id }),
        // REFERENCE's "Trace": open the Evidence Explorer on this run's lineage,
        // so the reviewer can walk back from the decision to the sources.
        ui.button("Trace evidence", {
          small: true,
          dataset: { evidenceTrace: current.run_id || current.id },
          onClick: function () {
            state.evidenceCentered = null;
            state.evidenceNodeId = null;
            state.evidenceType = "All types";
            go("evidence", current.run_id ? { run: current.run_id } : {});
          },
        })));
    const stages = (state.run && state.run.stages) || [];
    stages.forEach(function (record) {
      const done = record.status === "succeeded" || record.status === "skipped";
      const here = record.stage_id === current.stage_id;
      spine.appendChild(el("button", {
        type: "button", "aria-pressed": here ? "true" : "false",
        dataset: { approvalStage: record.stage_id },
        on: { click: function () { state.stageId = record.stage_id; render(); } },
      },
        el("span", { text: here ? "●" : (done ? "✓" : "○") }),
        el("b", { text: record.name }),
        el("small", { text: String(record.status).replace(/_/g, " ") })));
    });

    const dossier = el("main.v5-approval-dossier",
      renderDossier(current),
      renderRiskPanel(current),
      renderDecisionPanel(current));

    const queue = items.filter(function (item) { return item.pending; });
    const nodes = [banner, el("div.v5-approval-layout", spine, dossier)];
    if (queue.length > 1) {
      const list = el("div.v5-inbox-list");
      queue.forEach(function (item) {
        list.appendChild(el("button.v5-inbox-item", {
          type: "button", "aria-pressed": item.id === state.approvalId ? "true" : "false",
          on: { click: function () { state.approvalId = item.id; reloadView(); } },
        },
          el("span.v5-item-top", el("b", { text: item.id }),
            stateBadge(ageOf(item.age_seconds), "high")),
          el("strong", { text: item.title }),
          el("small", { text: item.run_id + " · " + item.stage_id })));
      });
      nodes.push(ui.panel({
        title: "Approval queue", badge: queue.length + " pending", body: list,
      }));
    }
    return nodes;
  }

  function renderDossier(approval) {
    const payload = approval.payload || {};
    const context = approval.context || {};
    const cards = el("div.v5-evidence-cards");
    const upstream = payload.upstream || payload.inputs || {};
    Object.keys(upstream).forEach(function (key) {
      const value = upstream[key];
      const text = typeof value === "object" && value !== null
        ? JSON.stringify(value) : String(value);
      cards.appendChild(el("article.support",
        el("span", { text: key.toUpperCase() }),
        el("p", { text: text.length > 400 ? text.slice(0, 400) + "…" : text })));
    });
    if (!cards.children.length) {
      cards.appendChild(el("article.support",
        el("span", { text: "NO UPSTREAM PAYLOAD" }),
        el("p", { text: "The approval stage received no structured output from the stages before "
          + "it. The run's own inputs and logs are the record." })));
    }

    return ui.panel({
      title: "Decision dossier",
      badge: approval.stage_name || approval.stage_id,
      body: el("div.v5-dossier-main",
        el("p.v5-eyebrow", { text: "REQUESTED ACTION" }),
        el("h3", { text: approval.requested_action || approval.title }),
        el("p.v5-lead", { text: approval.objective || approval.summary || "" }),
        el("dl.v5-kv",
          kvPair("Run", approval.run_id),
          kvPair("Pipeline", (approval.pipeline_id || "—")
            + (approval.pipeline_version ? " v" + approval.pipeline_version : "")),
          kvPair("Agent", agentLabel(approval.agent_id)),
          kvPair("Waiting", ageOf(approval.age_seconds)),
          kvPair("Symbol", context.symbol || "—"),
          kvPair("Universe", context.universe || "—")),
        cards),
    });
  }

  function renderRiskPanel(approval) {
    const run = state.run;
    const usage = (run && run.usage) || {};
    const contract = approval.contract || {};
    const metrics = el("div.v5-risk-metrics",
      riskMetric("Run cost", money(run ? run.cost_usd : null)),
      riskMetric("Input tokens", ui.fmt.int(usage.input_tokens)),
      riskMetric("Output tokens", ui.fmt.int(usage.output_tokens)),
      riskMetric("Stages done", run ? (run.progress.completed + "/" + run.progress.total) : "—"),
      riskMetric("Duration", durationOf(run ? run.duration_ms : null)),
      riskMetric("Trigger", run ? run.trigger : "—"));

    const missing = contract.missing || [];
    const columns = el("div.v5-risk-columns",
      el("article",
        el("h4", { text: "Contract" }),
        el("p", { text: contract.text || "No output contract was declared for this stage." }),
        missing.length
          ? el("p.red", { text: "Missing required keys: " + missing.join(", ") })
          : el("p", { text: "Required keys: "
            + ((contract.required || []).join(", ") || "none declared") })),
      el("article",
        el("h4", { text: "What approving does" }),
        el("p", { text: "The paused stage is marked succeeded and the run continues with the "
          + "stages after it." }),
        el("p", { text: "Rejecting kills the run: no further stage executes." })),
      el("article",
        el("h4", { text: "Reproducibility" }),
        el("p", { text: (approval.pipeline_id || "—")
          + " v" + (approval.pipeline_version || "—")
          + " · requested " + stampOf(approval.requested_at) }),
        ui.button("Open run artifacts", {
          onClick: function () { state.runId = approval.run_id; go("pipeline", { run: approval.run_id }); },
        })));

    return ui.panel({
      className: "v5-risk-panel",
      title: "Run cost, contract and consequences",
      badge: approval.run_status || "—",
      body: [metrics, columns],
    });
  }

  function riskMetric(label, value) {
    return el("div", el("small", { text: label }), el("b", { text: String(value) }));
  }

  function renderDecisionPanel(approval) {
    if (!approval.pending) {
      return ui.panel({
        className: "v5-decision-panel",
        title: "Human decision",
        badge: approval.state,
        badgeTone: approval.state === "approved" ? "green" : "red",
        body: el("div.v5-decision-form",
          el("p.v5-lead", {
            text: "Decided " + stampOf(approval.decided_at) + " by "
              + (approval.decided_by || "—") + ".",
          }),
          el("p", { text: approval.comment || "No rationale was recorded." })),
      });
    }

    const rationale = el("textarea", {
      dataset: { approvalRationale: "" },
      placeholder: "Explain the evidence and risk judgment…",
      text: state.approvalRationale,
    });
    const counter = el("small", { text: state.approvalRationale.length + "/12 minimum" });
    const conditions = el("input.input", {
      dataset: { approvalConditions: "" },
      value: state.approvalConditions,
      placeholder: "Example: review after Q3 filing",
    });
    const approve = ui.button("Approve — resume the run", { primary: true, disabled: true });
    const reject = ui.button("Reject — kill the run", { className: "danger", disabled: true });

    // "Request more validation" is not a third verdict: the request stays
    // pending and the run stays blocked. It records WHICH further evidence the
    // reviewer wants, so the ask becomes a work item instead of a comment.
    const targets = state.validationTargets || [];
    const targetSelect = el("select.select", { dataset: { validationTarget: "" } });
    if (!targets.length) {
      targetSelect.disabled = true;
      targetSelect.appendChild(el("option", { text: "— no targets published —" }));
    }
    targets.forEach(function (target) {
      targetSelect.appendChild(el("option", {
        value: target.value, text: target.label, selected: target.value === state.validationTarget,
      }));
    });
    targetSelect.addEventListener("change", function (event) {
      state.validationTarget = event.target.value;
    });
    const requestMore = ui.button("Request more validation", { disabled: true });

    function sync() {
      state.approvalRationale = rationale.value;
      state.approvalConditions = conditions.value;
      const ok = rationale.value.trim().length >= 12;
      counter.textContent = rationale.value.length + "/12 minimum";
      approve.disabled = !ok;
      reject.disabled = !ok;
      requestMore.disabled = !ok || !targets.length;
    }
    rationale.addEventListener("input", sync);
    conditions.addEventListener("input", function () {
      state.approvalConditions = conditions.value;
    });
    approve.addEventListener("click", function () { decide(approval.id, "approve"); });
    reject.addEventListener("click", function () { decide(approval.id, "reject"); });
    requestMore.addEventListener("click", function () {
      decide(approval.id, "request_validation", targetSelect.value);
    });
    setTimeout(sync, 0);

    const asks = (approval.payload && approval.payload.validation_requests) || [];
    const history = el("div");
    asks.forEach(function (ask) {
      history.appendChild(ui.kv(ask.label || ask.target,
        "asked by " + (ask.requested_by || "—") + " · " + stampOf(ask.requested_at)
        + (ask.comment ? " · " + ask.comment : "")));
    });

    return ui.panel({
      className: "v5-decision-panel",
      title: "Human decision",
      badge: "rationale required",
      badgeTone: "yellow",
      body: el("div.v5-decision-form",
        el("label",
          el("span", { text: "Decision rationale · minimum 12 characters" }),
          rationale, counter),
        el("div.v5-form-grid",
          el("label", el("span", { text: "Conditions or review trigger" }), conditions),
          el("label", el("span", { text: "Validation request target" }), targetSelect)),
        el("div.v5-decision-actions", approve, reject, requestMore),
        asks.length
          ? el("div", el("p.v5-eyebrow", { text: "VALIDATION ALREADY REQUESTED" }), history)
          : null,
        el("p.v5-lead", {
          text: "Requesting more validation leaves this request pending and the run blocked; "
            + "it records what evidence is missing so it can be produced and re-reviewed.",
        })),
    });
  }

  async function decide(approvalId, decision, target) {
    const comment = [state.approvalRationale.trim(),
      state.approvalConditions.trim() ? "Conditions: " + state.approvalConditions.trim() : null]
      .filter(Boolean).join(" — ");
    const body = { decision: decision, comment: comment, by: "user" };
    if (target) body.target = target;
    try {
      const result = await req("POST", "/approvals/" + encodeURIComponent(approvalId), body);
      state.approvalRationale = "";
      state.approvalConditions = "";
      if (decision === "request_validation") {
        ctx.toast("Recorded: " + (target || "more validation")
          + " requested — run " + result.run_id + " stays blocked", "yellow");
      } else {
        ctx.toast(decision === "approve"
          ? "Approved — run " + result.run_id + " resumes"
          : "Rejected — run " + result.run_id + " killed",
        decision === "approve" ? "green" : "yellow");
      }
      announce("Approval " + approvalId + " " + result.state + ".");
      await refreshShared();
      await reloadView();
    } catch (err) {
      fail("Recording the decision", err);
    }
  }

  // ---------------------------------------------------------- agent studio

  async function loadStudio() {
    const [definitions, runs] = await Promise.all([
      req("GET", "/definitions"),
      req("GET", "/runs", { limit: 50 }),
    ]);
    state.definitions = definitions.agents || [];
    state.agentCounts = definitions.counts || null;
    state.runs = runs.runs || [];
    if (state.definitions.length
      && !state.definitions.some(function (d) { return d.id === state.studioAgentId; })) {
      state.studioAgentId = state.definitions[0].id;
    }
  }

  function studioRows() {
    return state.definitions.filter(function (definition) {
      if (state.studioKind === "all") return true;
      if (state.studioKind === "stage") return definition.kind === "stage";
      if (state.studioKind === "worker") return definition.kind === "worker";
      return true;
    });
  }

  function renderStudio() {
    const rows = studioRows();
    const filters = el("div.asm-filters");
    [["all", "All kinds"], ["stage", "Pipeline stage"], ["worker", "Standing worker"]]
      .forEach(function (pair) {
        filters.appendChild(el("button", {
          type: "button", dataset: { asmKind: pair[0] },
          "aria-selected": state.studioKind === pair[0] ? "true" : "false",
          text: pair[1],
          on: { click: function () { state.studioKind = pair[0]; render(); } },
        }));
      });

    const table = el("table.asm-table",
      el("thead", el("tr",
        el("th", { text: "Agent" }), el("th", { text: "Kind" }),
        el("th", { text: "Status · last active" }), el("th", { text: "Ver" }),
        el("th", { text: "Model" }), el("th.asm-r", { text: "Eval" }),
        el("th.asm-r", { text: "Fail" }), el("th", { text: "Spend" }))));
    const body = el("tbody");
    rows.forEach(function (definition) {
      const stats = definition.stats || {};
      const success = stats.failure_rate === null || stats.failure_rate === undefined
        ? "—" : ui.fmt.pct((1 - stats.failure_rate) * 100);
      const failure = stats.failure_rate === null || stats.failure_rate === undefined
        ? "—" : ui.fmt.pct(stats.failure_rate * 100);
      const row = el("tr", {
        dataset: { asmSelect: definition.id },
        "aria-selected": definition.id === state.studioAgentId ? "true" : "false",
        on: {
          click: function () {
            state.studioAgentId = definition.id;
            state.studioTab = "overview";
            // A preset belongs to the agent it was picked on.
            state.studioPreset = null;
            render();
          },
        },
      },
        el("td",
          el("span.asm-name", { text: definition.name }),
          el("span.asm-tag", { text: definition.id }),
          el("br"),
          el("span.asm-sub", { text: definition.role })),
        el("td", el("span.asm-kind", { class: definition.kind, text: definition.kind })),
        el("td",
          el("span", {
            class: definition.active ? "asm-grn" : (definition.enabled ? "asm-dim" : "asm-red"),
            text: definition.active ? "RUNNING"
              : (definition.enabled ? definition.status : "disabled"),
          }),
          el("br"),
          el("span.asm-sub", {
            text: stats.last_run_at ? "last " + stampOf(stats.last_run_at) : "never run",
          })),
        el("td.asm-dim", { text: "v" + definition.version }),
        el("td.asm-dim", { text: definition.model || "No model" }),
        el("td.asm-r", { text: success, title: "Share of recorded stage runs that succeeded" }),
        el("td.asm-r", { text: failure }),
        el("td", el("span.asm-sub", {
          text: stats.cost_usd ? money(stats.cost_usd) + " total"
            : "— no spend recorded",
        })));
      body.appendChild(row);
    });
    if (!rows.length) {
      body.appendChild(el("tr", el("td", { colspan: "8", class: "asm-sub",
        text: "No agent matches this filter." })));
    }
    table.appendChild(body);

    const inventory = el("section.panel.asm-inventory",
      el("div.panel-title",
        el("span", { text: "Agent inventory" }),
        el("span.badge", { text: state.definitions.length + " agents · one list" }),
        ui.button("+ New agent", { small: true, onClick: newAgent })),
      filters,
      el("div.asm-scroll", table));

    return [inventory, renderStudioDetail()];
  }

  function renderStudioDetail() {
    const definition = definitionOf(state.studioAgentId);
    if (!definition) {
      return ui.panel({ title: "Agent", body: el("div.asm-empty", { text: "No agent selected." }) });
    }
    const tabs = el("div.asm-tabs");
    [["overview", "Overview"], ["config", "Config"], ["versions", "Versions"],
      ["cycles", "Cycles"], ["runs", "Runs"]]
      .forEach(function (pair) {
        tabs.appendChild(el("button", {
          type: "button", dataset: { asmTab: pair[0] },
          "aria-selected": state.studioTab === pair[0] ? "true" : "false",
          text: pair[1],
          on: {
            click: function () {
              state.studioTab = pair[0];
              if (pair[0] === "versions") loadAgentVersions(definition.id);
              else render();
            },
          },
        }));
      });

    return el("section.panel.asm-detail",
      el("div.panel-title",
        el("span", { text: definition.name }),
        el("span.badge", { text: definition.kind }),
        ui.button("Duplicate", { small: true, onClick: function () { duplicateAgent(definition.id); } }),
        ui.button("Delete", { small: true, onClick: function () { deleteAgent(definition.id); } })),
      tabs,
      el("div", { id: "asmTabBody" }, renderStudioTab(definition)));
  }

  function renderStudioTab(definition) {
    if (state.studioTab === "config") return renderAgentConfig(definition);
    if (state.studioTab === "cycles") {
      return el("div.asm-empty", {
        text: definition.kind === "stage"
          ? "Pipeline stages have no cycles of their own — their execution happens inside a "
            + "pipeline run."
          : "This agent has no cycle engine; it executes as a pipeline stage.",
      }, ui.button("Open Pipeline →", {
        small: true, className: "asm-link", onClick: function () { go("pipeline"); },
      }));
    }
    if (state.studioTab === "versions") return renderAgentVersions(definition);
    if (state.studioTab === "runs") return renderAgentRuns(definition);

    const stats = definition.stats || {};
    return el("div.asm-pane",
      el("div.asm-box", el("label", { text: "System instructions" }), definition.system_prompt),
      el("div.asm-box", el("label", { text: "Execution record" }),
        stats.stage_runs
          ? stats.stage_runs + " stage runs across " + stats.runs + " pipeline runs · "
            + stats.completed + " completed · " + stats.failed + " failed · "
            + money(stats.cost_usd) + " spent"
          : "This agent has not executed a stage yet."),
      el("div.asm-box",
        kvBox("Role", definition.role),
        kvBox("Kind", definition.kind),
        kvBox("Status", definition.status),
        kvBox("Provider", definition.provider),
        kvBox("Default model", definition.model || "No model"),
        kvBox("Fallback model", definition.fallback_model || "No fallback"),
        kvBox("Tools", (definition.tools || []).join(", ") || "none"),
        kvBox("Permissions", (definition.permissions || []).join(", ") || "none"),
        kvBox("Temperature", definition.temperature),
        kvBox("Max tokens", ui.fmt.int(definition.max_tokens)),
        kvBox("Budget", definition.budget_usd === null || definition.budget_usd === undefined
          ? "unbounded" : money(definition.budget_usd)),
        kvBox("Timeout", definition.timeout_s ? definition.timeout_s + "s" : "none"),
        kvBox("Retries", definition.retries),
        kvBox("Version", "v" + definition.version),
        kvBox("Average stage", stats.avg_duration_ms
          ? Math.round(stats.avg_duration_ms) + " ms" : "—"),
        kvBox("Last active", stats.last_run_at ? stampOf(stats.last_run_at) : "never")));
  }

  /* Version history — GET /definitions/{id}/versions. Every edit freezes the
   * configuration it replaced, so the archive is complete; the working version
   * is listed too, marked as not yet frozen. */
  async function loadAgentVersions(agentId) {
    state.agentVersions = null;
    state.agentVersionsFor = agentId;
    render();
    try {
      const payload = await req("GET",
        "/definitions/" + encodeURIComponent(agentId) + "/versions");
      if (state.agentVersionsFor !== agentId) return;
      state.agentVersions = payload.versions || [];
    } catch (err) {
      if (aborted(err)) return;
      state.agentVersions = [];
      fail("Loading the version history", err);
    }
    render();
  }

  function renderAgentVersions(definition) {
    const versions = state.agentVersionsFor === definition.id ? state.agentVersions : null;
    const host = el("div.asm-pane");
    host.appendChild(el("div", { style: "display:flex;gap:6px;margin-bottom:8px" },
      ui.button("Freeze current as v" + definition.version, {
        small: true, primary: true,
        onClick: function () { freezeAgentVersion(definition); },
      })));
    if (versions === null) {
      host.appendChild(el("div.asm-empty", { text: "Loading the version history…" }));
      return host;
    }
    if (!versions.length) {
      host.appendChild(el("div.asm-empty", {
        text: "No versions recorded yet. The next edit freezes the configuration it replaces.",
      }));
      return host;
    }
    versions.forEach(function (entry) {
      const row = el("div.asm-box",
        el("label", {
          text: "v" + entry.version + (entry.current ? " · CURRENT" : "")
            + (entry.frozen ? "" : " · working copy, not frozen"),
        }),
        el("div.dim.mini", {
          text: (entry.note || "no note") + " · " + (entry.model || "no model")
            + " on " + (entry.provider || "—") + " · " + entry.tools + " tool(s) · "
            + entry.prompt_chars + " prompt chars · "
            + (entry.created_at ? stampOf(entry.created_at) : "—"),
        }));
      if (entry.frozen && !entry.current) {
        row.appendChild(el("div", { style: "margin-top:6px" },
          ui.button("Restore v" + entry.version, {
            small: true,
            onClick: function () { restoreAgentVersion(definition, entry.version); },
          })));
      }
      host.appendChild(row);
    });
    host.appendChild(el("div.dim.mini", { style: "margin-top:8px",
      text: "Restoring is itself recorded as an edit: the state it replaces is frozen first, "
        + "so the history is never rewound." }));
    return host;
  }

  async function freezeAgentVersion(definition) {
    const note = await askText("Freeze version",
      "Note for v" + definition.version, "", "what this version changed");
    if (note === null) return;
    try {
      await req("POST", "/definitions/" + encodeURIComponent(definition.id) + "/versions",
        { note: note || null });
      ctx.toast("Froze " + definition.id + " v" + definition.version, "green");
      await loadAgentVersions(definition.id);
    } catch (err) {
      fail("Freezing the version", err);
    }
  }

  async function restoreAgentVersion(definition, version) {
    const ok = await askConfirm("Restore version",
      "Make v" + version + " of " + definition.id + " current? The configuration it replaces "
      + "is frozen first, so nothing is lost.", "Restore v" + version);
    if (!ok) return;
    try {
      await req("POST", "/definitions/" + encodeURIComponent(definition.id)
        + "/versions/" + version + "/restore", {});
      ctx.toast("Restored " + definition.id + " to v" + version, "green");
      await reloadView();
      await loadAgentVersions(definition.id);
    } catch (err) {
      fail("Restoring the version", err);
    }
  }

  function kvBox(label, value) {
    return el("div.asm-kv", el("span", { text: label }), el("span", { text: String(value) }));
  }

  /** A one-line "field=value" description of a preset, for the chip's tooltip. */
  function presetSummary(values) {
    const parts = Object.keys(values || {}).map(function (key) {
      const value = values[key];
      return key + " = " + (value && typeof value === "object"
        ? JSON.stringify(value) : String(value));
    });
    return parts.length ? parts.join(" · ") : "no overrides";
  }

  /**
   * The value a field takes once the selected preset is layered over the
   * stored definition — the reference's `vals` merge, with the presets coming
   * from the registry rather than from a table in the page.
   */
  function presetValue(overrides, key, fallback) {
    return Object.prototype.hasOwnProperty.call(overrides, key) ? overrides[key] : fallback;
  }

  /** Render an optional number as an input value ("" when unset). */
  function numText(value) {
    return value === null || value === undefined ? "" : String(value);
  }

  function renderAgentConfig(definition) {
    const form = {};
    // PRESET chips are the definition's own named effort dials. Selecting one
    // rewrites the form; nothing is stored until "Save agent" is pressed, so
    // the chip can never disagree with what the registry holds.
    const presets = definition.presets || {};
    const presetNames = Object.keys(presets);
    const activePreset = state.studioPreset && presets[state.studioPreset]
      ? state.studioPreset : null;
    const overrides = activePreset ? (presets[activePreset] || {}) : {};
    const vals = {
      max_tokens: presetValue(overrides, "max_tokens", definition.max_tokens),
      temperature: presetValue(overrides, "temperature", definition.temperature),
      budget_usd: presetValue(overrides, "budget_usd", definition.budget_usd),
      timeout_s: presetValue(overrides, "timeout_s", definition.timeout_s),
      retries: presetValue(overrides, "retries", definition.retries),
      scope: presetValue(overrides, "scope", definition.scope || {}),
    };
    const models = (state.models && state.models.models) || [];
    const modelPicker = el("select");
    modelPicker.appendChild(el("option", { value: "", text: "No model", selected: !definition.model }));
    models.forEach(function (model) {
      modelPicker.appendChild(el("option", {
        value: model.provider + "/" + model.id,
        text: model.provider + " · " + model.label,
        selected: model.id === definition.model,
      }));
    });
    form.model = modelPicker;

    const pane = el("div.panel", { id: "cfgform", style: "margin-bottom:12px" },
      el("div", { style: "display:flex;align-items:center;gap:10px;margin-bottom:10px" },
        el("span.hi", { style: "font-family:system-ui;font-size:15px;font-weight:600",
          text: definition.name }),
        el("span.dim.mini", { text: definition.id }),
        presetNames.length
          ? el("span.dim.mini", { style: "margin-left:auto", text: "PRESET" })
          : null,
        presetNames.map(function (name) {
          return el("span.chip.mini" + (name === activePreset ? ".amb" : ""), {
            style: "cursor:pointer",
            dataset: { preset: name },
            title: presetSummary(presets[name]),
            on: {
              click: function () {
                state.studioPreset = name === activePreset ? null : name;
                render();
              },
            },
          }, name);
        }),
        el("span.dim.mini", {
          style: presetNames.length ? "" : "margin-left:auto",
          text: "v" + definition.version,
        })),
      el("div.grid.g3", { style: "gap:10px" },
        el("div", el("label", { text: "MODEL" }), modelPicker),
        el("div", el("label", { text: "MAX TOKENS" }),
          (form.maxTokens = el("input", { value: String(vals.max_tokens) }))),
        el("div", el("label", { text: "TEMPERATURE" }),
          (form.temperature = el("input", { value: String(vals.temperature) }))),
        el("div", el("label", { text: "BUDGET (USD)" }),
          (form.budget = el("input", {
            value: numText(vals.budget_usd),
            placeholder: "unbounded",
          }))),
        el("div", el("label", { text: "TIMEOUT (S)" }),
          (form.timeout = el("input", {
            value: numText(vals.timeout_s),
            placeholder: "none",
          }))),
        el("div", el("label", { text: "RETRIES" }),
          (form.retries = el("input", { value: String(vals.retries) }))),
        el("div", { style: "grid-column:1/-1" },
          el("label", { text: "SCOPE (JSON)" }),
          (form.scope = el("textarea", {
            rows: "3",
            text: JSON.stringify(vals.scope || {}, null, 2),
          }))),
        el("div", { style: "grid-column:1/-1" },
          el("label", { text: "TOOLS (COMMA-SEPARATED)" }),
          (form.tools = el("input", { value: (definition.tools || []).join(", ") }))),
        el("div", { style: "grid-column:1/-1" },
          el("label", { text: "PERMISSIONS (COMMA-SEPARATED)" }),
          (form.permissions = el("input", { value: (definition.permissions || []).join(", ") }))),
        el("div", { style: "grid-column:1/-1" },
          el("label", { text: "SYSTEM INSTRUCTIONS" }),
          (form.prompt = el("textarea", { text: definition.system_prompt, rows: "6" }))),
        el("div", { style: "grid-column:1/-1" },
          el("label", { text: "NOTES" }),
          (form.notes = el("input", { value: definition.notes || "" })))),
      el("div", { style: "display:flex;gap:18px;margin-top:10px;align-items:center" },
        el("label", { style: "display:flex;gap:6px;align-items:center" },
          (form.enabled = el("input", { type: "checkbox", checked: !!definition.enabled })),
          el("span.mid", { text: "Enabled" }))),
      el("div.dim.mini", {
        style: "border-top:1px solid var(--line);margin-top:10px;padding-top:8px",
        text: definition.locked
          ? "This definition is locked: it is the mandatory human-approval authority."
          : activePreset
            ? "Preset " + activePreset + " is applied to this form but not yet stored. "
              + "Save the agent to persist it; the chip clears on save."
            : "Saving bumps the definition's version. Runs already recorded keep the version "
              + "that produced them.",
      }),
      el("div", { style: "display:flex;gap:6px;margin-top:8px" },
        ui.button("Save agent", {
          primary: true, disabled: !!definition.locked,
          onClick: function () { saveAgent(definition, form); },
        })));
    return pane;
  }

  function renderAgentRuns(definition) {
    const rows = [];
    state.runs.forEach(function (run) {
      (run.stages || []).forEach(function (record) {
        if (record.agent_id === definition.id) rows.push({ run: run, record: record });
      });
    });
    if (!rows.length) {
      return el("div.asm-empty", {
        text: "This agent has not executed a stage in any recorded run.",
      });
    }
    return ui.table({
      columns: [
        { label: "Run", render: function (row) { return row.run.id; } },
        { label: "Stage", render: function (row) { return row.record.name; } },
        { label: "Status", render: function (row) {
          return String(row.record.status).replace(/_/g, " ");
        } },
        { label: "Model", render: function (row) { return row.record.model || "—"; } },
        { label: "Cost", num: true, render: function (row) {
          return money(row.record.cost_usd);
        } },
        { label: "Started", render: function (row) { return stampOf(row.record.started_at); } },
      ],
      rows: rows,
    });
  }

  async function newAgent() {
    const models = (state.models && state.models.models) || [];
    if (!models.length) {
      ctx.toast("No provider is available, so a model-calling agent cannot be created", "red");
      return;
    }
    const answers = await askForm("New agent", [
      { name: "name", label: "Name", value: "New research agent" },
      { name: "role", label: "Role", value: "Research",
        placeholder: "what this agent is for" },
      { name: "prompt", label: "System instructions", multiline: true,
        value: "You are a research agent." },
    ]);
    if (!answers || !answers.name || !answers.role || !answers.prompt) return;
    try {
      const created = await req("POST", "/definitions", {
        name: answers.name, role: answers.role, system_prompt: answers.prompt,
        provider: models[0].provider, model: models[0].id,
      });
      state.studioAgentId = created.id;
      ctx.toast("Agent " + created.name + " created", "green");
      await refreshShared();
      await reloadView();
    } catch (err) {
      fail("Creating the agent", err);
    }
  }

  async function saveAgent(definition, form) {
    const modelValue = form.model.value;
    const slash = modelValue.indexOf("/");
    const body = {
      system_prompt: form.prompt.value,
      max_tokens: Number(form.maxTokens.value) || definition.max_tokens,
      temperature: Number(form.temperature.value) || 0,
      retries: Number(form.retries.value) || 0,
      tools: form.tools.value.split(",").map(function (t) { return t.trim(); }).filter(Boolean),
      permissions: form.permissions.value.split(",").map(function (p) { return p.trim(); })
        .filter(Boolean),
      notes: form.notes.value,
      enabled: form.enabled.checked,
    };
    if (form.scope) {
      const raw = form.scope.value.trim();
      try {
        const parsed = raw ? JSON.parse(raw) : {};
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("scope must be a JSON object");
        }
        body.scope = parsed;
      } catch (err) {
        ctx.toast("Scope is not a JSON object: " + err.message, "red");
        return;
      }
    }
    if (slash > 0) {
      body.provider = modelValue.slice(0, slash);
      body.model = modelValue.slice(slash + 1);
    } else {
      // "No model" clears the model but keeps the provider: the registry only
      // lets a human-review agent have none at all.
      body.model = "";
    }
    const budget = form.budget.value.trim();
    const timeout = form.timeout.value.trim();
    if (budget) body.budget_usd = Number(budget);
    if (timeout) body.timeout_s = Number(timeout);
    try {
      await req("PUT", "/definitions/" + encodeURIComponent(definition.id), body);
      // The preset is now the stored state, so the chip has nothing left to
      // preview: clear it rather than leaving it highlighted over saved values.
      state.studioPreset = null;
      ctx.toast("Agent " + definition.name + " saved", "green");
      await refreshShared();
      await reloadView();
    } catch (err) {
      fail("Saving the agent", err);
    }
  }

  async function duplicateAgent(agentId) {
    try {
      const copy = await req("POST",
        "/definitions/" + encodeURIComponent(agentId) + "/duplicate", {});
      state.studioAgentId = copy.id;
      ctx.toast("Duplicated as " + copy.name, "green");
      await refreshShared();
      await reloadView();
    } catch (err) {
      fail("Duplicating the agent", err);
    }
  }

  async function deleteAgent(agentId) {
    const ok = await askConfirm("Delete agent",
      "Delete agent " + agentId + "? Pipeline stages assigned to it will fail validation until "
      + "they are reassigned.", "Delete agent");
    if (!ok) return;
    try {
      await req("DELETE", "/definitions/" + encodeURIComponent(agentId));
      state.studioAgentId = null;
      ctx.toast("Agent deleted", "green");
      await refreshShared();
      await reloadView();
    } catch (err) {
      fail("Deleting the agent", err);
    }
  }

  // ------------------------------------------------------------- audit log

  async function loadAudit() {
    const query = { limit: 300 };
    if (state.auditActor) query.actor = state.auditActor;
    if (state.auditAction) query.action = state.auditAction;
    if (state.auditQuery.trim()) query.q = state.auditQuery.trim();
    const runId = state.auditRunId || (ctx.params && ctx.params.run);
    if (runId) query.run_id = runId;
    state.audit = await req("GET", "/audit", query);
  }

  function renderAudit() {
    const payload = state.audit || { records: [], actors: [], actions: [] };
    const records = payload.records || [];
    const runId = state.auditRunId || (ctx.params && ctx.params.run);

    const toolbar = el("div.v5-toolbar",
      el("label", el("span", { text: "Actor" }),
        select(["", ...(payload.actors || [])], state.auditActor, function (value) {
          state.auditActor = value; reloadView();
        }, { auditActor: "" }, "All actors")),
      el("label", el("span", { text: "Action" }),
        select(["", ...(payload.actions || [])], state.auditAction, function (value) {
          state.auditAction = value; reloadView();
        }, { auditAction: "" }, "All actions")),
      el("label", el("span", { text: "Search" }),
        el("input.input", {
          type: "search", value: state.auditQuery, placeholder: "Actor, action, target…",
          on: {
            change: function (e) { state.auditQuery = e.target.value; reloadView(); },
            keydown: function (e) {
              if (e.key === "Enter") { state.auditQuery = e.target.value; reloadView(); }
            },
          },
        })),
      runId
        ? ui.button("Clear run filter (" + runId + ")", {
          onClick: function () { state.auditRunId = null; go("audit"); },
        })
        : null,
      ui.button("Export CSV", {
        onClick: function () { exportAudit("csv", records); },
      }),
      ui.button("Export JSON", {
        onClick: function () { exportAudit("json", records); },
      }),
      el("span.v5-toolbar-count", { text: records.length + " of " + payload.total + " events" }));

    const table = el("table.data-table.v5-audit-table",
      el("thead", el("tr",
        el("th", { text: "Time" }), el("th", { text: "Actor" }),
        el("th", { text: "Action" }), el("th", { text: "Object" }),
        el("th", { text: "Result" }))));
    const body = el("tbody");
    if (!records.length) {
      body.appendChild(el("tr", el("td", { colspan: "5", class: "muted",
        text: "No audit record matches." })));
    }
    records.forEach(function (record) {
      const target = record.target || record.run_id;
      body.appendChild(el("tr",
        el("td", { text: clockOf(record.ts) }),
        el("td", { text: record.actor + " (" + record.actor_kind + ")" }),
        el("td", { text: record.action }),
        el("td", target
          ? el("button", {
            type: "button", dataset: { auditObject: target }, text: target,
            on: { click: function () { openAuditObject(record); } },
          })
          : "—"),
        el("td", record.result
          ? stateBadge(record.result, modifierOf(record.result))
          : el("span.dim", { text: "—" }))));
    });
    table.appendChild(body);

    return [toolbar, ui.panel({
      title: "Immutable audit events",
      badge: payload.total + " events",
      body: el("div.v5-table-scroll", table),
    })];
  }

  /**
   * Write the audit rows currently on screen to a file.
   *
   * Exactly the filtered set is exported — what you see is what you get — and
   * the JSON form keeps the whole record, including the fields the five-column
   * table has no room for.
   * @param {"csv"|"json"} format
   * @param {object[]} records
   */
  function exportAudit(format, records) {
    if (!records.length) {
      ctx.toast("Nothing to export: no audit record matches the current filter", "yellow");
      return;
    }
    const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    if (format === "json") {
      download("tradinglib-audit-" + stamp + ".json", JSON.stringify({
        exported_at: new Date().toISOString(),
        filters: {
          actor: state.auditActor || null,
          action: state.auditAction || null,
          query: state.auditQuery || null,
          run_id: state.auditRunId || null,
        },
        count: records.length,
        records: records,
      }, null, 2));
      ctx.toast("Exported " + records.length + " audit events as JSON", "green");
      return;
    }
    const columns = ["ts", "actor", "actor_kind", "action", "target", "run_id", "result", "detail"];
    const escape = function (value) {
      if (value === null || value === undefined) return "";
      const text = typeof value === "object" ? JSON.stringify(value) : String(value);
      return /[",\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
    };
    const lines = [columns.join(",")];
    records.forEach(function (record) {
      lines.push(columns.map(function (key) { return escape(record[key]); }).join(","));
    });
    download("tradinglib-audit-" + stamp + ".csv", lines.join("\n") + "\n", "text/csv");
    ctx.toast("Exported " + records.length + " audit events as CSV", "green");
  }

  function openAuditObject(record) {
    if (record.run_id) {
      state.runId = record.run_id;
      go("pipeline", { run: record.run_id });
      return;
    }
    if (String(record.target || "").indexOf("APV-") === 0) {
      state.approvalId = record.target;
      go("approval", { id: record.target });
      return;
    }
    ctx.toast("Object " + record.target + " has no page of its own", "cyan");
  }

  // ---------------------------------------------------------- configuration

  async function loadConfiguration() {
    state.config = await req("GET", "/config", { refresh: true });
    state.models = await req("GET", "/models");
    const pipelines = await req("GET", "/pipelines");
    state.pipelines = pipelines.pipelines || [];
  }

  function renderConfiguration() {
    const config = state.config || {};
    const budgets = config.budgets || {};
    const spend = config.spend || {};
    const withApproval = state.pipelines.filter(function (pipeline) {
      return (pipeline.stages || []).some(function (stage) {
        return stage.kind === "human_approval";
      });
    }).length;

    const policy = config.policy || {};
    const choices = config.policy_choices || {};
    const autonomyChoices = choices.autonomy || [];
    const notifyChoices = choices.notifications || {};

    const autonomy = ui.panel({
      title: "Research autonomy",
      badge: "human boundary enforced",
      badgeTone: "yellow",
      body: [
        configRow("Create findings and hypotheses",
          policySelect("research_generation", policy.research_generation, autonomyChoices)),
        configRow("Run sandbox experiments",
          policySelect("sandbox_experiments", policy.sandbox_experiments, autonomyChoices)),
        configRow("Candidate disposition", (function () {
          const node = el("select.select", { disabled: true });
          node.appendChild(el("option", { text: "Mandatory human approval" }));
          return node;
        })()),
        configRow("Pipelines with an approval boundary",
          el("span", { text: withApproval + " of " + state.pipelines.length })),
        configRow("Agents that may call a model",
          el("span", { text: state.definitions.filter(function (d) {
            return d.calls_a_model;
          }).length + " of " + state.definitions.length })),
        el("div.note", {
          text: "A human-approval stage is locked: it cannot be switched off, reassigned or "
            + "given a model. A run that reaches it stops until a person decides.",
        }),
      ],
    });

    const form = {};
    const budgetPanel = ui.panel({
      title: "Budgets and retention",
      badge: config.budget_source === "saved" ? "saved in this workspace" : "from configuration",
      body: [
        configRow("Per-run budget", (form.perRun = el("input.input", {
          value: String(budgets.per_run_usd), dataset: { config: "per_run" },
        }))),
        configRow("Daily research budget", (form.daily = el("input.input", {
          value: String(budgets.daily_usd), dataset: { config: "daily" },
        }))),
        configRow("Monthly budget", (form.monthly = el("input.input", {
          value: String(budgets.monthly_usd), dataset: { config: "monthly" },
        }))),
        configRow("Maximum concurrent runs", (form.concurrency = el("input.input", {
          value: String(policy.max_concurrent_runs === undefined ? "" : policy.max_concurrent_runs),
          dataset: { config: "concurrency" },
        }))),
        configRow("Artifact retention",
          policySelect("artifact_retention", policy.artifact_retention, choices.retention || [])),
        el("div", { style: "padding:0 11px 11px" },
          ui.button("Save budgets", {
            primary: true, onClick: function () { saveBudgets(form); },
          })),
        el("div.note", { text: "Saved to " + (config.budget_file || "the workspace data directory")
          + " and enforced immediately: a stage whose projected cost breaks a budget is refused. "
          + "Retention decides how long a finished run keeps its artifacts." }),
      ],
    });

    const notifications = ui.panel({
      title: "Notifications",
      badge: "workspace policy",
      body: [
        configRow("Approval requests", policySelect("notify_approval_requests",
          policy.notify_approval_requests, notifyChoices.notify_approval_requests || [])),
        configRow("Failed validation", policySelect("notify_failed_validation",
          policy.notify_failed_validation, notifyChoices.notify_failed_validation || [])),
        configRow("Stale evidence", policySelect("notify_stale_evidence",
          policy.notify_stale_evidence, notifyChoices.notify_stale_evidence || [])),
        el("div.note", {
          text: "Notification policy is stored with the workspace and read by the run "
            + "publisher; it decides which events raise a toast rather than only landing "
            + "in the attention queue.",
        }),
      ],
    });

    const spendPanel = ui.panel({
      title: "Spend",
      badge: money(spend.used) + " today",
      body: [
        ui.healthRow("Today", money(spend.used) + " of "
          + money(spend.budget)),
        ui.healthRow("This month", spend.monthly
          ? money(spend.monthly.used) + " of "
            + money(spend.monthly.budget)
          : "—"),
        ui.healthRow("All time", money(spend.total)),
        ui.healthRow("Charges recorded", ui.fmt.int(spend.entries)),
        ui.healthRow("Charges with no price", ui.fmt.int(config.unpriced_charges),
          { tone: config.unpriced_charges ? "yellow" : "" }),
        ui.healthRow("Runs", (config.runs || {}).total + " total · "
          + (config.runs || {}).active + " active · "
          + (config.runs || {}).pending_approvals + " awaiting approval"),
      ],
    });

    const providers = ui.panel({
      title: "Providers",
      badge: (config.providers || []).filter(function (p) { return p.available; }).length
        + " available",
      body: (config.providers || []).map(function (provider) {
        return el("div.list-row",
          el("b", { class: provider.available ? "green" : "red",
            text: provider.provider + " · " + (provider.available ? "available" : "unavailable") }),
          el("span", { text: provider.detail + " · " + provider.model_count + " models"
            + (provider.verified ? " · verified" : " · not verified by a real request") }));
      }),
    });

    const keys = config.keys || {};
    const keyPanel = ui.panel({
      title: "Credentials",
      badge: "set them in Settings",
      body: Object.keys(keys).map(function (name) {
        const entry = keys[name];
        return ui.healthRow(name,
          (entry.configured ? "configured" : "not configured")
          + (entry.key ? " · " + entry.key : ""),
          { tone: entry.configured ? "green" : "" });
      }),
    });

    const auto = config.autopipeline || {};
    const autoPanel = ui.panel({
      title: "AutoPipeline",
      badge: auto.running ? "timers running" : "timers stopped",
      badgeTone: auto.running ? "green" : "yellow",
      body: [
        ui.healthRow("Schedules", (auto.total || 0) + " total · " + (auto.enabled || 0) + " enabled"),
        ui.healthRow("Next fire", auto.next_run_at ? stampOf(auto.next_run_at) : "nothing scheduled"),
      ],
    });

    return el("div.v5-config-grid",
      autonomy, budgetPanel, notifications, spendPanel, providers, keyPanel, autoPanel);
  }

  function configRow(label, control) {
    return el("div.v5-config-row", el("span", { text: label }), control);
  }

  /**
   * A `<select>` bound to one workspace-policy field.
   *
   * `choices` is the {value,label} list the backend publishes in
   * `config.policy_choices`, so the options can never drift from what
   * `PUT /api/agents/config` will accept. Choosing an option writes it through
   * immediately — there is no separate save for policy, and the toast reports
   * what the server actually stored.
   * @param {string} field
   * @param {string} current
   * @param {{value: string, label: string}[]} choices
   */
  function policySelect(field, current, choices) {
    const node = el("select.select", {
      dataset: { config: field },
      on: { change: function (event) { savePolicy(field, event.target.value, node); } },
    });
    if (!choices.length) {
      node.disabled = true;
      node.appendChild(el("option", { text: current || "—" }));
      node.title = "the backend published no choices for " + field;
      return node;
    }
    choices.forEach(function (choice) {
      node.appendChild(el("option", {
        value: choice.value, text: choice.label, selected: choice.value === current,
      }));
    });
    return node;
  }

  /** Write one policy field through `PUT /config` and report what stuck. */
  async function savePolicy(field, value, node) {
    const body = {};
    body[field] = value;
    const previous = node ? node.value : value;
    try {
      state.config = await req("PUT", "/config", body);
      const stored = (state.config.policy || {})[field];
      ctx.toast(field.replace(/_/g, " ") + " set to " + stored, "green");
      render();
    } catch (err) {
      if (node) node.value = previous;
      fail("Saving the " + field.replace(/_/g, " ") + " policy", err);
    }
  }

  async function saveBudgets(form) {
    const body = {
      per_run_usd: Number(form.perRun.value),
      daily_usd: Number(form.daily.value),
      monthly_usd: Number(form.monthly.value),
    };
    if ([body.per_run_usd, body.daily_usd, body.monthly_usd].some(function (v) {
      return isNaN(v) || v < 0;
    })) {
      ctx.toast("Budgets must be non-negative numbers", "red");
      return;
    }
    if (form.concurrency && form.concurrency.value !== "") {
      const concurrency = Number(form.concurrency.value);
      if (!isFinite(concurrency) || concurrency < 1 || concurrency !== Math.floor(concurrency)) {
        ctx.toast("Maximum concurrent runs must be a whole number of at least 1", "red");
        return;
      }
      body.max_concurrent_runs = concurrency;
    }
    try {
      state.config = await req("PUT", "/config", body);
      ctx.toast("Budgets saved and enforced", "green");
      render();
    } catch (err) {
      fail("Saving the budgets", err);
    }
  }

  // ------------------------------------------------------- small components

  function kvPair(label, value) {
    return el("div", el("dt", { text: label }), el("dd", { text: String(value) }));
  }

  // -------------------------------------------------------------- dialogs

  /**
   * Ask the person something in the page, using REFERENCE's `<dialog
   * class="dialog">` markup (head / body / footer) rather than a browser
   * `prompt()`/`confirm()` box.
   *
   * Native dialogs are unstyled, cannot be inspected the way the rest of the
   * workspace can, and freeze the page while they are open — including the
   * WebSocket repaint loop. This one is part of the document, so it looks like
   * the rest of the console and a running pipeline keeps streaming behind it.
   *
   * @param {{title: string, body?: any, confirmLabel?: string,
   *          cancelLabel?: string, danger?: boolean,
   *          value?: () => any}} options
   * @returns {Promise<any|null>} `options.value()` on confirm (or `true` when
   *   no value getter was given), and `null` when the person cancels.
   */
  function ask(options) {
    return new Promise(function (resolve) {
      const opts = options || {};
      const dialog = el("dialog.dialog");
      const confirmButton = ui.button(opts.confirmLabel || "Confirm", { primary: true });
      if (opts.danger) confirmButton.classList.add("danger");
      const cancelButton = ui.button(opts.cancelLabel || "Cancel");
      const closeButton = el("button.dialog-close", {
        type: "button", "aria-label": "Close", text: "×",
      });

      let settled = false;
      function finish(value) {
        if (settled) return;
        settled = true;
        try {
          dialog.close();
        } catch (err) {
          void err;
        }
        dialog.remove();
        resolve(value);
      }

      const form = el("form", { method: "dialog" },
        el("div.dialog-head", el("strong", { text: opts.title }), closeButton),
        el("div.dialog-body", opts.body || null),
        el("div.dialog-footer", cancelButton, confirmButton));
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        finish(opts.value ? opts.value() : true);
      });
      confirmButton.addEventListener("click", function (event) {
        event.preventDefault();
        finish(opts.value ? opts.value() : true);
      });
      cancelButton.addEventListener("click", function (event) {
        event.preventDefault();
        finish(null);
      });
      closeButton.addEventListener("click", function () { finish(null); });
      // Escape and the backdrop both mean "no".
      dialog.addEventListener("cancel", function (event) {
        event.preventDefault();
        finish(null);
      });
      dialog.addEventListener("click", function (event) {
        if (event.target === dialog) finish(null);
      });

      dialog.appendChild(form);
      // Hosted on the always-visible overlay root so it is not hidden with the
      // workspace, and torn down with the page if the route changes.
      const host = document.getElementById("overlay-root") || document.body;
      host.appendChild(dialog);
      teardown.push(function () { finish(null); });
      dialog.showModal();
      const firstInput = dialog.querySelector("input, textarea, select");
      if (firstInput) firstInput.focus();
      else confirmButton.focus();
    });
  }

  /**
   * Ask for one line of text.
   * @returns {Promise<string|null>} the trimmed value, or null when cancelled.
   */
  function askText(title, label, value, placeholder) {
    const input = el("input.input", {
      value: value === undefined || value === null ? "" : String(value),
      placeholder: placeholder || "",
    });
    return ask({
      title: title,
      body: el("label.pipeline-field", el("span", { text: label }), input),
      confirmLabel: "OK",
      value: function () { return input.value.trim() || null; },
    });
  }

  /**
   * Ask the person to pick one option from a list.
   * @param {string} title
   * @param {string} note explanation shown above the picker
   * @param {{value: string, label: string}[]} options
   * @param {string} [current] option preselected
   * @returns {Promise<string|null>} the chosen value, or null when cancelled.
   */
  function askChoice(title, note, options, current) {
    const picker = el("select.select");
    options.forEach(function (option) {
      picker.appendChild(el("option", {
        value: option.value, text: option.label, selected: option.value === current,
      }));
    });
    return ask({
      title: title,
      body: el("div",
        el("p.v5-lead", { text: note }),
        el("label.pipeline-field", el("span", { text: "STAGE" }), picker)),
      confirmLabel: "OK",
      value: function () { return picker.value || null; },
    });
  }

  /**
   * Ask for several fields at once.
   * @param {string} title
   * @param {Array<{name: string, label: string, value?: string,
   *                multiline?: boolean, placeholder?: string}>} fields
   * @returns {Promise<Record<string,string>|null>}
   */
  function askForm(title, fields) {
    const inputs = {};
    const body = el("div");
    fields.forEach(function (spec) {
      const control = spec.multiline
        ? el("textarea", { text: spec.value || "", rows: "4" })
        : el("input.input", { value: spec.value || "", placeholder: spec.placeholder || "" });
      inputs[spec.name] = control;
      body.appendChild(el("label.pipeline-field", el("span", { text: spec.label }), control));
    });
    return ask({
      title: title,
      body: body,
      confirmLabel: "Create",
      value: function () {
        const out = {};
        let empty = true;
        Object.keys(inputs).forEach(function (name) {
          out[name] = inputs[name].value.trim();
          if (out[name]) empty = false;
        });
        return empty ? null : out;
      },
    });
  }

  /** Ask a yes/no question. @returns {Promise<boolean>} */
  function askConfirm(title, message, confirmLabel) {
    return ask({
      title: title,
      body: el("p", { style: "margin:0;font-size:12px;line-height:1.5", text: message }),
      confirmLabel: confirmLabel || "Delete",
      danger: true,
    }).then(function (value) { return value === true; });
  }

  function segmented(label, options, current, onPick) {
    const group = el("div.v5-segmented", { "aria-label": label });
    options.forEach(function (option) {
      group.appendChild(el("button", {
        type: "button",
        "aria-pressed": option === current ? "true" : "false",
        dataset: { inboxQueue: option },
        text: option,
        on: { click: function () { onPick(option); } },
      }));
    });
    return group;
  }

  function select(options, current, onPick, dataset, emptyLabel) {
    const node = el("select.select", {
      on: { change: function (e) { onPick(e.target.value); } },
    });
    Object.keys(dataset || {}).forEach(function (key) { node.dataset[key] = String(dataset[key]); });
    options.forEach(function (option) {
      node.appendChild(el("option", {
        value: option,
        text: option === "" ? (emptyLabel || "All") : option,
        selected: option === current,
      }));
    });
    return node;
  }

  // ------------------------------------------------------------------ live

  /** Subscribe to the `runs` and `approvals` channels for this mount. */
  function subscribeLive() {
    teardown.push(ctx.stream.listen("runs", onRunFrame));
    teardown.push(ctx.stream.listen("approvals", onApprovalFrame));
  }

  /** One frame from the `runs` channel. */
  function onRunFrame(payload) {
    if (!payload || payload.source !== "agents") return;
    const kind = payload.kind;
    if (kind === "log") {
      applyLiveLog(payload);
      return;
    }
    if (kind === "run") {
      state.liveLine = "Run " + payload.run_id + " · " + String(payload.phase).replace(/_/g, " ")
        + (payload.error ? " · " + payload.error : "");
      announce(state.liveLine);
      touchRun(payload);
      if (["complete", "failed", "killed", "awaiting_approval"].indexOf(payload.phase) >= 0) {
        refreshAfterRunChange(payload);
      }
      return;
    }
    if (kind === "stage") {
      state.liveLine = "Stage " + (payload.name || payload.stage_id) + " · "
        + String(payload.phase).replace(/_/g, " ")
        + (payload.cost_usd ? " · " + money(payload.cost_usd) : "");
      announce(state.liveLine);
      touchRun(payload);
      refreshAfterRunChange(payload);
    }
  }

  /** Append a streamed log line to the inspector without a full repaint. */
  function applyLiveLog(payload) {
    if (state.view !== "pipeline" || payload.run_id !== state.runId) return;
    const stage = selectedStage();
    if (state.inspectorTab !== "logs") return;
    if (payload.stage_id && stage && payload.stage_id !== stage.id) return;
    const box = ctx.root.querySelector("#acLogBox");
    if (!box) return;
    // The placeholder line goes as soon as there is a real one.
    if (box.dataset.placeholder !== "cleared" && box.children.length === 1
      && !box.children[0].dataset.live) {
      box.innerHTML = "";
      box.dataset.placeholder = "cleared";
    }
    const row = logRow(payload);
    row.dataset.live = "1";
    box.appendChild(row);
    box.scrollTop = box.scrollHeight;
  }

  /** Update the live status line shown above the flow panel. */
  function touchRun(payload) {
    const node = ctx.root.querySelector("#v5xLive");
    if (node) node.textContent = state.liveLine;
    if (payload.progress && state.run && state.run.id === payload.run_id) {
      state.run.progress = payload.progress;
    }
  }

  /** Reload the run-shaped parts of the page after a live state change. */
  let refreshTimer = 0;
  function refreshAfterRunChange(payload) {
    if (payload.run_id && !state.runId) state.runId = payload.run_id;
    if (refreshTimer) return;
    // Frames arrive in bursts; coalesce so a fast pipeline does not trigger a
    // reload per stage.
    refreshTimer = setTimeout(async function () {
      refreshTimer = 0;
      try {
        await refreshShared();
        if (["pipeline", "inbox", "approval", "audit"].indexOf(state.view) >= 0) {
          await loadView();
        } else {
          paintChrome();
        }
      } catch (err) {
        if (!aborted(err)) console.error("agent center: live refresh failed", err);
      }
    }, 400);
    teardown.push(function () {
      if (refreshTimer) {
        clearTimeout(refreshTimer);
        refreshTimer = 0;
      }
    });
  }

  /** One frame from the `approvals` channel. */
  function onApprovalFrame(payload) {
    if (!payload) return;
    if (typeof payload.pending === "number") {
      if (!state.approvals) state.approvals = { approvals: [], counts: {} };
      state.approvals.pending = payload.pending;
      paintCounts();
    }
    if (payload.phase === "requested" && payload.approval) {
      ctx.toast("Approval required: " + payload.approval.title, "yellow");
      announce("Approval " + payload.approval.id + " is waiting for a decision.");
    }
    if (["pipeline", "inbox", "approval"].indexOf(state.view) >= 0) {
      refreshAfterRunChange({ run_id: payload.approval && payload.approval.run_id });
    }
  }

  /** Reload the counters every view depends on. */
  async function refreshShared() {
    try {
      const [config, definitions] = await Promise.all([
        req("GET", "/config"),
        req("GET", "/definitions", { include_stats: false }),
      ]);
      state.config = config;
      state.definitions = definitions.agents || [];
      state.agentCounts = definitions.counts || null;
      paintCounts();
    } catch (err) {
      if (!aborted(err)) console.error("agent center: refreshing counts failed", err);
    }
  }

  // -------------------------------------------------------------- lifecycle

  /** Resolve ctx.sub (and its aliases) to one of the nine view keys. */
  function viewFor(sub) {
    if (!sub) return "inbox";
    const key = String(sub).toLowerCase();
    if (VIEWS.some(function (view) { return view.key === key; })) return key;
    if (ALIASES[key]) return ALIASES[key];
    return "inbox";
  }

  global.AlgoPages.register("agents", {
    // Every view key plus its readable aliases; the router rewrites anything
    // else to the default rather than leaving a dead hash on screen.
    subs: VIEWS.map(function (view) { return view.key; }).concat(Object.keys(ALIASES)),
    defaultSub: VIEWS[0].key,
    mount: function (pageCtx) {
      ctx = pageCtx;
      ui = pageCtx.ui;
      el = ui.el;
      teardown = [];
      state = blankState();
      state.view = viewFor(pageCtx.sub);
      applyParams();
      buildShell();
      render();
      subscribeLive();
      (async function () {
        try {
          await loadShared();
          state.loading = false;
          await loadView();
        } catch (err) {
          if (aborted(err)) return;
          state.loading = false;
          state.error = err;
          render();
        }
      })();
    },

    route: function (pageCtx) {
      ctx = pageCtx;
      const next = viewFor(pageCtx.sub);
      const changed = next !== state.view;
      state.view = next;
      applyParams();
      if (changed) state.error = null;
      render();
      loadView();
    },

    unmount: function () {
      teardown.forEach(function (fn) {
        try {
          fn();
        } catch (err) {
          console.warn("agent center: teardown failed", err);
        }
      });
      teardown = [];
      workspace = null;
    },
  });

  /** Read the deep-link query parameters this page understands. */
  function applyParams() {
    const params = ctx.params || {};
    if (params.run) state.runId = params.run;
    if (params.id && state.view === "approval") state.approvalId = params.id;
    if (params.id && state.view === "findings") state.findingId = params.id;
    if (params.id && state.view === "pipeline") state.pipelineId = params.id;
    if (params.agent && state.view === "agents") state.studioAgentId = params.agent;
    if (params.run && state.view === "audit") state.auditRunId = params.run;
    if (params.finding && state.view === "evidence") state.evidenceCentered = params.finding;
    if (params.node && state.view === "evidence") state.evidenceNodeId = params.node;
  }
})(window);
