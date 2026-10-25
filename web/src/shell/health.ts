/* What the ENGINE indicator and the datastrip's DATA label say, as pure
 * functions of the status payload, the socket and REST reachability, so
 * the degraded and recovered states are tested without a server. */
import type { Status } from "../api/schemas/status";
import type { StreamSnapshot } from "../live/stream";

export type Tone = "ok" | "warn" | "bad" | "dim";

export interface Indicator {
  text: string;
  tone: Tone;
  title: string;
}

export interface Health {
  status: Status | undefined;
  stream: Pick<StreamSnapshot, "state" | "lastError">;
  /** ApiClient.online: null before the first request settles. */
  apiOnline: boolean | null;
  apiError: string | null;
}

/** The topbar ENGINE pill: engine health, socket and REST reachability combined. */
export function engineIndicator({ status, stream, apiOnline, apiError }: Health): Indicator {
  if (apiOnline === false) {
    return { text: "● ENGINE UNREACHABLE", tone: "bad", title: apiError ?? "the API is not answering" };
  }
  if (!status) return { text: "● ENGINE …", tone: "dim", title: "waiting for /api/status" };
  if (status.engine_health !== "healthy") {
    return {
      text: "● ENGINE DEGRADED",
      tone: "warn",
      title:
        status.degraded_subsystems.join(", ") || status.engine_detail || "one or more subsystems are down",
    };
  }
  if (stream.state !== "open") {
    return {
      text: `● ENGINE HEALTHY · FEED ${stream.state.toUpperCase()}`,
      tone: "warn",
      title: stream.lastError ?? "the live feed is not connected",
    };
  }
  return {
    text: "● ENGINE HEALTHY",
    tone: "ok",
    title: `${status.engine_detail ?? "all subsystems up"} · ws ${status.ws_connections} connection(s)`,
  };
}

/** The datastrip's first cell: is data flowing, and if not, which link broke. */
export function dataIndicator({ status, stream, apiOnline }: Health): Indicator {
  if (!status) return { text: "● DATA —", tone: "dim", title: "waiting for /api/status" };
  if (apiOnline === false) return { text: "● BACKEND UNREACHABLE", tone: "bad", title: "the API is not answering" };
  if (stream.state !== "open") {
    return { text: "● LIVE FEED LOST", tone: "bad", title: stream.lastError ?? "the live feed is not connected" };
  }
  if (status.data_live) return { text: "● DATA LIVE", tone: "ok", title: "market data is streaming" };
  return { text: "● DATA IDLE", tone: "warn", title: "no live market data right now" };
}

export const toneClass: Record<Tone, string> = {
  ok: "text-up",
  warn: "text-warn",
  bad: "text-down",
  dim: "text-text-subtle",
};
