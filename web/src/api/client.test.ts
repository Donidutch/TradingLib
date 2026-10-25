import { z } from "zod";
import { ApiClient, ApiError, ApiSchemaError, queryString } from "./client";

const Body = z.looseObject({ ok: z.boolean() });

function respond(status: number, body: unknown, type = "application/json") {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return new Response(status === 204 ? null : text, { status, headers: { "content-type": type } });
}

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("queryString", () => {
  it("drops empty values and repeats array keys the way FastAPI reads them", () => {
    expect(queryString({ q: "nv", limit: 3, skip: null, blank: "", symbols: ["A", "B"], on: true })).toBe(
      "?q=nv&limit=3&symbols=A&symbols=B&on=true",
    );
    expect(queryString({ none: undefined })).toBe("");
  });
});

describe("ApiClient.request", () => {
  it("returns the parsed, validated body and sends JSON with query", async () => {
    fetchMock.mockResolvedValue(respond(200, { ok: true, extra: 1 }));
    const client = new ApiClient();
    await expect(client.post("/api/x", Body, { a: 1 })).resolves.toEqual({ ok: true, extra: 1 });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/x");
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"a":1}');
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("throws ApiError with status, URL, payload and the server's detail", async () => {
    fetchMock.mockResolvedValue(respond(422, { detail: [{ msg: "bad symbol" }] }));
    const failure = await new ApiClient().get("/api/x", Body, { q: "a" }).catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(ApiError);
    const error = failure as ApiError;
    expect(error.status).toBe(422);
    expect(error.url).toBe("/api/x?q=a");
    expect(error.payload).toEqual({ detail: [{ msg: "bad symbol" }] });
    expect(error.message).toContain("bad symbol");
    expect(error.missing).toBe(false);
  });

  it("rejects a 2xx body that does not match the schema, with the issues", async () => {
    fetchMock.mockResolvedValue(respond(200, { ok: "yes" }));
    const failure = await new ApiClient().get("/api/x", Body).catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(ApiSchemaError);
    expect((failure as ApiSchemaError).issues[0]?.path).toEqual(["ok"]);
    expect((failure as ApiSchemaError).message).toContain("ok:");
  });

  it("reports malformed JSON as an error, not as text", async () => {
    fetchMock.mockResolvedValue(respond(200, "{not json"));
    await expect(new ApiClient().get("/api/x", Body)).rejects.toThrow(/malformed JSON/);
  });

  it("passes a non-JSON body to the schema as text", async () => {
    fetchMock.mockResolvedValue(respond(200, "plain", "text/plain"));
    await expect(new ApiClient().get("/api/x", z.string())).resolves.toBe("plain");
  });
});

describe("optional()", () => {
  it.each([404, 501])("resolves to null when the server answers %i", async (status) => {
    fetchMock.mockResolvedValue(respond(status, { detail: "Not Found" }));
    const client = new ApiClient();
    await expect(client.optional(client.get("/api/x", Body))).resolves.toBeNull();
  });

  it("still throws on a genuine failure", async () => {
    fetchMock.mockResolvedValue(respond(500, { detail: "boom" }));
    const client = new ApiClient();
    await expect(client.optional(client.get("/api/x", Body))).rejects.toMatchObject({ status: 500 });
  });

  it("still throws on a schema mismatch", async () => {
    fetchMock.mockResolvedValue(respond(200, {}));
    const client = new ApiClient();
    await expect(client.optional(client.get("/api/x", Body))).rejects.toBeInstanceOf(ApiSchemaError);
  });
});

describe("reachability", () => {
  it("goes offline when the server cannot be reached, and back on its next answer", async () => {
    const client = new ApiClient();
    const seen: (boolean | null)[] = [];
    client.onReachability((online) => seen.push(online));

    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const failure = await client.get("/api/x", Body).catch((e: unknown) => e);
    expect((failure as ApiError).offline).toBe(true);
    expect(client.online).toBe(false);
    expect(client.lastError).toBe(failure);

    fetchMock.mockResolvedValueOnce(respond(500, { detail: "boom" }));
    await client.get("/api/x", Body).catch(() => undefined);
    expect(client.online).toBe(true); // a 500 is still an answer
    expect(seen).toEqual([false, true]);
  });

  it("does not count a cancelled request as an outage", async () => {
    const client = new ApiClient();
    const controller = new AbortController();
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) =>
          init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))),
        ),
    );
    const pending = client.get("/api/x", Body, null, controller.signal).catch((e: unknown) => e);
    controller.abort();
    const failure = (await pending) as ApiError;
    expect(failure.cancelled).toBe(true);
    expect(failure.offline).toBe(false);
    expect(client.online).toBeNull();
  });

  it("times out a request that never answers", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) =>
          init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))),
        ),
    );
    const client = new ApiClient({ timeoutMs: 1000 });
    const pending = client.get("/api/x", Body).catch((e: unknown) => e);
    await vi.advanceTimersByTimeAsync(1000);
    expect(((await pending) as ApiError).offline).toBe(true);
    expect(client.online).toBe(false);
    vi.useRealTimers();
  });
});
