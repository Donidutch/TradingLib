import type { Status } from "../api/schemas/status";
import { fixture } from "../test/render-app";
import { dataIndicator, engineIndicator, type Health } from "./health";

const base = fixture<Status>("status");
const healthy: Status = { ...base, engine_health: "healthy", degraded_subsystems: [], data_live: true };

function health(patch: Partial<Health>): Health {
  return {
    status: healthy,
    stream: { state: "open", lastError: null },
    apiOnline: true,
    apiError: null,
    ...patch,
  };
}

describe("engineIndicator", () => {
  it("is healthy only when the engine, socket and REST all are", () => {
    expect(engineIndicator(health({}))).toMatchObject({ text: "● ENGINE HEALTHY", tone: "ok" });
  });

  it("names the degraded subsystems", () => {
    const status = { ...healthy, engine_health: "degraded", degraded_subsystems: ["ingest_scheduler"] };
    expect(engineIndicator(health({ status }))).toEqual({
      text: "● ENGINE DEGRADED",
      tone: "warn",
      title: "ingest_scheduler",
    });
  });

  it("reports the feed state, and why, while the socket is down", () => {
    const stream = { state: "reconnecting" as const, lastError: "socket closed (code 1006)" };
    expect(engineIndicator(health({ stream }))).toEqual({
      text: "● ENGINE HEALTHY · FEED RECONNECTING",
      tone: "warn",
      title: "socket closed (code 1006)",
    });
  });

  it("says unreachable when REST fails, whatever the last status said", () => {
    expect(engineIndicator(health({ apiOnline: false, apiError: "cannot reach GET /api/status" }))).toEqual({
      text: "● ENGINE UNREACHABLE",
      tone: "bad",
      title: "cannot reach GET /api/status",
    });
  });

  it("waits, rather than guessing, before the first status", () => {
    expect(engineIndicator(health({ status: undefined, apiOnline: null }))).toMatchObject({
      text: "● ENGINE …",
      tone: "dim",
    });
  });
});

describe("dataIndicator", () => {
  it.each([
    [{}, "● DATA LIVE", "ok"],
    [{ status: { ...healthy, data_live: false } }, "● DATA IDLE", "warn"],
    [{ stream: { state: "reconnecting" as const, lastError: null } }, "● LIVE FEED LOST", "bad"],
    [{ apiOnline: false }, "● BACKEND UNREACHABLE", "bad"],
    [{ status: undefined }, "● DATA —", "dim"],
  ] as [Partial<Health>, string, string][])("%o → %s", (patch, text, tone) => {
    expect(dataIndicator(health(patch))).toMatchObject({ text, tone });
  });
});
