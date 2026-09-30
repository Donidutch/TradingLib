/* TradingLib — hash router and the page-registration contract.
 * =============================================================================
 * PAGE MODULES: READ THIS FIRST
 * =============================================================================
 * Every workspace is one file in ui/js/pages/ that registers itself:
 *
 *     window.AlgoPages.register("stock", {
 *       mount(ctx) { ... },     // required: build/refresh the workspace
 *       unmount(ctx) { ... },   // optional: tear down timers, charts, sockets
 *       route(ctx) { ... },     // optional: hash changed while already mounted
 *     });
 *
 * The seven keys are fixed and each owns exactly one container:
 *
 *     overview  → #view-overview       stock     → #view-stock
 *     lab       → #page-lab            agents    → #view-agents
 *     data      → #view-data           docs      → #view-docs
 *     settings  → #view-settings
 *
 * `ctx` (a plain object, rebuilt for each navigation):
 *     ctx.root     HTMLElement — your container, already visible. Own it
 *                  entirely; the router never writes inside it.
 *     ctx.key      "stock"
 *     ctx.sub      string|null — the sub-route: "#data/screener" → "screener",
 *                  "#tl/lab/backtests" → "backtests", "#stock/NVDA" → "NVDA".
 *     ctx.params   Record<string,string> — the query part of the hash:
 *                  "#tl/lab/backtests?id=BT-3" → {id:"BT-3"}.
 *     ctx.hash     the full hash this mount was routed from.
 *     ctx.signal   AbortSignal — aborted right before unmount. Pass it to
 *                  api calls and addEventListener so late work is dropped.
 *     ctx.api      the shared ApiClient (window.api).
 *     ctx.stream   the shared LiveStream (window.stream).
 *     ctx.router   this router.
 *     ctx.toast    toast(message, tone) helper.
 *     ctx.ui       shared render helpers (window.AlgoUI).
 *
 * Lifecycle:
 *   - entering a workspace           → unmount(previous), then mount(next)
 *   - hash changes *within* the same workspace (sub/params only)
 *        → route(ctx) if you defined it, otherwise unmount + mount
 *   - registering after the router already routed to your key → mounted at once
 *
 * Navigating from code (never write location.hash by hand):
 *     AlgoRouter.go("lab", { sub: "backtests", params: { id: "BT-3" } });
 *     AlgoRouter.go("stock", { sub: "NVDA" });
 * =============================================================================
 *
 * Hash grammar
 *   #<key>[/<sub>][?<query>]                  ac-tree workspaces
 *   #tl/lab[/<sub>][?<query>]                 the TL-tree Strategy Lab
 *   #agents/<sub>                             Agent Center sub-view
 * An unknown or empty hash routes to #overview.
 */
(function (global) {
  "use strict";

  /** Static workspace table: key → DOM tree, container id and rail label. */
  const WORKSPACES = {
    overview: { src: "ac", container: "view-overview", title: "Overview" },
    stock: { src: "ac", container: "view-stock", title: "Stock Research" },
    lab: { src: "tl", container: "page-lab", title: "Strategy Lab" },
    agents: { src: "ac", container: "view-agents", title: "Agent Center" },
    data: { src: "ac", container: "view-data", title: "Market & Data" },
    docs: { src: "ac", container: "view-docs", title: "Documentation" },
    settings: { src: "ac", container: "view-settings", title: "Settings" },
  };
  const DEFAULT_KEY = "overview";

  /** Registry of page modules. */
  const pages = new Map();

  const AlgoPages = {
    /**
     * Register a workspace module.
     * @param {string} key one of the seven workspace keys
     * @param {{mount: Function, unmount?: Function, route?: Function}} module
     */
    register: function (key, module) {
      if (!WORKSPACES[key]) {
        console.error("AlgoPages.register: unknown workspace key " + key);
        return;
      }
      if (!module || typeof module.mount !== "function") {
        console.error("AlgoPages.register(" + key + "): a mount() function is required");
        return;
      }
      if (pages.has(key)) {
        console.warn("AlgoPages.register(" + key + "): replacing an earlier registration");
      }
      pages.set(key, module);
      // A module that loads after the router already routed here gets mounted
      // immediately, so script order never decides whether a page appears.
      if (router && router.started && router.current && router.current.key === key
        && !router._mounted) {
        router._mountCurrent();
      }
    },
    /** @param {string} key @returns {object|undefined} */
    get: function (key) { return pages.get(key); },
    /** @returns {string[]} registered keys */
    keys: function () { return Array.from(pages.keys()); },
    /** @returns {object} the workspace table (read-only use) */
    workspaces: function () { return WORKSPACES; },
  };

  /**
   * Parse a hash into a route.
   * @param {string} hash
   * @returns {{key: string, sub: string|null, params: Record<string,string>, hash: string}|null}
   */
  function parseHash(hash) {
    const raw = String(hash || "");
    const body = raw.charAt(0) === "#" ? raw.slice(1) : raw;
    if (!body) return null;

    const qIndex = body.indexOf("?");
    const pathPart = qIndex >= 0 ? body.slice(0, qIndex) : body;
    const queryPart = qIndex >= 0 ? body.slice(qIndex + 1) : "";

    /** @type {Record<string,string>} */
    const params = {};
    if (queryPart) {
      new URLSearchParams(queryPart).forEach(function (value, name) { params[name] = value; });
    }

    let segments = pathPart.split("/").filter(function (s) { return s !== ""; });
    if (segments.length === 0) return null;

    // "#tl/<key>" addresses the TL tree explicitly.
    if (segments[0] === "tl") {
      segments = segments.slice(1);
      if (segments.length === 0) return null;
    }
    const key = segments[0];
    if (!WORKSPACES[key]) return null;
    const sub = segments.length > 1 ? decodeURIComponent(segments.slice(1).join("/")) : null;
    return { key: key, sub: sub, params: params, hash: raw };
  }

  /**
   * Repair a route whose sub-route the target workspace does not recognise.
   *
   * A workspace module may declare `subs` (the sub-route ids it serves) and
   * optionally `defaultSub`. A deep link such as "#data/catalog" — a plausible
   * but wrong spelling of "#data/source-catalog" — would otherwise leave the
   * hash pointing at nothing while the previously selected panel stayed on
   * screen. Here it is rewritten to the workspace's default instead, so the
   * address bar and the visible panel always agree.
   *
   * Workspaces whose sub-route is free-form (a ticker, a run id) declare no
   * `subs` list and are never rewritten.
   *
   * @param {{key: string, sub: string|null, params: object, hash: string}} route
   * @returns {{key: string, sub: string|null, params: object, hash: string}}
   */
  function normalizeRoute(route) {
    if (!route || !route.sub) return route;
    const module = pages.get(route.key);
    const known = module && Array.isArray(module.subs) ? module.subs : null;
    if (!known || !known.length) return route;
    const wanted = String(route.sub).toLowerCase();
    if (known.some(function (id) { return String(id).toLowerCase() === wanted; })) return route;
    const fallback = module.defaultSub === undefined ? known[0] : module.defaultSub;
    console.warn("router: #" + route.key + "/" + route.sub
      + " is not a sub-route of " + route.key + "; showing "
      + (fallback ? "#" + route.key + "/" + fallback : "#" + route.key));
    const repaired = {
      key: route.key,
      sub: fallback || null,
      params: route.params,
      hash: hashFor(route.key, { sub: fallback || null, params: route.params }),
    };
    return repaired;
  }

  /**
   * Build the canonical hash for a route.
   * @param {string} key
   * @param {{sub?: string|null, params?: Record<string,any>}} [options]
   */
  function hashFor(key, options) {
    const opts = options || {};
    const space = WORKSPACES[key];
    const prefix = space && space.src === "tl" ? "#tl/" + key : "#" + key;
    let out = prefix;
    if (opts.sub) out += "/" + String(opts.sub).split("/").map(encodeURIComponent).join("/");
    const params = opts.params || {};
    const names = Object.keys(params).filter(function (n) {
      return params[n] !== undefined && params[n] !== null && params[n] !== "";
    });
    if (names.length) {
      const qs = new URLSearchParams();
      names.forEach(function (n) { qs.append(n, String(params[n])); });
      out += "?" + qs.toString();
    }
    return out;
  }

  class Router {
    constructor() {
      /** @type {{key: string, sub: string|null, params: object, hash: string}|null} */
      this.current = null;
      this.started = false;
      this._mounted = null;      // {key, module, ctx} of the mounted page
      this._suppress = false;    // ignore the hashchange our own writes cause
      this._listeners = new Set();
      this._onHashChange = this._onHashChange.bind(this);
    }

    /**
     * Start routing. Uses the launch hash captured in index.html so a deep link
     * survives anything that rewrites location.hash during boot.
     * @param {{launchHash?: string}} [options]
     */
    start(options) {
      if (this.started) return;
      this.started = true;
      this.rail = document.getElementById("rail");
      this.main = document.getElementById("main");
      this.acRoot = document.getElementById("ac-root");
      this.tlRoot = document.getElementById("tl-root");

      const self = this;
      if (this.rail) {
        this.rail.addEventListener("click", function (event) {
          const button = event.target.closest(".navitem");
          if (!button || !button.dataset.key) return;
          event.preventDefault();
          self.go(button.dataset.key, { focus: true });
        });
      }
      global.addEventListener("hashchange", this._onHashChange);

      const launch = (options && options.launchHash) || global.__launchHash || global.location.hash;
      const target = parseHash(launch) || parseHash(global.location.hash) || {
        key: DEFAULT_KEY, sub: null, params: {}, hash: "#" + DEFAULT_KEY,
      };
      this._apply(target, { replace: true });
    }

    /**
     * Navigate to a workspace.
     * @param {string} key
     * @param {{sub?: string|null, params?: object, replace?: boolean, focus?: boolean}} [options]
     */
    go(key, options) {
      const opts = options || {};
      if (!WORKSPACES[key]) {
        console.error("router.go: unknown workspace " + key);
        return;
      }
      const next = {
        key: key,
        sub: opts.sub === undefined ? null : opts.sub,
        params: opts.params || {},
        hash: hashFor(key, opts),
      };
      this._apply(next, { replace: !!opts.replace, focus: !!opts.focus });
    }

    /** Re-run mount for the current route (after a data change, say). */
    reload() {
      if (!this.current) return;
      const route = this.current;
      this._unmountCurrent();
      this.current = route;
      this._mountCurrent();
    }

    /**
     * Observe navigation.
     * @param {(route: object) => void} fn
     * @returns {() => void} unsubscribe
     */
    onNavigate(fn) {
      this._listeners.add(fn);
      const self = this;
      return function () { self._listeners.delete(fn); };
    }

    /** @returns {string} canonical hash for a route (for href attributes). */
    hashFor(key, options) { return hashFor(key, options); }

    // ------------------------------------------------------------- internals

    _onHashChange() {
      if (this._suppress) return;
      const target = parseHash(global.location.hash);
      if (!target) {
        this.go(DEFAULT_KEY, { replace: true });
        return;
      }
      this._apply(target, { fromHash: true });
    }

    /**
     * @param {{key: string, sub: string|null, params: object, hash: string}} route
     * @param {{replace?: boolean, fromHash?: boolean, focus?: boolean}} [options]
     */
    _apply(route, options) {
      const opts = options || {};
      route = normalizeRoute(route);
      const previous = this.current;
      const sameWorkspace = !!previous && previous.key === route.key && !!this._mounted;
      const identical = !!previous && previous.hash === route.hash && !!this._mounted;

      this.current = route;
      this._writeHash(route.hash, opts.replace);
      this._paintDom(route.key);

      if (identical) {
        this._notify(route);
        return;
      }
      if (sameWorkspace) {
        const mounted = this._mounted;
        const ctx = mounted.ctx;
        ctx.sub = route.sub;
        ctx.params = route.params;
        ctx.hash = route.hash;
        if (typeof mounted.module.route === "function") {
          try {
            mounted.module.route(ctx);
          } catch (err) {
            console.error("page " + route.key + " route() failed", err);
          }
        } else {
          this._unmountCurrent();
          this._mountCurrent();
        }
      } else {
        this._unmountCurrent();
        this._mountCurrent();
      }

      if (opts.focus && this.main) this.main.focus();
      global.scrollTo(0, 0);
      this._notify(route);
    }

    /** Show the right tree and the right container inside it. */
    _paintDom(key) {
      const space = WORKSPACES[key];
      if (this.acRoot) this.acRoot.hidden = space.src !== "ac";
      if (this.tlRoot) this.tlRoot.hidden = space.src !== "tl";

      if (this.acRoot) {
        this.acRoot.querySelectorAll(".view").forEach(function (view) {
          view.classList.toggle("active", view.id === space.container);
        });
      }
      if (this.tlRoot) {
        this.tlRoot.querySelectorAll(".page").forEach(function (page) {
          page.classList.toggle("active", page.id === space.container);
        });
      }
      if (this.rail) {
        this.rail.querySelectorAll(".navitem").forEach(function (button) {
          const on = button.dataset.key === key;
          button.classList.toggle("active", on);
          if (on) button.setAttribute("aria-current", "page");
          else button.removeAttribute("aria-current");
        });
      }
      document.title = "TRADINGLIB — " + space.title.toLowerCase();
    }

    _writeHash(hash, replace) {
      if (global.location.hash === hash) return;
      this._suppress = true;
      try {
        if (replace && global.history && global.history.replaceState) {
          global.history.replaceState(null, "", global.location.pathname + global.location.search + hash);
        } else {
          global.location.hash = hash;
        }
      } finally {
        // The hashchange from our own write lands asynchronously; clear the
        // guard a tick later so it is swallowed, not re-routed.
        const self = this;
        setTimeout(function () { self._suppress = false; }, 0);
      }
    }

    _mountCurrent() {
      const route = this.current;
      if (!route) return;
      const module = pages.get(route.key);
      const container = document.getElementById(WORKSPACES[route.key].container);
      if (!container) {
        console.error("router: missing container for " + route.key);
        return;
      }
      if (!module) {
        // No module registered yet. Say so honestly instead of a blank page.
        container.innerHTML = "";
        container.appendChild(pendingPanel(route.key));
        return;
      }
      const controller = new AbortController();
      const ctx = {
        root: container,
        key: route.key,
        sub: route.sub,
        params: route.params,
        hash: route.hash,
        signal: controller.signal,
        api: global.api,
        stream: global.stream,
        router: this,
        toast: global.toast,
        ui: global.AlgoUI,
      };
      this._mounted = { key: route.key, module: module, ctx: ctx, controller: controller };
      try {
        module.mount(ctx);
      } catch (err) {
        console.error("page " + route.key + " mount() failed", err);
        container.innerHTML = "";
        container.appendChild(errorPanel(route.key, err));
      }
    }

    _unmountCurrent() {
      const mounted = this._mounted;
      this._mounted = null;
      if (!mounted) return;
      try {
        mounted.controller.abort();
      } catch (err) {
        console.warn("router: abort failed", err);
      }
      if (typeof mounted.module.unmount === "function") {
        try {
          mounted.module.unmount(mounted.ctx);
        } catch (err) {
          console.error("page " + mounted.key + " unmount() failed", err);
        }
      }
    }

    _notify(route) {
      this._listeners.forEach(function (fn) {
        try {
          fn(route);
        } catch (err) {
          console.error("router listener failed", err);
        }
      });
    }
  }

  /** Panel shown when a workspace has no module registered. */
  function pendingPanel(key) {
    const ui = global.AlgoUI;
    const title = WORKSPACES[key].title;
    if (ui && typeof ui.panel === "function") {
      return ui.panel({
        title: title,
        badge: "not registered",
        body: "No page module registered for \"" + key + "\". "
          + "ui/js/pages/" + key + ".js must call AlgoPages.register(\"" + key + "\", …).",
      });
    }
    const div = document.createElement("div");
    div.className = "panel";
    div.textContent = title + ": no page module registered for \"" + key + "\".";
    return div;
  }

  /** Panel shown when a page module throws during mount. */
  function errorPanel(key, err) {
    const ui = global.AlgoUI;
    const message = (err && err.message) ? err.message : String(err);
    if (ui && typeof ui.panel === "function") {
      return ui.panel({
        title: WORKSPACES[key].title,
        badge: "failed",
        badgeTone: "red",
        body: "This workspace failed to mount: " + message,
      });
    }
    const div = document.createElement("div");
    div.className = "panel";
    div.textContent = "This workspace failed to mount: " + message;
    return div;
  }

  const router = new Router();

  global.AlgoPages = AlgoPages;
  global.AlgoRouter = router;
  global.parseAlgoHash = parseHash;
})(window);
