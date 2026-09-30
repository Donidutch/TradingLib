/* TradingLib — Market & Data workspace.
 *
 * Five tabs, addressed as "#data/<tab>":
 *
 *   collections     Universes & Watchlists — real CRUD over /api/universes and
 *                   /api/watchlists, plus point-in-time membership, the
 *                   snapshot change log and real coverage.
 *   screener        the rule builder. Every funnel number is a real count from
 *                   POST /api/screens/preview; "Run screen" runs a saved screen
 *                   and "Backtest this screen" creates a REAL lab backtest.
 *   source-catalog  the ingestion sources as a catalog: statuses, detail,
 *                   subscription configuration and the active-subscription
 *                   table, all from /api/ingest/*.
 *   ingestion       the full configure form (one control per SourceConfig
 *                   field), a real "Test connection" and a real save.
 *   quality         /api/quality summary, per-dataset check suite and the
 *                   incident review queue.
 *
 * The markup mirrors the REFERENCE DOM (SPEC D1a); every value in it comes from
 * the API. Where the backend has no equivalent of a REFERENCE control, the page
 * says so in plain words instead of showing a number nobody computed.
 */
(function (global) {
  "use strict";

  /** Styles for the blocks the REFERENCE's feature scripts styled inline.
   *  Written here (not in ui/css/) because ui/css/ is the REFERENCE's own CSS,
   *  reused verbatim. */
  const STYLE_ID = "tradinglib-data-style";
  const STYLE_CSS = [
    /* `hidden` has to win over the layout rules: .field-grid sets display:grid
       and .btn display:inline-block, both of which out-specify the UA's
       [hidden]{display:none}. Every conditional field on this page relies on it. */
    "#view-data [hidden]{display:none !important}",
    /* TL-dialect panels injected into the agent-center tree need TL's padding
       back: #ac-root .panel wins and carries none. */
    "#view-data .tl-block .panel{padding:12px;overflow:visible}",
    "#view-data .tl-block .tbl{width:100%;border-collapse:collapse;table-layout:fixed}",
    "#view-data .sec-head{display:flex;align-items:center;gap:8px;margin:18px 0 8px;",
    "  color:var(--dim);font-size:10px;letter-spacing:1px}",
    "#view-data .sec-head::after{content:\"\";flex:1;height:1px;background:var(--line)}",
    "#view-data .row-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}",
    "#view-data .row-actions .btn{padding:2px 9px;font-size:11px;height:auto}",

    /* collections / point-in-time */
    "#tl-universe-tables{margin-top:12px}",
    "#tl-universe-tables input[type=range]{flex:1;accent-color:var(--blu);padding:0;height:18px}",
    "#collectionRows tr{cursor:pointer}",
    "#collectionRows tr:hover{background:#101627}",
    "#collectionRows tr[aria-selected=\"true\"]{background:#101d33;box-shadow:inset 2px 0 var(--blu)}",
    "#collectionRows tr:focus-visible{outline:2px solid var(--blu);outline-offset:-2px}",

    /* screener */
    "#screen-wrap{display:grid;grid-template-columns:200px minmax(0,1fr);gap:11px;margin-top:11px}",
    "#screen-wrap .rule-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;",
    "  align-items:center;padding:6px 0;border-bottom:1px solid var(--line2)}",
    "#screen-wrap .rule-parts{display:flex;gap:6px;align-items:center;flex-wrap:wrap;min-width:0}",
    "#screen-wrap .rule-parts .select,#screen-wrap .rule-parts .input{height:26px;padding:0 6px}",
    "#screen-wrap .rule-parts .input{width:84px}",
    "#screen-wrap .funnel{color:var(--dim);white-space:nowrap;font-size:11px}",
    "#screen-wrap .funnel b{color:var(--text)}",
    "#screen-wrap .funnel.last b{color:var(--grn)}",
    "#screen-wrap .rule-drop{appearance:none;background:transparent;border:0;color:var(--dim);",
    "  cursor:pointer;font:inherit;padding:0 4px}",
    "#screen-wrap .rule-drop:hover{color:var(--red)}",
    "#screen-results{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:11px;margin-top:11px}",
    "#saved-screens .rowitem{cursor:pointer}",
    "#screen-query{display:none;margin-top:8px;font:11px/1.6 ui-monospace,monospace;",
    "  background:var(--inset);border:1px solid var(--line2);border-radius:8px;padding:10px;",
    "  white-space:pre-wrap}",
    "#screen-query.open{display:block}",

    /* source catalog additions */
    "#active-subs{margin-top:11px}",
    "#active-subs .dot{font-size:9px;vertical-align:middle}",
    "#pollBlock .poll-note{margin-top:6px;line-height:1.5}",
    "#pollBlock .poll-custom{width:96px;margin-left:6px}",
    "#pollBlock .frequency-options{flex-wrap:wrap}",
    "#catalogDetail .catalog-empty{padding:22px 14px}",

    /* ingestion */
    "#view-data .test-result{margin-top:10px;border:1px solid var(--line2);border-radius:6px;",
    "  padding:10px;background:var(--panel2);font-size:11px;line-height:1.55}",
    "#view-data .test-result.ok{border-color:var(--grn)}",
    "#view-data .test-result.bad{border-color:var(--red)}",
    "#view-data .test-result pre{margin-top:7px;max-height:190px;overflow:auto;",
    "  font:10px/1.5 ui-monospace,monospace;color:var(--mid);white-space:pre-wrap}",
    "#view-data .field-note{color:var(--dim);font-size:10px;margin-top:4px}",

    /* quality */
    "#dq-wrap{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:11px;margin-top:11px}",
    "#dq-wrap .outlier{padding:10px 0;border-bottom:1px solid var(--line2)}",
    "#dq-wrap .outlier:last-child{border-bottom:0}",
    "#dq-wrap .outlier b{display:block;font-weight:600}",
    "#dq-wrap .outlier .meta{display:block;color:var(--dim);font-size:11px;margin-top:3px}",
    "#dq-wrap .outlier .acts{display:flex;gap:6px;margin-top:7px;flex-wrap:wrap}",
    "#dq-wrap .outlier .acts .btn{padding:2px 8px;font-size:11px;height:auto}",
    "#dq-note{border:1px solid var(--amb);border-radius:8px;padding:10px 12px;margin-top:11px;",
    "  background:rgba(255,176,32,.06);line-height:1.55}",
    "#view-data .sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}",
    "@media(max-width:1050px){#screen-wrap,#screen-results,#dq-wrap{grid-template-columns:1fr}}",
  ].join("\n");

  /** Tabs, in REFERENCE order (Source Catalog is inserted before Ingestion). */
  const TABS = [
    { key: "collections", label: "Universes & Watchlists" },
    { key: "screener", label: "Screener" },
    { key: "source-catalog", label: "Source Catalog" },
    { key: "ingestion", label: "Data Ingestion" },
    { key: "quality", label: "Data Quality" },
  ];

  /** REFERENCE's protocol table. `token` is what SourceConfig.from_form parses. */
  const PROTOCOLS = {
    WebSocket: {
      token: "websocket",
      label: "WebSocket endpoint",
      placeholder: "wss://provider.example/v2/stream",
      hint: "Persistent streaming connection for live events. Configure endpoint, subscriptions, heartbeat, and reconnect behavior.",
      frequency: "Streaming",
    },
    REST: {
      token: "rest",
      label: "REST base URL",
      placeholder: "https://api.provider.example/v1",
      hint: "Request-response connector. Configure authentication, rate limits, pagination, and resource paths.",
      frequency: "Daily",
    },
    Polling: {
      token: "polling",
      label: "Polling endpoint",
      placeholder: "https://api.provider.example/v1/updates",
      hint: "Scheduled API polling. Configure frequency, cursor, pagination, and deduplication.",
      frequency: "Every 5 minutes",
    },
    Database: {
      token: "database",
      label: "Connection URI / database path",
      placeholder: "postgresql://host/db or /data/market.duckdb",
      hint: "Read from PostgreSQL, DuckDB, or another database. Put table, SQL query, and watermark column in advanced configuration.",
      frequency: "Hourly",
    },
    File: {
      token: "file",
      label: "File path / discovery pattern",
      placeholder: "/data/import/**/*.parquet",
      hint: "Ingest database files or flat files. Configure discovery pattern, schema mapping, and archive behavior.",
      frequency: "On file change",
    },
  };

  /** protocol token → the label the REFERENCE select shows. */
  const PROTOCOL_BY_TOKEN = {
    websocket: "WebSocket", rest: "REST", polling: "Polling",
    database: "Database", file: "File",
  };

  /** Poll-interval presets offered on a pull-based source. */
  const POLL_PRESETS = ["15s", "30s", "1m", "5m", "15m", "1h", "1d"];

  /**
   * Protocols whose connector implements a historical backfill, in the
   * REFERENCE's chip order. Mirrors `Protocol.can_backfill` in
   * tradinglib/data/ingest/source.py — the source payload's `can_backfill` is
   * the authority; this list only decides which chips are drawn.
   */
  const BACKFILL_PROTOCOLS = [
    { token: "rest", label: "REST" },
    { token: "polling", label: "POLL" },
    { token: "database", label: "DB" },
    { token: "file", label: "FILE" },
    { token: "library", label: "LIB" },
  ];

  /** Catalog status pill classes the source-catalog stylesheet defines. */
  const STATUS_PILL = {
    connected: { text: "Connected", cls: "connected" },
    degraded: { text: "Degraded", cls: "credentials" },
    disconnected: { text: "Disconnected", cls: "credentials" },
    not_configured: { text: "Credentials required", cls: "credentials" },
    unknown: { text: "Not probed", cls: "available" },
  };

  const SHELL_HTML = `
    <div class="page-head">
      <div><small>MARKET &amp; DATA</small><h1>Collections, screening, ingestion, and data quality</h1></div>
      <div class="collection-toolbar"><button class="btn" id="newWatchlist">+ New watchlist</button><button class="btn primary" id="newUniverse">+ New universe</button></div>
    </div>
    <div class="tabs" data-tabs="data-tabs"></div>

    <div class="tab-panel" data-panel-group="data-tabs" data-panel="collections">
      <div class="collection-layout">
        <section class="panel">
          <div class="panel-title">Universes &amp; watchlists <span class="badge" id="collectionCount">loading…</span></div>
          <table class="data-table"><thead><tr><th>Name</th><th>Type</th><th>Construction</th><th class="num">Symbols</th><th>Refresh</th><th>Status</th></tr></thead>
            <tbody id="collectionRows"></tbody>
          </table>
        </section>
        <aside class="panel collection-card">
          <div class="panel-title">How collections work</div>
          <div class="list-row"><b>Universes</b><span>Reusable research populations built statically, from screens, or from rules. They can refresh automatically and feed agents, experiments, and backtests.</span></div>
          <div class="list-row"><b>Watchlists</b><span>Focused monitoring lists. They can be manual or derived from a universe, screen, earnings calendar, or agent findings.</span></div>
          <div class="list-row"><b>One selector everywhere</b><span>The global selector can switch between either type while retaining the collection definition.</span></div>
        </aside>
      </div>
      <div class="tl-block" id="tl-universe-tables"></div>
    </div>

    <div class="tab-panel" data-panel-group="data-tabs" data-panel="screener">
      <div class="tl-block" id="screen-wrap"></div>
    </div>

    <div class="tab-panel" data-panel-group="data-tabs" data-panel="source-catalog">
      <div class="source-catalog-layout">
        <aside class="panel catalog-sidebar" aria-label="Source catalog">
          <div class="panel-title">Source catalog <span class="badge" id="catalogCount">loading…</span></div>
          <div class="catalog-source-list" id="catalogSourceList"></div>
          <div class="catalog-source-footer">
            <button class="btn catalog-request" id="requestCatalogSource">+ Request new source</button>
            <div class="protocol-key"><b>Protocol key</b><span><strong>WS</strong> · real-time streaming</span><span><strong>REST</strong> · request and backfill</span><span><strong>FILE / DB</strong> · batch or CDC</span><span><strong>POLL</strong> · scheduled pull</span></div>
          </div>
        </aside>
        <section class="panel catalog-detail" id="catalogDetail" aria-live="polite"></section>
        <aside class="panel catalog-subscription" id="catalogSubscription" aria-live="polite"></aside>
      </div>
      <div class="tl-block"><section class="panel" id="active-subs">
        <div class="panel-title" style="padding:0 0 8px">Active subscriptions <span class="badge" id="as-count">—</span></div>
        <table class="data-table">
          <thead><tr><th>Source</th><th>Datatype</th><th>Protocol</th><th>Freq</th><th>Range / mode</th><th>Last ingest</th><th>Status</th><th class="num">Actions</th></tr></thead>
          <tbody id="as-rows"></tbody>
        </table>
      </section></div>
    </div>

    <div class="tab-panel" data-panel-group="data-tabs" data-panel="ingestion">
      <div class="ingestion-layout">
        <section class="panel">
          <div class="panel-title">Configure ingestion source <span class="badge" id="ingestFormBadge">new connection</span></div>
          <form class="ingest-form" id="ingestionForm">
            <div class="field-grid">
              <div class="field-group"><label for="sourceName">Source name</label><input id="sourceName" required placeholder="e.g. Alpaca live bars"></div>
              <div class="field-group"><label for="sourceDomain">Data domain</label><select id="sourceDomain"><option>Prices &amp; quotes</option><option>Fundamentals</option><option>News</option><option>Macro / economic</option><option>Alternative data</option><option>Reference data</option></select></div>
            </div>
            <div class="field-group"><label for="sourceProtocol">Protocol / connector</label><select id="sourceProtocol"><option value="WebSocket">WebSocket stream</option><option value="REST">REST API</option><option value="Polling">Polling API</option><option value="Database">Database</option><option value="File">Database file / flat file</option></select></div>
            <div class="protocol-hint" id="protocolHint"></div>
            <div class="field-group"><label id="endpointLabel" for="sourceEndpoint">Endpoint</label><input id="sourceEndpoint" required></div>
            <div class="field-grid">
              <div class="field-group"><label for="sourceAuth">Authentication</label><select id="sourceAuth"><option>API key</option><option>OAuth 2.0</option><option>Basic auth</option><option>Connection secret</option><option>None</option></select></div>
              <div class="field-group"><label for="sourceFormat">Format</label><select id="sourceFormat"><option>JSON</option><option>NDJSON</option><option>CSV</option><option>Parquet</option><option>Arrow</option><option>Database rows</option></select></div>
            </div>
            <div class="field-grid" id="authFields" hidden>
              <div class="field-group"><label for="authKeyName" id="authKeyNameLabel">Key / header name</label><input id="authKeyName" placeholder="X-API-KEY" autocomplete="off"></div>
              <div class="field-group"><label for="authKeyValue" id="authKeyValueLabel">Key value</label><input id="authKeyValue" type="password" placeholder="write-only" autocomplete="off"><div class="field-note">Stored with the source; never returned by the API.</div></div>
            </div>
            <div class="field-grid">
              <div class="field-group"><label for="sourceFrequency">Frequency / schedule</label><select id="sourceFrequency"><option>Streaming</option><option>Every second</option><option>Every minute</option><option>Every 5 minutes</option><option>Hourly</option><option>Daily</option><option>On file change</option><option>Custom cron…</option></select></div>
              <div class="field-group"><label for="sourceScope">Scope</label><select id="sourceScope"></select></div>
            </div>
            <div class="field-grid" id="cronFields" hidden>
              <div class="field-group"><label for="sourceCron">Cron expression</label><input id="sourceCron" placeholder="*/5 * * * *"></div>
              <div class="field-group"><label for="sourceSymbols" id="symbolsLabel">Custom symbols</label><input id="sourceSymbols" placeholder="AAPL, MSFT, NVDA"></div>
            </div>
            <div class="field-grid">
              <div class="field-group"><label for="sourceTimezone">Source timezone</label><select id="sourceTimezone"><option>UTC</option><option>America/New_York</option><option>Europe/Copenhagen</option><option>Exchange local</option></select></div>
              <div class="field-group"><label for="sourceRetries">Retry / reconnect policy</label><select id="sourceRetries"><option>Exponential backoff</option><option>Fixed 30 seconds</option><option>Immediate, max 5</option><option>Manual only</option></select></div>
            </div>
            <label class="check-field"><input type="checkbox" id="backfillEnabled" checked> Enable historical backfill on first connection</label>
            <div class="field-grid" id="backfillFields">
              <div class="field-group"><label for="backfillRange">Backfill range</label><select id="backfillRange"><option>All available history</option><option>Last 10 years</option><option>Last 5 years</option><option>Last 1 year</option><option>Custom start date…</option></select></div>
              <div class="field-group"><label for="backfillBatch">Backfill batch size</label><select id="backfillBatch"><option>Automatic</option><option>1 day</option><option>1 month</option><option>1 year</option><option>10,000 rows</option></select></div>
            </div>
            <div class="field-group" id="backfillStartField" hidden><label for="backfillStart">Backfill start date</label><input id="backfillStart" type="date"></div>
            <div class="field-group"><label for="advancedConfig">Advanced configuration</label><textarea id="advancedConfig" placeholder="Optional headers, subscriptions, SQL query, file glob, pagination, schema mapping…"></textarea></div>
            <div class="form-actions"><button type="button" class="btn" id="resetIngestForm" hidden>Cancel edit</button><button type="button" class="btn" id="testConnection">Test connection</button><button type="submit" class="btn primary" id="saveSource">Save data source</button></div>
            <div id="testResult"></div>
          </form>
        </section>
        <section class="panel">
          <div class="panel-title">Configured ingestion sources <span class="badge" id="sourceCountBadge">loading…</span></div>
          <table class="data-table"><thead><tr><th>Source</th><th>Domain</th><th>Protocol</th><th>Frequency</th><th>Backfill</th><th>Freshness</th><th>Coverage</th><th>Status</th><th class="num">Actions</th></tr></thead>
            <tbody id="sourceRows"></tbody>
          </table>
          <div class="list-row"><b>Ingestion lifecycle</b><span>Test connection → save source → optional backfill → live or scheduled ingestion → monitor freshness, coverage, retries, and schema drift.</span></div>
        </section>
      </div>
    </div>

    <div class="tab-panel" data-panel-group="data-tabs" data-panel="quality">
      <div class="dashboard-cards" style="margin-top:11px" id="qualityCards"></div>
      <section class="panel"><div class="panel-title">Quality incidents <span class="badge" id="incidentBadge">—</span></div>
        <table class="data-table"><thead><tr><th>Check</th><th>Dataset</th><th>Severity</th><th>Status</th><th class="num">Action</th></tr></thead><tbody id="incidentRows"></tbody></table>
      </section>
      <div class="tl-block" id="dq-wrap"></div>
      <div class="sr-only" role="status" aria-live="polite" id="dq-live"></div>
    </div>

    <dialog class="dialog" id="collectionDialog">
      <form id="collectionForm">
        <div class="dialog-head"><strong id="collectionDialogTitle">New collection</strong><button type="button" class="dialog-close" id="closeCollectionDialog">×</button></div>
        <div class="dialog-body">
          <div class="field-group"><label>Collection type</label><div class="segmented"><button type="button" class="segment-option active" data-collection-type="Watchlist">Watchlist</button><button type="button" class="segment-option" data-collection-type="Universe">Universe</button></div><input type="hidden" id="collectionType" value="Watchlist"></div>
          <div class="field-group"><label for="collectionName">Name</label><input id="collectionName" required placeholder="e.g. Semiconductor research"></div>
          <div class="field-group"><label for="collectionDescription">Description</label><textarea id="collectionDescription" placeholder="What this collection represents"></textarea></div>
          <div class="field-grid"><div class="field-group"><label for="constructionMethod">Construction method</label><select id="constructionMethod"><option>Manual symbols</option><option>Screener result</option><option>Rule-based</option><option>Agent findings</option><option>Earnings calendar</option></select></div><div class="field-group"><label for="collectionRefresh">Refresh</label><select id="collectionRefresh"><option>Manual</option><option>Live</option><option>Hourly</option><option>Daily</option><option>Weekly</option></select></div></div>
          <div class="field-group"><label for="collectionSymbols">Symbols or rules</label><textarea id="collectionSymbols" placeholder="GRAB, UBER, SEA — or describe the rule"></textarea></div>
          <label class="check-field"><input type="checkbox" id="makeDefault"> Make this the default global collection</label>
        </div>
        <div class="dialog-footer"><button type="button" class="btn" id="cancelCollection">Cancel</button><button type="submit" class="btn primary" id="collectionSubmit">Create collection</button></div>
      </form>
    </dialog>`;

  // ------------------------------------------------------------------ helpers

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = STYLE_CSS;
    document.head.appendChild(style);
  }

  /** Seconds in an interval token ("15s", "5m", "1h", "1d"), or null. */
  function toSeconds(token) {
    const match = /^\s*(\d+(?:\.\d+)?)\s*(s|m|h|d)\s*$/i.exec(String(token || ""));
    if (!match) return null;
    const unit = { s: 1, m: 60, h: 3600, d: 86400 }[match[2].toLowerCase()];
    return Number(match[1]) * unit;
  }

  /**
   * Turn a dataset granularity into an interval token toSeconds understands.
   * Reporting cadences ("quarterly", "annual") and "realtime" have no bar
   * length, so they return "" — the poll advice then says nothing about them
   * rather than inventing a bar size to compare against.
   */
  function barToken(granularity) {
    const map = { "1wk": "7d", "1mo": "30d" };
    const token = String(granularity || "").toLowerCase();
    return map[token] || (toSeconds(token) ? token : "");
  }

  /** "5m" for 300 — the inverse of toSeconds, for advice text. */
  function humanInterval(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    if (seconds < 60) return Math.round(seconds) + "s";
    if (seconds < 3600) return Math.round(seconds / 60) + "m";
    if (seconds < 86400) return Math.round(seconds / 3600) + "h";
    return Math.round(seconds / 86400) + "d";
  }

  /** Turn a backend detail phrase ("advisory: …") into a UI sentence. */
  function sentence(text) {
    const value = String(text || "").trim();
    if (!value) return "";
    return value.charAt(0).toUpperCase() + value.slice(1) + (/[.!?]$/.test(value) ? "" : ".");
  }

  /** "12s ago" / "live" / "never". */
  function agoText(iso, fmt) {
    if (!iso) return "never";
    return fmt.ago(iso) + " ago";
  }

  /** Compact integer with thousands separators. */
  function intText(value) {
    if (value === undefined || value === null || isNaN(Number(value))) return "—";
    return Math.round(Number(value)).toLocaleString("en-US");
  }

  /** Extract a human message out of an ApiError. */
  function errorText(err) {
    if (!err) return "unknown error";
    if (err.payload && err.payload.detail) {
      const detail = err.payload.detail;
      if (typeof detail === "string") return detail;
      if (detail.detail) return String(detail.detail);
      return JSON.stringify(detail);
    }
    return err.message || String(err);
  }

  // ==========================================================================
  // the page
  // ==========================================================================

  const page = {
    /** @type {object|null} the router context of the current mount */
    ctx: null,
    /** @type {string} active tab key */
    tab: "collections",
    /** @type {Array<Function>} teardown callbacks run on unmount */
    cleanups: [],
    /** loaded-once-per-mount caches, all straight from the API */
    universes: [],
    watchlists: [],
    sources: [],
    subscriptions: [],
    screens: [],
    metrics: null,
    settings: null,

    // ---------------------------------------------------------------- lifecycle

    mount: function (ctx) {
      ensureStyle();
      this.ctx = ctx;
      this.cleanups = [];
      this.ui = ctx.ui;
      this.api = ctx.api;
      this.fmt = ctx.ui.fmt;

      ctx.root.innerHTML = SHELL_HTML;
      // The DOM is new, so anything that remembers "already built" from a
      // previous mount has to go with it.
      this._screenerReady = false;
      this._editingSource = null;
      this._editing = null;
      this.buildTabs();
      this.wireShell();

      this.tab = this.tabFor(ctx.sub);
      this.showTab(this.tab, { silent: true });

      // Live channels: an ingest run or a quality check must move this page
      // without a reload.
      const self = this;
      this.cleanups.push(ctx.stream.listen("ingest", function (event) {
        // Backfill progress is written straight into the subscription panel:
        // coalescing it into a catalog reload would throw away the FROM/TO
        // the operator just typed.
        if (event && typeof event.event === "string" && event.event.indexOf("backfill_") === 0) {
          self.onBackfillEvent(event);
          if (event.event !== "backfill_finished") return;
        }
        self.scheduleRefresh("ingest");
      }));
      this.cleanups.push(ctx.stream.listen("quality", function () {
        self.scheduleRefresh("quality");
      }));
      // A pinned screen whose membership moved must show up without a reload.
      this.cleanups.push(ctx.stream.listen("notifications", function (event) {
        if (!event || event.event !== "screen_alert") return;
        if (self.tab === "screener") self.loadScreenAlerts();
      }));

      this.loadSettings();
      this.loadTab(this.tab);
      this.startTicker();
    },

    route: function (ctx) {
      this.ctx.sub = ctx.sub;
      this.ctx.params = ctx.params;
      const next = this.tabFor(ctx.sub);
      if (next === this.tab) return;
      this.showTab(next, { silent: true });
      this.loadTab(next);
    },

    unmount: function () {
      this.cleanups.forEach(function (fn) {
        try {
          fn();
        } catch (err) {
          console.warn("data page cleanup failed", err);
        }
      });
      this.cleanups = [];
      if (this._ticker) clearInterval(this._ticker);
      this._ticker = 0;
      if (this._refreshTimer) clearTimeout(this._refreshTimer);
      this._refreshTimer = 0;
      if (this._funnelTimer) clearTimeout(this._funnelTimer);
      this._funnelTimer = 0;
      if (this._funnelController) this._funnelController.abort();
      this._funnelController = null;
      this.ctx = null;
    },

    /** True while the page is still mounted (guards late API replies). */
    alive: function () {
      return !!this.ctx && !this.ctx.signal.aborted;
    },

    /** Query inside this page's root. */
    $: function (selector) {
      return this.ctx ? this.ctx.root.querySelector(selector) : null;
    },
    $$: function (selector) {
      return this.ctx ? Array.prototype.slice.call(this.ctx.root.querySelectorAll(selector)) : [];
    },

    // ------------------------------------------------------------------ tabs

    tabFor: function (sub) {
      const wanted = String(sub || "").toLowerCase();
      for (let i = 0; i < TABS.length; i += 1) {
        if (TABS[i].key === wanted) return TABS[i].key;
      }
      return TABS[0].key;
    },

    buildTabs: function () {
      const host = this.$('[data-tabs="data-tabs"]');
      const self = this;
      host.innerHTML = "";
      TABS.forEach(function (tab) {
        const button = document.createElement("button");
        button.className = "tab";
        button.type = "button";
        button.dataset.tab = tab.key;
        button.textContent = tab.label;
        button.addEventListener("click", function () {
          if (!self.alive()) return;
          self.ctx.router.go("data", { sub: tab.key });
        });
        host.appendChild(button);
      });
    },

    /** Show one tab. `silent` skips the router write (the router asked for it). */
    showTab: function (key, options) {
      this.tab = key;
      this.$$('[data-tabs="data-tabs"] .tab').forEach(function (button) {
        button.classList.toggle("active", button.dataset.tab === key);
      });
      this.$$('[data-panel-group="data-tabs"]').forEach(function (panel) {
        panel.classList.toggle("active", panel.dataset.panel === key);
      });
      if (!(options && options.silent)) this.ctx.router.go("data", { sub: key });
    },

    /** Load (or reload) whichever tab is showing. */
    loadTab: function (key) {
      if (key === "collections") return this.loadCollections();
      if (key === "screener") return this.loadScreener();
      if (key === "source-catalog") return this.loadCatalog();
      if (key === "ingestion") return this.loadIngestion();
      if (key === "quality") return this.loadQuality();
      return undefined;
    },

    /** Coalesce live-event driven refreshes so a burst reloads once. */
    scheduleRefresh: function (reason) {
      const self = this;
      if (this._refreshTimer) clearTimeout(this._refreshTimer);
      this._refreshTimer = setTimeout(function () {
        self._refreshTimer = 0;
        if (!self.alive()) return;
        if (reason === "quality" && self.tab === "quality") self.loadQuality();
        if (reason === "ingest" && (self.tab === "ingestion" || self.tab === "source-catalog")) {
          self.loadTab(self.tab);
        }
      }, 700);
    },

    /** Re-render the ages in the two tables that show "x ago". */
    startTicker: function () {
      const self = this;
      this._ticker = setInterval(function () {
        if (!self.alive()) return;
        self.$$("[data-age]").forEach(function (cell) {
          cell.textContent = agoText(cell.dataset.age || null, self.fmt);
        });
      }, 5000);
    },

    wireShell: function () {
      const self = this;
      this.$("#newWatchlist").addEventListener("click", function () {
        self.openCollectionDialog("Watchlist");
      });
      this.$("#newUniverse").addEventListener("click", function () {
        self.openCollectionDialog("Universe");
      });
      this.wireCollectionDialog();
      this.wireIngestionForm();
    },

    /** Load the settings page's stored preferences (benchmark drives backtests). */
    loadSettings: function () {
      const self = this;
      this.api.optional(this.api.settings.get({ signal: this.ctx.signal }))
        .then(function (payload) {
          if (payload) self.settings = payload;
        })
        .catch(function (err) {
          if (!err.cancelled) console.warn("settings load failed", err);
        });
    },

    // ======================================================================
    // TAB 1 — Universes & Watchlists
    // ======================================================================

    loadCollections: function () {
      const self = this;
      const badge = this.$("#collectionCount");
      badge.textContent = "loading…";
      Promise.all([
        this.api.universes.list({ include_members: true }, { signal: this.ctx.signal }),
        this.api.watchlists.list(null, { signal: this.ctx.signal }),
      ]).then(function (results) {
        if (!self.alive()) return;
        self.universes = (results[0] && results[0].universes) || [];
        self.activeUniverse = results[0] && results[0].active;
        self.watchlists = (results[1] && results[1].watchlists) || [];
        self.defaultWatchlist = results[1] && results[1].default;
        self.renderCollections();
      }).catch(function (err) {
        if (err.cancelled || !self.alive()) return;
        badge.textContent = "failed";
        badge.classList.add("red");
        self.$("#collectionRows").innerHTML = "";
        self.$("#collectionRows").appendChild(
          self.ui.el("tr", self.ui.el("td.red", { colspan: "6", text: errorText(err) }))
        );
      });
    },

    /** Every collection as one flat list — the table shows both kinds. */
    collectionList: function () {
      const self = this;
      const rows = this.universes.map(function (universe) {
        return {
          kind: "Universe",
          id: universe.id,
          name: universe.name,
          construction: universe.source === "builtin-static" ? "Static built-in definition"
            : universe.source === "wikipedia:constituents" ? "Published index constituents"
              : universe.description || "Manual symbols",
          symbols: universe.member_count || universe.symbol_count || 0,
          refresh: universe.builtin ? "On demand" : "Manual",
          status: universe.id === self.activeUniverse ? { text: "Active", tone: "green" }
            : (universe.member_count || universe.symbol_count)
              ? { text: "Ready", tone: "" }
              : { text: "Not fetched", tone: "yellow" },
          raw: universe,
        };
      });
      this.watchlists.forEach(function (watchlist) {
        rows.push({
          kind: "Watchlist",
          id: watchlist.id,
          name: watchlist.name,
          construction: "Manual symbols",
          symbols: (watchlist.symbols || []).length,
          refresh: "Manual",
          status: watchlist.id === self.defaultWatchlist
            ? { text: "Default", tone: "green" } : { text: "Active", tone: "" },
          raw: watchlist,
        });
      });
      return rows;
    },

    renderCollections: function () {
      const self = this;
      const ui = this.ui;
      const rows = this.collectionList();
      const badge = this.$("#collectionCount");
      badge.classList.remove("red");
      badge.textContent = rows.length + (rows.length === 1 ? " collection" : " collections");

      const body = this.$("#collectionRows");
      body.innerHTML = "";
      if (!rows.length) {
        body.appendChild(ui.el("tr", ui.el("td.muted", {
          colspan: "6",
          text: "No universes or watchlists yet — create one with “+ New universe”.",
        })));
      }
      rows.forEach(function (row) {
        const tr = ui.el("tr", { tabindex: "0", role: "button", "aria-selected": "false" });
        tr.dataset.collectionKind = row.kind;
        tr.dataset.collectionId = row.id;
        tr.appendChild(ui.el("td", { text: row.name }));
        tr.appendChild(ui.el("td", ui.el("span.type-pill", { text: row.kind })));
        tr.appendChild(ui.el("td", { text: row.construction }));
        tr.appendChild(ui.el("td.num", { text: String(row.symbols) }));
        tr.appendChild(ui.el("td", { text: row.refresh }));
        const status = ui.el("td", { text: row.status.text });
        if (row.status.tone) status.classList.add(row.status.tone);
        tr.appendChild(status);
        const select = function () { self.selectCollection(row.kind, row.id); };
        tr.addEventListener("click", select);
        tr.addEventListener("keydown", function (event) {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          select();
        });
        body.appendChild(tr);
      });

      const wanted = this.selected && rows.filter(function (row) {
        return row.kind === self.selected.kind && row.id === self.selected.id;
      })[0];
      const target = wanted || rows[0];
      if (target) this.selectCollection(target.kind, target.id);
      else this.$("#tl-universe-tables").innerHTML = "";
    },

    selectCollection: function (kind, id) {
      const self = this;
      this.selected = { kind: kind, id: id };
      this.$$("#collectionRows tr").forEach(function (tr) {
        tr.setAttribute("aria-selected",
          String(tr.dataset.collectionKind === kind && tr.dataset.collectionId === id));
      });
      this.renderPitShell();
      this.loadMembership();
      if (kind === "Universe") {
        this.loadSnapshots();
        this.loadCoverage();
      }
    },

    /** The point-in-time membership block (REFERENCE's universe-pit layout). */
    renderPitShell: function () {
      const self = this;
      const host = this.$("#tl-universe-tables");
      const isUniverse = this.selected.kind === "Universe";
      const thisYear = new Date().getUTCFullYear();
      host.innerHTML = `
        <div class="sec-head">POINT-IN-TIME MEMBERSHIP</div>
        <div class="grid" style="grid-template-columns:minmax(0,1.4fr) minmax(0,1fr)">
          <div class="panel">
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
              <span class="hi" style="font-weight:600" id="pit-title"></span>
              <span style="margin-left:auto" class="dim" id="pit-count"></span>
            </div>
            <div style="display:flex;align-items:center;gap:10px;margin:10px 0"${isUniverse ? "" : ' hidden'}>
              <span class="dim mini">2016</span>
              <input type="range" min="2016" max="${thisYear}" value="${thisYear}" id="asof" aria-label="As-of year">
              <span class="dim mini">today</span>
              <span class="chip mini" id="pit-basis">—</span>
            </div>
            <table class="tbl">
              <thead><tr>
                <th style="width:16%">TICKER</th><th style="width:34%">NAME</th>
                <th style="width:28%">IN COLLECTION</th><th style="width:22%">STATUS TODAY</th>
              </tr></thead>
              <tbody id="pit-rows"></tbody>
            </table>
            <div class="note mini" style="margin-top:10px" id="pit-note"></div>
            <div class="row-actions" id="pit-actions"></div>
          </div>
          <div style="display:flex;flex-direction:column;gap:12px">
            <div class="panel tight">
              <div class="ptitle">CHANGE LOG · SNAPSHOTS</div>
              <div id="pit-changelog"></div>
              <div class="dim mini" id="pit-rule"
                   style="border-top:1px solid var(--line);margin-top:6px;padding-top:6px"></div>
            </div>
            <div class="panel tight">
              <div class="ptitle">COVERAGE @ AS-OF DATE</div>
              <div id="pit-coverage"></div>
              <div class="dim mini" id="pit-covnote"
                   style="border-top:1px solid var(--line);margin-top:6px;padding-top:6px"></div>
            </div>
          </div>
        </div>
        <div class="sr-only" role="status" aria-live="polite" id="pit-live"></div>`;

      const slider = this.$("#asof");
      if (slider) {
        slider.addEventListener("input", function () {
          self.$("#pit-title").textContent =
            self.selected.id + " · membership @ " + slider.value + "-06-30";
        });
        slider.addEventListener("change", function () {
          self.loadMembership();
          self.loadCoverage();
        });
      }
      this.renderPitActions();
      const rule = this.$("#pit-rule");
      const record = this.currentCollection();
      rule.textContent = record
        ? (record.rule || record.description || (this.selected.kind === "Watchlist"
          ? "Manual watchlist · no automatic refresh" : "No construction rule recorded"))
        : "";
      if (!isUniverse) {
        this.$("#pit-changelog").innerHTML =
          '<div class="dim mini">Watchlists carry no snapshot history.</div>';
        this.$("#pit-coverage").innerHTML =
          '<div class="dim mini">Coverage is computed for universes.</div>';
        this.$("#pit-covnote").textContent = "";
      }
    },

    /** The selected collection's API record. */
    currentCollection: function () {
      const self = this;
      if (!this.selected) return null;
      const list = this.selected.kind === "Universe" ? this.universes : this.watchlists;
      return list.filter(function (item) { return item.id === self.selected.id; })[0] || null;
    },

    renderPitActions: function () {
      const self = this;
      const host = this.$("#pit-actions");
      const record = this.currentCollection();
      if (!host || !record) return;
      host.innerHTML = "";
      const button = function (label, handler, primary) {
        const node = self.ui.button(label, { primary: primary, onClick: handler });
        host.appendChild(node);
        return node;
      };

      if (this.selected.kind === "Universe") {
        if (record.id !== this.activeUniverse) {
          button("Set active", function (event) {
            self.withBusy(event.currentTarget, function () {
              return self.api.universes.setActive(record.id).then(function () {
                global.toast.ok("Active universe: " + record.name);
                self.loadCollections();
                if (global.AlgoShell) global.AlgoShell.refreshStatus();
              });
            });
          }, true);
        }
        if (record.builtin) {
          button("Refresh members", function (event) {
            self.withBusy(event.currentTarget, function () {
              return self.api.universes.refresh(record.id).then(function (updated) {
                global.toast.ok(record.name + " · " + (updated.member_count || 0) + " members fetched");
                self.loadCollections();
              });
            });
          });
        }
        button("Freeze snapshot", function (event) {
          self.withBusy(event.currentTarget, function () {
            return self.api.universes.snapshot(record.id, {}).then(function (snap) {
              global.toast.ok("Snapshot " + snap.as_of + " · " + (snap.symbol_count || 0) + " symbols");
              self.loadSnapshots();
            });
          });
        });
      }
      button("Edit", function () { self.openCollectionDialog(self.selected.kind, record); });
      if (!record.builtin) {
        const kind = this.selected.kind;
        button("Delete", function (event) {
          self.withBusy(event.currentTarget, function () {
            const remove = kind === "Universe"
              ? self.api.universes.remove(record.id)
              : self.api.watchlists.remove(record.id);
            return remove.then(function () {
              global.toast.ok(record.name + " deleted");
              self.selected = null;
              self.loadCollections();
            });
          });
        });
      }
    },

    /** Disable a button while its action runs; report failures honestly. */
    withBusy: function (button, work) {
      const label = button.textContent;
      button.disabled = true;
      button.textContent = "…";
      return work().catch(function (err) {
        if (err && err.cancelled) return;
        console.error("action failed", err);
        global.toast.error(errorText(err));
      }).then(function () {
        button.disabled = false;
        button.textContent = label;
      });
    },

    loadMembership: function () {
      const self = this;
      const kind = this.selected.kind;
      const id = this.selected.id;
      const slider = this.$("#asof");
      const year = slider ? Number(slider.value) : null;
      const asOf = kind === "Universe" && year && year < new Date().getUTCFullYear()
        ? year + "-06-30" : null;

      const request = kind === "Universe"
        ? this.api.universes.members(id, asOf ? { as_of: asOf } : null, { signal: this.ctx.signal })
        : this.api.watchlists.get(id, { signal: this.ctx.signal });

      request.then(function (payload) {
        if (!self.alive() || !self.selected || self.selected.id !== id) return;
        self.renderMembership(payload, asOf);
      }).catch(function (err) {
        if (err.cancelled || !self.alive()) return;
        self.$("#pit-rows").innerHTML = "";
        self.$("#pit-rows").appendChild(
          self.ui.el("tr", self.ui.el("td.bad", { colspan: "4", text: errorText(err) }))
        );
      });
    },

    renderMembership: function (payload, asOf) {
      const ui = this.ui;
      const kind = this.selected.kind;
      const record = this.currentCollection();
      const name = (record && record.name) || this.selected.id;

      let members;
      if (kind === "Universe") {
        members = payload.members || [];
      } else {
        members = (payload.symbols || []).map(function (symbol) {
          return { symbol: symbol, name: null, added_on: null, removed_on: null };
        });
      }

      this.$("#pit-title").textContent = name + " · membership @ "
        + (asOf || "today");
      this.$("#pit-count").textContent = members.length
        + (members.length === 1 ? " name" : " names");

      const basis = this.$("#pit-basis");
      if (basis) {
        const value = payload.basis || (kind === "Watchlist" ? "manual list" : "current");
        basis.textContent = value === "snapshot" ? "PIT ✓ snapshot"
          : value === "membership-dates" ? "PIT · membership dates"
            : value === "current" ? "current membership" : value;
        basis.classList.toggle("grn", value === "snapshot");
        basis.classList.toggle("amb", value === "membership-dates");
      }

      const body = this.$("#pit-rows");
      body.innerHTML = "";
      if (!members.length) {
        body.appendChild(ui.el("tr", ui.el("td.dim", {
          colspan: "4",
          text: kind === "Universe" && (record && record.builtin) && !members.length
            ? "No members stored yet — use “Refresh members” to fetch the constituents."
            : "No members at this as-of date.",
        })));
      }
      members.slice(0, 200).forEach(function (member) {
        const tr = ui.el("tr");
        tr.appendChild(ui.el("td.hi", { text: member.symbol }));
        tr.appendChild(ui.el("td", { text: member.name || "—" }));
        tr.appendChild(ui.el("td", {
          text: (member.added_on || "—") + " → " + (member.removed_on || "now"),
        }));
        const status = member.removed_on
          ? ui.el("td.bad", { text: "REMOVED " + member.removed_on })
          : ui.el("td.ok", { text: "ACTIVE" });
        tr.appendChild(status);
        body.appendChild(tr);
      });
      if (members.length > 200) {
        body.appendChild(ui.el("tr", ui.el("td.dim", {
          colspan: "4", text: "… " + (members.length - 200) + " more",
        })));
      }

      const gone = members.filter(function (m) { return !!m.removed_on; });
      this.$("#pit-note").textContent = gone.length
        ? "✓ " + gone.length + " former member(s) are kept with their [added, removed] window, so a"
          + " backtest over this period includes them while they were members — survivorship"
          + " bias is guarded by construction."
        : "No member has left this collection, so there is nothing to reconstruct for"
          + " survivorship. Membership dates are stored for every name that has them.";

      const live = this.$("#pit-live");
      if (live) live.textContent = name + ", " + members.length + " names as of " + (asOf || "today");
    },

    loadSnapshots: function () {
      const self = this;
      const id = this.selected.id;
      this.api.universes.changelog(id, { limit: 6 }, { signal: this.ctx.signal })
        .then(function (payload) {
          if (!self.alive() || !self.selected || self.selected.id !== id) return;
          self.renderChangeLog(payload);
        }).catch(function (err) {
          if (err.cancelled) return;
          console.warn("change log failed", err);
          const host = self.$("#pit-changelog");
          if (host) {
            host.innerHTML = "";
            host.appendChild(self.ui.errorPanel("Change log", err));
          }
        });
    },

    /**
     * The reference's CHANGE LOG panel: per-transition IN / OUT names with the
     * rule that admitted or dropped them, plus the corporate actions EDGAR
     * confirmed (a ticker change is not a membership decision and is shown on
     * its own row, as in the reference's GOOG/GOOGL line).
     */
    renderChangeLog: function (payload) {
      const self = this;
      const ui = this.ui;
      const host = this.$("#pit-changelog");
      if (!host) return;
      const entries = payload.entries || [];
      host.innerHTML = "";
      if (!entries.length) {
        host.appendChild(ui.el("div.dim.mini", {
          text: "No snapshot recorded yet. “Freeze snapshot” stores today's membership as"
            + " point-in-time truth.",
        }));
        this.renderRuleFooter(payload);
        return;
      }

      entries.forEach(function (entry) {
        const head = ui.el("div.stat");
        head.appendChild(ui.el("span.blu", { text: entry.as_of }));
        head.appendChild(document.createTextNode(" " + entry.symbol_count + " symbols"));
        if (entry.net) {
          head.appendChild(ui.el(entry.net > 0 ? "span.ok" : "span.bad", {
            text: " " + (entry.net > 0 ? "+" : "") + entry.net,
          }));
        }
        head.appendChild(ui.el("span.dim", { text: " (" + (entry.source || "manual") + ")" }));
        host.appendChild(head);

        if (entry.baseline) {
          host.appendChild(ui.el("div.stat",
            ui.el("span.dim", { text: "baseline snapshot · no predecessor to diff against" })));
          return;
        }

        // The rule attribution shown against each side. It is the rule stored
        // on the snapshot, so an edited rule cannot re-explain an old change.
        const attribution = entry.rule
          ? "rule: " + entry.rule
          : "source: " + (entry.source || "manual");
        const added = entry.added_by_rule || entry.added || [];
        const dropped = entry.dropped_by_rule || entry.dropped || [];

        if (added.length) {
          host.appendChild(ui.el("div.stat",
            ui.el("span.ok", { text: "+ IN" }),
            document.createTextNode(" " + added.join(", ") + " "),
            ui.el("span.dim", { text: "(" + attribution + ")" })));
        }
        if (dropped.length) {
          host.appendChild(ui.el("div.stat",
            ui.el("span.bad", { text: "− OUT" }),
            document.createTextNode(" " + dropped.join(", ") + " "),
            ui.el("span.dim", { text: "(" + attribution + ")" })));
        }
        (entry.corporate_actions || []).forEach(function (action) {
          const row = ui.el("div.stat",
            ui.el("span.warn", { text: "Δ CORP" }),
            document.createTextNode(" " + action.from + "/" + action.to + " handled"));
          row.title = action.detail;
          host.appendChild(row);
        });
        if (entry.rule_changed) {
          host.appendChild(ui.el("div.stat",
            ui.el("span.warn", { text: "Δ RULE" }),
            document.createTextNode(" construction rule changed at this snapshot")));
        }
        if (!added.length && !dropped.length && !(entry.corporate_actions || []).length) {
          host.appendChild(ui.el("div.stat",
            ui.el("span.dim", { text: "no membership change" })));
        }
      });
      this.renderRuleFooter(payload);
    },

    /** The panel footer: which rule revision the history is on. */
    renderRuleFooter: function (payload) {
      const node = this.$("#pit-rule");
      if (!node) return;
      const record = this.currentCollection();
      const version = payload && payload.rule_version;
      const rule = record
        ? (record.rule || record.description || (this.selected.kind === "Watchlist"
          ? "Manual watchlist · no automatic refresh" : "No construction rule recorded"))
        : "";
      node.textContent = version
        ? "Rule def v" + version + " · " + rule
        : rule;
    },

    /** Real coverage: the screener's metric panel reports what it could price. */
    loadCoverage: function () {
      const self = this;
      const id = this.selected.id;
      const host = this.$("#pit-coverage");
      if (!host) return;
      host.innerHTML = '<div class="dim mini">measuring…</div>';
      const slider = this.$("#asof");
      const year = slider ? Number(slider.value) : null;
      const asOf = year && year < new Date().getUTCFullYear() ? year + "-06-30" : null;

      Promise.all([
        this.api.post("/api/screens/preview", {
          universe_id: id, rules: [], as_of: asOf,
        }, { signal: this.ctx.signal, timeoutMs: 120000 }),
        // Options IV has its own ingested series (options/iv30_<SYMBOL>), so the
        // row counts the members that really have one rather than omitting it.
        this.api.get("/api/quality/datasets", null, { signal: this.ctx.signal })
          .catch(function () { return { datasets: [] }; }),
      ]).then(function (results) {
        const payload = results[0];
        const stored = (results[1] && results[1].datasets) || [];
        if (!self.alive() || !self.selected || self.selected.id !== id) return;
        const coverage = (payload.coverage && payload.coverage.coverage) || {};
        const symbols = (payload.symbols || (payload.coverage && payload.coverage.symbols) || [])
          .map(function (item) {
            return String(typeof item === "string" ? item : (item.symbol || "")).toUpperCase();
          }).filter(Boolean);
        const withIv = stored.filter(function (ref) {
          return ref.namespace === "options" && /^iv\d+_/.test(String(ref.name || ""));
        }).map(function (ref) {
          return String(ref.name).split("_").slice(1).join("_").toUpperCase();
        });
        const ivCovered = symbols.length
          ? symbols.filter(function (sym) { return withIv.indexOf(sym) >= 0; }).length
          : withIv.length;
        host.innerHTML = "";
        const rows = [
          ["Requested", coverage.requested, coverage.requested],
          ["Evaluated", coverage.evaluated, coverage.requested],
          ["Priced", coverage.priced, coverage.requested],
          ["Fundamentals", coverage.with_fundamentals, coverage.requested],
          ["Options IV", ivCovered, symbols.length || coverage.requested],
        ];
        rows.forEach(function (row) {
          if (row[1] === undefined || row[1] === null) return;
          const complete = row[2] ? row[1] >= row[2] : true;
          host.appendChild(self.ui.kv(row[0], row[1] + (row[2] ? "/" + row[2] : ""), {
            tone: complete ? "ok" : "warn",
          }));
        });
        self.$("#pit-covnote").textContent = (coverage.price_source
          ? "Source: " + coverage.price_source
            + (coverage.truncated ? " · truncated at " + coverage.max_symbols + " symbols" : "")
          : "Measured by the screener's metric panel.")
          + " Options IV counts members with an ingested options/iv30 series"
          + (withIv.length ? "" : " — subscribe the options-iv source to populate it.");
      }).catch(function (err) {
        if (err.cancelled || !self.alive()) return;
        host.innerHTML = "";
        host.appendChild(self.ui.el("div.bad.mini", { text: errorText(err) }));
      });
    },

    // ---------------------------------------------------- collection dialog

    wireCollectionDialog: function () {
      const self = this;
      const dialog = this.$("#collectionDialog");
      this.$("#closeCollectionDialog").addEventListener("click", function () { dialog.close(); });
      this.$("#cancelCollection").addEventListener("click", function () { dialog.close(); });
      this.$$("[data-collection-type]").forEach(function (button) {
        button.addEventListener("click", function () {
          if (self._editing) return;   // the kind of an existing collection is fixed
          self.setCollectionType(button.dataset.collectionType);
        });
      });
      this.$("#collectionForm").addEventListener("submit", function (event) {
        event.preventDefault();
        self.submitCollection();
      });
    },

    setCollectionType: function (type) {
      if (!this.alive()) return;
      this.$("#collectionType").value = type;
      this.$("#collectionDialogTitle").textContent = (this._editing ? "Edit " : "New ")
        + type.toLowerCase();
      this.$$("[data-collection-type]").forEach(function (button) {
        button.classList.toggle("active", button.dataset.collectionType === type);
      });
    },

    openCollectionDialog: function (type, record) {
      // The previous mount's markup stays in #view-data while another workspace
      // is showing; its handlers still point here, so ignore them once this
      // page has been unmounted.
      if (!this.alive()) return;
      this._editing = record || null;
      this.setCollectionType(type);
      this.$("#collectionName").value = record ? record.name : "";
      this.$("#collectionDescription").value = (record && record.description) || "";
      this.$("#collectionSymbols").value = record
        ? ((record.symbols && record.symbols.join
          ? record.symbols.join(", ")
          : (record.members || []).map(function (m) { return m.symbol; }).join(", ")) || "")
        : "";
      this.$("#makeDefault").checked = false;
      this.$("#collectionSubmit").textContent = record ? "Save changes" : "Create collection";
      this.$$("[data-collection-type]").forEach(function (button) {
        button.disabled = !!record;
      });
      const dialog = this.$("#collectionDialog");
      dialog.showModal();
      const name = this.$("#collectionName");
      setTimeout(function () { name.focus(); }, 0);
    },

    submitCollection: function () {
      const self = this;
      if (!this.alive()) return;
      const type = this.$("#collectionType").value;
      const name = this.$("#collectionName").value.trim();
      if (!name) return;
      const symbols = this.$("#collectionSymbols").value;
      const editing = this._editing;

      const body = {
        collectionName: name,
        collectionSymbols: symbols,
        collectionDescription: this.$("#collectionDescription").value.trim() || null,
        constructionMethod: this.$("#constructionMethod").value,
        collectionRefresh: this.$("#collectionRefresh").value,
        makeDefault: this.$("#makeDefault").checked,
      };

      let request;
      if (editing && type === "Universe") {
        request = this.api.universes.update(editing.id, {
          name: name, description: body.collectionDescription, symbols: symbols,
        });
      } else if (editing) {
        request = this.api.watchlists.update(editing.id, {
          name: name, symbols: symbols, is_default: body.makeDefault || undefined,
        });
      } else if (type === "Universe") {
        request = this.api.universes.create(body);
      } else {
        request = this.api.watchlists.create(body);
      }

      const submit = this.$("#collectionSubmit");
      submit.disabled = true;
      request.then(function (payload) {
        self.$("#collectionDialog").close();
        self.$("#collectionForm").reset();
        self._editing = null;
        global.toast.ok(type + " “" + name + "” " + (editing ? "saved" : "created"));
        if (payload && payload.ignored_fields && payload.ignored_fields.length) {
          global.toast.warn(payload.ignored_detail
            || ("ignored: " + payload.ignored_fields.join(", ")));
        }
        self.selected = { kind: type, id: payload.id };
        self.loadCollections();
      }).catch(function (err) {
        console.error("collection save failed", err);
        global.toast.error(errorText(err));
      }).then(function () {
        submit.disabled = false;
      });
    },

    // ======================================================================
    // TAB 2 — Screener
    // ======================================================================

    loadScreener: function () {
      const self = this;
      const host = this.$("#screen-wrap");
      if (this._screenerReady) {
        this.refreshSavedScreens();
        return;
      }
      host.innerHTML = '<div class="panel dim">loading the metric catalogue…</div>';

      Promise.all([
        this.api.get("/api/screens/metrics", null, { signal: this.ctx.signal }),
        this.api.screens.list(null, { signal: this.ctx.signal }),
        this.universes.length && this.activeUniverse
          ? Promise.resolve({ universes: this.universes, active: this.activeUniverse })
          : this.api.universes.list({ include_members: true }, { signal: this.ctx.signal }),
      ]).then(function (results) {
        if (!self.alive()) return;
        self.metrics = results[0];
        self.screens = (results[1] && results[1].screens) || [];
        self.universes = (results[2] && results[2].universes) || self.universes;
        self.activeUniverse = (results[2] && results[2].active) || self.activeUniverse;
        self.buildScreener();
        self._screenerReady = true;
        self.loadScreenAlerts();
      }).catch(function (err) {
        if (err.cancelled || !self.alive()) return;
        host.innerHTML = "";
        host.appendChild(self.ui.errorPanel("Screener", err));
      });
    },

    /** Default rules: the four REFERENCE criteria plus the three TL rules,
     *  filtered to the metrics this build actually screens on. */
    defaultRules: function () {
      const known = {};
      (this.metrics.metrics || []).forEach(function (metric) { known[metric.key] = metric; });
      const wanted = [
        { field: "mkt_cap", operator: ">", value: 2e9 },
        { field: "rev_growth", operator: ">", value: 15 },
        { field: "fcf_margin", operator: ">", value: 5 },
        { field: "country", operator: "in", value: "Any" },
        { field: "roic", operator: "top_pctl", value: 30, scope: "sector" },
        { field: "mom_12_1", operator: "top_pctl", value: 25 },
        { field: "net_debt_ebitda", operator: "<", value: 2.0 },
      ];
      return wanted.filter(function (rule) { return !!known[rule.field]; });
    },

    buildScreener: function () {
      const self = this;
      const host = this.$("#screen-wrap");
      this.rules = this.defaultRules();
      this.rankBy = "composite";
      this.currentScreen = null;
      this.lastRun = null;

      host.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:11px">
          <section class="panel" id="saved-screens">
            <div class="ptitle">SAVED SCREENS</div>
            <div id="savedScreenList"></div>
            <div class="dim mini" style="padding:6px 2px;cursor:pointer" id="new-screen">+ NEW SCREEN</div>
          </section>
          <section class="panel">
            <div class="ptitle">BASE UNIVERSE</div>
            <select class="select" id="screenUniverse" style="width:100%"></select>
            <div class="dim mini" id="screenUniverseNote" style="margin-top:6px"></div>
          </section>
        </div>
        <div id="screen-main" style="display:flex;flex-direction:column;gap:11px;min-width:0">
          <section class="panel form-card">
            <div class="panel-title" style="padding:0 0 8px">Screen criteria
              <span style="color:var(--dim);font-weight:400">Pass funnel</span></div>
            <div id="ruleList"></div>
            <div id="ruleFoot" style="display:flex;gap:8px;align-items:center;border-top:1px solid var(--line);margin-top:8px;padding-top:8px;flex-wrap:wrap">
              <button class="btn" id="add-rule" type="button">+ Add rule</button>
              <span class="dim mini">Rank by</span>
              <select class="select" id="rank-by" style="height:26px"></select>
              <button class="btn primary" id="run-screen" type="button" style="margin-left:auto">Run screen</button>
              <span class="blu" id="query-toggle" role="button" tabindex="0" style="cursor:pointer">View as query ⌄</span>
            </div>
            <div id="screen-query"></div>
          </section>
          <div id="screen-results">
            <section class="panel">
              <div class="panel-title">Results <span class="badge" id="resultBadge">not run</span></div>
              <table class="data-table" id="resultTable">
                <thead><tr><th>#</th><th>Symbol</th><th class="num">Composite</th><th class="num">ROIC</th><th class="num">Mom 12-1</th></tr></thead>
                <tbody><tr><td class="muted" colspan="5">Run the screen to see the names that pass.</td></tr></tbody>
              </table>
            </section>
            <div style="display:flex;flex-direction:column;gap:11px">
              <section class="panel form-card">
                <div class="panel-title" style="padding:0 0 8px">Result profile <span class="badge" id="rp-count">—</span></div>
                <div class="form-row"><span>Sector mix</span><span id="rp-sectors">—</span></div>
                <div class="form-row"><span>Median mkt cap</span><span id="rp-median">—</span></div>
                <div class="form-row"><span>Turnover vs last run</span><span class="yellow" id="rp-turnover">—</span></div>
                <div class="form-row"><span>Overlap w/ live book</span><span id="rp-overlap">—</span></div>
              </section>
              <section class="panel form-card" style="display:flex;flex-direction:column;gap:8px">
                <button class="btn primary" id="sc-backtest" type="button">→ Backtest this screen</button>
                <button class="btn" id="sc-universe" type="button">Save as universe</button>
                <button class="btn" id="sc-pin" type="button">Pin + alert on changes</button>
              </section>
              <section class="panel form-card" id="screenAlerts"></section>
            </div>
          </div>
        </div>`;

      // universe selector
      const select = this.$("#screenUniverse");
      this.universes.forEach(function (universe) {
        const option = document.createElement("option");
        option.value = universe.id;
        option.textContent = universe.name;
        if (universe.id === self.activeUniverse) option.selected = true;
        select.appendChild(option);
      });
      select.addEventListener("change", function () {
        self.renderScreenUniverseNote();
        self.runFunnel();
      });
      this.renderScreenUniverseNote();

      // rank-by options — the numeric metrics the backend ranks on
      const rank = this.$("#rank-by");
      (this.metrics.metrics || []).filter(function (metric) {
        return !metric.categorical;
      }).forEach(function (metric) {
        const option = document.createElement("option");
        option.value = metric.key;
        option.textContent = metric.key;
        if (metric.key === "composite") option.selected = true;
        rank.appendChild(option);
      });
      rank.addEventListener("change", function () {
        self.rankBy = rank.value;
        if (self.lastRun) self.renderResults(self.lastRun);
      });

      this.$("#add-rule").addEventListener("click", function () {
        const numeric = (self.metrics.metrics || []).filter(function (m) { return !m.categorical; })[0];
        self.rules.push({ field: numeric ? numeric.key : "quality", operator: ">", value: 0 });
        self.renderRules();
        self.runFunnel();
        global.toast("Rule added");
      });
      this.$("#run-screen").addEventListener("click", function (event) {
        self.runScreen(event.currentTarget);
      });
      this.$("#new-screen").addEventListener("click", function () {
        self.currentScreen = null;
        self.rules = self.defaultRules();
        self.renderSavedScreens();
        self.renderRules();
        self.runFunnel();
        global.toast("New screen — save it by running it");
      });
      const toggle = this.$("#query-toggle");
      const flip = function () {
        const query = self.$("#screen-query");
        const open = query.classList.toggle("open");
        toggle.textContent = open ? "View as query ⌃" : "View as query ⌄";
      };
      toggle.addEventListener("click", flip);
      toggle.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); flip(); }
      });

      this.$("#sc-backtest").addEventListener("click", function (event) {
        self.backtestScreen(event.currentTarget);
      });
      this.$("#sc-universe").addEventListener("click", function (event) {
        self.saveScreenAsUniverse(event.currentTarget);
      });
      this.$("#sc-pin").addEventListener("click", function (event) {
        self.pinScreen(event.currentTarget);
      });

      const list = this.$("#ruleList");
      list.addEventListener("input", function (event) {
        const field = event.target.closest('[data-k="value"]');
        if (field) self.editRule(field, false);
      });
      list.addEventListener("change", function (event) {
        const field = event.target.closest("[data-k]");
        if (field && field.dataset.k !== "value") self.editRule(field, true);
      });
      list.addEventListener("click", function (event) {
        const drop = event.target.closest("[data-drop]");
        if (!drop) return;
        if (self.rules.length === 1) {
          global.toast.warn("A screen needs at least one rule");
          return;
        }
        self.rules.splice(Number(drop.dataset.drop), 1);
        self.renderRules();
        self.runFunnel();
      });

      this.renderSavedScreens();
      this.renderRules();
      this.runFunnel();
    },

    renderScreenUniverseNote: function () {
      const self = this;
      const id = this.$("#screenUniverse").value;
      const universe = this.universes.filter(function (u) { return u.id === id; })[0];
      const note = this.$("#screenUniverseNote");
      if (!universe) { note.textContent = ""; return; }
      const count = universe.member_count || universe.symbol_count || 0;
      note.textContent = count
        ? count + " names · " + (universe.builtin ? "built-in" : "user universe")
        : "no members stored yet — refresh it on the Universes tab";
    },

    refreshSavedScreens: function () {
      const self = this;
      this.api.screens.list(null, { signal: this.ctx.signal }).then(function (payload) {
        if (!self.alive()) return;
        self.screens = (payload && payload.screens) || [];
        self.renderSavedScreens();
      }).catch(function (err) {
        if (!err.cancelled) console.warn("screens reload failed", err);
      });
    },

    renderSavedScreens: function () {
      const self = this;
      const host = this.$("#savedScreenList");
      if (!host) return;
      host.innerHTML = "";
      if (!this.screens.length) {
        host.appendChild(this.ui.el("div.dim.mini", {
          text: "No saved screens yet. Running a screen saves it.",
        }));
        return;
      }
      this.screens.forEach(function (screen) {
        const row = self.ui.el("div.rowitem");
        if (self.currentScreen && screen.id === self.currentScreen.id) row.classList.add("sel");
        row.appendChild(document.createTextNode(screen.name));
        row.appendChild(document.createElement("br"));
        row.appendChild(self.ui.el("span.dim.mini", {
          text: (screen.active_rule_count || 0) + " active rules · "
            + (screen.universe_id || "—"),
        }));
        row.addEventListener("click", function () { self.selectScreen(screen); });
        host.appendChild(row);
      });
    },

    selectScreen: function (screen) {
      this.currentScreen = screen;
      this.rules = (screen.rules || []).map(function (rule) {
        return {
          field: rule.field,
          operator: rule.operator || rule.op,
          value: rule.value,
          value2: rule.value2,
          scope: rule.scope,
          source: rule.source,
        };
      });
      this.rankBy = screen.rank_by || "composite";
      const rank = this.$("#rank-by");
      if (rank) rank.value = this.rankBy;
      const select = this.$("#screenUniverse");
      if (select && screen.universe_id) select.value = screen.universe_id;
      this.renderScreenUniverseNote();
      this.renderSavedScreens();
      this.renderRules();
      this.runFunnel();
      global.toast("Loaded screen “" + screen.name + "”");
    },

    metricDef: function (key) {
      const metrics = (this.metrics && this.metrics.metrics) || [];
      return metrics.filter(function (metric) { return metric.key === key; })[0] || null;
    },

    /** Format a rule's value for its input box (market cap is edited in $bn). */
    ruleInputValue: function (rule) {
      const def = this.metricDef(rule.field);
      if (rule.operator === "top_pctl" || rule.operator === "bottom_pctl") return rule.value;
      if (def && def.unit === "usd" && typeof rule.value === "number") return rule.value / 1e9;
      return rule.value;
    },

    renderRules: function () {
      const self = this;
      const list = this.$("#ruleList");
      const metrics = (this.metrics.metrics || []);
      const operators = this.metrics.operators || [">", "<", "top_pctl", "in"];
      const scopes = this.metrics.scopes || ["all", "sector"];
      list.innerHTML = "";

      this.rules.forEach(function (rule, index) {
        const def = self.metricDef(rule.field);
        const row = self.ui.el("div.rule-row");
        const parts = self.ui.el("div.rule-parts");

        const field = self.ui.el("select.select", { dataset: { k: "field", i: index } });
        metrics.forEach(function (metric) {
          const option = document.createElement("option");
          option.value = metric.key;
          option.textContent = metric.key;
          option.title = metric.description || "";
          if (metric.key === rule.field) option.selected = true;
          field.appendChild(option);
        });
        parts.appendChild(field);

        const operator = self.ui.el("select.select", { dataset: { k: "operator", i: index } });
        operators.forEach(function (op) {
          const option = document.createElement("option");
          option.value = op;
          option.textContent = op === ">" ? ">" : op === "<" ? "<"
            : op === "top_pctl" ? "top pctl" : op === "bottom_pctl" ? "bottom pctl"
              : op === "in" ? "is" : op === "not_in" ? "is not" : op;
          if (op === rule.operator) option.selected = true;
          operator.appendChild(option);
        });
        parts.appendChild(operator);

        if (rule.operator === "in" || rule.operator === "not_in") {
          const value = self.ui.el("input.input", {
            dataset: { k: "value", i: index },
            value: rule.value === null || rule.value === undefined ? "Any" : String(rule.value),
          });
          value.style.width = "132px";
          parts.appendChild(value);
        } else if (rule.operator === "between") {
          parts.appendChild(self.ui.el("input.input", {
            dataset: { k: "value", i: index }, value: String(self.ruleInputValue(rule)),
          }));
          parts.appendChild(self.ui.el("span.dim.mini", { text: "and" }));
          parts.appendChild(self.ui.el("input.input", {
            dataset: { k: "value2", i: index },
            value: rule.value2 === undefined || rule.value2 === null ? "" : String(rule.value2),
          }));
        } else {
          parts.appendChild(self.ui.el("input.input", {
            dataset: { k: "value", i: index }, value: String(self.ruleInputValue(rule)),
          }));
        }

        const unit = rule.operator === "top_pctl" || rule.operator === "bottom_pctl" ? "%"
          : def && def.unit === "usd" ? "B"
            : def && def.unit === "pct" ? "%"
              : def && def.unit === "x" ? "×" : "";
        if (unit) parts.appendChild(self.ui.el("span.dim.mini", { text: unit }));

        if (rule.operator === "top_pctl" || rule.operator === "bottom_pctl") {
          const scope = self.ui.el("select.select", { dataset: { k: "scope", i: index } });
          scopes.forEach(function (item) {
            const option = document.createElement("option");
            option.value = item;
            option.textContent = item === "sector" ? "within sector" : "overall";
            if (item === (rule.scope || "all")) option.selected = true;
            scope.appendChild(option);
          });
          parts.appendChild(scope);
        }
        if (def) parts.appendChild(self.ui.el("span.dim.mini", { text: "← " + def.source }));

        row.appendChild(parts);
        const right = self.ui.el("span", { style: "display:flex;align-items:center;gap:6px" });
        const funnel = self.ui.el("span.funnel", { text: "…" });
        funnel.dataset.funnel = String(index);
        if (index === self.rules.length - 1) funnel.classList.add("last");
        right.appendChild(funnel);
        right.appendChild(self.ui.el("button.rule-drop", {
          type: "button", dataset: { drop: index }, "aria-label": "Remove rule", text: "×",
        }));
        row.appendChild(right);
        list.appendChild(row);
      });
    },

    editRule: function (element, structural) {
      const index = Number(element.dataset.i);
      const rule = this.rules[index];
      if (!rule) return;
      const key = element.dataset.k;

      if (key === "field") {
        rule.field = element.value;
        delete rule.source;
        const def = this.metricDef(rule.field);
        if (def && def.categorical) { rule.operator = "in"; rule.value = "Any"; }
        else if (rule.operator === "in" || rule.operator === "not_in") {
          rule.operator = ">"; rule.value = 0;
        }
      } else if (key === "operator") {
        rule.operator = element.value;
        if (rule.operator === "in" || rule.operator === "not_in") rule.value = "Any";
        else if (rule.operator === "top_pctl" || rule.operator === "bottom_pctl") {
          rule.value = 25; rule.scope = rule.scope || "all";
        } else if (typeof rule.value !== "number") rule.value = 0;
      } else if (key === "scope") {
        rule.scope = element.value;
      } else if (key === "value2") {
        rule.value2 = Number(element.value);
      } else {
        const def = this.metricDef(rule.field);
        if (rule.operator === "in" || rule.operator === "not_in") {
          rule.value = element.value;
        } else if (def && def.unit === "usd" && rule.operator !== "top_pctl"
          && rule.operator !== "bottom_pctl") {
          rule.value = Number(element.value) * 1e9;
        } else {
          rule.value = Number(element.value);
        }
        if (typeof rule.value === "number" && isNaN(rule.value)) rule.value = 0;
      }
      // Only a value edit leaves the markup valid; anything else rebuilds it
      // (rebuilding on every keystroke would drop the caret).
      if (structural) this.renderRules();
      this.runFunnel();
    },

    /** Rules in the shape the API takes, dropping the UI-only fields. */
    apiRules: function () {
      return this.rules.map(function (rule) {
        const out = { field: rule.field, operator: rule.operator, value: rule.value };
        if (rule.value2 !== undefined && rule.value2 !== null) out.value2 = rule.value2;
        if (rule.scope) out.scope = rule.scope;
        return out;
      });
    },

    /** Real per-stage counts: one preview per rule prefix, newest wins. */
    runFunnel: function () {
      const self = this;
      if (this._funnelTimer) clearTimeout(this._funnelTimer);
      this._funnelTimer = setTimeout(function () {
        self._funnelTimer = 0;
        self.runFunnelNow();
      }, 350);
    },

    runFunnelNow: function () {
      const self = this;
      if (!this.alive()) return;
      if (this._funnelController) this._funnelController.abort();
      const controller = new AbortController();
      this._funnelController = controller;
      const rules = this.apiRules();
      const universeId = this.$("#screenUniverse").value;
      const token = ++this._funnelToken;

      const signal = controller.signal;
      const counts = [];
      let chain = Promise.resolve(null);
      rules.forEach(function (_rule, index) {
        chain = chain.then(function () {
          if (signal.aborted) return null;
          return self.api.post("/api/screens/preview", {
            universe_id: universeId,
            rules: rules.slice(0, index + 1),
            rank_by: self.rankBy,
          }, { signal: signal, timeoutMs: 120000 }).then(function (payload) {
            counts.push({ count: payload.count, base: payload.base_count, query: payload.query });
            if (token !== self._funnelToken) return null;
            self.paintFunnel(counts, payload.base_count);
            self.$("#screen-query").textContent = payload.query || "";
            return null;
          });
        });
      });
      chain.catch(function (err) {
        if (err.cancelled || signal.aborted || !self.alive()) return;
        console.warn("funnel preview failed", err);
        self.$$("[data-funnel]").forEach(function (node) {
          node.textContent = "preview failed";
          node.classList.add("bad");
        });
        global.toast.error("Rule preview failed: " + errorText(err));
      });
    },
    _funnelToken: 0,

    paintFunnel: function (counts, base) {
      const self = this;
      this.$$("[data-funnel]").forEach(function (node) {
        const index = Number(node.dataset.funnel);
        const entry = counts[index];
        node.classList.remove("bad");
        if (!entry) { node.textContent = "…"; return; }
        const before = index === 0 ? base : counts[index - 1].count;
        node.innerHTML = "";
        node.appendChild(document.createTextNode(intText(before) + " → "));
        node.appendChild(self.ui.el("b", { text: intText(entry.count) }));
      });
    },

    /** Persist the current rules as a screen (creating one when needed). */
    saveCurrentScreen: function () {
      const self = this;
      const universeId = this.$("#screenUniverse").value;
      const rules = this.apiRules();
      if (this.currentScreen) {
        return this.api.screens.update(this.currentScreen.id, {
          rules: rules, rank_by: this.rankBy, universe_id: universeId,
        }).then(function (screen) {
          self.currentScreen = screen;
          return screen;
        });
      }
      const stamp = new Date().toISOString().slice(0, 16).replace("T", " ");
      return this.api.screens.create({
        screenName: "Screen " + stamp,
        universeId: universeId,
        rules: rules,
        rank_by: this.rankBy,
        description: "Created from the Market & Data screener.",
      }).then(function (screen) {
        self.currentScreen = screen;
        self.refreshSavedScreens();
        return screen;
      });
    },

    runScreen: function (button) {
      const self = this;
      return this.withBusy(button, function () {
        return self.saveCurrentScreen().then(function (screen) {
          return self.api.screens.run(screen.id, { limit: 200 }, { timeoutMs: 180000 });
        }).then(function (run) {
          if (!self.alive()) return;
          self.lastRun = run;
          self.renderResults(run);
          global.toast.ok("Screen executed · " + intText(run.pass_count) + " of "
            + intText(run.base_count) + " names pass");
        });
      });
    },

    renderResults: function (run) {
      const self = this;
      const rank = this.rankBy;
      const survivors = (run.survivors || []).slice();
      survivors.sort(function (a, b) {
        const left = Number(a[rank]);
        const right = Number(b[rank]);
        if (isNaN(left) && isNaN(right)) return 0;
        if (isNaN(left)) return 1;
        if (isNaN(right)) return -1;
        return right - left;
      });

      this.$("#resultBadge").textContent = intText(run.pass_count) + " symbols";
      const body = this.$("#resultTable").querySelector("tbody");
      body.innerHTML = "";
      if (!survivors.length) {
        body.appendChild(this.ui.el("tr", this.ui.el("td.muted", {
          colspan: "5", text: "No names pass this screen — loosen a rule.",
        })));
      }
      survivors.slice(0, 12).forEach(function (row, index) {
        const tr = self.ui.el("tr");
        tr.appendChild(self.ui.el("td.muted", { text: String(index + 1) }));
        tr.appendChild(self.ui.el("td", { text: row.symbol }));
        const composite = self.ui.el("td.num", { text: self.fmt.num(row.composite, 1) });
        if (Number(row.composite) >= 90) composite.classList.add("green");
        tr.appendChild(composite);
        tr.appendChild(self.ui.el("td.num", { text: self.fmt.pct(row.roic, { decimals: 1 }) }));
        tr.appendChild(self.ui.el("td.num", {
          text: row.mom_12_1_pctl === null || row.mom_12_1_pctl === undefined
            ? "—" : "p" + Math.round(Number(row.mom_12_1_pctl) * 100),
        }));
        body.appendChild(tr);
      });
      if (survivors.length > 12) {
        body.appendChild(this.ui.el("tr", this.ui.el("td.muted", {
          colspan: "5", text: "… " + (survivors.length - 12) + " more",
        })));
      }

      // The funnel now shows the run's own stages, which are authoritative.
      if (run.stages && run.stages.length) {
        this.$$("[data-funnel]").forEach(function (node) {
          const stage = run.stages[Number(node.dataset.funnel)];
          if (!stage) return;
          node.innerHTML = "";
          node.appendChild(document.createTextNode(intText(stage.input_count) + " → "));
          node.appendChild(self.ui.el("b", { text: intText(stage.output_count) }));
        });
      }

      const stats = run.stats || {};
      this.$("#rp-count").textContent = intText(stats.count) + " names";
      const mix = stats.sector_mix || [];
      this.$("#rp-sectors").textContent = mix.length
        ? mix.slice(0, 3).map(function (entry) {
          return entry.sector + " " + Math.round(entry.share * 100) + "%";
        }).join(" · ")
        : "—";
      this.$("#rp-median").textContent = stats.median_market_cap
        ? this.fmt.usd(stats.median_market_cap, { compact: true, decimals: 1 })
        : "—";
      const turnover = stats.turnover;
      this.$("#rp-turnover").textContent = turnover
        ? turnover.entered_count + " in / " + turnover.exited_count + " out (vs "
          + turnover.compared_with_run + ")"
        : "first run — nothing to compare against";
      const overlap = stats.live_book_overlap || {};
      this.$("#rp-overlap").textContent = overlap.source
        ? overlap.count + " names (" + overlap.source + ")"
        : (overlap.reason || "—");

      (run.warnings || []).forEach(function (warning) { global.toast.warn(warning); });
    },

    backtestScreen: function (button) {
      const self = this;
      const benchmark = (this.settings && this.settings.benchmark) || "SPY";
      return this.withBusy(button, function () {
        const ready = self.lastRun && self.currentScreen
          ? Promise.resolve(self.lastRun)
          : self.saveCurrentScreen().then(function (screen) {
            return self.api.screens.run(screen.id, { limit: 200 }, { timeoutMs: 180000 });
          }).then(function (run) {
            self.lastRun = run;
            self.renderResults(run);
            return run;
          });
        return ready.then(function (run) {
          return self.api.screens.queueBacktest(self.currentScreen.id, {
            run_id: run.id, benchmark: benchmark,
          }, { timeoutMs: 120000 });
        }).then(function (result) {
          const id = result.backtest_id || result.id || result.run_id;
          const count = (result.symbols && result.symbols.length)
            || result.symbol_count || (self.lastRun && self.lastRun.pass_count);
          global.toast.ok("Backtest " + (id || "queued") + " · " + intText(count)
            + " names vs " + benchmark);
          if (id) {
            self.ctx.router.go("lab", { sub: "backtests", params: { id: id } });
          } else if (result.detail) {
            global.toast.warn(result.detail);
          }
        });
      });
    },

    saveScreenAsUniverse: function (button) {
      const self = this;
      if (!this.lastRun) {
        global.toast.warn("Run the screen first — there are no survivors to save yet");
        return Promise.resolve();
      }
      const name = "Screen " + new Date().toISOString().slice(0, 10)
        + " (" + this.lastRun.pass_count + ")";
      return this.withBusy(button, function () {
        return self.api.universes.create({
          collectionName: name,
          collectionSymbols: self.lastRun.symbols || [],
          collectionDescription: "Survivors of " + (self.currentScreen
            ? self.currentScreen.name : "an unsaved screen") + " run " + self.lastRun.id,
          constructionMethod: "Screener result",
        }).then(function (universe) {
          global.toast.ok("Saved universe “" + universe.name + "” · "
            + (universe.symbol_count || 0) + " symbols");
          self.universes = [];
          self.loadCollections();
        });
      });
    },

    /**
     * REFERENCE's "PIN + ALERT ON CHANGES". Both halves are real: `pinned`
     * keeps the screen at the top of the saved list, `alert` enrols it with
     * the backend's membership monitor, which re-runs it on a cadence and
     * publishes an alert on the `notifications` channel whenever a symbol
     * enters or leaves. The first cycle records the baseline, so the toast
     * says when the watch actually begins.
     * @param {HTMLElement} button the control to show busy
     * @returns {Promise<void>}
     */
    pinScreen: function (button) {
      const self = this;
      return this.withBusy(button, function () {
        return self.saveCurrentScreen().then(function (screen) {
          const tags = (screen.tags || []).slice();
          if (tags.indexOf("pinned") < 0) tags.push("pinned");
          if (tags.indexOf("alert") < 0) tags.push("alert");
          return self.api.screens.update(screen.id, { tags: tags });
        }).then(function (screen) {
          self.currentScreen = screen;
          self.refreshSavedScreens();
          // Establish the baseline immediately rather than waiting a cycle.
          return self.api.post("/api/screens/alerts/check", null, { signal: self.ctx.signal })
            .then(function () { return self.loadScreenAlerts(); })
            .then(function () {
              const every = self.screenAlertMonitor && self.screenAlertMonitor.interval_seconds
                ? Math.round(self.screenAlertMonitor.interval_seconds / 60) + " min"
                : "the configured interval";
              global.toast.ok("“" + screen.name + "” pinned and watched — it is re-run every "
                + every + ", and any symbol entering or leaving raises an alert.");
            });
        });
      });
    },

    /**
     * Load the membership alerts raised on pinned screens and paint the
     * Screener's alert strip. Absent monitor (older backend) is reported, not
     * hidden.
     * @returns {Promise<void>}
     */
    loadScreenAlerts: function () {
      const self = this;
      return this.api.optional(
        this.api.get("/api/screens/alerts", { limit: 20 }, { signal: this.ctx.signal })
      ).then(function (payload) {
        if (!self.alive()) return;
        self.screenAlerts = (payload && payload.alerts) || [];
        self.screenAlertMonitor = (payload && payload.monitor) || null;
        self.renderScreenAlerts();
      }).catch(function (err) {
        if (err && err.cancelled) return;
        console.warn("screener: loading membership alerts failed", err);
      });
    },

    /** Paint #screenAlerts from `this.screenAlerts`. */
    renderScreenAlerts: function () {
      const self = this;
      const host = this.$("#screenAlerts");
      if (!host) return;
      host.innerHTML = "";
      const monitor = this.screenAlertMonitor;
      const watched = (monitor && monitor.watched) || [];
      const head = this.ui.el("div.panel-title", { text: "Membership alerts" });
      head.appendChild(this.ui.badge(
        watched.length ? watched.length + " screen" + (watched.length === 1 ? "" : "s") + " watched"
          : "no screen pinned"));
      host.appendChild(head);

      if (monitor && monitor.running === false) {
        host.appendChild(this.ui.el("div.list-row",
          this.ui.el("span.yellow", {
            text: "The alert monitor is not running in this process, so pinned screens are"
              + " not being re-run.",
          })));
      }
      const alerts = this.screenAlerts || [];
      if (!alerts.length) {
        host.appendChild(this.ui.el("div.list-row", this.ui.el("span", {
          text: watched.length
            ? "No membership change since the baseline was recorded"
              + (monitor && monitor.last_cycle_at
                ? " (last checked " + agoText(monitor.last_cycle_at, this.fmt) + ")." : ".")
            : "Pin a screen to watch its membership. Nothing is watched yet.",
        })));
        return;
      }
      alerts.forEach(function (alert) {
        const detail = self.ui.el("span");
        detail.appendChild(document.createTextNode(
          agoText(alert.detected_at, self.fmt) + " · "
          + alert.previous_count + " → " + alert.pass_count));
        const row = self.ui.el("div.list-row",
          self.ui.el("b", { text: alert.headline }), detail);
        if (!alert.acknowledged_at) {
          const ack = self.ui.button("Acknowledge", {
            small: true,
            onClick: function (event) {
              self.withBusy(event.currentTarget, function () {
                return self.api.post("/api/screens/alerts/"
                  + encodeURIComponent(alert.id) + "/acknowledge", null)
                  .then(function () { return self.loadScreenAlerts(); });
              });
            },
          });
          row.appendChild(ack);
        }
        host.appendChild(row);
      });
    },

    // ======================================================================
    // TAB 3 — Source Catalog
    // ======================================================================

    loadCatalog: function () {
      const self = this;
      Promise.all([
        this.api.ingest.sources(null, { signal: this.ctx.signal }),
        this.api.ingest.subscriptions(null, { signal: this.ctx.signal }),
        this.universes.length && this.activeUniverse
          ? Promise.resolve({ universes: this.universes, active: this.activeUniverse })
          : this.api.universes.list({ include_members: true }, { signal: this.ctx.signal }),
        this.api.watchlists.list(null, { signal: this.ctx.signal }),
      ]).then(function (results) {
        if (!self.alive()) return;
        self.sources = (results[0] && results[0].sources) || [];
        self.subscriptions = (results[1] && results[1].subscriptions) || [];
        self.universes = (results[2] && results[2].universes) || self.universes;
        self.activeUniverse = (results[2] && results[2].active) || self.activeUniverse;
        self.watchlists = (results[3] && results[3].watchlists) || self.watchlists;
        if (!self.catalogSourceId
          || !self.sources.filter(function (s) { return s.id === self.catalogSourceId; }).length) {
          self.catalogSourceId = self.sources.length ? self.sources[0].id : null;
        }
        self.renderCatalog();
        self.renderActiveSubscriptions();
      }).catch(function (err) {
        if (err.cancelled || !self.alive()) return;
        self.$("#catalogDetail").innerHTML = "";
        self.$("#catalogDetail").appendChild(self.ui.errorPanel("Source catalog", err));
      });
    },

    catalogSource: function () {
      const self = this;
      return this.sources.filter(function (s) { return s.id === self.catalogSourceId; })[0] || null;
    },

    renderCatalog: function () {
      const self = this;
      this.$("#catalogCount").textContent = this.sources.length
        + (this.sources.length === 1 ? " source" : " sources");

      const list = this.$("#catalogSourceList");
      list.innerHTML = "";
      this.sources.forEach(function (source) {
        const state = source.state || {};
        const pill = STATUS_PILL[state.status] || { text: state.status_label || "Unknown", cls: "available" };
        const button = self.ui.el("button.catalog-source", {
          type: "button",
          "aria-pressed": String(source.id === self.catalogSourceId),
        });
        const name = self.ui.el("span.catalog-source-name");
        name.appendChild(document.createTextNode(source.name));
        name.appendChild(self.ui.el("span.catalog-status." + pill.cls, { text: pill.text }));
        button.appendChild(name);
        button.appendChild(self.ui.el("span.catalog-source-meta", {
          text: (source.protocol_label || source.protocol) + " · " + (source.data_domain_label || ""),
        }));
        button.addEventListener("click", function () {
          self.catalogSourceId = source.id;
          // The granularity chips are per-source, so a stale selection must not
          // survive a switch to a source that does not offer it.
          self.catalogGranularity = null;
          self.renderCatalog();
        });
        list.appendChild(button);
      });

      const request = this.$("#requestCatalogSource");
      if (!request.dataset.wired) {
        request.dataset.wired = "1";
        request.addEventListener("click", function () {
          self.resetIngestForm();
          self.showTab("ingestion");
          self.loadIngestion();
          global.toast("Describe the new source in the ingestion form and test it");
        });
      }

      this.renderCatalogDetail();
      this.renderSubscriptionPanel();
    },

    renderCatalogDetail: function () {
      const self = this;
      const host = this.$("#catalogDetail");
      const source = this.catalogSource();
      host.innerHTML = "";
      if (!source) {
        host.appendChild(this.ui.el("div.catalog-empty", {
          text: "No ingestion sources are registered in this build.",
        }));
        return;
      }
      const state = source.state || {};
      const pill = STATUS_PILL[state.status] || { text: state.status_label || "Unknown", cls: "available" };

      const head = this.ui.el("div.catalog-detail-head");
      const left = this.ui.el("div");
      const title = this.ui.el("h2");
      title.appendChild(document.createTextNode(source.name));
      title.appendChild(this.ui.el("span.catalog-status." + pill.cls, { text: pill.text }));
      left.appendChild(title);
      const summary = this.ui.el("p");
      summary.appendChild(document.createTextNode(source.endpoint || source.provider || "—"));
      summary.appendChild(document.createElement("br"));
      summary.appendChild(document.createTextNode(
        [
          (source.builtin ? "BUILT-IN" : "USER SOURCE"),
          (source.protocol_label || source.protocol || "").toUpperCase(),
          "AUTH " + String((source.auth && source.auth.label) || "none").toUpperCase()
            + ((source.auth && source.auth.configured) ? " ✓" : " — NOT CONFIGURED"),
          (source.schedule_detail && source.schedule_detail.label) || source.schedule || "",
        ].join(" · ")
      ));
      left.appendChild(summary);
      head.appendChild(left);
      head.appendChild(this.ui.button("Credentials", {
        onClick: function () {
          if (source.auth && source.auth.configured) {
            global.toast.ok(source.name + " credentials are configured ("
              + (source.auth.label || "none") + ")");
          } else {
            global.toast.warn("Add credentials for " + source.name + " in Settings → API keys");
            self.ctx.router.go("settings");
          }
        },
      }));
      host.appendChild(head);

      // protocols
      const protocols = this.ui.el("div.catalog-section");
      protocols.appendChild(this.ui.el("span.catalog-label", { text: "Protocols supported" }));
      const pills = this.ui.el("div.protocol-pills");
      const names = ["WebSocket", "REST", "Polling", "Database", "File"];
      if (source.protocol === "library") names.push("Python library");
      names.forEach(function (name) {
        const token = name === "Python library" ? "library" : PROTOCOLS[name].token;
        const enabled = token === source.protocol;
        const button = self.ui.el("button.protocol-pill", { type: "button", text: name });
        if (enabled) {
          button.classList.add("enabled", "selected");
        } else {
          button.disabled = true;
        }
        pills.appendChild(button);
      });
      protocols.appendChild(pills);
      protocols.appendChild(this.ui.el("span.catalog-dataset-note", {
        text: "A source speaks exactly one protocol; the connector class follows from it.",
      }));
      host.appendChild(protocols);

      // datasets = the subscriptions this source carries
      const datasets = this.ui.el("div.catalog-section");
      datasets.appendChild(this.ui.el("span.catalog-label", {
        text: "Subscriptions · select to configure",
      }));
      const grid = this.ui.el("div.catalog-dataset-grid");
      const subs = this.subscriptions.filter(function (sub) { return sub.source_id === source.id; });
      if (!subs.length) {
        grid.appendChild(this.ui.el("div.catalog-dataset-note", {
          text: "No subscription configured for this source yet. The panel on the right"
            + " creates one; the source still ingests its own configured scope.",
        }));
      }
      subs.forEach(function (sub) {
        const card = self.ui.el("button.catalog-dataset", {
          type: "button",
          "aria-pressed": String(sub.id === self.catalogSubscriptionId),
        });
        card.appendChild(self.ui.el("strong", { text: sub.channel }));
        card.appendChild(self.ui.el("span.catalog-dataset-meta", {
          text: (sub.active ? "ACTIVE" : "PAUSED") + " · " + (sub.scope || []).length + " scope items",
        }));
        card.appendChild(self.ui.el("span.catalog-dataset-note", {
          text: (sub.scope || []).join(", ") || "no explicit scope — the source's own scope applies",
        }));
        card.addEventListener("click", function () {
          self.catalogSubscriptionId = sub.id;
          self.renderCatalogDetail();
          self.renderSubscriptionPanel();
        });
        grid.appendChild(card);
      });
      datasets.appendChild(grid);
      host.appendChild(datasets);

      // observed state
      const observed = this.ui.el("div.catalog-section");
      observed.appendChild(this.ui.el("span.catalog-label", { text: "Observed state" }));
      observed.appendChild(this.ui.kv("Status", state.status_label || "unknown", {
        tone: state.status === "connected" ? "ok" : state.status === "unknown" ? "" : "warn",
      }));
      observed.appendChild(this.ui.kv("Last ingest", agoText(state.last_ingest_at, this.fmt)));
      observed.appendChild(this.ui.kv("Rows ingested", intText(state.rows_ingested)));
      observed.appendChild(this.ui.kv("Latency",
        state.latency_ms === null || state.latency_ms === undefined
          ? "—" : Math.round(state.latency_ms) + " ms"));
      observed.appendChild(this.ui.kv("Last probe", agoText(state.checked_at, this.fmt)));
      if (state.last_error) {
        observed.appendChild(this.ui.el("div.catalog-dataset-note.bad", { text: state.last_error }));
      }
      host.appendChild(observed);
    },

    renderSubscriptionPanel: function () {
      const self = this;
      const host = this.$("#catalogSubscription");
      const source = this.catalogSource();
      host.innerHTML = "";
      if (!source) return;

      const state = source.state || {};
      const needsCredentials = !!(source.auth && !source.auth.configured);
      const schedule = source.schedule_detail || {};
      const pull = source.protocol === "polling" || source.protocol === "rest"
        || source.protocol === "library";

      const title = this.ui.el("div.subscription-title");
      title.appendChild(document.createTextNode("Subscription"));
      title.appendChild(this.ui.el("b", { text: source.name }));
      host.appendChild(title);

      const form = this.ui.el("div.subscription-form");
      form.appendChild(this.ui.el(
        needsCredentials ? "div.catalog-credential-warning" : "div.catalog-credential-warning.catalog-ready-note",
        {
          text: needsCredentials
            ? "Credentials are required before this connector can ingest: "
              + (state.last_error || "the configured auth has no key")
            : "Connector is implemented and configured — it can be subscribed.",
        }
      ));

      // channel
      const channelBlock = this.ui.el("div.subscription-block");
      channelBlock.appendChild(this.ui.el("span.catalog-label", { text: "Channel / dataset" }));
      const channel = this.ui.el("input.subscription-input", {
        id: "catalogChannel",
        value: this.defaultChannel(source),
      });
      channelBlock.appendChild(channel);
      channelBlock.appendChild(this.ui.el("span.catalog-dataset-note", {
        text: "The connector's channel name, e.g. bars, quotes, series, filings.",
      }));
      form.appendChild(channelBlock);

      // feed mode — what this subscription asks the source to do. The control
      // never lies about capability: a source with no stream says so, and the
      // subscription is still saved with the mode that was asked for.
      const feedBlock = this.ui.el("div.subscription-block");
      feedBlock.appendChild(this.ui.el("span.catalog-label", { text: "Live / scheduled feed" }));
      const streams = source.protocol === "websocket"
        || (schedule.kind || "") === "streaming";
      const feed = this.ui.el("select.subscription-input", { id: "catalogFeedMode" });
      [["scheduled", "Scheduled — poll on the source's own cadence"],
       ["live", "Live — stream as the source publishes"]].forEach(function (option) {
        const node = document.createElement("option");
        node.value = option[0];
        node.textContent = option[1];
        feed.appendChild(node);
      });
      feed.value = streams ? "live" : "scheduled";
      feedBlock.appendChild(feed);
      const feedNote = this.ui.el("div.catalog-dataset-note", { id: "catalogFeedNote" });
      const describeFeed = function () {
        if (feed.value === "live") {
          feedNote.textContent = streams
            ? source.name + " streams over " + (source.protocol_label || source.protocol) + "."
            : "⚠ " + source.name + " is a " + (source.protocol_label || source.protocol)
              + " source with no stream. The subscription is saved as live, but it will"
              + " actually be polled on its " + (schedule.label || source.schedule || "manual")
              + " schedule.";
        } else {
          feedNote.textContent = streams
            ? "⚠ " + source.name + " only streams; there is no polling cadence to fall back on."
            : "Polled on the source's " + (schedule.label || source.schedule || "manual")
              + " schedule (set below).";
        }
      };
      describeFeed();
      feed.addEventListener("change", describeFeed);
      feedBlock.appendChild(feedNote);
      form.appendChild(feedBlock);

      // point-in-time
      const pitBlock = this.ui.el("div.subscription-block");
      const pitLabel = this.ui.el("label.subscription-check");
      const pit = this.ui.el("input", { type: "checkbox", id: "catalogPit" });
      pitLabel.appendChild(pit);
      pitLabel.appendChild(this.ui.el("span", {
        text: "Preserve point-in-time values",
      }));
      pitBlock.appendChild(pitLabel);
      pitBlock.appendChild(this.ui.el("span.catalog-dataset-note", {
        text: "With this on, an observation already stored for a timestamp is kept and a"
          + " later vendor restatement of it is refused; only new timestamps are written.",
      }));
      form.appendChild(pitBlock);

      // frequency — the DATASET's own resolution, which is a different question
      // from how often the connector runs (that is the poll interval below).
      form.appendChild(this.renderFrequencyBlock(source));

      // poll interval — this really is the source's schedule
      const pollBlock = this.ui.el("div.subscription-block", { id: "pollBlock" });
      form.appendChild(pollBlock);

      // historical / backfill — REFERENCE's HISTORICAL / BACKFILL section:
      // protocol chips (data-hp) over a FROM / TO pair, posting to
      // POST /api/ingest/sources/{id}/backfill.
      form.appendChild(this.renderBackfillBlock(source));

      // scope
      const scopeBlock = this.ui.el("div.subscription-block");
      scopeBlock.appendChild(this.ui.el("span.catalog-label", {
        text: "Universe / watchlist scope",
      }));
      const scope = this.ui.el("select.subscription-input", { id: "catalogScope" });
      this.scopeOptions().forEach(function (option) {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        scope.appendChild(node);
      });
      scopeBlock.appendChild(scope);
      const custom = this.ui.el("input.subscription-input", {
        id: "catalogScopeSymbols", placeholder: "AAPL, MSFT", hidden: true,
        style: "margin-top:7px",
      });
      scopeBlock.appendChild(custom);
      scope.addEventListener("change", function () {
        custom.hidden = scope.value !== "__custom__";
      });
      form.appendChild(scopeBlock);

      // estimate — real counters
      const estimate = this.ui.el("div.subscription-estimate");
      estimate.appendChild(this.ui.el("b", { text: intText(state.rows_ingested) + " rows ingested" }));
      estimate.appendChild(document.createTextNode(
        (schedule.label || source.schedule || "manual")
        + " · last ingest " + agoText(state.last_ingest_at, this.fmt)
        + " · backfill " + (source.backfill && source.backfill.enabled
          ? source.backfill.range : "disabled")
      ));
      form.appendChild(estimate);

      const actions = this.ui.el("div.subscription-actions");
      const add = this.ui.button("+ Add subscription", {
        primary: true,
        onClick: function (event) { self.addSubscription(event.currentTarget); },
      });
      if (needsCredentials) add.disabled = true;
      actions.appendChild(add);
      actions.appendChild(this.ui.button("Dry run", {
        onClick: function (event) { self.dryRunSource(event.currentTarget); },
      }));
      actions.appendChild(this.ui.button("Use custom configuration", {
        className: "subscription-custom",
        onClick: function () { self.useCustomConfiguration(); },
      }));
      form.appendChild(actions);
      host.appendChild(form);

      this.renderPollBlock(pull);
    },

    /**
     * REFERENCE's "HISTORICAL / BACKFILL": the protocols that can serve
     * history, then the FROM / TO window, then the button that actually runs
     * it. A source speaks one protocol, so exactly one chip is live and the
     * rest are shown disabled — the same shape as "Protocols supported".
     * @param {Object} source catalog source record
     * @returns {HTMLElement}
     */
    renderBackfillBlock: function (source) {
      const self = this;
      const block = this.ui.el("div.subscription-block", { id: "backfillBlock" });
      block.appendChild(this.ui.el("span.catalog-label", { text: "Historical / backfill" }));

      const chips = this.ui.el("div.protocol-pills");
      BACKFILL_PROTOCOLS.forEach(function (entry) {
        const selected = entry.token === source.protocol;
        const chip = self.ui.el("button.protocol-pill", {
          type: "button",
          text: entry.label + (selected ? " ●" : ""),
          dataset: { hp: entry.token },
          "aria-pressed": String(selected),
        });
        if (selected) chip.classList.add("enabled", "selected");
        else chip.disabled = true;
        chips.appendChild(chip);
      });
      block.appendChild(chips);

      if (!source.can_backfill) {
        block.appendChild(this.ui.el("div.catalog-dataset-note", {
          text: "No history endpoint — " + (source.protocol_label || source.protocol)
            + " sources only see what the vendor publishes while the connector is"
            + " subscribed, so there is nothing to backfill.",
        }));
        return block;
      }

      const backfill = source.backfill || {};
      const grid = this.ui.el("div.subscription-grid");
      const fromGroup = this.ui.el("div");
      fromGroup.appendChild(this.ui.el("span.catalog-label", { text: "From" }));
      const from = this.ui.el("input.subscription-input", {
        id: "bf-from",
        value: backfill.start_date || backfill.range || "1y",
        placeholder: "1y, 5y, all, or 2016-01-04",
      });
      fromGroup.appendChild(from);
      const toGroup = this.ui.el("div");
      toGroup.appendChild(this.ui.el("span.catalog-label", { text: "To" }));
      const to = this.ui.el("input.subscription-input", {
        id: "bf-to", value: "now", placeholder: "now, or 2024-12-31",
      });
      toGroup.appendChild(to);
      grid.appendChild(fromGroup);
      grid.appendChild(toGroup);
      block.appendChild(grid);

      block.appendChild(this.ui.el("div.catalog-dataset-note", {
        text: "FROM takes a duration (1y, 6mo, 30d), “all” for everything the vendor has,"
          + " or a calendar boundary (2016-01-04, 2009-Q2). TO takes “now” or a date."
          + " Configured default: "
          + (backfill.enabled ? backfill.range + " · batches of " + backfill.batch
            : "backfill disabled on this source") + ".",
      }));
      // The last backfill line is held in page state, not only in the DOM: an
      // `ingest` event reloads the catalog and rebuilds this panel, and the
      // operator must not lose sight of the backfill they just started.
      block.appendChild(this.ui.el("div.catalog-dataset-note", {
        id: "backfillProgress",
        text: this.backfillSourceId === source.id ? (this.backfillStatus || "") : "",
      }));

      const run = this.ui.button("Run backfill", {
        onClick: function (event) { self.runBackfill(event.currentTarget); },
      });
      if (source.auth && !source.auth.configured) {
        run.disabled = true;
        run.title = "This connector has no credentials configured";
      }
      const actions = this.ui.el("div.subscription-actions");
      actions.appendChild(run);
      block.appendChild(actions);
      return block;
    },

    /**
     * Start the backfill described by the FROM / TO controls. The request
     * returns as soon as the task is scheduled; progress arrives on the
     * `ingest` WebSocket channel and is written into #backfillProgress.
     * @param {HTMLElement} button the control to show busy
     * @returns {Promise<void>}
     */
    runBackfill: function (button) {
      const self = this;
      const source = this.catalogSource();
      if (!source) return Promise.resolve();
      const fromNode = this.$("#bf-from");
      const toNode = this.$("#bf-to");
      const range = fromNode ? fromNode.value.trim() : "";
      const end = toNode ? toNode.value.trim() : "now";
      if (!range) {
        global.toast.warn("A backfill needs a FROM boundary");
        return Promise.resolve();
      }
      this.backfillSourceId = source.id;
      return this.withBusy(button, function () {
        return self.api.ingest.backfill(source.id, { range: range, end: end })
          .then(function (result) {
            self.setBackfillStatus("Backfill started · " + range + " → " + (end || "now")
              + ". Progress arrives on the ingest channel.");
            global.toast.ok("Backfilling " + source.name + " from " + range
              + " to " + (end || "now") + (result.started ? "" : " (ran inline)"));
          }, function (err) {
            // A second backfill of the same source is a 409, not a crash: say
            // so and let withBusy restore the button.
            const detail = (err && err.payload && err.payload.detail) || {};
            if (detail && detail.error === "backfill_running") {
              global.toast.warn(source.name + " is already backfilling; wait for it to finish.");
              return;
            }
            throw err;
          });
      });
    },

    /**
     * Write a `backfill_*` event from the ingest channel into the panel.
     * @param {Object} event WebSocket payload
     */
    onBackfillEvent: function (event) {
      if (!event || !this.backfillSourceId || event.source_id !== this.backfillSourceId) return;
      if (event.event === "backfill_progress") {
        this.setBackfillStatus("Batch " + event.batch + " · "
          + intText(event.rows_written) + " rows written"
          + (event.dataset ? " into " + event.dataset : ""));
      } else if (event.event === "backfill_finished") {
        this.setBackfillStatus(event.ok
          ? "Backfill complete · " + event.batches + " batch"
            + (event.batches === 1 ? "" : "es") + " · "
            + intText(event.rows_written) + " rows written"
          : "Backfill failed · " + event.error);
        if (event.ok) global.toast.ok("Backfill finished: " + intText(event.rows_written) + " rows");
        else global.toast.error("Backfill failed: " + event.error);
      }
    },

    /**
     * Record and paint the current backfill line. Held in page state so a
     * catalog reload rebuilds the panel with the line still on it.
     * @param {string} text
     */
    setBackfillStatus: function (text) {
      this.backfillStatus = text;
      const note = this.$("#backfillProgress");
      if (note) note.textContent = text;
    },

    defaultChannel: function (source) {
      const map = {
        market: "bars", fundamentals: "filings", macro: "series",
        news: "news", alternative: "trends", reference: "reference",
      };
      return map[source.data_domain] || source.data_domain || "default";
    },

    scopeOptions: function () {
      const options = this.universes.map(function (universe) {
        return { value: "universe:" + universe.id, label: universe.name + " (universe)" };
      });
      this.watchlists.forEach(function (watchlist) {
        options.push({ value: "watchlist:" + watchlist.id, label: watchlist.name + " (watchlist)" });
      });
      options.push({ value: "", label: "All available symbols" });
      options.push({ value: "__custom__", label: "Custom symbols…" });
      return options;
    },

    /**
     * The per-dataset FREQUENCY chips: the resolution the subscription asks the
     * source for. The options come from the source's own data domain, so a
     * fundamentals feed offers quarterly/annual and a price feed offers bar
     * sizes — the catalog decides, not this module.
     * @param {Object} source catalog source record
     * @returns {HTMLElement}
     */
    renderFrequencyBlock: function (source) {
      const self = this;
      const block = this.ui.el("div.subscription-block", { id: "frequencyBlock" });
      block.appendChild(this.ui.el("span.catalog-label", { text: "Frequency" }));
      const options = source.granularity_options || [];
      if (!options.length) {
        block.appendChild(this.ui.el("span.catalog-dataset-note", {
          text: "This source states no dataset resolutions.",
        }));
        return block;
      }
      if (options.indexOf(this.catalogGranularity) < 0) {
        this.catalogGranularity = options.indexOf(source.granularity_default) >= 0
          ? source.granularity_default : options[0];
      }
      const chips = this.ui.el("div.frequency-options");
      options.forEach(function (token) {
        const chip = self.ui.el("button.frequency-option", {
          type: "button", text: token,
          dataset: { catalogFrequency: token },
          "aria-pressed": String(token === self.catalogGranularity),
        });
        chip.addEventListener("click", function () {
          self.catalogGranularity = token;
          chips.querySelectorAll("[data-catalog-frequency]").forEach(function (other) {
            other.setAttribute("aria-pressed", String(other === chip));
          });
          self.renderFrequencyNote(source);
          // The two knobs constrain each other, so the poll advice is re-run.
          self.renderPollBlock(self.pullSource(source));
        });
        chips.appendChild(chip);
      });
      block.appendChild(chips);
      // The note node is passed explicitly: this block is not in the document
      // yet on the first render, so looking it up by id would find nothing.
      const note = this.ui.el("div.catalog-dataset-note", { id: "frequencyNote" });
      block.appendChild(note);
      this.renderFrequencyNote(source, note);
      return block;
    },

    /**
     * Say plainly whether the chosen resolution reaches the fetch, or is
     * recorded only. The rule belongs to the API (`_granularity_effect` in
     * `routes/ingest.py`, which mirrors `IngestScheduler.requested_interval()`)
     * and is not re-derived here: when a subscription of this source already
     * carries this resolution, its own `granularity_detail` is quoted verbatim.
     * For a resolution nothing is subscribed at yet there is no verdict to
     * quote, so the note predicts one from the two facts the API decides on —
     * the source's published `granularity_reaches_fetch`, and the bar sizes the
     * source's other ACTIVE subscriptions already ask for (two that disagree
     * cancel out and neither is imposed).
     */
    renderFrequencyNote: function (source, node) {
      const note = node || this.$("#frequencyNote");
      if (!note) return;
      const token = this.catalogGranularity;
      const mine = (this.subscriptions || []).filter(function (sub) {
        return sub.source_id === source.id;
      });
      const decided = mine.filter(function (sub) {
        return sub.granularity === token && typeof sub.granularity_applied === "boolean";
      }).sort(function (a, b) { return (b.active ? 1 : 0) - (a.active ? 1 : 0); })[0];
      if (decided) {
        note.textContent = sentence(decided.granularity_detail);
        note.classList.toggle("green", decided.granularity_applied === true);
        return;
      }
      const reaches = source.granularity_reaches_fetch === true && !!barToken(token);
      const clashing = [];
      mine.forEach(function (sub) {
        if (!sub.active || sub.granularity === token || !barToken(sub.granularity)) return;
        if (clashing.indexOf(sub.granularity) < 0) clashing.push(sub.granularity);
      });
      const applies = reaches && !clashing.length;
      note.textContent = !reaches
        ? "Advisory: " + token + " would be stored on the subscription, but "
          + source.name + "'s fetch path has no bar-size parameter to apply it to."
        : clashing.length
          ? "Subscribing at " + token + " would disagree with this source's active "
            + clashing.join(", ") + " subscription"
            + (clashing.length === 1 ? "" : "s") + ", and the scheduler imposes none of"
            + " them — " + source.name + " would keep its configured resolution."
          : "The scheduler will fetch " + token + " bars for this source while the"
            + " subscription is active.";
      note.classList.toggle("green", applies);
    },

    /** Whether this source is polled (rather than pushed or landed). */
    pullSource: function (source) {
      return source.protocol === "polling" || source.protocol === "rest"
        || source.protocol === "library";
    },

    /** The poll-interval control. It edits the SOURCE's schedule, which is the
     *  cadence the ingest scheduler really runs the connector at. Beside the
     *  presets sits a free-text field, because a cadence the presets do not
     *  offer ("45s", "4h") is a legitimate thing to want. */
    renderPollBlock: function (pull) {
      const self = this;
      const block = this.$("#pollBlock");
      const source = this.catalogSource();
      if (!block || !source) return;
      block.innerHTML = "";
      block.appendChild(this.ui.el("span.catalog-label", { text: "Poll interval (source schedule)" }));

      if (!pull) {
        block.appendChild(this.ui.el("div.catalog-dataset-note", {
          text: "Not applicable — a " + (source.protocol_label || source.protocol)
            + " source pushes or lands data as it arrives, so there is no polling schedule to set.",
        }));
        return;
      }

      const current = source.schedule;
      const options = this.ui.el("div.frequency-options");
      POLL_PRESETS.forEach(function (preset) {
        const button = self.ui.el("button.frequency-option", {
          type: "button", text: preset,
          dataset: { poll: preset },
          "aria-pressed": String(preset === current),
        });
        button.addEventListener("click", function () { self.setSourceSchedule(preset, button); });
        options.appendChild(button);
      });

      const custom = this.ui.el("input.subscription-input.poll-custom", {
        id: "pollCustom",
        placeholder: "custom",
        "aria-label": "Custom poll interval",
        value: POLL_PRESETS.indexOf(current) >= 0 ? "" : (toSeconds(current) ? current : ""),
      });
      options.appendChild(custom);
      block.appendChild(options);

      const note = this.ui.el("div.catalog-dataset-note.poll-note", { id: "pollNote" });
      block.appendChild(note);
      this.renderPollNote(source, current);

      // Typing only re-runs the advice, so the field keeps focus and the caret.
      custom.addEventListener("input", function () {
        self.renderPollNote(source, custom.value.trim() || current);
      });
      // Committing (Enter or blur) is what actually rewrites the schedule; an
      // unparseable value is refused here as well as by the API.
      const commit = function () {
        const value = custom.value.trim();
        if (!value || value === current) return;
        if (toSeconds(value) === null) {
          global.toast.warn("Unrecognised interval “" + value
            + "” — use a value like 45s, 2m, 4h or 1d.");
          return;
        }
        self.setSourceSchedule(value, custom);
      };
      custom.addEventListener("change", commit);
      custom.addEventListener("keydown", function (event) {
        if (event.key === "Enter") { event.preventDefault(); commit(); }
      });
    },

    /**
     * The line under the poll control. It states the request cost, and — when a
     * dataset resolution is selected — whether the two cadences agree, which is
     * the mistake this pair of controls exists to prevent.
     */
    renderPollNote: function (source, candidate) {
      const note = this.$("#pollNote");
      if (!note) return;
      note.classList.remove("green", "yellow");
      const seconds = toSeconds(candidate);
      if (!seconds) {
        note.textContent = candidate && candidate !== source.schedule
          ? "Unrecognised interval — use a value like 45s, 2m, 4h or 1d."
          : "Current schedule: " + (source.schedule_detail
            ? source.schedule_detail.label : source.schedule)
            + ". Pick an interval to switch this source to a periodic poll.";
        note.classList.add("yellow");
        return;
      }
      const perDay = Math.round(86400 / seconds);
      const cost = "≈ " + intText(perDay) + " connector run"
        + (perDay === 1 ? "" : "s") + "/day.";
      const bar = toSeconds(barToken(this.catalogGranularity));
      if (!bar) {
        note.textContent = cost + " Changing this rewrites the source's schedule and"
          + " re-registers it with the ingest scheduler.";
        return;
      }
      if (seconds < bar) {
        note.textContent = "Polling faster than the " + humanInterval(bar) + " bar — "
          + Math.floor(bar / seconds) + "× of these runs return the same unchanged bar. " + cost;
        note.classList.add("yellow");
      } else if (seconds > bar) {
        note.textContent = "Polling slower than the " + humanInterval(bar) + " bar — up to "
          + Math.max(0, Math.floor(seconds / bar) - 1)
          + " bar(s) can be missed between runs; backfill closes the gap. " + cost;
        note.classList.add("yellow");
      } else {
        note.textContent = "Aligned with the " + humanInterval(bar) + " bar interval. " + cost;
        note.classList.add("green");
      }
    },

    setSourceSchedule: function (preset, button) {
      const self = this;
      const source = this.catalogSource();
      if (!source) return;
      // Rewriting the schedule means re-posting the whole source, and the API
      // never returns a stored credential — so re-posting would clear it. Say
      // that instead of silently deleting the key.
      if (source.auth && source.auth.key_set) {
        global.toast.warn(source.name + " stores a credential the API never returns."
          + " Change its schedule on the Data Ingestion tab and re-enter the key,"
          + " so saving cannot clear it.");
        return;
      }
      this.withBusy(button, function () {
        return self.api.ingest.updateSource(source.id, self.sourceToForm(source, {
          schedule: preset,
        })).then(function (result) {
          global.toast.ok(source.name + " now polls every " + preset
            + (result.job ? " (scheduler job updated)" : ""));
          self.loadCatalog();
        });
      });
    },

    /** Turn an API source record back into a form payload the API accepts. */
    sourceToForm: function (source, overrides) {
      const payload = {
        id: source.id,
        name: source.name,
        data_domain: source.data_domain,
        protocol: source.protocol,
        endpoint: source.endpoint,
        auth: source.auth,
        format: source.format,
        schedule: source.schedule,
        scope: source.scope,
        scope_label: source.scope_label,
        source_tz: source.source_tz,
        // The mode token, not the policy object: SourceConfig.from_form parses
        // `retry_policy` as a mode name before it looks at a dict, so posting
        // the whole object is rejected.
        retry_policy: (source.retry_policy && source.retry_policy.mode) || "exponential",
        backfill: source.backfill,
        advanced: source.advanced,
        provider: source.provider,
        builtin: source.builtin,
        enabled: source.enabled,
      };
      return Object.assign(payload, overrides || {});
    },

    addSubscription: function (button) {
      const self = this;
      const source = this.catalogSource();
      const channel = this.$("#catalogChannel").value.trim();
      if (!channel) {
        global.toast.warn("A subscription needs a channel name");
        return Promise.resolve();
      }
      const scopeValue = this.$("#catalogScope").value;
      let scope = [];
      if (scopeValue === "__custom__") {
        scope = this.$("#catalogScopeSymbols").value.split(/[,\s]+/).filter(Boolean);
      } else if (scopeValue) {
        scope = [scopeValue];
      }
      const feedNode = this.$("#catalogFeedMode");
      const pitNode = this.$("#catalogPit");
      const feedMode = feedNode ? feedNode.value : "scheduled";
      const preservePit = !!(pitNode && pitNode.checked);
      return this.withBusy(button, function () {
        return self.api.post("/api/ingest/subscriptions", {
          source_id: source.id, channel: channel, scope: scope, active: true,
          feed_mode: feedMode, preserve_pit: preservePit,
          granularity: self.catalogGranularity || "",
        }).then(function (subscription) {
          global.toast.ok("Subscribed " + source.name + " · " + subscription.channel
            + " · " + subscription.feed_mode
            + (subscription.granularity ? " · " + subscription.granularity : "")
            + (subscription.preserve_pit ? " · point-in-time" : ""));
          if (subscription.feed_mode_supported === false) {
            global.toast.warn(subscription.feed_mode_detail);
          }
          if (subscription.granularity && subscription.granularity_applied === false) {
            global.toast.warn(subscription.granularity_detail);
          }
          self.catalogSubscriptionId = subscription.id;
          self.loadCatalog();
        });
      });
    },

    dryRunSource: function (button) {
      const self = this;
      const source = this.catalogSource();
      return this.withBusy(button, function () {
        return self.api.post("/api/ingest/sources/" + encodeURIComponent(source.id) + "/test",
          {}, { timeoutMs: 90000 }).then(function (result) {
          const state = result.state || {};
          const ok = state.status === "connected";
          (ok ? global.toast.ok : global.toast.warn)(
            source.name + " · " + (state.status_label || state.status)
            + (state.latency_ms ? " · " + Math.round(state.latency_ms) + " ms" : "")
            + (state.last_error ? " · " + state.last_error : "")
          );
          self.loadCatalog();
        });
      });
    },

    useCustomConfiguration: function () {
      const source = this.catalogSource();
      if (!source) return;
      this.showTab("ingestion");
      this.loadIngestion();
      this.fillIngestForm(source);
      global.toast(source.name + " copied into the ingestion form");
    },

    renderActiveSubscriptions: function () {
      const self = this;
      const body = this.$("#as-rows");
      const badge = this.$("#as-count");
      body.innerHTML = "";
      const rows = this.subscriptions;
      badge.textContent = rows.length + (rows.length === 1 ? " feed" : " feeds");
      if (!rows.length) {
        body.appendChild(this.ui.el("tr", this.ui.el("td.muted", {
          colspan: "8",
          text: "No subscriptions configured. Sources still ingest their own scope on their"
            + " own schedule — a subscription narrows one to a specific channel and universe.",
        })));
        return;
      }
      rows.forEach(function (sub) {
        const source = self.sources.filter(function (s) { return s.id === sub.source_id; })[0] || {};
        const state = source.state || {};
        const protocol = String(source.protocol || "").toLowerCase();
        const abbrev = protocol === "polling" ? "POLL" : protocol === "websocket" ? "WS"
          : protocol === "file" || protocol === "database" ? "FILE/DB"
            : protocol === "library" ? "LIB" : protocol.toUpperCase();
        const tone = abbrev === "POLL" ? "yellow" : abbrev === "WS" ? "green"
          : abbrev === "FILE/DB" ? "muted" : "cyan";
        const tr = self.ui.el("tr");
        tr.appendChild(self.ui.el("td", { text: source.name || sub.source_id }));
        tr.appendChild(self.ui.el("td.muted", {
          text: sub.channel + (sub.granularity ? " · " + sub.granularity : ""),
          title: sub.granularity ? (sub.granularity_detail || "") : "",
        }));
        tr.appendChild(self.ui.el("td." + tone, { text: abbrev }));
        tr.appendChild(self.ui.el("td", {
          text: (source.schedule_detail && source.schedule_detail.label) || source.schedule || "—",
        }));
        tr.appendChild(self.ui.el("td.muted", {
          text: (sub.scope || []).join(", ") || "source scope",
        }));
        const age = self.ui.el("td", { text: agoText(state.last_ingest_at, self.fmt) });
        if (state.last_ingest_at) age.dataset.age = state.last_ingest_at;
        tr.appendChild(age);
        const status = self.ui.el("td", {
          text: (sub.active ? "" : "PAUSED · ") + (state.status_label || "unknown"),
        });
        status.classList.add(state.status === "connected" && sub.active ? "green" : "yellow");
        tr.appendChild(status);
        const actions = self.ui.el("td");
        const wrap = self.ui.el("div.source-actions");
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Remove";
        remove.addEventListener("click", function () {
          self.withBusy(remove, function () {
            return self.api.del("/api/ingest/subscriptions/" + encodeURIComponent(sub.id))
              .then(function () {
                global.toast.ok("Subscription removed");
                self.loadCatalog();
              });
          });
        });
        wrap.appendChild(remove);
        actions.appendChild(wrap);
        tr.appendChild(actions);
        body.appendChild(tr);
      });
    },

    // ======================================================================
    // TAB 4 — Data Ingestion
    // ======================================================================

    loadIngestion: function () {
      const self = this;
      this.populateScopeSelect();
      Promise.all([
        this.api.ingest.sources(null, { signal: this.ctx.signal }),
        this.universes.length && this.activeUniverse
          ? Promise.resolve({ universes: this.universes, active: this.activeUniverse })
          : this.api.universes.list({ include_members: true }, { signal: this.ctx.signal }),
      ]).then(function (results) {
        if (!self.alive()) return;
        self.sources = (results[0] && results[0].sources) || [];
        self.universes = (results[1] && results[1].universes) || self.universes;
        self.activeUniverse = (results[1] && results[1].active) || self.activeUniverse;
        self.populateScopeSelect();
        self.renderSourceTable(results[0] && results[0].summary);
      }).catch(function (err) {
        if (err.cancelled || !self.alive()) return;
        const body = self.$("#sourceRows");
        body.innerHTML = "";
        body.appendChild(self.ui.el("tr", self.ui.el("td.red", {
          colspan: "9", text: errorText(err),
        })));
      });
    },

    populateScopeSelect: function () {
      const select = this.$("#sourceScope");
      if (!select) return;
      const previous = select.value;
      select.innerHTML = "";
      this.universes.forEach(function (universe) {
        const option = document.createElement("option");
        option.value = universe.name;
        option.textContent = universe.name;
        select.appendChild(option);
      });
      this.watchlists.forEach(function (watchlist) {
        const option = document.createElement("option");
        option.value = watchlist.name;
        option.textContent = watchlist.name;
        select.appendChild(option);
      });
      ["All available symbols", "Custom symbols…"].forEach(function (label) {
        const option = document.createElement("option");
        option.value = label;
        option.textContent = label;
        select.appendChild(option);
      });
      if (previous) select.value = previous;
    },

    renderSourceTable: function (summary) {
      const self = this;
      const body = this.$("#sourceRows");
      const badge = this.$("#sourceCountBadge");
      body.innerHTML = "";
      const connected = summary ? summary.connected : this.sources.filter(function (s) {
        return s.state && s.state.status === "connected";
      }).length;
      badge.textContent = connected + " / " + this.sources.length + " connected";
      badge.classList.toggle("green", connected > 0);

      if (!this.sources.length) {
        body.appendChild(this.ui.el("tr", this.ui.el("td.muted", {
          colspan: "9", text: "No ingestion sources are registered.",
        })));
        return;
      }

      this.sources.forEach(function (source) {
        const state = source.state || {};
        const tr = self.ui.el("tr");
        tr.appendChild(self.ui.el("td", { text: source.name }));
        tr.appendChild(self.ui.el("td", { text: source.data_domain_label || source.data_domain }));
        tr.appendChild(self.ui.el("td", { text: source.protocol_label || source.protocol }));
        tr.appendChild(self.ui.el("td", {
          text: (source.schedule_detail && source.schedule_detail.label) || source.schedule,
        }));
        const backfill = source.backfill || {};
        const backfillCell = self.ui.el("td", {
          text: backfill.enabled ? backfill.range : "Disabled",
        });
        backfillCell.classList.add(backfill.enabled ? "green" : "muted");
        tr.appendChild(backfillCell);
        const freshness = self.ui.el("td", { text: agoText(state.last_ingest_at, self.fmt) });
        if (state.last_ingest_at) freshness.dataset.age = state.last_ingest_at;
        tr.appendChild(freshness);
        tr.appendChild(self.ui.el("td", {
          text: state.rows_ingested ? intText(state.rows_ingested) + " rows" : "—",
        }));
        const status = self.ui.el("td", { text: state.status_label || "Unknown" });
        status.classList.add(state.status === "connected" ? "green"
          : state.status === "unknown" ? "muted" : "yellow");
        if (state.last_error) status.title = state.last_error;
        tr.appendChild(status);

        const actions = self.ui.el("td");
        const wrap = self.ui.el("div.source-actions");
        const test = document.createElement("button");
        test.type = "button";
        test.textContent = "Test";
        test.addEventListener("click", function () {
          self.withBusy(test, function () {
            return self.api.post("/api/ingest/sources/" + encodeURIComponent(source.id) + "/test",
              {}, { timeoutMs: 90000 }).then(function (result) {
              const next = result.state || {};
              const ok = next.status === "connected";
              (ok ? global.toast.ok : global.toast.warn)(
                source.name + " · " + (next.status_label || next.status)
                + (next.latency_ms ? " · " + Math.round(next.latency_ms) + " ms" : "")
                + (next.last_error ? " · " + next.last_error : "")
              );
              self.loadIngestion();
            });
          });
        });
        const edit = document.createElement("button");
        edit.type = "button";
        edit.textContent = "Edit";
        edit.addEventListener("click", function () {
          self.fillIngestForm(source);
          self.$("#sourceName").scrollIntoView({ block: "center", behavior: "smooth" });
        });
        const pause = document.createElement("button");
        pause.type = "button";
        pause.textContent = source.enabled ? "Pause" : "Resume";
        pause.addEventListener("click", function () {
          self.withBusy(pause, function () {
            return self.api.post("/api/ingest/sources/" + encodeURIComponent(source.id) + "/enabled",
              { enabled: !source.enabled }).then(function () {
              global.toast.ok(source.name + (source.enabled ? " paused" : " resumed"));
              self.loadIngestion();
            });
          });
        });
        wrap.appendChild(test);
        wrap.appendChild(edit);
        wrap.appendChild(pause);
        if (!source.builtin) {
          const remove = document.createElement("button");
          remove.type = "button";
          remove.textContent = "Delete";
          remove.addEventListener("click", function () {
            self.withBusy(remove, function () {
              return self.api.ingest.deleteSource(source.id).then(function () {
                global.toast.ok(source.name + " deleted");
                self.loadIngestion();
              });
            });
          });
          wrap.appendChild(remove);
        }
        actions.appendChild(wrap);
        tr.appendChild(actions);
        body.appendChild(tr);
      });
    },

    wireIngestionForm: function () {
      const self = this;
      const protocol = this.$("#sourceProtocol");
      protocol.addEventListener("change", function () { self.applyProtocol(); });
      this.applyProtocol();

      this.$("#sourceAuth").addEventListener("change", function () { self.applyAuthFields(); });
      this.applyAuthFields();

      this.$("#sourceFrequency").addEventListener("change", function () { self.applyFrequency(); });
      this.$("#sourceScope").addEventListener("change", function () { self.applyFrequency(); });
      this.applyFrequency();

      this.$("#backfillEnabled").addEventListener("change", function (event) {
        self.$("#backfillFields").style.opacity = event.target.checked ? "1" : ".35";
        self.applyBackfill();
      });
      this.$("#backfillRange").addEventListener("change", function () { self.applyBackfill(); });
      this.applyBackfill();

      this.$("#testConnection").addEventListener("click", function (event) {
        self.testConnection(event.currentTarget);
      });
      this.$("#resetIngestForm").addEventListener("click", function () {
        self.resetIngestForm();
      });
      this.$("#ingestionForm").addEventListener("submit", function (event) {
        event.preventDefault();
        self.saveSource();
      });
    },

    applyProtocol: function () {
      if (!this.alive()) return;
      const name = this.$("#sourceProtocol").value;
      const config = PROTOCOLS[name];
      if (!config) return;
      this.$("#endpointLabel").textContent = config.label;
      this.$("#sourceEndpoint").placeholder = config.placeholder;
      this.$("#protocolHint").textContent = config.hint;
      if (!this._editingSource) this.$("#sourceFrequency").value = config.frequency;
      this.applyFrequency();
      // A database source needs SQL and a file source a glob; say so where the
      // user has to type it.
      const advanced = this.$("#advancedConfig");
      advanced.placeholder = name === "Database"
        ? '{"sql": "SELECT ts, symbol, close FROM bars WHERE ts > :watermark"}'
        : name === "File"
          ? '{"glob": "/data/import/**/*.parquet", "schema_mapping": {"ts": "timestamp"}}'
          : name === "WebSocket"
            ? '{"subscriptions": ["AAPL", "MSFT"], "subscribe_message": {"action": "subscribe"}}'
            : "Optional headers, subscriptions, SQL query, file glob, pagination, schema mapping…";
    },

    applyAuthFields: function () {
      if (!this.alive()) return;
      const auth = this.$("#sourceAuth").value;
      const fields = this.$("#authFields");
      fields.hidden = auth === "None";
      const nameLabel = this.$("#authKeyNameLabel");
      const valueLabel = this.$("#authKeyValueLabel");
      if (auth === "Basic auth") {
        nameLabel.textContent = "Username";
        valueLabel.textContent = "Password";
      } else if (auth === "OAuth 2.0") {
        nameLabel.textContent = "Client id";
        valueLabel.textContent = "Client secret";
      } else if (auth === "Connection secret") {
        nameLabel.textContent = "Secret name";
        valueLabel.textContent = "Secret value";
      } else {
        nameLabel.textContent = "Key / header name";
        valueLabel.textContent = "Key value";
      }
    },

    applyFrequency: function () {
      if (!this.alive()) return;
      const frequency = this.$("#sourceFrequency").value;
      const scope = this.$("#sourceScope").value;
      const cronNeeded = frequency === "Custom cron…";
      const symbolsNeeded = scope === "Custom symbols…";
      this.$("#cronFields").hidden = !cronNeeded && !symbolsNeeded;
      this.$("#sourceCron").closest(".field-group").hidden = !cronNeeded;
      this.$("#sourceSymbols").closest(".field-group").hidden = !symbolsNeeded;
    },

    applyBackfill: function () {
      if (!this.alive()) return;
      const enabled = this.$("#backfillEnabled").checked;
      const custom = this.$("#backfillRange").value === "Custom start date…";
      this.$("#backfillStartField").hidden = !(enabled && custom);
    },

    /** Read the form into the payload SourceConfig.from_form expects. */
    formPayload: function () {
      const advancedText = this.$("#advancedConfig").value.trim();
      const payload = {
        sourceName: this.$("#sourceName").value.trim(),
        sourceDomain: this.$("#sourceDomain").value,
        sourceProtocol: PROTOCOLS[this.$("#sourceProtocol").value].token,
        sourceEndpoint: this.$("#sourceEndpoint").value.trim(),
        sourceAuth: this.$("#sourceAuth").value,
        sourceFormat: this.$("#sourceFormat").value,
        sourceFrequency: this.$("#sourceFrequency").value,
        sourceScope: this.$("#sourceScope").value,
        sourceTimezone: this.$("#sourceTimezone").value,
        sourceRetries: this.$("#sourceRetries").value,
        backfillEnabled: this.$("#backfillEnabled").checked,
        backfillRange: this.$("#backfillRange").value,
        backfillBatch: this.$("#backfillBatch").value,
        advancedConfig: advancedText || null,
      };
      if (this._editingSource) payload.id = this._editingSource.id;

      const auth = this.$("#sourceAuth").value;
      if (auth !== "None") {
        const keyName = this.$("#authKeyName").value.trim();
        const keyValue = this.$("#authKeyValue").value;
        if (auth === "Basic auth") {
          if (keyName) payload.auth_username = keyName;
          if (keyValue) payload.auth_password = keyValue;
        } else if (auth === "OAuth 2.0") {
          if (keyName) payload.auth_client_id = keyName;
          if (keyValue) payload.auth_client_secret = keyValue;
        } else {
          if (keyName) payload.auth_key_name = keyName;
          if (keyValue) payload.auth_key_value = keyValue;
        }
      }
      if (this.$("#sourceFrequency").value === "Custom cron…") {
        payload.cron = this.$("#sourceCron").value.trim();
      }
      if (this.$("#sourceScope").value === "Custom symbols…") {
        payload.scope = this.$("#sourceSymbols").value;
      }
      if (this.$("#backfillEnabled").checked
        && this.$("#backfillRange").value === "Custom start date…") {
        payload.backfillStart = this.$("#backfillStart").value;
      }
      return payload;
    },

    fillIngestForm: function (source) {
      if (!this.alive()) return;
      this._editingSource = source;
      this.$("#ingestFormBadge").textContent = "editing " + source.id;
      this.$("#resetIngestForm").hidden = false;
      this.$("#saveSource").textContent = "Save changes";

      this.$("#sourceName").value = source.name;
      this.setSelect("#sourceDomain", source.data_domain_label);
      const protocolName = PROTOCOL_BY_TOKEN[source.protocol];
      if (protocolName) this.$("#sourceProtocol").value = protocolName;
      this.applyProtocol();
      this.$("#sourceEndpoint").value = source.endpoint || "";
      this.setSelect("#sourceAuth", (source.auth && source.auth.label) || "None");
      this.applyAuthFields();
      this.$("#authKeyName").value = (source.auth && source.auth.key_name) || "";
      this.$("#authKeyValue").value = "";
      // The API never returns a stored key, so saving without re-entering it
      // clears it. Say so where the user is about to decide.
      const keySet = !!(source.auth && source.auth.key_set);
      this.$("#authKeyValue").placeholder = keySet
        ? "a key is stored — re-enter it or saving will clear it"
        : "write-only";
      this.setSelect("#sourceFormat", source.format_label);
      this.setSelect("#sourceFrequency",
        (source.schedule_detail && source.schedule_detail.label) || "Daily");
      this.setSelect("#sourceScope", source.scope_label || "All available symbols");
      if ((source.scope || []).length) {
        this.setSelect("#sourceScope", "Custom symbols…");
        this.$("#sourceSymbols").value = source.scope.join(", ");
      }
      this.applyFrequency();
      this.setSelect("#sourceTimezone", source.source_tz || "UTC");
      this.setSelect("#sourceRetries", (source.retry_policy && source.retry_policy.label)
        || "Exponential backoff");
      const backfill = source.backfill || {};
      this.$("#backfillEnabled").checked = !!backfill.enabled;
      this.$("#backfillFields").style.opacity = backfill.enabled ? "1" : ".35";
      this.applyBackfill();
      this.$("#advancedConfig").value = source.advanced
        ? JSON.stringify(source.advanced, null, 2) : "";
      this.$("#testResult").innerHTML = "";
    },

    /** Select the option whose text (or value) matches, when there is one. */
    setSelect: function (selector, wanted) {
      const select = this.$(selector);
      if (!select || !wanted) return;
      const options = Array.prototype.slice.call(select.options);
      const match = options.filter(function (option) {
        return option.value === wanted || option.textContent === wanted;
      })[0];
      if (match) select.value = match.value;
    },

    resetIngestForm: function () {
      if (!this.alive()) return;
      this._editingSource = null;
      this.$("#ingestionForm").reset();
      this.$("#ingestFormBadge").textContent = "new connection";
      this.$("#resetIngestForm").hidden = true;
      this.$("#saveSource").textContent = "Save data source";
      this.$("#sourceProtocol").value = "WebSocket";
      this.applyProtocol();
      this.applyAuthFields();
      this.applyFrequency();
      this.applyBackfill();
      this.$("#testResult").innerHTML = "";
    },

    testConnection: function (button) {
      const self = this;
      if (!this.alive()) return Promise.resolve();
      const payload = this.formPayload();
      if (!payload.sourceName) {
        global.toast.warn("A source needs a name before it can be tested");
        return Promise.resolve();
      }
      const host = this.$("#testResult");
      host.innerHTML = "";
      host.appendChild(this.ui.el("div.test-result", {
        text: "Testing " + payload.sourceName + " via " + payload.sourceProtocol + "…",
      }));
      return this.withBusy(button, function () {
        return self.api.ingest.test(payload, { timeoutMs: 90000 }).then(function (result) {
          self.renderTestResult(result);
          (result.ok ? global.toast.ok : global.toast.error)(
            result.ok ? "Connection successful · " + (result.message || "")
              : "Connection failed · " + (result.error || result.message || "")
          );
        }).catch(function (err) {
          self.renderTestResult({
            ok: false, message: "The request was rejected before it reached the vendor.",
            error: errorText(err),
          });
          throw err;
        });
      });
    },

    renderTestResult: function (result) {
      const ui = this.ui;
      const host = this.$("#testResult");
      host.innerHTML = "";
      const box = ui.el("div.test-result." + (result.ok ? "ok" : "bad"));
      box.appendChild(ui.el("b", {
        className: result.ok ? "green" : "red",
        text: result.ok ? "✓ Connection successful" : "✗ Connection failed",
      }));
      const facts = [
        ["Reachable", result.reachable === null || result.reachable === undefined
          ? "not determined" : String(result.reachable)],
        ["Authenticated", result.authenticated === null || result.authenticated === undefined
          ? "not determined" : String(result.authenticated)],
        ["HTTP status", result.status_code === null || result.status_code === undefined
          ? "—" : String(result.status_code)],
        ["Latency", result.latency_ms === null || result.latency_ms === undefined
          ? "—" : Math.round(result.latency_ms) + " ms"],
        ["Tested at", result.tested_at || "—"],
      ];
      facts.forEach(function (fact) {
        box.appendChild(ui.kv(fact[0], fact[1]));
      });
      // A connector that fails usually reports the same sentence as both its
      // message and its error; printing it twice reads like two problems.
      const message = (result.message || "").trim();
      const error = (result.error || "").trim();
      if (message) box.appendChild(ui.el("div", { text: message }));
      if (error && error !== message) box.appendChild(ui.el("div.red", { text: error }));
      const schema = result.inferred_schema && Object.keys(result.inferred_schema);
      if (schema && schema.length) {
        box.appendChild(ui.el("div.dim", {
          text: "Inferred schema: " + schema.map(function (key) {
            return key + ":" + result.inferred_schema[key];
          }).join(", "),
        }));
      }
      if (result.sample && result.sample.length) {
        box.appendChild(ui.el("div.dim", { text: "Sample record(s):" }));
        box.appendChild(ui.el("pre", { text: JSON.stringify(result.sample, null, 2) }));
      }
      host.appendChild(box);
    },

    saveSource: function () {
      const self = this;
      if (!this.alive()) return;
      const payload = this.formPayload();
      if (!payload.sourceName) return;
      const button = this.$("#saveSource");
      const editing = this._editingSource;
      const request = editing
        ? this.api.ingest.updateSource(editing.id, payload)
        : this.api.ingest.createSource(payload);
      this.withBusy(button, function () {
        return request.then(function (result) {
          const source = result.source || {};
          global.toast.ok("Data source “" + (source.name || payload.sourceName) + "” "
            + (editing ? "updated" : "saved")
            + (result.job ? " · scheduled " + (result.job.schedule || "") : ""));
          // "Enable historical backfill on first connection" is acted on by the
          // save endpoint; report what it actually did either way, so the
          // checkbox is never a promise nothing kept.
          const backfill = result.backfill;
          if (backfill && backfill.started) {
            global.toast.ok("Historical backfill started over " + backfill.range
              + " · progress on the ingest channel");
          } else if (backfill && backfill.reason) {
            global.toast.warn("Backfill not started: " + backfill.reason);
          }
          self.resetIngestForm();
          self.loadIngestion();
        });
      });
    },

    // ======================================================================
    // TAB 5 — Data Quality
    // ======================================================================

    loadQuality: function () {
      const self = this;
      Promise.all([
        this.api.quality.summary(null, { signal: this.ctx.signal }),
        this.api.quality.series(null, { signal: this.ctx.signal }),
        this.api.quality.incidents({ include_resolved: true }, { signal: this.ctx.signal }),
        this.api.get("/api/quality/datasets", null, { signal: this.ctx.signal }),
        this.api.get("/api/quality/outliers", { limit: 200 }, { signal: this.ctx.signal }),
        this.api.get("/api/quality/sla", null, { signal: this.ctx.signal }),
        this.api.get("/api/quality/tickets", null, { signal: this.ctx.signal }),
      ]).then(function (results) {
        if (!self.alive()) return;
        self.renderQuality(results[0], results[1], results[2], results[3],
          results[4], results[5], results[6]);
      }).catch(function (err) {
        if (err.cancelled || !self.alive()) return;
        const cards = self.$("#qualityCards");
        cards.innerHTML = "";
        cards.appendChild(self.ui.errorPanel("Data quality", err));
      });
    },

    renderQuality: function (summary, series, incidents, datasets, outliers, sla, tickets) {
      const self = this;
      const ui = this.ui;
      const cards = this.$("#qualityCards");
      cards.innerHTML = "";

      const overall = summary.overall_quality_pct;
      const overallCard = ui.statCard("OVERALL QUALITY",
        overall === null || overall === undefined ? "no run yet" : this.fmt.num(overall, 1) + "%",
        { tone: overall === null || overall === undefined ? "muted" : overall >= 95 ? "green" : "yellow" });
      const bar = ui.el("div.quality-bar", ui.el("i", {
        style: "width:" + (overall === null || overall === undefined ? 0 : overall) + "%",
      }));
      overallCard.appendChild(bar);
      cards.appendChild(overallCard);
      cards.appendChild(ui.statCard("STALE SERIES", intText(summary.stale_series),
        { tone: summary.stale_series ? "yellow" : "green" }));
      cards.appendChild(ui.statCard("MISSING VALUES",
        this.fmt.num(summary.missing_values_pct, 2) + "%"));
      cards.appendChild(ui.statCard("FAILED CHECKS", intText(summary.failed_checks),
        { tone: summary.failed_checks ? "red" : "green" }));

      // incidents
      const openCount = (incidents.incidents || []).filter(function (i) {
        return i.status === "open";
      }).length;
      this.$("#incidentBadge").textContent = openCount + " open";
      this.$("#incidentBadge").classList.toggle("red", openCount > 0);
      const body = this.$("#incidentRows");
      body.innerHTML = "";
      if (!(incidents.incidents || []).length) {
        body.appendChild(ui.el("tr", ui.el("td.muted", {
          colspan: "5",
          text: summary.last_run
            ? "No incidents recorded — every check passed on the last run."
            : "No checks have run yet. Run the suite below to record real results.",
        })));
      }
      (incidents.incidents || []).forEach(function (incident) {
        const tr = ui.el("tr");
        tr.appendChild(ui.el("td", { text: incident.check }));
        tr.appendChild(ui.el("td", { text: incident.dataset }));
        const severity = ui.el("td", { text: incident.severity });
        severity.classList.add(incident.severity === "error" ? "red"
          : incident.severity === "warn" ? "yellow" : "muted");
        tr.appendChild(severity);
        tr.appendChild(ui.el("td", { text: incident.status }));
        const actions = ui.el("td.num");
        if (incident.status === "open") {
          const resolve = ui.button("Resolve", {
            onClick: function (event) {
              self.withBusy(event.currentTarget, function () {
                return self.api.post("/api/quality/incidents/"
                  + encodeURIComponent(incident.id) + "/resolve", {}).then(function () {
                  global.toast.ok("Incident resolved");
                  self.loadQuality();
                });
              });
            },
          });
          resolve.style.padding = "2px 8px";
          resolve.style.height = "auto";
          actions.appendChild(resolve);
        } else {
          actions.appendChild(ui.el("span.muted", { text: "—" }));
        }
        tr.appendChild(actions);
        body.appendChild(tr);
      });

      this.renderCheckSuite(summary, series, datasets, outliers, sla, tickets);
    },

    /**
     * The reference's three quality panels: the named check suite with its
     * per-row action, and the outlier review queue where a person quarantines
     * or accepts a flagged observation. Every row is derived from a real check
     * run; nothing is listed that the engine did not measure.
     */
    renderCheckSuite: function (summary, series, datasets, outliers, sla, tickets) {
      const self = this;
      const ui = this.ui;
      const host = this.$("#dq-wrap");
      const openTickets = (tickets && tickets.tickets) || [];
      host.innerHTML = `
        <section class="panel">
          <div class="panel-title" style="padding:0 0 8px">Check suite
            <span class="badge" id="dq-lastrun">—</span></div>
          <table class="data-table">
            <thead><tr><th>Check</th><th>Scope</th><th>Result</th><th class="num">Action</th></tr></thead>
            <tbody id="dq-checks"></tbody>
          </table>
          <div class="row-actions" id="dq-actions"></div>
          <div class="mini muted" id="dq-tickets"></div>
        </section>
        <div style="display:flex;flex-direction:column;gap:11px">
          <section class="panel">
            <div class="panel-title" style="padding:0 0 4px">Outlier review queue
              <span class="badge" id="dq-open">—</span></div>
            <div id="dq-queue"></div>
          </section>
          <div id="dq-note" class="mini"></div>
        </div>`;

      this.$("#dq-lastrun").textContent = summary.last_run
        ? "last run " + agoText(summary.last_run, this.fmt) : "never run";

      this.renderChecksTable(series, sla);
      this.renderOutlierQueue(outliers);

      const actions = this.$("#dq-actions");
      actions.appendChild(ui.button("Run checks", {
        primary: true,
        onClick: function (event) {
          self.withBusy(event.currentTarget, function () {
            return self.api.post("/api/quality/run", {}, { timeoutMs: 300000 })
              .then(function (result) {
                global.toast.ok("Quality suite ran over " + result.count + " dataset(s)");
                self.loadQuality();
              });
          });
        },
      }));
      const stored = (datasets && datasets.datasets) || [];
      actions.appendChild(ui.el("span.dim.mini", {
        style: "align-self:center",
        text: stored.length + " dataset(s) in the store · "
          + (series.datasets || []).length + " scored on the last run",
      }));

      const ticketNote = this.$("#dq-tickets");
      ticketNote.textContent = openTickets.length
        ? openTickets.length + " open quality ticket(s): "
          + openTickets.slice(0, 3).map(function (t) { return t.title; }).join(" · ")
          + ". Tickets are recorded in this workspace — no external tracker is configured."
        : "";

      this.renderQualityNote(summary, outliers);
    },

    /**
     * Aggregate the per-dataset check results into the reference's named rows.
     * Each row names the scope it really covered, so "CLEAN" can never mean
     * "we did not look".
     */
    renderChecksTable: function (series, sla) {
      const self = this;
      const ui = this.ui;
      const body = this.$("#dq-checks");
      body.innerHTML = "";
      const rows = series.datasets || [];

      if (!rows.length) {
        body.appendChild(ui.el("tr", ui.el("td.muted", {
          colspan: "4",
          text: "No check has been recorded yet. “Run checks” runs completeness,"
            + " staleness, outlier, duplicate, schema-drift and look-ahead"
            + " detection over every stored dataset.",
        })));
      }

      // check id -> [label, scope, how to phrase the failure count]
      const NAMED = [
        ["completeness", "Gap scan", "all stored series", "GAP", "review"],
        ["outliers", "Outlier detection", "all stored series", "FLAG", "review"],
        ["staleness", "Stale tickers", "all stored series", "STALE", "list"],
        ["schema_drift", "Schema drift", "all sources", "DRIFTED", "review"],
        ["duplicates", "Dupe rows", "all ingests", "DUPE", "review"],
        ["lookahead", "Lookahead audit", "feature store", "OFFENDER", "review"],
      ];

      NAMED.forEach(function (entry) {
        const id = entry[0];
        const failing = [];
        let seen = 0;
        rows.forEach(function (dataset) {
          (dataset.checks || []).forEach(function (check) {
            if (check.check !== id) return;
            seen += 1;
            if (!check.passed) failing.push({ dataset: dataset, check: check });
          });
        });
        const tr = ui.el("tr");
        tr.appendChild(ui.el("td", { text: entry[1] }));
        tr.appendChild(ui.el("td.muted", {
          text: seen ? entry[2] + " · " + seen + " checked" : "not checked yet",
        }));
        const clean = id === "duplicates" || id === "schema_drift" ? "0"
          : id === "lookahead" ? "CLEAN" : "CLEAN";
        const label = !seen ? "NOT RUN"
          : failing.length
            ? failing.length + " " + entry[3] + (failing.length > 1 ? "S" : "")
            : (id === "schema_drift" ? "NONE" : clean);
        const worst = failing.some(function (f) { return f.check.severity === "error"; });
        const result = ui.el("td", { text: label });
        result.classList.add(!seen ? "muted" : failing.length ? (worst ? "red" : "yellow") : "green");
        result.title = failing.length
          ? failing.slice(0, 8).map(function (f) {
            return (f.dataset.label || f.dataset.dataset) + ": " + f.check.detail;
          }).join("\n")
          : "";
        tr.appendChild(result);
        const action = ui.el("td.num");
        if (failing.length) {
          action.appendChild(self.checkAction(entry[4], entry[1], failing));
        } else {
          action.appendChild(ui.el("span.muted", { text: "—" }));
        }
        tr.appendChild(action);
        body.appendChild(tr);
      });

      // One SLA row per source whose own cadence it has missed.
      const slaRows = (sla && sla.sla) || [];
      slaRows.filter(function (row) { return row.state !== "ok"; }).forEach(function (row) {
        const tr = ui.el("tr");
        tr.appendChild(ui.el("td", { text: "SLA · " + row.source_id }));
        tr.appendChild(ui.el("td.muted", { text: row.schedule + " schedule" }));
        const hours = row.age_seconds === null || row.age_seconds === undefined
          ? null : row.age_seconds / 3600;
        const result = ui.el("td", {
          text: row.state === "breached"
            ? "BREACHED" + (hours === null ? "" : " " + self.fmt.num(hours, 1) + "H")
            : "PENDING",
        });
        result.classList.add(row.state === "breached" ? "red" : "yellow");
        result.title = row.detail;
        tr.appendChild(result);
        const action = ui.el("td.num");
        action.appendChild(self.ticketButton(row));
        tr.appendChild(action);
        body.appendChild(tr);
      });
      if (!slaRows.length) {
        const tr = ui.el("tr");
        tr.appendChild(ui.el("td", { text: "SLA" }));
        tr.appendChild(ui.el("td.muted", { text: "enabled sources" }));
        tr.appendChild(ui.el("td.muted", { text: "NO SOURCES" }));
        tr.appendChild(ui.el("td.num", ui.el("span.muted", { text: "—" })));
        body.appendChild(tr);
      } else if (!slaRows.some(function (row) { return row.state !== "ok"; })) {
        const tr = ui.el("tr");
        tr.appendChild(ui.el("td", { text: "SLA" }));
        tr.appendChild(ui.el("td.muted", { text: slaRows.length + " enabled sources" }));
        tr.appendChild(ui.el("td.green", { text: "ALL WITHIN CADENCE" }));
        tr.appendChild(ui.el("td.num", ui.el("span.muted", { text: "—" })));
        body.appendChild(tr);
      }
    },

    /** REVIEW / LIST button for one failing check row. */
    checkAction: function (kind, checkName, failing) {
      const self = this;
      const button = this.ui.button(kind === "list" ? "List" : "Review", {
        onClick: function () {
          if (kind === "review" && checkName === "Outlier detection") {
            const queue = self.$("#dq-queue");
            if (queue && queue.scrollIntoView) queue.scrollIntoView({ block: "nearest" });
            self.announce("Outlier review queue focused.");
            return;
          }
          self.showCheckDetail(checkName, failing);
        },
      });
      button.style.padding = "2px 8px";
      button.style.height = "auto";
      return button;
    },

    /** Expand one check's failing datasets underneath its row. */
    showCheckDetail: function (checkName, failing) {
      const ui = this.ui;
      const existing = this.$("#dq-detail");
      if (existing) existing.remove();
      const panel = ui.el("div.note", { id: "dq-detail", style: "margin-top:8px" });
      panel.appendChild(ui.el("b", { text: checkName + " · " + failing.length + " dataset(s)" }));
      failing.slice(0, 30).forEach(function (item) {
        panel.appendChild(ui.el("div.mini", {
          text: (item.dataset.label || item.dataset.dataset) + " — " + item.check.detail,
        }));
      });
      if (failing.length > 30) {
        panel.appendChild(ui.el("div.dim.mini", {
          text: "… " + (failing.length - 30) + " more",
        }));
      }
      const actions = this.$("#dq-actions");
      actions.parentNode.insertBefore(panel, actions.nextSibling);
      this.announce(checkName + ": " + failing.length + " dataset(s) failing.");
    },

    /** TICKET button for a breached SLA row. */
    ticketButton: function (row) {
      const self = this;
      const button = this.ui.button("Ticket", {
        onClick: function (event) {
          self.withBusy(event.currentTarget, function () {
            return self.api.post("/api/quality/tickets", {
              title: "SLA breach · " + row.name,
              dataset: "",
              check: "sla",
              body: row.detail + " (schedule: " + row.schedule + ")",
              severity: row.state === "breached" ? "error" : "warn",
            }).then(function (payload) {
              global.toast.ok("Ticket " + payload.ticket.id.slice(0, 8) + " opened · "
                + payload.tracker);
              self.loadQuality();
            });
          });
        },
      });
      button.style.padding = "2px 8px";
      button.style.height = "auto";
      return button;
    },

    /**
     * The outlier review queue. A decision here is not cosmetic: quarantining
     * removes the observation from every store read, so features, screens and
     * backtests stop seeing it until the decision is undone.
     */
    renderOutlierQueue: function (outliers) {
      const self = this;
      const ui = this.ui;
      const queue = this.$("#dq-queue");
      const rows = (outliers && outliers.outliers) || [];
      const counts = (outliers && outliers.counts) || {};
      queue.innerHTML = "";
      // Escalated points are not resolved — they are still awaiting a human, so
      // the badge counts them alongside the untouched ones.
      const awaiting = counts.awaiting_review !== undefined
        ? counts.awaiting_review : (counts.open || 0) + (counts.escalated || 0);
      this.$("#dq-open").textContent = awaiting
        ? awaiting + " open" + (counts.escalated ? " · " + counts.escalated + " escalated" : "")
        : "cleared";

      if (!rows.length) {
        queue.appendChild(ui.el("div.muted.mini", {
          text: "Nothing flagged. The outlier check populates this queue when a"
            + " robust z-score exceeds the configured threshold.",
        }));
        return;
      }

      const needsReview = function (row) {
        return row.needs_review !== undefined
          ? !!row.needs_review : (row.state === "open" || row.state === "escalated");
      };
      const open = rows.filter(needsReview);
      const decided = rows.filter(function (row) { return !needsReview(row); });

      open.slice(0, 25).forEach(function (row) {
        queue.appendChild(self.outlierRow(row));
      });
      if (open.length > 25) {
        queue.appendChild(ui.el("div.dim.mini", {
          text: "… " + (open.length - 25) + " more open flag(s)",
        }));
      }
      decided.slice(0, 10).forEach(function (row) {
        queue.appendChild(self.outlierRow(row));
      });
    },

    /** One queue entry with its QUARANTINE / ACCEPT VALUE / ESCALATE / undo controls. */
    outlierRow: function (row) {
      const self = this;
      const ui = this.ui;
      const awaiting = row.needs_review !== undefined
        ? !!row.needs_review : (row.state === "open" || row.state === "escalated");
      const item = ui.el("div.outlier");
      if (!awaiting) item.classList.add("done");
      if (row.state === "escalated") item.classList.add("escalated");
      const shown = row.value === null || row.value === undefined
        ? "" : " " + this.fmt.num(row.value, 4);
      item.appendChild(ui.el("b", {
        text: (row.label || row.dataset) + shown + " · z " + this.fmt.num(row.z, 1),
      }));
      const when = String(row.ts).slice(0, 10);
      if (awaiting) {
        const meta = ui.el("span.meta");
        meta.appendChild(ui.el("span", {
          text: when + " · " + row.basis + " change "
            + this.fmt.num(row.change, 4) + " · agent suggests: "
            + (row.suggestion === "human" ? "needs human" : row.suggestion),
        }));
        if (row.state === "escalated") {
          // Escalation is a request, not a verdict: say who asked, and say that
          // the raw value is still in the data.
          meta.appendChild(ui.el("span.yellow", {
            text: " · escalated by " + (row.decided_by || "user")
              + " — value still in the data, awaiting a human",
          }));
        }
        item.appendChild(meta);
        const acts = ui.el("div.acts");
        acts.appendChild(this.decisionButton(row, "quarantine", "Quarantine"));
        acts.appendChild(this.decisionButton(row, "accept", "Accept value"));
        if (row.state === "escalated") {
          const undo = ui.el("button.dq-undo", { type: "button", text: "clear escalation" });
          undo.addEventListener("click", function () { self.decideOutlier(row, "open", undo); });
          acts.appendChild(undo);
        } else {
          acts.appendChild(this.decisionButton(row, "escalate", "Escalate"));
        }
        item.appendChild(acts);
      } else {
        const meta = ui.el("span.meta");
        meta.appendChild(ui.el("span", { text: when + " · " }));
        meta.appendChild(ui.el("span", {
          class: row.state === "quarantined" ? "red" : "green",
          text: row.state + " by " + (row.decided_by || "user"),
        }));
        meta.appendChild(ui.el("span", { text: " · " }));
        const undo = ui.el("button.dq-undo", { type: "button", text: "undo" });
        undo.addEventListener("click", function () { self.decideOutlier(row, "open", undo); });
        meta.appendChild(undo);
        item.appendChild(meta);
      }
      return item;
    },

    decisionButton: function (row, decision, label) {
      const self = this;
      // The detector's own verb for "escalate" is "human", so that suggestion
      // highlights the Escalate button rather than nothing.
      const suggested = row.suggestion === decision
        || (row.suggestion === "human" && decision === "escalate");
      const button = this.ui.button(label, {
        primary: suggested,
        onClick: function (event) { self.decideOutlier(row, decision, event.currentTarget); },
      });
      button.style.padding = "2px 8px";
      button.style.height = "auto";
      return button;
    },

    decideOutlier: function (row, decision, control) {
      const self = this;
      return this.withBusy(control, function () {
        return self.api.post("/api/quality/outliers/" + encodeURIComponent(row.id) + "/decide",
          { decision: decision, by: "user" }).then(function (payload) {
          const state = payload.outlier.state;
          global.toast.ok((row.label || row.dataset) + " · " + (state === "escalated"
            ? "escalated for human review — still in the queue, value untouched" : state));
          self.loadQuality();
        });
      });
    },

    /** The warning under the queue, which states what quarantine actually does. */
    renderQualityNote: function (summary, outliers) {
      const counts = (outliers && outliers.counts) || {};
      const note = this.$("#dq-note");
      const quarantined = counts.quarantined || 0;
      const escalated = counts.escalated || 0;
      const openIncidents = summary.open_incidents || 0;
      const escalation = escalated
        ? " " + escalated + " point(s) are escalated for human review: their raw values are"
          + " still being used, which is what escalation means."
        : "";
      if (quarantined) {
        note.className = "note warn mini";
        note.textContent = "⚠ " + quarantined + " quarantined observation(s) are excluded"
          + " from every store read, so no feature, screen or backtest sees them until the"
          + " decision is undone." + escalation;
      } else if (escalated) {
        note.className = "note warn mini";
        note.textContent = "⚠ Nothing is quarantined." + escalation;
      } else if (openIncidents) {
        note.className = "note warn mini";
        note.textContent = "⚠ " + openIncidents + " open incident(s). Flagged observations stay"
          + " in the data until somebody quarantines them in the queue.";
      } else {
        note.className = "mini";
        note.textContent = summary.last_run
          ? "✓ No open incidents. " + (summary.datasets || 0) + " dataset(s) scored on the last"
            + " run, " + (summary.datasets_in_store || 0) + " present in the store."
          : "No checks have run yet, so nothing is known about the stored data's quality.";
      }
      const live = this.$("#dq-live");
      if (live) live.textContent = note.textContent;
    },

    announce: function (text) {
      const live = this.$("#dq-live");
      if (live) live.textContent = text;
    },
  };

  global.AlgoPages.register("data", {
    // Declared so the router can repair a deep link with an unknown sub-route
    // ("#data/catalog") instead of leaving the hash and the visible tab apart.
    subs: TABS.map(function (tab) { return tab.key; }),
    defaultSub: TABS[0].key,
    mount: function (ctx) { page.mount(ctx); },
    route: function (ctx) { page.route(ctx); },
    unmount: function (ctx) { page.unmount(ctx); },
  });
})(window);
