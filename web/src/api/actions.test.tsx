import { act, renderHook, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { ToastProvider } from "../shell/ToastProvider";
import { useAction } from "./actions";
import { ApiError } from "./client";
import { errorReason } from "./errors";

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ToastProvider>{children}</ToastProvider>
    </QueryClientProvider>
  );
  // The toast region lives in the provider; render one so it is on screen.
  render(<div />, { wrapper });
  return { client, wrapper };
}

describe("useAction", () => {
  it("runs, applies, refetches what changed and confirms", async () => {
    const { client, wrapper } = setup();
    let reads = 0;
    const { result } = renderHook(
      () => ({
        list: useQuery({ queryKey: ["things"], queryFn: async () => ++reads }),
        add: useAction({
          run: async (name: string) => ({ name }),
          apply: (c, created) => c.setQueryData(["last"], created.name),
          invalidate: () => [["things"]],
          done: (created) => `${created.name} added`,
          failed: (name) => `Could not add ${name}`,
        }),
      }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.list.data).toBe(1));
    await act(() => result.current.add.mutateAsync("NVDA"));
    expect(client.getQueryData(["last"])).toBe("NVDA");
    await waitFor(() => expect(result.current.list.data).toBe(2));
    expect(screen.getAllByRole("status").some((s) => s.textContent === "NVDA added")).toBe(true);
  });

  it("shows the server's reason in a toast and on the action", async () => {
    const { wrapper } = setup();
    const failure = new ApiError("POST /x → 409", { status: 409, payload: { detail: "already exists" } });
    const run = vi.fn<(name: string) => Promise<never>>().mockRejectedValue(failure);
    const { result } = renderHook(
      () => useAction({ run, failed: (name) => `Could not add ${name}` }),
      { wrapper },
    );
    act(() => result.current.mutate("NVDA"));
    await waitFor(() => expect(result.current.error).toBe(failure));
    expect(screen.getAllByRole("status").some((s) => s.textContent === "Could not add NVDA: already exists")).toBe(true);
  });
});

describe("errorReason", () => {
  it.each([
    [new ApiError("m", { payload: { detail: "plain" } }), "plain"],
    [new ApiError("m", { payload: { detail: { error: "not_found", detail: "structured" } } }), "structured"],
    [new ApiError("m", { payload: { detail: [{ msg: "a" }, { msg: "b" }] } }), "a; b"],
    [new ApiError("message", { payload: "<html>" }), "message"],
    [new Error("plain error"), "plain error"],
    ["text", "text"],
  ])("%#", (err, text) => {
    expect(errorReason(err)).toBe(text);
  });
});
