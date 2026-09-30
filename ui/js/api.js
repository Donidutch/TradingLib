/* TradingLib — ApiClient.
 *
 * One fetch wrapper for the whole SPA, with a method per REST domain
 * (SPEC section 5). Plain script: it publishes `window.AlgoApi` (the class)
 * and `window.api` (the shared instance the pages use).
 *
 * Rules this file keeps:
 *   - every failure is a real failure. A non-2xx response throws ApiError with
 *     the status and whatever detail the server sent; nothing is faked, and no
 *     placeholder object is returned in its place.
 *   - a route the backend has not implemented yet answers 404. Callers that can
 *     degrade gracefully use `optional()`, which resolves to null on 404/501
 *     and still throws on everything else, so a genuine outage is never
 *     mistaken for "not built yet".
 *   - reachability is observable: every request updates `client.online` and
 *     notifies `onReachability()` listeners, which app.js folds into the
 *     ENGINE indicator alongside the WebSocket state.
 */
(function (global) {
  "use strict";

  /** Error carrying the HTTP status and parsed body of a failed API call. */
  class ApiError extends Error {
    /**
     * @param {string} message human-readable summary
     * @param {{status?: number, url?: string, payload?: any, cause?: any}} info
     */
    constructor(message, info) {
      super(message);
      this.name = "ApiError";
      this.status = (info && info.status) || 0;
      this.url = (info && info.url) || "";
      this.payload = info ? info.payload : null;
      this.cause = info ? info.cause : undefined;
    }

    /** True when the server answered, but said the route does not exist. */
    get missing() {
      return this.status === 404 || this.status === 501;
    }

    /** True when the request never reached the server at all. */
    get offline() {
      return this.status === 0;
    }
  }

  /**
   * Build a query string from a plain object; null/undefined/'' are dropped and
   * arrays repeat the key (`symbols=A&symbols=B`), which is what FastAPI reads.
   * @param {Record<string, any>|undefined|null} params
   * @returns {string} "" or "?a=1&b=2"
   */
  function queryString(params) {
    if (!params) return "";
    const qs = new URLSearchParams();
    Object.keys(params).forEach(function (key) {
      const value = params[key];
      if (value === undefined || value === null || value === "") return;
      if (Array.isArray(value)) {
        value.forEach(function (item) {
          if (item !== undefined && item !== null && item !== "") qs.append(key, String(item));
        });
      } else if (typeof value === "boolean") {
        qs.append(key, value ? "true" : "false");
      } else {
        qs.append(key, String(value));
      }
    });
    const out = qs.toString();
    return out ? "?" + out : "";
  }

  class AlgoApi {
    /**
     * @param {{base?: string, timeoutMs?: number}} [options]
     *   base: URL prefix for every call (default "" — same origin).
     *   timeoutMs: per-request abort timeout (default 30000).
     */
    constructor(options) {
      const opts = options || {};
      this.base = opts.base || "";
      this.timeoutMs = typeof opts.timeoutMs === "number" ? opts.timeoutMs : 30000;
      /** @type {boolean|null} null until the first request settles. */
      this.online = null;
      /** @type {ApiError|null} the failure that last flipped `online` to false. */
      this.lastError = null;
      /** @type {Array<(online: boolean, error: ApiError|null) => void>} */
      this._reachabilityListeners = [];
    }

    // ---------------------------------------------------------------- core

    /**
     * Perform one HTTP request against the API.
     * @param {string} method HTTP verb
     * @param {string} path path beginning with "/"
     * @param {{query?: object, body?: any, signal?: AbortSignal,
     *          headers?: Record<string,string>, timeoutMs?: number}} [options]
     * @returns {Promise<any>} the parsed JSON body (or text for non-JSON)
     * @throws {ApiError} on transport failure or a non-2xx status
     */
    async request(method, path, options) {
      const opts = options || {};
      const url = this.base + path + queryString(opts.query);
      const headers = Object.assign({ Accept: "application/json" }, opts.headers || {});

      /** @type {RequestInit} */
      const init = { method: method, headers: headers };
      if (opts.body !== undefined && opts.body !== null) {
        if (opts.body instanceof FormData) {
          init.body = opts.body;
        } else {
          headers["Content-Type"] = "application/json";
          init.body = JSON.stringify(opts.body);
        }
      }

      // Own timeout controller, chained to any caller-supplied signal so both
      // an unmounting page and the timeout can cancel the same request.
      const controller = new AbortController();
      const timeoutMs = typeof opts.timeoutMs === "number" ? opts.timeoutMs : this.timeoutMs;
      const timer = timeoutMs > 0 ? setTimeout(function () { controller.abort(); }, timeoutMs) : 0;
      if (opts.signal) {
        if (opts.signal.aborted) controller.abort();
        else opts.signal.addEventListener("abort", function () { controller.abort(); }, { once: true });
      }
      init.signal = controller.signal;

      let response;
      try {
        response = await fetch(url, init);
      } catch (err) {
        if (timer) clearTimeout(timer);
        // A caller-cancelled request is not an outage: report it, do not let it
        // flip the shell into "degraded".
        if (opts.signal && opts.signal.aborted) {
          const cancelled = new ApiError("request cancelled: " + method + " " + url, {
            status: 0, url: url, cause: err,
          });
          cancelled.cancelled = true;
          throw cancelled;
        }
        const failure = new ApiError(
          "cannot reach " + method + " " + url + " (" + (err && err.message ? err.message : err) + ")",
          { status: 0, url: url, cause: err }
        );
        this._setReachable(false, failure);
        throw failure;
      }
      if (timer) clearTimeout(timer);

      const payload = await this._parse(response);
      if (!response.ok) {
        const detail = payload && typeof payload === "object" && payload.detail !== undefined
          ? (typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail))
          : String(payload || response.statusText || "");
        const failure = new ApiError(
          method + " " + url + " → " + response.status + (detail ? " " + detail : ""),
          { status: response.status, url: url, payload: payload }
        );
        // The server answered, so the backend is reachable even at 500.
        this._setReachable(true, null);
        throw failure;
      }
      this._setReachable(true, null);
      return payload;
    }

    /** Parse a response body as JSON when it claims to be, otherwise as text. */
    async _parse(response) {
      if (response.status === 204) return null;
      const type = response.headers.get("content-type") || "";
      const text = await response.text();
      if (!text) return null;
      if (type.indexOf("json") >= 0) {
        try {
          return JSON.parse(text);
        } catch (err) {
          throw new ApiError("malformed JSON from " + response.url, {
            status: response.status, url: response.url, cause: err,
          });
        }
      }
      return text;
    }

    /** @param {boolean} online @param {ApiError|null} error */
    _setReachable(online, error) {
      const changed = this.online !== online;
      this.online = online;
      this.lastError = online ? null : error || this.lastError;
      if (!changed) return;
      const self = this;
      this._reachabilityListeners.forEach(function (fn) {
        try {
          fn(online, self.lastError);
        } catch (err) {
          console.error("api reachability listener failed", err);
        }
      });
    }

    /**
     * Observe whether the REST API is answering.
     * @param {(online: boolean, error: ApiError|null) => void} fn
     * @returns {() => void} unsubscribe
     */
    onReachability(fn) {
      this._reachabilityListeners.push(fn);
      const list = this._reachabilityListeners;
      return function () {
        const i = list.indexOf(fn);
        if (i >= 0) list.splice(i, 1);
      };
    }

    /** GET helper. @returns {Promise<any>} */
    get(path, query, options) {
      return this.request("GET", path, Object.assign({ query: query }, options || {}));
    }
    /** POST helper. @returns {Promise<any>} */
    post(path, body, options) {
      return this.request("POST", path, Object.assign({ body: body }, options || {}));
    }
    /** PUT helper. @returns {Promise<any>} */
    put(path, body, options) {
      return this.request("PUT", path, Object.assign({ body: body }, options || {}));
    }
    /** PATCH helper. @returns {Promise<any>} */
    patch(path, body, options) {
      return this.request("PATCH", path, Object.assign({ body: body }, options || {}));
    }
    /** DELETE helper. @returns {Promise<any>} */
    del(path, options) {
      return this.request("DELETE", path, options || {});
    }

    /**
     * Absolute URL of an endpoint, for the few responses the browser fetches
     * itself rather than through `request` — a PDF download link, an <img> src.
     * @param {string} path path beginning with "/"
     * @param {object} [query]
     * @returns {string}
     */
    url(path, query) {
      return this.base + path + queryString(query);
    }

    /**
     * Run a call whose route may not exist in this build yet.
     * @param {Promise<any>} promise
     * @returns {Promise<any|null>} null when the server answered 404/501
     */
    async optional(promise) {
      try {
        return await promise;
      } catch (err) {
        if (err instanceof ApiError && err.missing) return null;
        throw err;
      }
    }

    // ------------------------------------------------------------- domains
    // One namespace per SPEC section 5 domain. Pages call these instead of
    // hand-writing paths, so a route change lands in exactly one place.

    /** GET /api/status — the datastrip payload. */
    status(options) {
      return this.get("/api/status", null, options);
    }

    /** Universes and the active-universe selection. */
    get universes() {
      const c = this;
      return {
        list: function (query, options) { return c.get("/api/universes", query, options); },
        create: function (body, options) { return c.post("/api/universes", body, options); },
        get: function (id, query, options) { return c.get("/api/universes/" + encodeURIComponent(id), query, options); },
        update: function (id, body, options) { return c.put("/api/universes/" + encodeURIComponent(id), body, options); },
        remove: function (id, options) { return c.del("/api/universes/" + encodeURIComponent(id), options); },
        members: function (id, query, options) { return c.get("/api/universes/" + encodeURIComponent(id) + "/members", query, options); },
        resolve: function (id, query, options) { return c.get("/api/universes/" + encodeURIComponent(id) + "/resolve", query, options); },
        snapshot: function (id, body, options) { return c.post("/api/universes/" + encodeURIComponent(id) + "/snapshot", body || {}, options); },
        snapshots: function (id, options) { return c.get("/api/universes/" + encodeURIComponent(id) + "/snapshots", null, options); },
        changelog: function (id, params, options) { return c.get("/api/universes/" + encodeURIComponent(id) + "/changelog", params, options); },
        refresh: function (id, body, options) { return c.post("/api/universes/" + encodeURIComponent(id) + "/refresh", body || {}, options); },
        active: function (options) { return c.get("/api/universes/active", null, options); },
        setActive: function (universeId, options) { return c.post("/api/universes/active", { universe_id: universeId }, options); },
        breadth: function (id, query, options) { return c.get("/api/universes/" + encodeURIComponent(id) + "/breadth", query, options); },
      };
    }

    /** FRED macro series levels (Overview intel panel). */
    get macro() {
      const c = this;
      return {
        panel: function (query, options) { return c.get("/api/macro/series", query, options); },
        series: function (id, query, options) { return c.get("/api/macro/series/" + encodeURIComponent(id), query, options); },
      };
    }

    /** Watchlists. */
    get watchlists() {
      const c = this;
      return {
        list: function (query, options) { return c.get("/api/watchlists", query, options); },
        create: function (body, options) { return c.post("/api/watchlists", body, options); },
        get: function (id, options) { return c.get("/api/watchlists/" + encodeURIComponent(id), null, options); },
        update: function (id, body, options) { return c.put("/api/watchlists/" + encodeURIComponent(id), body, options); },
        remove: function (id, options) { return c.del("/api/watchlists/" + encodeURIComponent(id), options); },
      };
    }

    /** Stock Research — any ticker, one method per tab payload. */
    get research() {
      const c = this;
      const at = function (symbol, suffix) {
        return "/api/research/company/" + encodeURIComponent(String(symbol || "").toUpperCase()) + (suffix || "");
      };
      return {
        company: function (symbol, query, options) { return c.get(at(symbol), query, options); },
        financials: function (symbol, query, options) { return c.get(at(symbol, "/financials"), query, options); },
        estimates: function (symbol, query, options) { return c.get(at(symbol, "/estimates"), query, options); },
        valuation: function (symbol, query, options) { return c.get(at(symbol, "/valuation"), query, options); },
        news: function (symbol, query, options) { return c.get(at(symbol, "/news"), query, options); },
        altdata: function (symbol, query, options) { return c.get(at(symbol, "/altdata"), query, options); },
        thesis: function (symbol, query, options) { return c.get(at(symbol, "/thesis"), query, options); },
        filings: function (symbol, query, options) { return c.get(at(symbol, "/filings"), query, options); },
        memory: function (symbol, query, options) { return c.get(at(symbol, "/memory"), query, options); },
        price: function (symbol, range, options) { return c.get(at(symbol, "/price"), { range: range }, options); },
      };
    }

    /** Screener. */
    get screens() {
      const c = this;
      return {
        list: function (query, options) { return c.get("/api/screens", query, options); },
        create: function (body, options) { return c.post("/api/screens", body, options); },
        get: function (id, options) { return c.get("/api/screens/" + encodeURIComponent(id), null, options); },
        update: function (id, body, options) { return c.put("/api/screens/" + encodeURIComponent(id), body, options); },
        remove: function (id, options) { return c.del("/api/screens/" + encodeURIComponent(id), options); },
        run: function (id, body, options) { return c.post("/api/screens/" + encodeURIComponent(id) + "/run", body || {}, options); },
        queueBacktest: function (id, body, options) { return c.post("/api/screens/" + encodeURIComponent(id) + "/queue-backtest", body || {}, options); },
      };
    }

    /** Strategy Lab: hypotheses, experiments, backtests, validation, evaluation. */
    get lab() {
      const c = this;
      return {
        summary: function (options) { return c.get("/api/lab", null, options); },

        hypotheses: function (query, options) { return c.get("/api/lab/hypotheses", query, options); },
        hypothesis: function (id, options) { return c.get("/api/lab/hypotheses/" + encodeURIComponent(id), null, options); },
        createHypothesis: function (body, options) { return c.post("/api/lab/hypotheses", body, options); },
        updateHypothesis: function (id, body, options) { return c.put("/api/lab/hypotheses/" + encodeURIComponent(id), body, options); },
        deleteHypothesis: function (id, options) { return c.del("/api/lab/hypotheses/" + encodeURIComponent(id), options); },
        setHypothesisStatus: function (id, body, options) { return c.post("/api/lab/hypotheses/" + encodeURIComponent(id) + "/status", body, options); },
        addEvidence: function (id, body, options) { return c.post("/api/lab/hypotheses/" + encodeURIComponent(id) + "/evidence", body, options); },

        experiments: function (query, options) { return c.get("/api/lab/experiments", query, options); },
        experiment: function (id, options) { return c.get("/api/lab/experiments/" + encodeURIComponent(id), null, options); },
        createExperiment: function (body, options) { return c.post("/api/lab/experiments", body, options); },
        updateExperiment: function (id, body, options) { return c.put("/api/lab/experiments/" + encodeURIComponent(id), body, options); },
        deleteExperiment: function (id, options) { return c.del("/api/lab/experiments/" + encodeURIComponent(id), options); },
        runExperiment: function (id, body, options) { return c.post("/api/lab/experiments/" + encodeURIComponent(id) + "/run", body || {}, options); },
        experimentRuns: function (query, options) { return c.get("/api/lab/experiments/runs", query, options); },

        backtests: function (query, options) { return c.get("/api/lab/backtests", query, options); },
        backtest: function (id, options) { return c.get("/api/lab/backtests/" + encodeURIComponent(id), null, options); },
        backtestResults: function (id, options) { return c.get("/api/lab/backtests/" + encodeURIComponent(id) + "/results", null, options); },
        queueBacktest: function (body, options) { return c.post("/api/lab/backtests", body, options); },
        runBacktest: function (body, options) { return c.post("/api/lab/backtests/run", body, options); },
        executeBacktest: function (id, body, options) { return c.post("/api/lab/backtests/" + encodeURIComponent(id) + "/run", body || {}, options); },
        deleteBacktest: function (id, options) { return c.del("/api/lab/backtests/" + encodeURIComponent(id), options); },

        /** Block-bootstrap path cone behind the equity fan chart. */
        fan: function (id, query, options) { return c.get("/api/lab/backtests/" + encodeURIComponent(id) + "/fan", query, options); },
        /** Absolute URL of a run's tearsheet PDF (a plain download link). */
        tearsheetUrl: function (id) { return c.url("/api/lab/backtests/" + encodeURIComponent(id) + "/tearsheet.pdf"); },
        /** Absolute URL of a run's return series as CSV (a plain download link). */
        returnsCsvUrl: function (id) { return c.url("/api/lab/backtests/" + encodeURIComponent(id) + "/returns.csv"); },
        /** Dry run: what a config *would* do, without writing a ledger row. */
        validateConfig: function (body, options) { return c.post("/api/lab/backtests/validate", body, options); },
        /** The pinned cfg + data + code bundle that reproduces one run. */
        artifact: function (id, options) { return c.get("/api/lab/backtests/" + encodeURIComponent(id) + "/artifact", null, options); },

        analyses: function (query, options) { return c.get("/api/lab/analyses", query, options); },
        analysis: function (id, options) { return c.get("/api/lab/analyses/" + encodeURIComponent(id), null, options); },
        analysisOptions: function (options) { return c.get("/api/lab/analyses/options", null, options); },
        /** Sweep/walk-forward batch and cell counts behind the trial ledger. */
        analysisCounts: function (options) { return c.get("/api/lab/analyses/counts", null, options); },
        runSweep: function (body, options) { return c.post("/api/lab/analyses/sweep", body, options); },
        runWalkForward: function (body, options) { return c.post("/api/lab/analyses/walk-forward", body, options); },
        deleteAnalysis: function (id, options) { return c.del("/api/lab/analyses/" + encodeURIComponent(id), options); },

        validation: function (query, options) { return c.get("/api/lab/validation", query, options); },
        gates: function (options) { return c.get("/api/lab/validation/gates", null, options); },
        validate: function (backtestId, body, options) { return c.post("/api/lab/validation/" + encodeURIComponent(backtestId), body || {}, options); },
        backtestGates: function (backtestId, options) { return c.get("/api/lab/validation/" + encodeURIComponent(backtestId) + "/gates", null, options); },
        promote: function (backtestId, body, options) { return c.post("/api/lab/validation/" + encodeURIComponent(backtestId) + "/promote", body || {}, options); },
        promotionApprovals: function (backtestId, query, options) { return c.get("/api/lab/validation/" + encodeURIComponent(backtestId) + "/approvals", query, options); },
        withdrawApproval: function (backtestId, actor, options) { return c.del("/api/lab/validation/" + encodeURIComponent(backtestId) + "/approvals/" + encodeURIComponent(actor), options); },
        pendingPromotions: function (query, options) { return c.get("/api/lab/validation/promotions/pending", query, options); },
        strategies: function (query, options) { return c.get("/api/lab/validation/strategies", query, options); },

        evaluation: function (query, options) { return c.get("/api/lab/evaluation", query, options); },
        candidates: function (query, options) { return c.get("/api/lab/evaluation/candidates", query, options); },
        tracking: function (query, options) { return c.get("/api/lab/evaluation/tracking", query, options); },
      };
    }

    /** Data ingestion: sources, connection tests, backfills, gateway health. */
    get ingest() {
      const c = this;
      return {
        sources: function (query, options) { return c.get("/api/ingest/sources", query, options); },
        source: function (id, options) { return c.get("/api/ingest/sources/" + encodeURIComponent(id), null, options); },
        createSource: function (body, options) { return c.post("/api/ingest/sources", body, options); },
        updateSource: function (id, body, options) { return c.put("/api/ingest/sources/" + encodeURIComponent(id), body, options); },
        deleteSource: function (id, options) { return c.del("/api/ingest/sources/" + encodeURIComponent(id), options); },
        test: function (body, options) { return c.post("/api/ingest/sources/test", body, options); },
        // The route reads `range`, `end` and `wait` from the query string, so
        // the window travels there rather than in a body FastAPI would ignore.
        backfill: function (id, query, options) {
          return c.post("/api/ingest/sources/" + encodeURIComponent(id) + "/backfill", null,
            Object.assign({ query: query || {} }, options || {}));
        },
        subscriptions: function (query, options) { return c.get("/api/ingest/subscriptions", query, options); },
        gateway: function (query, options) { return c.get("/api/ingest/gateway", query, options); },
      };
    }

    /** Data quality checks, series scores and incidents. */
    get quality() {
      const c = this;
      return {
        summary: function (query, options) { return c.get("/api/quality/summary", query, options); },
        series: function (query, options) { return c.get("/api/quality/series", query, options); },
        incidents: function (query, options) { return c.get("/api/quality/incidents", query, options); },
      };
    }

    /** Agent Center. Routes land with the agents backend; `optional()` covers the gap. */
    get agents() {
      const c = this;
      return {
        models: function (options) { return c.get("/api/agents/models", null, options); },
        config: function (options) { return c.get("/api/agents/config", null, options); },
        saveConfig: function (body, options) { return c.put("/api/agents/config", body, options); },

        definitions: function (query, options) { return c.get("/api/agents/definitions", query, options); },
        createDefinition: function (body, options) { return c.post("/api/agents/definitions", body, options); },
        updateDefinition: function (id, body, options) { return c.put("/api/agents/definitions/" + encodeURIComponent(id), body, options); },
        deleteDefinition: function (id, options) { return c.del("/api/agents/definitions/" + encodeURIComponent(id), options); },

        pipelines: function (query, options) { return c.get("/api/agents/pipelines", query, options); },
        pipeline: function (id, options) { return c.get("/api/agents/pipelines/" + encodeURIComponent(id), null, options); },
        createPipeline: function (body, options) { return c.post("/api/agents/pipelines", body, options); },
        updatePipeline: function (id, body, options) { return c.put("/api/agents/pipelines/" + encodeURIComponent(id), body, options); },
        deletePipeline: function (id, options) { return c.del("/api/agents/pipelines/" + encodeURIComponent(id), options); },
        duplicatePipeline: function (id, body, options) { return c.post("/api/agents/pipelines/" + encodeURIComponent(id) + "/duplicate", body || {}, options); },
        pipelineVersions: function (id, options) { return c.get("/api/agents/pipelines/" + encodeURIComponent(id) + "/versions", null, options); },
        runPipeline: function (id, body, options) { return c.post("/api/agents/pipelines/" + encodeURIComponent(id) + "/run", body || {}, options); },

        runs: function (query, options) { return c.get("/api/agents/runs", query, options); },
        run: function (id, options) { return c.get("/api/agents/runs/" + encodeURIComponent(id), null, options); },
        runLogs: function (id, query, options) { return c.get("/api/agents/runs/" + encodeURIComponent(id) + "/logs", query, options); },
        runArtifacts: function (id, query, options) { return c.get("/api/agents/runs/" + encodeURIComponent(id) + "/artifacts", query, options); },

        inbox: function (query, options) { return c.get("/api/agents/inbox", query, options); },
        approval: function (id, options) { return c.get("/api/agents/approvals/" + encodeURIComponent(id), null, options); },
        decideApproval: function (id, body, options) { return c.post("/api/agents/approvals/" + encodeURIComponent(id), body, options); },

        findings: function (query, options) { return c.get("/api/agents/findings", query, options); },
        knowledge: function (query, options) { return c.get("/api/agents/knowledge", query, options); },
        audit: function (query, options) { return c.get("/api/agents/audit", query, options); },
        schedules: function (query, options) { return c.get("/api/agents/autopipeline", query, options); },
      };
    }

    /** Paper/live portfolio and orders (no UI surface yet, SPEC D4). */
    get trade() {
      const c = this;
      return {
        portfolio: function (query, options) { return c.get("/api/trade/portfolio", query, options); },
        orders: function (query, options) { return c.get("/api/trade/orders", query, options); },
      };
    }

    /** Application settings (theme, density, timezone, benchmark, key status). */
    get settings() {
      const c = this;
      return {
        get: function (options) { return c.get("/api/settings", null, options); },
        save: function (body, options) { return c.put("/api/settings", body, options); },
      };
    }

    /**
     * Palette search across pages, symbols and entities.
     * @param {string} q
     * @returns {Promise<any>} the search payload
     */
    search(q, query, options) {
      return this.get("/api/search", Object.assign({ q: q }, query || {}), options);
    }
  }

  global.ApiError = ApiError;
  global.AlgoApi = AlgoApi;
  /** Shared instance. Pages use this; tests may build their own. */
  global.api = new AlgoApi();
})(window);
