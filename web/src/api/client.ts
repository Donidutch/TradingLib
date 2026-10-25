/* The one fetch wrapper for the React console, ported from ui/js/api.js.
 *
 * Rules it keeps:
 *   - every failure is a real failure. A non-2xx response throws ApiError with
 *     the status, URL and whatever body the server sent; nothing is faked.
 *   - `optional()` resolves to null on 404/501 only, so a route this build
 *     lacks is "not built yet" while a genuine outage still throws.
 *   - reachability is observable: every request updates `online` and notifies
 *     listeners, which the shell folds into the ENGINE indicator.
 * And one it adds: every response is parsed with a Zod schema. A body that
 * does not match is an ApiSchemaError, never a silently empty panel.
 */
import type { z } from "zod";

export interface ApiErrorInfo {
  status?: number;
  url?: string;
  payload?: unknown;
  cause?: unknown;
}

/** Error carrying the HTTP status and parsed body of a failed API call. */
export class ApiError extends Error {
  readonly status: number;
  readonly url: string;
  readonly payload: unknown;
  /** True when the caller aborted the request; not an outage. */
  cancelled = false;

  constructor(message: string, info: ApiErrorInfo = {}) {
    super(message, { cause: info.cause });
    this.name = "ApiError";
    this.status = info.status ?? 0;
    this.url = info.url ?? "";
    this.payload = info.payload ?? null;
  }

  /** The server answered, but said the route or entity does not exist. */
  get missing(): boolean {
    return this.status === 404 || this.status === 501;
  }

  /** The request never reached the server. */
  get offline(): boolean {
    return this.status === 0 && !this.cancelled;
  }
}

/** The server answered 2xx, but the body is not the shape the app expects. */
export class ApiSchemaError extends ApiError {
  readonly issues: z.core.$ZodIssue[];

  constructor(url: string, status: number, payload: unknown, issues: z.core.$ZodIssue[]) {
    const first = issues[0];
    const where = first ? `${first.path.join(".") || "(root)"}: ${first.message}` : "unknown";
    super(`unexpected response from ${url} (${issues.length} issue(s); ${where})`, {
      status,
      url,
      payload,
    });
    this.name = "ApiSchemaError";
    this.issues = issues;
  }
}

export type QueryValue = string | number | boolean | null | undefined | readonly (string | number)[];
export type Query = Record<string, QueryValue>;

/**
 * Build a query string; null, undefined and "" are dropped, arrays repeat the
 * key (`symbols=A&symbols=B`), which is what FastAPI reads.
 */
export function queryString(params?: Query | null): string {
  if (!params) return "";
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value) if (item !== "") qs.append(key, String(item));
    } else {
      qs.append(key, String(value));
    }
  }
  const out = qs.toString();
  return out ? `?${out}` : "";
}

export interface RequestOptions {
  query?: Query | null;
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export type ReachabilityListener = (online: boolean, error: ApiError | null) => void;

export class ApiClient {
  readonly base: string;
  readonly timeoutMs: number;
  /** null until the first request settles. */
  online: boolean | null = null;
  /** The failure that last flipped `online` to false. */
  lastError: ApiError | null = null;
  private readonly listeners = new Set<ReachabilityListener>();

  constructor(options: { base?: string; timeoutMs?: number } = {}) {
    this.base = options.base ?? "";
    this.timeoutMs = options.timeoutMs ?? 30_000;
  }

  /** Perform one request and validate the JSON body against `schema`. */
  async request<S extends z.ZodType>(
    method: string,
    path: string,
    schema: S,
    options: RequestOptions = {},
  ): Promise<z.output<S>> {
    const url = this.base + path + queryString(options.query);
    const headers: Record<string, string> = { Accept: "application/json" };
    const init: RequestInit = { method, headers };
    if (options.body !== undefined && options.body !== null) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }

    // Own timeout controller, chained to the caller's signal so an unmounting
    // page and the timeout can both cancel the same request.
    const controller = new AbortController();
    const timeoutMs = options.timeoutMs ?? this.timeoutMs;
    const timer = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : undefined;
    const outer = options.signal;
    if (outer) {
      if (outer.aborted) controller.abort();
      else outer.addEventListener("abort", () => controller.abort(), { once: true });
    }
    init.signal = controller.signal;

    let response: Response;
    try {
      response = await fetch(url, init);
    } catch (err) {
      clearTimeout(timer);
      if (outer?.aborted) {
        const cancelled = new ApiError(`request cancelled: ${method} ${url}`, { url, cause: err });
        cancelled.cancelled = true;
        throw cancelled;
      }
      const reason = err instanceof Error ? err.message : String(err);
      const failure = new ApiError(`cannot reach ${method} ${url} (${reason})`, { url, cause: err });
      this.setReachable(false, failure);
      throw failure;
    }
    clearTimeout(timer);

    // The server answered, so it is reachable even at 500.
    this.setReachable(true, null);
    const payload = await parseBody(response, url);
    if (!response.ok) {
      throw new ApiError(`${method} ${url} → ${response.status}${describeDetail(payload, response)}`, {
        status: response.status,
        url,
        payload,
      });
    }
    const parsed = schema.safeParse(payload);
    if (!parsed.success) throw new ApiSchemaError(url, response.status, payload, parsed.error.issues);
    return parsed.data;
  }

  get<S extends z.ZodType>(path: string, schema: S, query?: Query | null, signal?: AbortSignal) {
    return this.request("GET", path, schema, { query, signal });
  }

  post<S extends z.ZodType>(path: string, schema: S, body?: unknown, signal?: AbortSignal) {
    return this.request("POST", path, schema, { body: body ?? {}, signal });
  }

  del<S extends z.ZodType>(path: string, schema: S, signal?: AbortSignal) {
    return this.request("DELETE", path, schema, { signal });
  }

  /** Run a call whose route or entity may not exist; null on 404/501. */
  async optional<T>(promise: Promise<T>): Promise<T | null> {
    try {
      return await promise;
    } catch (err) {
      if (err instanceof ApiError && err.missing) return null;
      throw err;
    }
  }

  /** Observe whether the REST API is answering. Returns an unsubscribe. */
  onReachability(listener: ReachabilityListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private setReachable(online: boolean, error: ApiError | null): void {
    const changed = this.online !== online;
    this.online = online;
    this.lastError = online ? null : (error ?? this.lastError);
    if (!changed) return;
    for (const listener of this.listeners) {
      try {
        listener(online, this.lastError);
      } catch (err) {
        console.error("api reachability listener failed", err);
      }
    }
  }
}

async function parseBody(response: Response, url: string): Promise<unknown> {
  if (response.status === 204) return null;
  const text = await response.text();
  if (!text) return null;
  if (!(response.headers.get("content-type") ?? "").includes("json")) return text;
  try {
    return JSON.parse(text) as unknown;
  } catch (err) {
    throw new ApiError(`malformed JSON from ${url}`, { status: response.status, url, cause: err });
  }
}

function describeDetail(payload: unknown, response: Response): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    return ` ${typeof detail === "string" ? detail : JSON.stringify(detail)}`;
  }
  const text = typeof payload === "string" ? payload : response.statusText;
  return text ? ` ${text}` : "";
}
