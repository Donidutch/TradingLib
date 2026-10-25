/* The Source Catalog, Data Ingestion and Data Quality rules, against the
 * cases the old page handled (ui/js/pages/data.js). */
import { fixture } from "../../test/render-app";
import type { Source, Subscription } from "../../api/schemas/ingest";
import type { QualitySeries, Sla } from "../../api/schemas/quality";
import {
  barToken,
  checkRows,
  EMPTY_FORM,
  formPayload,
  frequencyNote,
  humanInterval,
  pollNote,
  protocolAbbrev,
  qualityNote,
  sentence,
  sourceToForm,
  sourceToState,
  statusPill,
  toSeconds,
} from "./ingestRules";

const sources = fixture<{ sources: Source[] }>("ingest.sources").sources;
const byId = (id: string) => {
  const source = sources.find((s) => s.id === id);
  if (!source) throw new Error(id);
  return source;
};
const fred = byId("fred");
const sub = (over: Partial<Subscription>): Subscription => ({
  id: "s1",
  source_id: "fred",
  channel: "series",
  scope: [],
  active: true,
  feed_mode: "scheduled",
  preserve_pit: false,
  granularity: "",
  ...over,
});

describe("intervals", () => {
  it("parses interval tokens to seconds", () => {
    expect(toSeconds("15s")).toBe(15);
    expect(toSeconds(" 5m ")).toBe(300);
    expect(toSeconds("4H")).toBe(14400);
    expect(toSeconds("1d")).toBe(86400);
    expect(toSeconds("1.5h")).toBe(5400);
    expect(toSeconds("daily")).toBeNull();
    expect(toSeconds(null)).toBeNull();
  });

  it("maps resolutions to bar tokens, and reporting cadences to none", () => {
    expect(barToken("1wk")).toBe("7d");
    expect(barToken("1mo")).toBe("30d");
    expect(barToken("5m")).toBe("5m");
    expect(barToken("quarterly")).toBe("");
  });

  it("names a span in its largest whole unit", () => {
    expect(humanInterval(null)).toBe("—");
    expect(humanInterval(45)).toBe("45s");
    expect(humanInterval(300)).toBe("5m");
    expect(humanInterval(7200)).toBe("2h");
    expect(humanInterval(86400 * 7)).toBe("7d");
  });

  it("makes an API phrase a sentence", () => {
    expect(sentence("polled hourly")).toBe("Polled hourly.");
    expect(sentence("done!")).toBe("Done!");
    expect(sentence(null)).toBe("");
  });
});

describe("pollNote", () => {
  it("explains an unparseable value, and the current schedule when nothing is typed", () => {
    expect(pollNote(fred, "soon", null)).toEqual({ text: "Unrecognised interval — use a value like 45s, 2m, 4h or 1d.", tone: "warn" });
    const streaming = byId("alpaca");
    expect(pollNote(streaming, streaming.schedule, null).text).toBe("Current schedule: Streaming. Pick an interval to switch this source to a periodic poll.");
  });

  it("states the request cost, then whether the poll agrees with the bar", () => {
    expect(pollNote(fred, "1h", null)).toEqual({ text: "≈ 24 connector runs/day. Changing this rewrites the source's schedule and re-registers it with the ingest scheduler.", tone: null });
    expect(pollNote(fred, "1d", "1d")).toEqual({ text: "Aligned with the 1d bar interval. ≈ 1 connector run/day.", tone: "up" });
    expect(pollNote(fred, "1h", "1d")).toEqual({ text: "Polling faster than the 1d bar — 24× of these runs return the same unchanged bar. ≈ 24 connector runs/day.", tone: "warn" });
    expect(pollNote(fred, "1d", "1h").text).toBe("Polling slower than the 1h bar — up to 23 bar(s) can be missed between runs; backfill closes the gap. ≈ 1 connector run/day.");
  });
});

describe("frequencyNote", () => {
  const reaching = { ...fred, granularity_reaches_fetch: true };

  it("quotes the API's own verdict when a subscription already asks for this resolution", () => {
    const decided = sub({ granularity: "1d", granularity_applied: true, granularity_detail: "the scheduler fetches 1d bars" });
    expect(frequencyNote(fred, [decided], "1d")).toEqual({ text: "The scheduler fetches 1d bars.", applies: true });
  });

  it("predicts the verdict otherwise", () => {
    expect(frequencyNote({ ...fred, granularity_reaches_fetch: false }, [], "1d")).toEqual({
      text: "Advisory: 1d would be stored on the subscription, but FRED Macro's fetch path has no bar-size parameter to apply it to.",
      applies: false,
    });
    expect(frequencyNote(reaching, [], "1d")).toEqual({ text: "The scheduler will fetch 1d bars for this source while the subscription is active.", applies: true });
    expect(frequencyNote(reaching, [sub({ granularity: "1wk" }), sub({ id: "s2", granularity: "1mo", active: false })], "1d")).toEqual({
      text: "Subscribing at 1d would disagree with this source's active 1wk subscription, and the scheduler imposes none of them — FRED Macro would keep its configured resolution.",
      applies: false,
    });
  });
});

describe("sources", () => {
  it("labels status and protocol as the old page did", () => {
    expect(statusPill(fred.state)).toEqual({ text: "Connected", tone: "up" });
    expect(statusPill({ ...fred.state, status: "not_configured" }).text).toBe("Credentials required");
    expect(statusPill({ ...fred.state, status: "weird", status_label: "Weird" })).toEqual({ text: "Weird", tone: null });
    expect(protocolAbbrev("polling")).toEqual({ text: "POLL", tone: "warn" });
    expect(protocolAbbrev("websocket")).toEqual({ text: "WS", tone: "up" });
    expect(protocolAbbrev("database").text).toBe("FILE/DB");
    expect(protocolAbbrev("rest")).toEqual({ text: "REST", tone: "accent" });
  });

  it("re-posts a source with the retry mode token, not the policy object", () => {
    const body = sourceToForm(fred, { schedule: "15m" });
    expect(body.schedule).toBe("15m");
    expect(body.retry_policy).toBe(fred.retry_policy.mode);
    expect(body.id).toBe("fred");
  });
});

describe("the ingestion form", () => {
  it("posts the fields SourceConfig.from_form reads, with credentials only when typed", () => {
    const payload = formPayload({ ...EMPTY_FORM, name: " Feed ", protocol: "File", endpoint: "/x/*.csv", auth: "None", scope: "Nasdaq-100" }, null);
    expect(payload).toEqual({
      sourceName: "Feed",
      sourceDomain: "Prices & quotes",
      sourceProtocol: "file",
      sourceEndpoint: "/x/*.csv",
      sourceAuth: "None",
      sourceFormat: "JSON",
      sourceFrequency: "Streaming",
      sourceScope: "Nasdaq-100",
      sourceTimezone: "UTC",
      sourceRetries: "Exponential backoff",
      backfillEnabled: true,
      backfillRange: "All available history",
      backfillBatch: "Automatic",
      advancedConfig: null,
    });
  });

  it("names credentials by the auth kind", () => {
    expect(formPayload({ ...EMPTY_FORM, auth: "Basic auth", keyName: "me", keyValue: "pw" }, null)).toMatchObject({ auth_username: "me", auth_password: "pw" });
    expect(formPayload({ ...EMPTY_FORM, auth: "OAuth 2.0", keyName: "id", keyValue: "s" }, null)).toMatchObject({ auth_client_id: "id", auth_client_secret: "s" });
    const key = formPayload({ ...EMPTY_FORM, auth: "API key", keyName: "X-KEY", keyValue: "" }, null);
    expect(key.auth_key_name).toBe("X-KEY");
    expect(key).not.toHaveProperty("auth_key_value");
  });

  it("adds cron, custom symbols, a start date and the edited id only when they apply", () => {
    const payload = formPayload(
      { ...EMPTY_FORM, frequency: "Custom cron…", cron: " */5 * * * * ", scope: "Custom symbols…", symbols: "AAPL, MSFT", backfillRange: "Custom start date…", backfillStart: "2020-01-02" },
      "mine",
    );
    expect(payload).toMatchObject({ id: "mine", cron: "*/5 * * * *", scope: "AAPL, MSFT", backfillStart: "2020-01-02" });
    expect(formPayload({ ...EMPTY_FORM, backfillEnabled: false, backfillRange: "Custom start date…", backfillStart: "2020-01-02" }, null)).not.toHaveProperty("backfillStart");
  });

  it("reads a source back into the form for editing", () => {
    const state = sourceToState(fred);
    expect(state).toMatchObject({ name: "FRED Macro", domain: fred.data_domain_label, protocol: "Polling", frequency: fred.schedule_detail.label, keyValue: "" });
  });
});

describe("the check suite", () => {
  const sla = (rows: Partial<Sla["sla"][number]>[]): Sla => ({
    sla: rows.map((r) => ({ source_id: "fred", name: "FRED", schedule: "Hourly", state: "ok", age_seconds: null, detail: "", ...r })),
    count: rows.length,
  });
  const series = (checks: { check: string; passed: boolean; severity: string }[]): QualitySeries => ({
    datasets: [{ dataset: "bars:A:1d", label: "A 1d", checks: checks.map((c) => ({ ...c, detail: `${c.check} detail` })) }],
    count: 1,
  });

  it("says NOT RUN for a check nothing measured, and a clean word for one that passed", () => {
    const rows = checkRows(series([{ check: "duplicates", passed: true, severity: "info" }, { check: "schema_drift", passed: true, severity: "info" }, { check: "completeness", passed: true, severity: "info" }]), sla([]));
    const named = Object.fromEntries(rows.map((r) => [r.name, r]));
    expect(named["Dupe rows"]).toMatchObject({ result: "0", tone: "up", scope: "all ingests · 1 checked", action: null });
    expect(named["Schema drift"]?.result).toBe("NONE");
    expect(named["Gap scan"]?.result).toBe("CLEAN");
    expect(named["Stale tickers"]).toMatchObject({ result: "NOT RUN", tone: null, scope: "not checked yet" });
    expect(rows.at(-1)).toMatchObject({ name: "SLA", result: "NO SOURCES" });
  });

  it("counts failures, colours by the worst severity, and adds SLA rows", () => {
    const rows = checkRows(
      series([{ check: "outliers", passed: false, severity: "error" }, { check: "staleness", passed: false, severity: "warn" }]),
      sla([{ state: "breached", age_seconds: 13414 }, { source_id: "trends", state: "pending" }]),
    );
    const named = Object.fromEntries(rows.map((r) => [r.name, r]));
    expect(named["Outlier detection"]).toMatchObject({ result: "1 FLAG", tone: "down", action: "review", title: "A 1d: outliers detail" });
    expect(named["Stale tickers"]).toMatchObject({ result: "1 STALE", tone: "warn", action: "list" });
    expect(named["SLA · fred"]).toMatchObject({ result: "BREACHED 3.7H", tone: "down", action: "ticket", scope: "Hourly schedule" });
    expect(named["SLA · trends"]).toMatchObject({ result: "PENDING", tone: "warn" });
    expect(checkRows(series([]), sla([{}])).at(-1)).toMatchObject({ result: "ALL WITHIN CADENCE", scope: "1 enabled sources" });
  });
});

describe("qualityNote", () => {
  const counts = { open: 0, quarantined: 0, accepted: 0, escalated: 0, awaiting_review: 0 };
  it("leads with quarantine, then escalation, then open incidents", () => {
    expect(qualityNote({ last_run: "x" }, { ...counts, quarantined: 2, escalated: 1 })).toEqual({
      text: "⚠ 2 quarantined observation(s) are excluded from every store read, so no feature, screen or backtest sees them until the decision is undone. 1 point(s) are escalated for human review: their raw values are still being used, which is what escalation means.",
      warn: true,
    });
    expect(qualityNote({ last_run: "x" }, { ...counts, escalated: 1 }).text).toMatch(/^⚠ Nothing is quarantined\. 1 point/);
    expect(qualityNote({ last_run: "x", open_incidents: 3 }, counts).text).toBe("⚠ 3 open incident(s). Flagged observations stay in the data until somebody quarantines them in the queue.");
  });
  it("is calm when nothing is open, and honest when nothing ran", () => {
    expect(qualityNote({ last_run: "x", datasets: 3, datasets_in_store: 4 }, counts)).toEqual({ text: "✓ No open incidents. 3 dataset(s) scored on the last run, 4 present in the store.", warn: false });
    expect(qualityNote({ last_run: null }, undefined).text).toBe("No checks have run yet, so nothing is known about the stored data's quality.");
  });
});
