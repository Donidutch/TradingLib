/* Vitest global setup for `npm run check:api:writes`: an API of its own on a
 * temporary copy of data/, so write checks never touch the real data or the
 * server on 8200. Removed again when the run ends. */
import { spawn, type ChildProcess } from "node:child_process";
import { cpSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { TestProject } from "vitest/node";

const ROOT = resolve(import.meta.dirname, "..", "..", "..");
export const WRITES_PORT = Number(process.env.WRITES_PORT ?? 8299);

declare module "vitest" {
  export interface ProvidedContext {
    writesUrl: string;
  }
}

let server: ChildProcess | undefined;
let scratch: string | undefined;

export default async function setup(project: TestProject) {
  if (WRITES_PORT === 8200) throw new Error("refusing to run write checks on port 8200");
  scratch = mkdtempSync(join(tmpdir(), "tradinglib-writes-"));
  const data = join(scratch, "data");
  if (existsSync(join(ROOT, "data"))) cpSync(join(ROOT, "data"), data, { recursive: true });
  server = spawn("uv", ["run", "python", "-m", "tradinglib.api", "--port", String(WRITES_PORT)], {
    cwd: ROOT,
    env: {
      ...process.env,
      TRADINGLIB_DATA_DIR: data,
      // No tradinglib.toml: a settings write must not land in the real one.
      TRADINGLIB_CONFIG: join(scratch, "absent.toml"),
      TRADINGLIB_INGEST_AUTOSTART: "0",
    },
    stdio: ["ignore", "ignore", "pipe"],
  });
  const url = `http://127.0.0.1:${WRITES_PORT}`;
  const deadline = Date.now() + 90_000;
  for (;;) {
    try {
      if ((await fetch(`${url}/api/health`)).ok) break;
    } catch {
      // not up yet
    }
    if (server.exitCode !== null) throw new Error(`throwaway API exited with ${server.exitCode}`);
    if (Date.now() > deadline) throw new Error("throwaway API did not start within 90s");
    await new Promise((r) => setTimeout(r, 500));
  }
  project.provide("writesUrl", url);
  return async () => {
    server?.kill("SIGTERM");
    await new Promise((r) => setTimeout(r, 500));
    if (scratch) rmSync(scratch, { recursive: true, force: true });
  };
}
