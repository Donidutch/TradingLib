import type { Status } from "../../api/schemas/status";
import { cn } from "../../ui/lib/cn";
import { ago } from "../../ui/format";
import { useNow } from "../../shell/useNow";

/** Panel 1: the datastrip's figures in the reference's own wording. */
export function MarketStrip({ status, live, apiOnline }: { status: Status | undefined; live: boolean; apiOnline: boolean | null }) {
  const now = useNow(10_000);
  const online = apiOnline !== false;
  let state = "● DATA …";
  if (status) {
    if (!online) state = "● BACKEND UNREACHABLE";
    else if (!live) state = "● LIVE FEED LOST";
    else state = status.data_live ? "● DATA LIVE" : "● DATA IDLE";
  }
  const bad = status && (!online || !live || !status.data_live);
  const delayed = status?.delayed_series ?? null;
  return (
    <div
      aria-label="Market strip"
      className="mb-2 flex h-[29px] items-center gap-[18px] overflow-hidden whitespace-nowrap rounded-[5px] border border-border-subtle bg-surface-3 px-2.5 text-[10px] text-text-muted"
    >
      <strong className={cn("font-medium", bad ? "text-down" : status ? "text-up" : "text-text-subtle")}>{state}</strong>
      <span>Research gateway {status?.gateway_ms == null ? "—" : `${Math.round(status.gateway_ms)}ms`}</span>
      <span>
        {status
          ? status.sources_total
            ? `${status.sources_connected} of ${status.sources_total} sources connected`
            : `${status.sources_connected} sources connected`
          : "— sources connected"}
      </span>
      <span>Last ingest {status?.last_ingest ? `${ago(status.last_ingest, now)} ago` : "—"}</span>
      <span className={cn(delayed ? "text-warn" : undefined)}>{delayed ?? "—"} delayed series</span>
    </div>
  );
}
