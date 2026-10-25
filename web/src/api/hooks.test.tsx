import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ToastProvider } from "../shell/ToastProvider";
import { ApiProvider } from "./ApiProvider";
import { ApiClient, ApiError } from "./client";
import { useReachability } from "./context";
import { makeApi } from "./endpoints";
import { keys, useAddWatchSymbol, useCompany, useInbox, useWatchlists } from "./hooks";

const fixture = (name: string): unknown =>
  JSON.parse(readFileSync(join(import.meta.dirname, "__fixtures__", `${name}.json`), "utf8"));
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>();
beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function setup() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const api = makeApi(new ApiClient());
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <ApiProvider api={api}>
        <ToastProvider>{children}</ToastProvider>
      </ApiProvider>
    </QueryClientProvider>
  );
  return { queryClient, wrapper };
}

it("loads a research payload per symbol", async () => {
  fetchMock.mockResolvedValue(json(200, fixture("research.company")));
  const { wrapper, queryClient } = setup();
  const { result } = renderHook(() => useCompany("aapl"), { wrapper });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data?.profile.name).toBe("Apple Inc.");
  expect(queryClient.getQueryData(keys.research("AAPL", "company"))).toBeDefined();
});

it("surfaces an unknown ticker as a missing ApiError", async () => {
  fetchMock.mockResolvedValue(json(404, { detail: { error: "not_found", entity: "symbol", detail: "ZZZZQ" } }));
  const { wrapper } = setup();
  const { result } = renderHook(() => useCompany("ZZZZQ"), { wrapper });
  await waitFor(() => expect(result.current.isError).toBe(true));
  expect(result.current.error).toBeInstanceOf(ApiError);
  expect((result.current.error as ApiError).missing).toBe(true);
});

it("reads a missing agents route as null, not as an error", async () => {
  fetchMock.mockResolvedValue(json(404, { detail: "Not Found" }));
  const { wrapper } = setup();
  const { result } = renderHook(() => useInbox(), { wrapper });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toBeNull();
});

it("writes a changed watchlist back into the list", async () => {
  const list = fixture("watchlists.list") as { watchlists: { id: string; symbols: string[] }[] };
  const first = list.watchlists[0];
  if (!first) throw new Error("fixture has no watchlist");
  const changed = { ...first, symbols: [...first.symbols, "TSLA"], symbol_count: first.symbols.length + 1 };
  fetchMock.mockImplementation(async (_url, init) =>
    init?.method === "POST" ? json(200, changed) : json(200, list),
  );
  const { wrapper } = setup();
  const { result } = renderHook(() => ({ list: useWatchlists(), add: useAddWatchSymbol() }), { wrapper });
  await waitFor(() => expect(result.current.list.isSuccess).toBe(true));

  // The refetch after the mutation must agree with the server.
  fetchMock.mockImplementation(async (_url, init) =>
    json(200, init?.method === "POST" ? changed : { ...list, watchlists: [changed] }),
  );
  await act(() => result.current.add.mutateAsync({ id: first.id, symbol: "TSLA" }));
  const post = fetchMock.mock.calls.find(([, i]) => i?.method === "POST");
  const [url, init] = post ?? [];
  expect(url).toBe(`/api/watchlists/${first.id}/symbols`);
  expect(init?.body).toBe('{"symbol":"TSLA"}');
  await waitFor(() => expect(result.current.list.data?.watchlists[0]?.symbols).toContain("TSLA"));
});

it("tracks reachability for the ENGINE indicator", async () => {
  fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
  const { wrapper } = setup();
  const { result } = renderHook(() => ({ online: useReachability(), inbox: useInbox() }), { wrapper });
  expect(result.current.online).toBeNull();
  await waitFor(() => expect(result.current.online).toBe(false));
});
