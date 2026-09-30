/* TradingLib — application boot.
 *
 * Owns the shell chrome and nothing inside a workspace:
 *   - the ET clock (real America/New_York via Intl, ticking every second)
 *   - the universe selector (real universes; selecting one sets it server-side)
 *   - the datastrip (every figure from /api/status, updated over the `status`
 *     WebSocket channel, with a REST poll as the fallback while the socket is
 *     down)
 *   - the ENGINE indicator, which reports the *combination* of engine health,
 *     WebSocket state and REST reachability — so pulling the backend down shows
 *     a degraded shell, and restarting it shows recovery (SPEC section 8,
 *     acceptance test 3)
 *   - starting the router, the palette and pick mode
 *
 * Nothing here invents a value. Unknown figures render as "—".
 */
(function (global) {
  "use strict";

  const STATUS_POLL_MS = 15000;      // REST fallback cadence while WS is down
  const STATUS_REFRESH_MS = 60000;   // slow re-poll even when WS is healthy

  const shell = {
    /** @type {object|null} the last /api/status payload */
    status: null,
    /** @type {string|null} id of the active collection (universe or watchlist) */
    universeId: null,
    /** @type {"universe"|"watchlist"|null} which population `universeId` is in */
    scopeKind: null,
    /** @type {Array<object>} universes as the selector knows them */
    universes: [],
    /** @type {Array<object>} watchlists the selector also offers as scopes */
    watchlists: [],
    _universeListeners: new Set(),
    _statusListeners: new Set(),

    /**
     * Observe active-universe changes (pages scope their data by it).
     * @param {(universeId: string|null, universe: object|null) => void} fn
     * @returns {() => void} unsubscribe
     */
    onUniverseChange: function (fn) {
      shell._universeListeners.add(fn);
      return function () { shell._universeListeners.delete(fn); };
    },

    /**
     * Observe datastrip status frames.
     * @param {(status: object) => void} fn
     * @returns {() => void} unsubscribe
     */
    onStatus: function (fn) {
      shell._statusListeners.add(fn);
      return function () { shell._statusListeners.delete(fn); };
    },

    /** Force a REST status refresh (after an action that changes the figures). */
    refreshStatus: function () { return loadStatus(); },
  };

  // ------------------------------------------------------------------- clock

  function startClock() {
    const node = document.getElementById("clock");
    if (!node) return;
    const tick = function () {
      node.textContent = global.AlgoUI.fmt.etClock() + " ET";
    };
    tick();
    setInterval(tick, 1000);
  }

  // --------------------------------------------------------------- datastrip

  const ds = {};

  function cacheDatastripNodes() {
    ds.dataLive = document.getElementById("dsDataLive");
    ds.gateway = document.getElementById("dsGateway");
    ds.sources = document.getElementById("dsSources");
    ds.lastIngest = document.getElementById("dsLastIngest");
    ds.delayed = document.getElementById("dsDelayed");
    ds.trials = document.getElementById("dsTrials");
    ds.spend = document.getElementById("dsSpend");
    ds.engine = document.getElementById("engineHealth");
  }

  /** Set text plus exactly one tone class on a datastrip span. */
  function paint(node, text, tone) {
    if (!node) return;
    node.textContent = text;
    node.classList.remove("ok", "warn", "bad", "dim");
    if (tone) node.classList.add(tone);
  }

  /** Re-render the datastrip from the cached status and live connection state. */
  function renderDatastrip() {
    const status = shell.status;
    const fmt = global.AlgoUI.fmt;
    const connected = global.stream.connected;
    const apiOnline = global.api.online !== false;

    if (!status) {
      paint(ds.dataLive, "● DATA —", "dim");
      paint(ds.gateway, "GATEWAY —");
      paint(ds.sources, "— SOURCES CONNECTED");
      paint(ds.lastIngest, "LAST INGEST —");
      paint(ds.delayed, "— DELAYED SERIES");
      if (ds.trials) ds.trials.innerHTML = 'TRIALS LOGGED <b class="hi">—</b>';
      paint(ds.spend, "SPEND —");
      renderEngine();
      return;
    }

    if (!apiOnline) paint(ds.dataLive, "● BACKEND UNREACHABLE", "bad");
    else if (!connected) paint(ds.dataLive, "● LIVE FEED LOST", "bad");
    else if (status.data_live) paint(ds.dataLive, "● DATA LIVE", "ok");
    else paint(ds.dataLive, "● DATA IDLE", "warn");

    paint(ds.gateway, status.gateway_ms === null || status.gateway_ms === undefined
      ? "GATEWAY —"
      : "GATEWAY " + Math.round(status.gateway_ms) + "MS");

    // REFERENCE reads "13 SOURCES CONNECTED" — one number, no denominator.
    // The denominator is real and worth keeping, so it moves into the title
    // rather than into the copy, and the Source Catalog shows the full split.
    const total = status.sources_total;
    paint(ds.sources, status.sources_connected + " SOURCES CONNECTED");
    if (ds.sources) {
      ds.sources.title = total
        ? status.sources_connected + " of " + total
          + " configured sources are connected — see Market & Data → Source Catalog"
        : "no ingestion source is configured yet";
    }

    paint(ds.lastIngest, status.last_ingest
      ? "LAST INGEST " + fmt.ago(status.last_ingest).toUpperCase() + " AGO"
      : "LAST INGEST —");

    const delayed = Number(status.delayed_series || 0);
    paint(ds.delayed, delayed + " DELAYED SERIES", delayed > 0 ? "warn" : null);

    if (ds.trials) {
      ds.trials.textContent = "TRIALS LOGGED ";
      const bold = document.createElement("b");
      bold.className = "hi";
      bold.textContent = fmt.int(status.trials_logged);
      ds.trials.appendChild(bold);
    }

    const spend = status.spend || {};
    // Small real amounts must stay visible: $0.00055 as "$0.00" reads as zero.
    const usedDecimals = spend.used > 0 && spend.used < 1 ? 4 : 2;
    paint(ds.spend, "SPEND " + fmt.usd(spend.used, { decimals: usedDecimals })
      + " / " + fmt.usd(spend.budget, { decimals: 2 }));

    renderEngine();
  }

  /** The topbar ENGINE pill: health, socket and REST reachability combined. */
  function renderEngine() {
    if (!ds.engine) return;
    const status = shell.status;
    const streamState = global.stream.state;
    const apiOnline = global.api.online;

    let text;
    let tone;
    let title;
    if (apiOnline === false) {
      text = "● ENGINE UNREACHABLE";
      tone = "bad";
      title = global.api.lastError ? global.api.lastError.message : "the API is not answering";
    } else if (!status) {
      text = "● ENGINE …";
      tone = "dim";
      title = "waiting for /api/status";
    } else if (status.engine_health !== "healthy") {
      text = "● ENGINE DEGRADED";
      tone = "warn";
      title = (status.degraded_subsystems || []).join(", ")
        || status.engine_detail || "one or more subsystems are down";
    } else if (streamState !== "open") {
      text = "● ENGINE HEALTHY · FEED " + String(streamState).toUpperCase();
      tone = "warn";
      title = global.stream.lastError || "the live feed is not connected";
    } else {
      text = "● ENGINE HEALTHY";
      tone = "ok";
      title = (status.engine_detail || "all subsystems up")
        + " · ws " + (status.ws_connections || 0) + " connection(s)";
    }
    paint(ds.engine, text, tone);
    ds.engine.title = title;
  }

  /** Fetch /api/status once and re-render. Failures are reported, not hidden. */
  async function loadStatus() {
    try {
      const payload = await global.api.status();
      applyStatus(payload);
      return payload;
    } catch (err) {
      console.warn("status load failed", err);
      renderDatastrip();
      return null;
    }
  }

  /** @param {object} payload a /api/status body (or a `status` channel frame) */
  function applyStatus(payload) {
    if (!payload || typeof payload !== "object") return;
    shell.status = payload;
    renderDatastrip();
    shell._statusListeners.forEach(function (fn) {
      try {
        fn(payload);
      } catch (err) {
        console.error("status listener failed", err);
      }
    });
  }

  // --------------------------------------------------------------- universes

  async function loadUniverses() {
    const select = document.getElementById("universeSelect");
    if (!select) return;
    let payload;
    let watchlistPayload;
    try {
      // The selector is a *scope* picker: the reference offers watchlists
      // beside the universes ("Watchlist: Semis"), and the backend accepts
      // either as the active scope. A watchlist listing that fails degrades to
      // universes only rather than failing the whole selector.
      const results = await Promise.all([
        global.api.universes.list(),
        global.api.watchlists.list().catch(function (err) {
          console.warn("watchlist load failed", err);
          return null;
        }),
      ]);
      payload = results[0];
      watchlistPayload = results[1];
    } catch (err) {
      console.error("universe load failed", err);
      select.innerHTML = "";
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "unavailable";
      select.appendChild(option);
      select.disabled = true;
      return;
    }
    const universes = (payload && payload.universes) || [];
    const watchlists = (watchlistPayload && watchlistPayload.watchlists) || [];
    shell.universes = universes;
    shell.watchlists = watchlists;
    // `active` is the selected collection's id, whichever kind it is.
    shell.universeId = (payload && payload.active) || null;
    const activeWatchlist = watchlists.filter(function (w) { return w.active; })[0] || null;
    if (activeWatchlist) shell.universeId = activeWatchlist.id;
    shell.scopeKind = activeWatchlist ? "watchlist" : (shell.universeId ? "universe" : null);

    select.innerHTML = "";
    const addOption = function (value, label, selected) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      if (selected) option.selected = true;
      select.appendChild(option);
    };
    universes.forEach(function (universe) {
      const count = universe.symbol_count || universe.member_count || 0;
      addOption("universe:" + universe.id,
        universe.name + (count ? " (" + count + ")" : ""),
        shell.scopeKind === "universe" && universe.id === shell.universeId);
    });
    watchlists.forEach(function (watchlist) {
      const count = watchlist.symbol_count || (watchlist.symbols || []).length;
      addOption("watchlist:" + watchlist.id,
        "Watchlist: " + watchlist.name + (count ? " (" + count + ")" : ""),
        shell.scopeKind === "watchlist" && watchlist.id === shell.universeId);
    });
    const empty = !universes.length && !watchlists.length;
    select.disabled = empty;
    if (empty) addOption("", "no collections", true);
    notifyUniverse();

    select.addEventListener("change", async function () {
      const ref = select.value;
      if (!ref) return;
      const previousRef = (shell.scopeKind || "universe") + ":" + shell.universeId;
      if (ref === previousRef) return;
      select.disabled = true;
      try {
        const result = await global.api.universes.setActive(ref);
        const scope = (result && result.scope) || {};
        shell.universeId = scope.id || (result && result.active) || null;
        shell.scopeKind = scope.kind || null;
        global.toast.ok("Active "
          + (scope.kind === "watchlist" ? "watchlist" : "universe") + ": "
          + (scope.name || shell.universeId));
        notifyUniverse();
        loadStatus();
      } catch (err) {
        console.error("set active scope failed", err);
        global.toast.error("Could not switch scope: " + (err && err.message ? err.message : err));
        select.value = previousRef;
      } finally {
        select.disabled = false;
      }
    });
  }

  function notifyUniverse() {
    // Listeners get the selected collection whichever kind it is; a watchlist
    // is handed over with a `kind` so a page can label it correctly.
    const pool = shell.scopeKind === "watchlist"
      ? (shell.watchlists || []) : (shell.universes || []);
    const found = pool.filter(function (item) {
      return item.id === shell.universeId;
    })[0] || null;
    const universe = found
      ? Object.assign({ kind: shell.scopeKind || "universe" }, found)
      : null;
    shell._universeListeners.forEach(function (fn) {
      try {
        fn(shell.universeId, universe);
      } catch (err) {
        console.error("universe listener failed", err);
      }
    });
    // Also a DOM event, so a page can listen without holding a shell reference.
    global.dispatchEvent(new CustomEvent("tradinglib:universe", {
      detail: { id: shell.universeId, universe: universe },
    }));
  }

  // ------------------------------------------------------------ live wiring

  function wireStream() {
    let wasConnected = false;
    let announcedLoss = false;

    global.stream.onState(function (state) {
      renderDatastrip();
      if (state === "open") {
        if (announcedLoss) {
          global.toast.ok("Live connection restored");
          announcedLoss = false;
        }
        wasConnected = true;
        // The socket may have missed frames while it was down.
        loadStatus();
      } else if (wasConnected && (state === "reconnecting" || state === "closed") && !announcedLoss) {
        global.toast.error("Live connection lost — retrying");
        announcedLoss = true;
      }
    });

    // The datastrip channel: the server republishes the same payload the REST
    // endpoint builds, so the two can never disagree.
    global.stream.listen("status", function (payload) {
      applyStatus(payload);
    });

    // Toast channel: the backend's own user-facing notifications.
    global.stream.listen("notifications", function (payload) {
      if (!payload) return;
      const message = payload.message || payload.detail || payload.title;
      if (!message) return;
      const level = String(payload.level || payload.severity || "").toLowerCase();
      if (level === "error" || level === "critical") global.toast.error(message);
      else if (level === "warning" || level === "warn") global.toast.warn(message);
      else global.toast(message);
    });

    global.api.onReachability(function (online) {
      renderDatastrip();
      if (online) loadStatus();
    });

    global.stream.connect();
  }

  function startPolling() {
    setInterval(function () {
      // While the socket is open the server pushes status; poll slowly anyway so
      // a silently stalled publisher still gets corrected, and quickly when the
      // socket is down because then nothing else updates the strip.
      if (global.stream.connected) return;
      loadStatus();
    }, STATUS_POLL_MS);
    setInterval(function () {
      if (global.stream.connected) loadStatus();
    }, STATUS_REFRESH_MS);
    // The "last ingest 4m ago" text ages between frames.
    setInterval(function () {
      if (shell.status) renderDatastrip();
    }, 10000);
  }

  // ------------------------------------------------------------------- boot

  function boot() {
    cacheDatastripNodes();
    renderDatastrip();
    startClock();

    global.AlgoRouter.start({ launchHash: global.__launchHash });
    global.AlgoPalette.init();
    global.AlgoPick.init();

    wireStream();
    startPolling();
    loadStatus();
    loadUniverses();

    document.documentElement.dataset.booted = "true";
  }

  global.AlgoShell = shell;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})(window);
