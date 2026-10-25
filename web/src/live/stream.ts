/* The browser end of /ws, ported from ui/js/stream.js.
 *
 * Protocol (tradinglib/api/ws.py):
 *   client → {"action":"subscribe","channel":"tape","params":{...}}
 *            {"action":"unsubscribe","channel":"tape"} | {"action":"ping"}
 *   server → {"type":"welcome"|"subscribed"|"unsubscribed"|"pong"|"error"}
 *            {"type":"event","channel":"tape.NVDA","payload":{...},"ts":...,"id":...}
 *
 * Guarantees, as before:
 *   - subscriptions are declarative and re-sent after every reconnect;
 *   - reconnect with capped exponential backoff and jitter, forever, and a
 *     `state` the shell renders honestly ("live" only while open);
 *   - a ping plus an idle watchdog, so an open-but-dead socket is torn down;
 *   - a throwing handler is logged and cannot break dispatch.
 * New: each listener names a Zod schema, and a payload that fails it is
 * counted in `rejected` and logged instead of reaching the handler.
 */
import type { z } from "zod";

export type StreamState = "idle" | "connecting" | "open" | "reconnecting" | "closed";

export interface StreamSnapshot {
  state: StreamState;
  attempts: number;
  lastError: string | null;
  lastFrameAt: number | null;
  nextRetryInMs: number | null;
  received: number;
  rejected: number;
  serverChannels: readonly string[] | null;
}

export interface StreamOptions {
  url?: string;
  pingIntervalMs?: number;
  idleTimeoutMs?: number;
  baseBackoffMs?: number;
  maxBackoffMs?: number;
  /** Injected in tests; defaults to the browser's WebSocket. */
  socketFactory?: (url: string) => WebSocket;
}

interface EventFrame {
  type: "event";
  channel: string;
  payload: unknown;
}

type Handler = (payload: unknown, channel: string) => void;
type Params = Record<string, unknown>;

/**
 * Channel matching, mirroring tradinglib.events.channel_matches: an exact name,
 * "*", or a "root.*" prefix; a bare root also hears its children.
 */
export function channelMatches(pattern: string, channel: string): boolean {
  if (pattern === "*" || pattern === channel) return true;
  const root = pattern.endsWith(".*") ? pattern.slice(0, -2) : pattern;
  return channel === root || channel.startsWith(`${root}.`);
}

function defaultUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws`;
}

export class LiveStream {
  readonly url: string;
  private readonly pingIntervalMs: number;
  private readonly idleTimeoutMs: number;
  private readonly baseBackoffMs: number;
  private readonly maxBackoffMs: number;
  private readonly socketFactory: (url: string) => WebSocket;

  private socket: WebSocket | null = null;
  private closedByUs = false;
  private snap: StreamSnapshot = {
    state: "idle",
    attempts: 0,
    lastError: null,
    lastFrameAt: null,
    nextRetryInMs: null,
    received: 0,
    rejected: 0,
    serverChannels: null,
  };

  private readonly wanted = new Map<string, Params>();
  private readonly handlers = new Map<string, Set<Handler>>();
  private readonly snapshotListeners = new Set<() => void>();
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private pingTimer: ReturnType<typeof setInterval> | undefined;
  private watchdogTimer: ReturnType<typeof setInterval> | undefined;
  private visibilityBound = false;

  constructor(options: StreamOptions = {}) {
    this.url = options.url ?? defaultUrl();
    this.pingIntervalMs = options.pingIntervalMs ?? 20_000;
    this.idleTimeoutMs = options.idleTimeoutMs ?? 60_000;
    this.baseBackoffMs = options.baseBackoffMs ?? 1_000;
    this.maxBackoffMs = options.maxBackoffMs ?? 30_000;
    this.socketFactory = options.socketFactory ?? ((url) => new WebSocket(url));
  }

  /** An immutable snapshot, replaced on every change (useSyncExternalStore). */
  snapshot(): StreamSnapshot {
    return this.snap;
  }

  get connected(): boolean {
    return this.snap.state === "open";
  }

  onChange(listener: () => void): () => void {
    this.snapshotListeners.add(listener);
    return () => this.snapshotListeners.delete(listener);
  }

  // ------------------------------------------------------------ lifecycle

  /** Open the socket (idempotent) and keep it open until close(). */
  connect(): void {
    const ready = this.socket?.readyState;
    if (ready === WebSocket.OPEN || ready === WebSocket.CONNECTING) return;
    this.closedByUs = false;
    clearTimeout(this.reconnectTimer);
    this.update({ state: this.snap.attempts > 0 ? "reconnecting" : "connecting", nextRetryInMs: null });

    let socket: WebSocket;
    try {
      socket = this.socketFactory(this.url);
    } catch (err) {
      this.update({ lastError: err instanceof Error ? err.message : String(err) });
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.update({ state: "open", attempts: 0, lastError: null, lastFrameAt: Date.now() });
      for (const [channel, params] of this.wanted) this.send({ action: "subscribe", channel, params });
      this.startTimers();
    };
    socket.onmessage = (event: MessageEvent) => {
      if (this.socket !== socket) return;
      this.snap = { ...this.snap, lastFrameAt: Date.now() };
      this.handleFrame(String(event.data));
    };
    socket.onerror = () => {
      // The close that follows carries the detail; do not leave the text stale.
      if (!this.snap.lastError) this.update({ lastError: "websocket error" });
    };
    socket.onclose = (event: CloseEvent) => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.stopTimers();
      if (this.closedByUs) {
        this.update({ state: "closed" });
        return;
      }
      this.update({
        lastError: `socket closed (code ${event.code}${event.reason ? `, ${event.reason}` : ""})`,
      });
      this.scheduleReconnect();
    };

    if (!this.visibilityBound && typeof document !== "undefined") {
      document.addEventListener("visibilitychange", this.onVisibility);
      this.visibilityBound = true;
    }
  }

  /** Close and stop reconnecting. Subscriptions are kept for the next connect(). */
  close(): void {
    this.closedByUs = true;
    clearTimeout(this.reconnectTimer);
    this.stopTimers();
    const socket = this.socket;
    this.socket = null;
    socket?.close(1000, "client closed");
    if (this.visibilityBound) {
      document.removeEventListener("visibilitychange", this.onVisibility);
      this.visibilityBound = false;
    }
    this.update({ state: "closed", nextRetryInMs: null });
  }

  // --------------------------------------------------------- subscriptions

  /** Ask for a channel; recorded and (re)sent on every connect. */
  subscribe(channel: string, params: Params = {}): () => void {
    const previous = this.wanted.get(channel);
    this.wanted.set(channel, params);
    if (this.connected && (!previous || JSON.stringify(previous) !== JSON.stringify(params))) {
      this.send({ action: "subscribe", channel, params });
    }
    return () => this.unsubscribe(channel);
  }

  unsubscribe(channel: string): void {
    if (!this.wanted.delete(channel)) return;
    if (this.connected) this.send({ action: "unsubscribe", channel });
  }

  /**
   * Handle events on a channel (exact, "root.*" or bare root). Subscribing is
   * separate, so several components can share one subscription.
   */
  on<S extends z.ZodType>(
    channel: string,
    schema: S,
    handler: (payload: z.output<S>, channel: string) => void,
  ): () => void {
    const wrapped: Handler = (payload, from) => {
      const parsed = schema.safeParse(payload);
      if (!parsed.success) {
        this.snap = { ...this.snap, rejected: this.snap.rejected + 1 };
        console.error(`stream: ${from} payload rejected`, parsed.error.issues);
        return;
      }
      handler(parsed.data, from);
    };
    let set = this.handlers.get(channel);
    if (!set) this.handlers.set(channel, (set = new Set()));
    set.add(wrapped);
    return () => {
      const current = this.handlers.get(channel);
      current?.delete(wrapped);
      if (current?.size === 0) this.handlers.delete(channel);
    };
  }

  /** Subscribe and listen; the last listener to leave drops the subscription. */
  listen<S extends z.ZodType>(
    channel: string,
    schema: S,
    handler: (payload: z.output<S>, channel: string) => void,
    params?: Params,
  ): () => void {
    const off = this.on(channel, schema, handler);
    this.subscribe(channel, params);
    return () => {
      off();
      if (!this.handlers.has(channel)) this.unsubscribe(channel);
    };
  }

  // -------------------------------------------------------------- internals

  private send(frame: object): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    try {
      this.socket.send(JSON.stringify(frame));
      return true;
    } catch (err) {
      console.warn("stream send failed", err);
      return false;
    }
  }

  private handleFrame(raw: string): void {
    let frame: { type?: unknown; [key: string]: unknown };
    try {
      frame = JSON.parse(raw) as typeof frame;
    } catch (err) {
      console.warn("stream: unparsable frame", err);
      return;
    }
    switch (frame.type) {
      case "welcome":
        this.update({ serverChannels: Array.isArray(frame.channels) ? (frame.channels as string[]) : null });
        return;
      case "error":
        console.warn("stream error frame:", frame.error, frame.detail ?? "");
        this.update({ lastError: String(frame.error ?? "server error") });
        return;
      case "event":
        this.snap = { ...this.snap, received: this.snap.received + 1 };
        this.dispatch(frame as unknown as EventFrame);
        return;
      default:
        // pong, subscribed, unsubscribed, and frame types newer than this app.
        return;
    }
  }

  private dispatch(frame: EventFrame): void {
    const channel = String(frame.channel ?? "");
    for (const [pattern, handlers] of this.handlers) {
      if (!channelMatches(pattern, channel)) continue;
      for (const handler of handlers) {
        try {
          handler(frame.payload, channel);
        } catch (err) {
          console.error(`stream handler failed for ${channel}`, err);
        }
      }
    }
  }

  private startTimers(): void {
    this.stopTimers();
    this.pingTimer = setInterval(() => this.send({ action: "ping" }), this.pingIntervalMs);
    this.watchdogTimer = setInterval(
      () => {
        if (!this.connected) return;
        if (Date.now() - (this.snap.lastFrameAt ?? 0) < this.idleTimeoutMs) return;
        // Open but silent, not even a pong: treat as dead, never show stale "live".
        const socket = this.socket;
        this.socket = null;
        this.stopTimers();
        socket?.close(4000, "idle timeout");
        this.update({ lastError: `no traffic for ${Math.round(this.idleTimeoutMs / 1000)}s` });
        this.scheduleReconnect();
      },
      Math.max(5_000, Math.floor(this.idleTimeoutMs / 2)),
    );
  }

  private stopTimers(): void {
    clearInterval(this.pingTimer);
    clearInterval(this.watchdogTimer);
    this.pingTimer = undefined;
    this.watchdogTimer = undefined;
  }

  private scheduleReconnect(): void {
    if (this.closedByUs) return;
    const attempts = this.snap.attempts + 1;
    // Full jitter, capped: a long outage is not hammered, recovery stays quick.
    const ceiling = Math.min(this.maxBackoffMs, this.baseBackoffMs * 2 ** (attempts - 1));
    const delay = Math.round(ceiling * (0.5 + Math.random() * 0.5));
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
    this.update({ state: "reconnecting", attempts, nextRetryInMs: delay });
  }

  private readonly onVisibility = (): void => {
    // Back on a hidden tab: retry now rather than wait out an unseen backoff.
    if (document.visibilityState !== "visible" || this.closedByUs || this.connected) return;
    clearTimeout(this.reconnectTimer);
    this.connect();
  };

  private update(patch: Partial<StreamSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    for (const listener of this.snapshotListeners) {
      try {
        listener();
      } catch (err) {
        console.error("stream listener failed", err);
      }
    }
  }
}
