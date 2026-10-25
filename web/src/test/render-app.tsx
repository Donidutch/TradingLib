/* Render the whole app against fixture responses and a fake socket. */
import { render, screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { App } from "../App";
import { ApiClient } from "../api/client";
import { makeApi } from "../api/endpoints";
import { LiveStream } from "../live/stream";
import { makeRouter } from "../router";
import { FakeSocket, fakeSocketFactory } from "./fake-socket";

export function fixture<T = Record<string, unknown>>(name: string): T {
  const path = join(import.meta.dirname, "..", "api", "__fixtures__", `${name}.json`);
  return JSON.parse(readFileSync(path, "utf8")) as T;
}

export const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** GET path (without query) → fixture name, for the shell's own requests. */
const ROUTES: Record<string, string> = {
  "/api/status": "status",
  "/api/universes": "universes.list",
  "/api/universes/active": "universes.active",
  "/api/watchlists": "watchlists.list",
  "/api/search": "search",
  "/api/lab/backtests": "lab.backtests",
};

/**
 * A handler answering `path` (exact) or `path*` (prefix) with a body, a
 * Response, or a function of the URL. Exact paths win, then the longest prefix.
 */
export function serve(routes: Record<string, unknown>): Handler {
  const patterns = Object.keys(routes).sort((a, b) => {
    const wild = Number(a.endsWith("*")) - Number(b.endsWith("*"));
    return wild || b.length - a.length;
  });
  return (url) => {
    for (const pattern of patterns) {
      const hit = pattern.endsWith("*") ? url.pathname.startsWith(pattern.slice(0, -1)) : url.pathname === pattern;
      if (!hit) continue;
      const body = routes[pattern];
      if (body instanceof Response) return body.clone();
      if (typeof body === "function") return (body as (u: URL) => Response)(url);
      return json(200, body);
    }
    return undefined;
  };
}

export type Handler = (url: URL, init: RequestInit | undefined) => Response | Promise<Response> | undefined;

/** Render and wait until the router has mounted the shell. */
export async function renderApp(path = "/app/", handler?: Handler) {
  FakeSocket.instances = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, "http://localhost");
    const custom = await handler?.(url, init);
    if (custom) return custom;
    const name = ROUTES[url.pathname];
    return name ? json(200, fixture(name)) : json(404, { detail: "Not Found" });
  });
  vi.stubGlobal("fetch", fetchMock);
  const router = makeRouter(createMemoryHistory({ initialEntries: [path] }));
  const stream = new LiveStream({ url: "ws://test/ws", socketFactory: fakeSocketFactory });
  const api = makeApi(new ApiClient());
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<App router={router} queryClient={queryClient} api={api} stream={stream} />);
  await screen.findByRole("navigation", { name: "Workspaces" });
  return { ...view, router, stream, api, queryClient, fetchMock, socket: () => FakeSocket.latest() };
}
