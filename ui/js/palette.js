/* TradingLib — the ⌘K command palette.
 *
 * Opens on ⌘K / Ctrl-K (and on the topbar search button), closes on Escape or a
 * backdrop click. Typing queries `GET /api/search?q=` and merges the server's
 * hits with the local actions; ↑/↓ move the selection and Enter runs it.
 *
 * Server results are normalised leniently, so the search route can grow without
 * breaking this file. Each result is read as:
 *
 *     { type, title, subtitle|detail, hash | page/key [, sub, params]
 *       | symbol | id }
 *
 * `hash` wins when present ("#tl/lab/backtests"); otherwise a `symbol` opens
 * Stock Research for it, and a `page`/`key` navigates to that workspace. A
 * result that carries none of those is shown but not clickable, rather than
 * pretending to do something.
 *
 * Reference dead-ends this fixes (SPEC section 6):
 *   - "Open active backtest" really opens the Strategy Lab backtests tab, on
 *     the run the backend says is active — it asks /api/lab/backtests which one
 *     that is instead of hard-coding an id.
 *   - any ticker can be researched from here, not just the one demo symbol.
 */
(function (global) {
  "use strict";

  const SEARCH_DEBOUNCE_MS = 160;

  /** The static actions, always available and always first. */
  const STATIC_ACTIONS = [
    { label: "Go to Overview", hint: "⌂", key: "overview" },
    { label: "Open Stock Research", hint: "Stock Research", key: "stock" },
    { label: "Open Strategy Lab", hint: "⚗", key: "lab" },
    { label: "Open active backtest", hint: "Strategy Lab", action: "activeBacktest" },
    { label: "Open Agent Center", hint: "A", key: "agents" },
    { label: "Open Market & Data", hint: "D", key: "data" },
    { label: "Open Documentation", hint: "?", key: "docs" },
    { label: "Open Settings", hint: "⚙", key: "settings" },
  ];

  class Palette {
    constructor() {
      this.root = null;
      this.input = null;
      this.list = null;
      this.open = false;
      /** @type {Array<{label:string, hint:string, run:Function|null}>} */
      this.items = [];
      this.selected = 0;
      this._searchTimer = 0;
      this._searchController = null;
      this._searchFailed = false;
    }

    /** Wire the palette into the shell. Call once, after the DOM exists. */
    init() {
      this.root = document.getElementById("palette");
      this.input = document.getElementById("paletteInput");
      this.list = document.getElementById("paletteActions");
      if (!this.root || !this.input || !this.list) {
        console.error("palette: markup is missing (#palette / #paletteInput / #paletteActions)");
        return;
      }
      const self = this;

      const searchButton = document.getElementById("globalSearch");
      if (searchButton) searchButton.addEventListener("click", function () { self.show(); });

      document.addEventListener("keydown", function (event) {
        const key = (event.key || "").toLowerCase();
        if (key === "k" && (event.metaKey || event.ctrlKey)) {
          event.preventDefault();
          self.toggle();
          return;
        }
        if (!self.open) return;
        if (event.key === "Escape") {
          event.preventDefault();
          self.hide();
        } else if (event.key === "ArrowDown") {
          event.preventDefault();
          self.move(1);
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          self.move(-1);
        } else if (event.key === "Enter") {
          event.preventDefault();
          self.runSelected();
        }
      });

      // Backdrop click closes; a click inside the box must not.
      this.root.addEventListener("click", function (event) {
        if (event.target === self.root) self.hide();
      });

      this.input.addEventListener("input", function () { self.queueSearch(); });

      this.render([]);
    }

    /** Open the palette and focus its input. */
    show() {
      if (!this.root) return;
      this.open = true;
      this.root.classList.add("open");
      this.input.value = "";
      this.selected = 0;
      this._searchFailed = false;
      this.render([]);
      const input = this.input;
      setTimeout(function () { input.focus(); }, 0);
    }

    /** Close the palette and cancel any in-flight search. */
    hide() {
      if (!this.root) return;
      this.open = false;
      this.root.classList.remove("open");
      clearTimeout(this._searchTimer);
      if (this._searchController) {
        this._searchController.abort();
        this._searchController = null;
      }
    }

    toggle() {
      if (this.open) this.hide();
      else this.show();
    }

    /** Move the highlighted row. @param {number} delta */
    move(delta) {
      const runnable = this.items.filter(function (item) { return !!item.run; });
      if (!runnable.length) return;
      this.selected = (this.selected + delta + this.items.length) % this.items.length;
      // Skip rows that cannot be run (headings, un-navigable results).
      let guard = 0;
      while (!this.items[this.selected].run && guard < this.items.length) {
        this.selected = (this.selected + (delta >= 0 ? 1 : -1) + this.items.length) % this.items.length;
        guard += 1;
      }
      this.paintSelection();
    }

    /** Execute the highlighted row. */
    runSelected() {
      const item = this.items[this.selected];
      if (!item || !item.run) return;
      this.hide();
      Promise.resolve()
        .then(function () { return item.run(); })
        .catch(function (err) {
          console.error("palette action failed", err);
          if (global.toast) global.toast.error(err && err.message ? err.message : String(err));
        });
    }

    // ------------------------------------------------------------- searching

    queueSearch() {
      const self = this;
      clearTimeout(this._searchTimer);
      this._searchTimer = setTimeout(function () { self.search(); }, SEARCH_DEBOUNCE_MS);
    }

    /** Run the current query against /api/search and re-render. */
    async search() {
      const query = this.input.value.trim();
      if (!query) {
        this.render([]);
        return;
      }
      if (this._searchController) this._searchController.abort();
      const controller = new AbortController();
      this._searchController = controller;

      let results = [];
      try {
        const payload = await global.api.search(query, null, { signal: controller.signal });
        results = normaliseResults(payload);
        this._searchFailed = false;
      } catch (err) {
        if (controller.signal.aborted) return;
        if (err && err.missing) {
          // /api/search is not in this build yet. Local actions still work, and
          // the palette says so rather than looking broken.
          this._searchFailed = "no-route";
        } else {
          this._searchFailed = (err && err.message) ? err.message : String(err);
        }
        results = [];
      }
      if (this._searchController === controller) this._searchController = null;
      if (!this.open) return;
      this.render(results, query);
    }

    // ------------------------------------------------------------- rendering

    /**
     * Rebuild the action list.
     * @param {Array<object>} results normalised server results
     * @param {string} [query] the current query text
     */
    render(results, query) {
      const self = this;
      const text = (query || "").trim();
      const items = [];

      // 1. A direct symbol jump, when the query looks like a ticker.
      if (text && /^[A-Za-z.\-]{1,10}$/.test(text)) {
        const symbol = text.toUpperCase();
        items.push({
          label: "Research " + symbol,
          hint: "Stock Research",
          run: function () {
            global.AlgoRouter.go("stock", { sub: symbol });
          },
        });
      }

      // 2. Server results.
      (results || []).forEach(function (result) {
        items.push({
          label: result.title,
          hint: result.subtitle || result.type || "",
          run: runnerFor(result),
        });
      });

      // 3. Local actions, filtered by the query.
      STATIC_ACTIONS.forEach(function (action) {
        if (text && action.label.toLowerCase().indexOf(text.toLowerCase()) < 0) return;
        items.push({
          label: action.label,
          hint: action.hint,
          run: action.action === "activeBacktest"
            ? function () { return openActiveBacktest(); }
            : function () { global.AlgoRouter.go(action.key); },
        });
      });

      // 4. An honest note when the search route answered badly.
      if (text && this._searchFailed) {
        items.push({
          label: this._searchFailed === "no-route"
            ? "Search API not available in this build"
            : "Search failed: " + this._searchFailed,
          hint: "GET /api/search",
          run: null,
        });
      }
      if (!items.length) {
        items.push({ label: "No matches for “" + text + "”", hint: "", run: null });
      }

      this.items = items;
      this.selected = 0;
      while (this.selected < items.length && !items[this.selected].run) this.selected += 1;
      if (this.selected >= items.length) this.selected = 0;

      this.list.innerHTML = "";
      items.forEach(function (item, index) {
        const button = document.createElement("button");
        button.className = "palette-action";
        button.type = "button";
        const label = document.createElement("span");
        label.textContent = item.label;
        const hint = document.createElement("span");
        hint.textContent = item.hint || "";
        button.appendChild(label);
        button.appendChild(hint);
        if (!item.run) {
          button.disabled = true;
          button.style.opacity = "0.6";
        } else {
          button.addEventListener("click", function () {
            self.selected = index;
            self.runSelected();
          });
          button.addEventListener("mouseenter", function () {
            self.selected = index;
            self.paintSelection();
          });
        }
        self.list.appendChild(button);
      });
      this.paintSelection();
    }

    /** Highlight the selected row (the stylesheet has no :focus rule for it). */
    paintSelection() {
      const buttons = this.list.querySelectorAll(".palette-action");
      const self = this;
      buttons.forEach(function (button, index) {
        button.style.background = index === self.selected ? "var(--panel2)" : "";
      });
      const current = buttons[this.selected];
      if (current && current.scrollIntoView) current.scrollIntoView({ block: "nearest" });
    }
  }

  /**
   * Normalise whatever /api/search returned into a flat result list.
   * @param {any} payload
   * @returns {Array<{type:string,title:string,subtitle:string,hash?:string,
   *                  page?:string,sub?:string,params?:object,symbol?:string}>}
   */
  function normaliseResults(payload) {
    let raw = [];
    if (Array.isArray(payload)) raw = payload;
    else if (payload && Array.isArray(payload.results)) raw = payload.results;
    else if (payload && Array.isArray(payload.items)) raw = payload.items;
    else if (payload && payload.groups && typeof payload.groups === "object") {
      Object.keys(payload.groups).forEach(function (group) {
        const list = payload.groups[group];
        if (Array.isArray(list)) {
          list.forEach(function (entry) {
            raw.push(Object.assign({ type: group }, entry));
          });
        }
      });
    }
    return raw.map(function (entry) {
      const title = entry.title || entry.label || entry.name || entry.symbol || entry.id || "(untitled)";
      const subtitle = entry.subtitle || entry.detail || entry.description || entry.type || "";
      return {
        type: entry.type || entry.kind || "",
        title: String(title),
        subtitle: String(subtitle),
        hash: entry.hash || entry.href || null,
        page: entry.page || entry.key || entry.workspace || null,
        sub: entry.sub || entry.tab || null,
        params: entry.params || null,
        symbol: entry.symbol || null,
        id: entry.id || null,
      };
    }).slice(0, 24);
  }

  /**
   * Build the click handler for one search result.
   * @returns {Function|null} null when the result carries no destination
   */
  function runnerFor(result) {
    if (result.hash) {
      const hash = String(result.hash);
      const route = global.parseAlgoHash(hash.charAt(0) === "#" ? hash : "#" + hash);
      if (route) {
        return function () {
          global.AlgoRouter.go(route.key, { sub: route.sub, params: route.params });
        };
      }
      return null;
    }
    if (result.page && global.AlgoPages.workspaces()[result.page]) {
      return function () {
        global.AlgoRouter.go(result.page, { sub: result.sub, params: result.params || {} });
      };
    }
    if (result.symbol) {
      const symbol = String(result.symbol).toUpperCase();
      return function () { global.AlgoRouter.go("stock", { sub: symbol }); };
    }
    return null;
  }

  /**
   * "Open active backtest": ask the lab which run is active and open the
   * Strategy Lab backtests tab on it. Falls back to the tab itself when the lab
   * has no runs yet, and says so.
   */
  async function openActiveBacktest() {
    let backtests = [];
    try {
      const payload = await global.api.lab.backtests({ limit: 50 });
      backtests = (payload && (payload.backtests || payload.items || payload.results)) || [];
    } catch (err) {
      global.AlgoRouter.go("lab", { sub: "backtests" });
      if (global.toast) {
        global.toast.error("Could not load backtests: " + (err && err.message ? err.message : err));
      }
      return;
    }

    // "Active" = running first, then queued, then the most recent by time.
    const rank = { running: 0, queued: 1, pending: 1, complete: 2, completed: 2, failed: 3 };
    const sorted = backtests.slice().sort(function (a, b) {
      const ra = rank[String(a.status || "").toLowerCase()];
      const rb = rank[String(b.status || "").toLowerCase()];
      const da = ra === undefined ? 9 : ra;
      const db = rb === undefined ? 9 : rb;
      if (da !== db) return da - db;
      const ta = Date.parse(a.started_at || a.created_at || a.updated_at || 0) || 0;
      const tb = Date.parse(b.started_at || b.created_at || b.updated_at || 0) || 0;
      return tb - ta;
    });
    const active = sorted[0];

    if (!active) {
      global.AlgoRouter.go("lab", { sub: "backtests" });
      if (global.toast) global.toast.warn("No backtests yet — opening the backtests tab");
      return;
    }
    const id = active.id || active.backtest_id || active.run_id;
    global.AlgoRouter.go("lab", { sub: "backtests", params: id ? { id: id } : {} });
    if (global.toast) {
      global.toast("Opened backtest " + (id || "") + " (" + (active.status || "unknown") + ")");
    }
  }

  const palette = new Palette();
  global.AlgoPalette = palette;
  /** Kept for pages that want to open the palette from a button. */
  global.openPalette = function () { palette.show(); };
  global.closePalette = function () { palette.hide(); };
})(window);
