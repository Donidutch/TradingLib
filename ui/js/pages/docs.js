/* TradingLib — Documentation workspace.
 *
 * The REFERENCE's Documentation page, unchanged: a page-head plus the two-panel
 * `.data-grid` — "Methodology" and "Product guide" — carrying the same five
 * entries with the same wording. The REFERENCE rows are inert; here each one
 * opens the article it names in a panel below the grid (SPEC section 1:
 * reference dead-ends are fixed), and the article text describes what TradingLib
 * actually does — the gates the validation engine really applies, the shortcuts
 * this build really binds, the approval gate the runner really blocks on.
 *
 * Docs are static text by design (SPEC section 6). The only live values on the
 * page are the build version and the keyboard shortcuts, which are read from
 * /api/health and from the shell rather than retyped here.
 */
(function (global) {
  "use strict";

  const STYLE_ID = "tradinglib-docs-style";
  const STYLE_CSS = [
    "#view-docs .doc-article{margin-top:11px}",
    "#view-docs .list-row{cursor:pointer}",
    "#view-docs .list-row:hover{background:var(--panel2)}",
    "#view-docs .list-row[aria-selected=\"true\"]{background:#101d33;box-shadow:inset 2px 0 var(--blue)}",
    "#view-docs .doc-article{padding:16px 18px;line-height:1.65;font-size:12px}",
    "#view-docs .doc-article h2{font-size:15px;margin-bottom:4px}",
    "#view-docs .doc-article h3{font-size:12px;margin:16px 0 6px;color:var(--text);",
    "  letter-spacing:.04em;text-transform:uppercase}",
    "#view-docs .doc-article p{margin:8px 0;color:var(--muted)}",
    "#view-docs .doc-article ul{margin:8px 0 8px 18px;color:var(--muted)}",
    "#view-docs .doc-article li{margin:5px 0}",
    "#view-docs .doc-article code{font-family:ui-monospace,monospace;color:var(--cyan)}",
    "#view-docs .doc-lede{color:var(--text)}",
    "#view-docs .doc-meta{margin-top:18px;padding-top:10px;border-top:1px solid var(--line2);",
    "  color:var(--faint);font-size:11px}",
  ].join("\n");

  /**
   * The REFERENCE's five entries, each with the article it opens.
   * `body` is a list of blocks: ["h3"|"p"|"ul", content].
   */
  const ARTICLES = [
    {
      id: "hypotheses",
      group: "Methodology",
      lead: "Hypotheses and experiments",
      detail: "How evidence becomes reproducible research.",
      title: "Hypotheses and experiments",
      lede: "Research in TradingLib is a chain of records, not a notebook. Every claim starts as a"
        + " hypothesis, is tested by experiments, and each experiment run is stored — including"
        + " the ones that failed.",
      body: [
        ["h3", "The chain"],
        ["ul", [
          "A hypothesis states a mechanism and the condition that would falsify it. It lives in"
            + " the Strategy Lab and carries a status (draft, testing, supported, rejected).",
          "An experiment is a concrete test of one hypothesis: a symbol set, a date range, a"
            + " signal specification and the parameters being swept.",
          "Every run of an experiment is persisted with its parameters and its result, so the"
            + " number of trials behind a finding is a count, not an estimate.",
          "Evidence can be attached to a hypothesis; the record keeps what supported it and"
            + " what contradicted it.",
        ]],
        ["h3", "Why every trial is logged"],
        ["p", "The datastrip's TRIALS LOGGED figure counts the experiment and backtest runs in"
          + " the database. It exists so that the deflated Sharpe ratio in validation has a real"
          + " trial count to deflate by: a strategy selected from many attempts must clear a"
          + " higher bar than one tested once, and that only works if the attempts are counted"
          + " honestly."],
        ["h3", "Where to look"],
        ["p", "Strategy Lab → Hypotheses and Experiments. The API behind them is"
          + " /api/lab/hypotheses and /api/lab/experiments."],
      ],
    },
    {
      id: "backtests",
      group: "Methodology",
      lead: "Backtest integrity",
      detail: "Costs, leakage, benchmark, and OOS rules.",
      title: "Backtest integrity",
      lede: "A backtest is only useful if it could not have known the future and did not trade"
        + " for free. The backtester enforces both, and reports what it assumed.",
      body: [
        ["h3", "Point-in-time data"],
        ["ul", [
          "Universes store each member's added and removed dates, so a run over 2018 uses the"
            + " membership of 2018 — including companies that were later delisted or acquired."
            + " Market & Data → Universes shows exactly which names that adds back.",
          "Snapshots freeze a membership on a date. When a snapshot exists for the as-of date it"
            + " is used instead of reconstructing from membership dates, and the UI says which"
            + " of the two you are looking at.",
          "Fundamentals are stored as reported; where a vendor only publishes restated values"
            + " the data-quality page reports that rather than pretending they are point-in-time.",
        ]],
        ["h3", "Costs"],
        ["p", "A run applies commission, spread and slippage assumptions and reports them with"
          + " the result. A backtest with no cost model is not comparable to one with costs, so"
          + " the assumptions travel with the record instead of living in someone's memory."],
        ["h3", "Benchmark and out-of-sample"],
        ["ul", [
          "Every backtest carries a benchmark (SPY by default; change it in Settings → Research"
            + " defaults) and reports excess return against it, because a strategy that trails"
            + " buy-and-hold is not a strategy.",
          "The in-sample/out-of-sample split is part of the run configuration, and validation"
            + " gates read the out-of-sample statistics, never the in-sample ones.",
        ]],
        ["h3", "Where to look"],
        ["p", "Strategy Lab → Backtests. The equity curve, the benchmark line and the run's"
          + " statistics come from /api/lab/backtests/{id}/results."],
      ],
    },
    {
      id: "validation",
      group: "Methodology",
      lead: "Validation gates",
      detail: "Promotion requirements and statistical tests.",
      title: "Validation gates",
      lede: "Promotion is a decision with a checklist. The gate suite is evaluated against a"
        + " finished backtest and a strategy is promoted only when every gate passes.",
      body: [
        ["h3", "What a gate is"],
        ["p", "A gate is a named threshold applied to one statistic of a run — an out-of-sample"
          + " Sharpe floor, a maximum drawdown ceiling, a minimum number of trades, a deflated"
          + " Sharpe floor that accounts for how many trials were logged. The live suite, with"
          + " each gate's threshold, is served by /api/lab/validation/gates; the page shows the"
          + " suite this build actually applies rather than a picture of one."],
        ["h3", "Why the trial count matters"],
        ["p", "Testing enough variations will always produce one that looks good. The deflated"
          + " Sharpe ratio discounts a result by the number of trials that produced it, which is"
          + " why every experiment and backtest run is recorded even when it is abandoned."],
        ["h3", "Promotion"],
        ["p", "Promoting a strategy records who promoted it and against which report. After"
          + " promotion the evaluation workspace tracks it forward and reports decay, so a"
          + " strategy that stops working is visible rather than assumed to still hold."],
        ["h3", "Where to look"],
        ["p", "Strategy Lab → Validation and Evaluation."],
      ],
    },
    {
      id: "shortcuts",
      group: "Product guide",
      lead: "Keyboard shortcuts",
      detail: "Command palette, navigation, and tables.",
      title: "Keyboard shortcuts",
      lede: "The console is navigable from the keyboard. These are the bindings this build"
        + " actually registers — nothing here is aspirational.",
      body: [
        ["h3", "Command palette"],
        ["ul", [
          "⌘K / Ctrl-K — open or close the palette from anywhere.",
          "Type a ticker (for example NVDA) and press Enter to open Stock Research for it. Any"
            + " ticker the data providers know will load.",
          "↑ / ↓ move the selection, Enter runs it, Escape closes the palette.",
          "The palette searches the real backend: workspaces and tabs, universes and watchlists"
            + " (including by a symbol they contain), saved screens, and validated ticker"
            + " symbols. \"Open active backtest\" asks the lab which run is active and opens it.",
        ]],
        ["h3", "Navigation"],
        ["ul", [
          "Every workspace has a URL: #overview, #stock/NVDA, #tl/lab/backtests,"
            + " #agents, #data/ingestion, #docs, #settings. They are shareable and the browser's"
            + " Back button works.",
          "Tables and lists are focusable: Tab moves between rows, Enter or Space selects the"
            + " focused row (the collections table on Market & Data is driven this way).",
        ]],
        ["h3", "PICK mode"],
        ["p", "The PICK control in the topbar turns panels into selectable items and Export"
          + " builds a manifest of what you marked, so a set of panels can be handed to someone"
          + " else exactly as you saw it."],
      ],
    },
    {
      id: "autonomy",
      group: "Product guide",
      lead: "Agent autonomy",
      detail: "What agents can do and where humans approve.",
      title: "Agent autonomy",
      lede: "Agents run the research pipeline with real tools and a real budget. Everything they"
        + " do is recorded, and the actions that matter stop for a human.",
      body: [
        ["h3", "What an agent can do"],
        ["ul", [
          "Call the library itself: run a screen, fetch data, run a backtest, save a finding."
            + " The tools are the same functions the UI calls, so an agent cannot reach data a"
            + " person could not.",
          "Spend money, but only within the configured budget. The SPEND figure in the datastrip"
            + " is the real accumulated cost of model calls against the daily budget, and a run"
            + " that would exceed the budget is stopped rather than trimmed.",
        ]],
        ["h3", "What an agent cannot do"],
        ["ul", [
          "Place a live order. Trading is paper or sandbox only, and no agent path reaches a"
            + " live broker.",
          "Pass a Human Approval stage on its own. A pipeline that reaches one blocks until"
            + " somebody approves or rejects it in Agent Center → Approval Review; rejecting"
            + " ends the run.",
        ]],
        ["h3", "The audit log"],
        ["p", "Every stage execution, tool call, artifact and approval decision is appended to"
          + " the audit log. It is append-only: it answers \"what did this agent actually do\","
          + " which is the question that matters after the fact."],
        ["h3", "Where to look"],
        ["p", "Agent Center → Pipeline, Approval Review and Audit Log."],
      ],
    },
  ];

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = STYLE_CSS;
    document.head.appendChild(style);
  }

  const page = {
    ctx: null,
    selected: ARTICLES[0].id,

    mount: function (ctx) {
      ensureStyle();
      this.ctx = ctx;
      const ui = ctx.ui;
      const self = this;

      const head = ui.pageHead("DOCUMENTATION", "Research methodology and product guide");

      const groups = ["Methodology", "Product guide"];
      const index = ui.el("div.data-grid");
      groups.forEach(function (group) {
        const panel = ui.panel({ title: group });
        ARTICLES.filter(function (article) { return article.group === group; })
          .forEach(function (article) {
            const row = ui.listRow(article.lead, article.detail, {
              onClick: function () { self.select(article.id); },
            });
            row.tabIndex = 0;
            row.setAttribute("role", "button");
            row.dataset.article = article.id;
            row.addEventListener("keydown", function (event) {
              if (event.key !== "Enter" && event.key !== " ") return;
              event.preventDefault();
              self.select(article.id);
            });
            panel.appendChild(row);
          });
        index.appendChild(panel);
      });

      const article = ui.el("section.panel.doc-article", { id: "docArticle" });
      ui.render(ctx.root, [head, index, article]);

      const wanted = ARTICLES.filter(function (item) { return item.id === ctx.sub; })[0];
      this.select(wanted ? wanted.id : this.selected, { silent: true });

      // The build version is a fact about this server, so it is read, not typed.
      ctx.api.get("/api/health", null, { signal: ctx.signal }).then(function (payload) {
        const meta = ctx.root.querySelector("#docVersion");
        if (meta && payload) meta.textContent = "TradingLib " + (payload.version || "—");
      }).catch(function () { /* the footer simply stays generic */ });
    },

    route: function (ctx) {
      this.ctx.sub = ctx.sub;
      const wanted = ARTICLES.filter(function (item) { return item.id === ctx.sub; })[0];
      if (wanted) this.select(wanted.id, { silent: true });
    },

    unmount: function () {
      this.ctx = null;
    },

    select: function (id, options) {
      const ctx = this.ctx;
      if (!ctx) return;
      this.selected = id;
      const article = ARTICLES.filter(function (item) { return item.id === id; })[0];
      if (!article) return;

      Array.prototype.forEach.call(ctx.root.querySelectorAll("[data-article]"), function (row) {
        row.setAttribute("aria-selected", String(row.dataset.article === id));
      });

      const ui = ctx.ui;
      const host = ctx.root.querySelector("#docArticle");
      ui.clear(host);
      host.appendChild(ui.el("h2", { text: article.title }));
      host.appendChild(ui.el("p.doc-lede", { text: article.lede }));
      article.body.forEach(function (block) {
        if (block[0] === "ul") {
          const list = ui.el("ul");
          block[1].forEach(function (item) { list.appendChild(ui.el("li", { text: item })); });
          host.appendChild(list);
          return;
        }
        host.appendChild(ui.el(block[0], { text: block[1] }));
      });
      host.appendChild(ui.el("div.doc-meta",
        ui.el("span", { id: "docVersion", text: "TradingLib" }),
        document.createTextNode(" · " + article.group + " · this page describes what this build"
          + " implements; anything not implemented is named as such where it appears.")));

      if (!(options && options.silent)) ctx.router.go("docs", { sub: id });
    },
  };

  global.AlgoPages.register("docs", {
    mount: function (ctx) { page.mount(ctx); },
    route: function (ctx) { page.route(ctx); },
    unmount: function (ctx) { page.unmount(ctx); },
  });
})(window);
