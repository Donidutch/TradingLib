/* TradingLib — LiveStream: the browser end of /ws.
 *
 * Protocol (SPEC section 5, tradinglib/api/ws.py):
 *   client → {"action":"subscribe","channel":"tape","params":{...}}
 *            {"action":"unsubscribe","channel":"tape"}
 *            {"action":"ping"}
 *   server → {"type":"welcome"|"subscribed"|"unsubscribed"|"pong"|"error"}
 *            {"type":"event","channel":"status","payload":{...},"ts":...,"id":...}
 *
 * What this class guarantees:
 *   - subscriptions are declarative. `subscribe(channel, params)` records what
 *     this page wants; the socket is told on connect and re-told after every
 *     reconnect, so a dropped connection costs no data wiring.
 *   - reconnect with exponential backoff and jitter, capped, forever — with a
 *     `state` that the shell renders honestly ("DATA LIVE" only while open).
 *   - a heartbeat that detects a socket that is open but dead: a ping every
 *     `pingIntervalMs`, and a watchdog that tears the socket down when nothing
 *     at all has arrived for `idleTimeoutMs`.
 *   - handlers are per-channel and receive the event payload; a throwing
 *     handler is logged and cannot break the dispatch loop.
 *
 * Publishes `window.LiveStream` (class) and `window.stream` (shared instance).
 */
(function (global) {
  "use strict";

  /** Connection states, in the order the UI cares about. */
  const STATE = {
    IDLE: "idle",
    CONNECTING: "connecting",
    OPEN: "open",
    RECONNECTING: "reconnecting",
    CLOSED: "closed",
  };

  class LiveStream {
    /**
     * @param {{url?: string, pingIntervalMs?: number, idleTimeoutMs?: number,
     *          baseBackoffMs?: number, maxBackoffMs?: number}} [options]
     */
    constructor(options) {
      const opts = options || {};
      const proto = global.location.protocol === "https:" ? "wss:" : "ws:";
      this.url = opts.url || proto + "//" + global.location.host + "/ws";
      this.pingIntervalMs = opts.pingIntervalMs || 20000;
      this.idleTimeoutMs = opts.idleTimeoutMs || 60000;
      this.baseBackoffMs = opts.baseBackoffMs || 1000;
      this.maxBackoffMs = opts.maxBackoffMs || 30000;

      /** @type {WebSocket|null} */
      this._socket = null;
      /** @type {string} one of STATE */
      this.state = STATE.IDLE;
      /** @type {string|null} why the last connection ended */
      this.lastError = null;
      /** @type {number} consecutive failed attempts, drives the backoff */
      this.attempts = 0;
      /** @type {number} epoch ms of the last frame of any kind */
      this.lastFrameAt = 0;
      /** @type {number} total events dispatched since page load */
      this.received = 0;
      /** @type {string[]|null} channels the server said it supports */
      this.serverChannels = null;
      this._closedByUs = false;

      /** @type {Map<string, object>} channel → params, the desired state */
      this._wanted = new Map();
      /** @type {Map<string, Set<Function>>} channel → handlers */
      this._handlers = new Map();
      /** @type {Set<Function>} state listeners */
      this._stateListeners = new Set();

      this._reconnectTimer = 0;
      this._pingTimer = 0;
      this._watchdogTimer = 0;

      this._onVisibility = this._onVisibility.bind(this);
    }

    // ------------------------------------------------------------ lifecycle

    /** Open the socket (idempotent) and keep it open until `close()`. */
    connect() {
      if (this._socket && (this._socket.readyState === WebSocket.OPEN
        || this._socket.readyState === WebSocket.CONNECTING)) {
        return;
      }
      this._closedByUs = false;
      clearTimeout(this._reconnectTimer);
      this._setState(this.attempts > 0 ? STATE.RECONNECTING : STATE.CONNECTING);

      let socket;
      try {
        socket = new WebSocket(this.url);
      } catch (err) {
        this.lastError = String((err && err.message) || err);
        this._scheduleReconnect();
        return;
      }
      this._socket = socket;

      const self = this;
      socket.onopen = function () {
        if (self._socket !== socket) return;
        self.attempts = 0;
        self.lastError = null;
        self.lastFrameAt = Date.now();
        self._setState(STATE.OPEN);
        self._resubscribeAll();
        self._startTimers();
      };
      socket.onmessage = function (event) {
        if (self._socket !== socket) return;
        self.lastFrameAt = Date.now();
        self._handleFrame(event.data);
      };
      socket.onerror = function () {
        // The close event that follows carries the useful information; record
        // that something went wrong so the state text is not silently stale.
        self.lastError = self.lastError || "websocket error";
      };
      socket.onclose = function (event) {
        if (self._socket !== socket) return;
        self._socket = null;
        self._stopTimers();
        if (self._closedByUs) {
          self._setState(STATE.CLOSED);
          return;
        }
        self.lastError = "socket closed (code " + event.code
          + (event.reason ? ", " + event.reason : "") + ")";
        self._scheduleReconnect();
      };

      if (!this._visibilityBound) {
        document.addEventListener("visibilitychange", this._onVisibility);
        this._visibilityBound = true;
      }
    }

    /** Close the socket and stop reconnecting. Subscriptions are kept. */
    close() {
      this._closedByUs = true;
      clearTimeout(this._reconnectTimer);
      this._stopTimers();
      const socket = this._socket;
      this._socket = null;
      if (socket) {
        try {
          socket.close(1000, "client closed");
        } catch (err) {
          console.warn("stream close failed", err);
        }
      }
      this._setState(STATE.CLOSED);
    }

    /** @returns {boolean} whether the socket is open right now. */
    get connected() {
      return this.state === STATE.OPEN;
    }

    /** A snapshot for the shell's health rendering. */
    status() {
      return {
        state: this.state,
        connected: this.connected,
        url: this.url,
        attempts: this.attempts,
        lastError: this.lastError,
        lastFrameAt: this.lastFrameAt || null,
        channels: Array.from(this._wanted.keys()),
        received: this.received,
      };
    }

    // --------------------------------------------------------- subscriptions

    /**
     * Ask for a channel. Recorded as desired state and (re)sent on every
     * connect, so calling it before the socket opens is fine.
     * @param {string} channel e.g. "status", "tape", "tape.NVDA", "runs"
     * @param {object} [params] channel parameters (e.g. {symbols:[...]})
     * @returns {() => void} unsubscribe
     */
    subscribe(channel, params) {
      const previous = this._wanted.get(channel);
      const next = params || {};
      this._wanted.set(channel, next);
      if (this.connected && (!previous || JSON.stringify(previous) !== JSON.stringify(next))) {
        this._send({ action: "subscribe", channel: channel, params: next });
      }
      const self = this;
      return function () { self.unsubscribe(channel); };
    }

    /**
     * Drop a channel subscription (server-side too, when connected).
     * @param {string} channel
     */
    unsubscribe(channel) {
      if (!this._wanted.has(channel)) return;
      this._wanted.delete(channel);
      if (this.connected) this._send({ action: "unsubscribe", channel: channel });
    }

    /**
     * Handle events on a channel. Subscribing is separate on purpose: several
     * pages can listen to one subscription without fighting over it.
     * @param {string} channel exact channel name, or a "tape.*" style prefix
     * @param {(payload: any, frame: object) => void} handler
     * @returns {() => void} remove this handler
     */
    on(channel, handler) {
      let set = this._handlers.get(channel);
      if (!set) {
        set = new Set();
        this._handlers.set(channel, set);
      }
      set.add(handler);
      const self = this;
      return function () {
        const current = self._handlers.get(channel);
        if (!current) return;
        current.delete(handler);
        if (current.size === 0) self._handlers.delete(channel);
      };
    }

    /**
     * Subscribe and listen in one call — the common case for a page.
     * @param {string} channel
     * @param {(payload: any, frame: object) => void} handler
     * @param {object} [params]
     * @returns {() => void} removes the handler and, if it was the last one for
     *   this channel, drops the subscription too.
     */
    listen(channel, handler, params) {
      const off = this.on(channel, handler);
      this.subscribe(channel, params);
      const self = this;
      return function () {
        off();
        if (!self._handlers.has(channel)) self.unsubscribe(channel);
      };
    }

    /**
     * Observe connection-state changes.
     * @param {(state: string, stream: LiveStream) => void} fn
     * @returns {() => void} unsubscribe
     */
    onState(fn) {
      this._stateListeners.add(fn);
      const self = this;
      return function () { self._stateListeners.delete(fn); };
    }

    // -------------------------------------------------------------- internals

    _resubscribeAll() {
      const self = this;
      this._wanted.forEach(function (params, channel) {
        self._send({ action: "subscribe", channel: channel, params: params || {} });
      });
    }

    /** @param {object} frame @returns {boolean} whether it went out */
    _send(frame) {
      if (!this._socket || this._socket.readyState !== WebSocket.OPEN) return false;
      try {
        this._socket.send(JSON.stringify(frame));
        return true;
      } catch (err) {
        console.warn("stream send failed", err);
        return false;
      }
    }

    /** @param {string} raw one server frame */
    _handleFrame(raw) {
      let frame;
      try {
        frame = JSON.parse(raw);
      } catch (err) {
        console.warn("stream: unparsable frame", err);
        return;
      }
      switch (frame.type) {
        case "welcome":
          this.serverChannels = Array.isArray(frame.channels) ? frame.channels : null;
          return;
        case "pong":
        case "subscribed":
        case "unsubscribed":
          return;
        case "error":
          console.warn("stream error frame:", frame.error, frame.detail || "");
          this.lastError = String(frame.error || "server error");
          return;
        case "event":
          this.received += 1;
          this._dispatch(frame);
          return;
        default:
          // Unknown frame types are ignored on purpose: the server may grow new
          // ones, and an old page must not break on them.
          return;
      }
    }

    /** @param {{channel: string, payload: any}} frame */
    _dispatch(frame) {
      const channel = String(frame.channel || "");
      const self = this;
      this._handlers.forEach(function (handlers, pattern) {
        if (!self._matches(pattern, channel)) return;
        handlers.forEach(function (handler) {
          try {
            handler(frame.payload, frame);
          } catch (err) {
            console.error("stream handler failed for " + channel, err);
          }
        });
      });
    }

    /**
     * Channel matching, mirroring tradinglib.events.channel_matches: an exact name,
     * a "*" wildcard, or a "root.*" prefix that also matches the bare root.
     * @param {string} pattern @param {string} channel
     */
    _matches(pattern, channel) {
      if (pattern === "*" || pattern === channel) return true;
      if (pattern.slice(-2) === ".*") {
        const root = pattern.slice(0, -2);
        return channel === root || channel.indexOf(root + ".") === 0;
      }
      // A bare root subscribes to its children too ("tape" hears "tape.NVDA").
      return channel.indexOf(pattern + ".") === 0;
    }

    _startTimers() {
      this._stopTimers();
      const self = this;
      this._pingTimer = setInterval(function () {
        self._send({ action: "ping" });
      }, this.pingIntervalMs);
      this._watchdogTimer = setInterval(function () {
        if (!self.connected) return;
        if (Date.now() - self.lastFrameAt < self.idleTimeoutMs) return;
        // Open but silent: nothing has arrived, not even a pong. Treat it as
        // dead and force the reconnect path rather than showing stale "live".
        self.lastError = "no traffic for " + Math.round(self.idleTimeoutMs / 1000) + "s";
        const socket = self._socket;
        self._socket = null;
        self._stopTimers();
        if (socket) {
          try { socket.close(4000, "idle timeout"); } catch (err) { /* already gone */ }
        }
        self._scheduleReconnect();
      }, Math.max(5000, Math.floor(this.idleTimeoutMs / 2)));
    }

    _stopTimers() {
      if (this._pingTimer) clearInterval(this._pingTimer);
      if (this._watchdogTimer) clearInterval(this._watchdogTimer);
      this._pingTimer = 0;
      this._watchdogTimer = 0;
    }

    _scheduleReconnect() {
      if (this._closedByUs) return;
      this.attempts += 1;
      this._setState(STATE.RECONNECTING);
      // Exponential backoff with full jitter, capped: a server that is down for
      // a while must not be hammered, but recovery must still be quick.
      const ceiling = Math.min(this.maxBackoffMs, this.baseBackoffMs * Math.pow(2, this.attempts - 1));
      const delay = Math.round(ceiling * (0.5 + Math.random() * 0.5));
      const self = this;
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = setTimeout(function () { self.connect(); }, delay);
      this.nextRetryInMs = delay;
    }

    _onVisibility() {
      // Coming back to a backgrounded tab: retry immediately rather than
      // waiting out a long backoff the user cannot see.
      if (document.visibilityState !== "visible") return;
      if (this._closedByUs || this.connected) return;
      clearTimeout(this._reconnectTimer);
      this.connect();
    }

    /** @param {string} state */
    _setState(state) {
      if (this.state === state) return;
      this.state = state;
      const self = this;
      this._stateListeners.forEach(function (fn) {
        try {
          fn(state, self);
        } catch (err) {
          console.error("stream state listener failed", err);
        }
      });
    }
  }

  LiveStream.STATE = STATE;
  global.LiveStream = LiveStream;
  /** Shared instance; app.js connects it once the shell is wired. */
  global.stream = new LiveStream();
})(window);
