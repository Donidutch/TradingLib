/* Write responses captured on a throwaway server, against the schemas the
 * app parses them with (the reads are covered by endpoints.test.ts). */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { z } from "zod";
import { BackfillStarted, ConnectionTest, SourceProbe, SourceSaved, Subscription } from "./schemas/ingest";
import { ExperimentDetail, ExperimentRun } from "./schemas/lab";
import { Incident, OutlierDecided, QualityRun, TicketOpened } from "./schemas/quality";
import { QueuedBacktest, ScreenRun } from "./schemas/screens";
import { Snapshot, Universe } from "./schemas/universes";
import { Watchlist } from "./schemas/watchlists";

const fixture = (name: string): unknown => JSON.parse(readFileSync(join(import.meta.dirname, "__fixtures__", `${name}.json`), "utf8"));

const cases: [string, z.ZodType][] = [
  ["lab.experiment", ExperimentDetail],
  ["lab.runExperiment", ExperimentRun],
  ["ingest.subscribe", Subscription],
  ["ingest.probe", SourceProbe],
  ["ingest.backfill", BackfillStarted],
  ["ingest.create", SourceSaved],
  ["ingest.update", SourceSaved],
  ["ingest.setEnabled", SourceSaved],
  ["ingest.test", ConnectionTest],
  ["quality.decide", OutlierDecided],
  ["quality.resolve", Incident],
  ["quality.ticket", TicketOpened],
  ["quality.run", QualityRun],
  ["screens.run", ScreenRun],
  ["screens.queueBacktest", QueuedBacktest],
  ["universes.snapshot", Snapshot],
  ["universes.create", Universe],
  ["watchlists.get", Watchlist],
];

it.each(cases)("%s matches its schema", (name, schema) => {
  const parsed = schema.safeParse(fixture(name));
  expect(parsed.success ? [] : parsed.error.issues).toEqual([]);
});
