/* Every figure comes from /api/status and the `status` channel; unknown
 * figures are "—", never invented. */
import type { Status } from "../api/schemas/status";
import { cn } from "../ui/lib/cn";
import { ago, int, usd } from "../ui/format";
import { toneClass, type Indicator } from "./health";
import { useNow } from "./useNow";

export function Datastrip({ status, data }: { status: Status | undefined; data: Indicator }) {
  // "LAST INGEST 4M AGO" ages between frames.
  const now = useNow(10_000);
  const delayed = status?.delayed_series ?? null;
  const spend = status?.spend;
  // Small real amounts stay visible: $0.00055 as "$0.00" would read as zero.
  const usedDecimals = spend && spend.used > 0 && spend.used < 1 ? 4 : 2;

  return (
    <div
      aria-label="Datastrip"
      className="flex gap-5 overflow-x-auto whitespace-nowrap border-b border-border-subtle bg-surface-3 px-4 py-1.5 text-[11px] text-text-subtle"
    >
      <span className={toneClass[data.tone]} title={data.title}>
        {data.text}
      </span>
      <span>
        GATEWAY {status?.gateway_ms == null ? "—" : `${Math.round(status.gateway_ms)}MS`}
      </span>
      <span
        title={
          status
            ? status.sources_total
              ? `${status.sources_connected} of ${status.sources_total} configured sources are connected`
              : "no ingestion source is configured yet"
            : undefined
        }
      >
        {status ? status.sources_connected : "—"} SOURCES CONNECTED
      </span>
      <span>LAST INGEST {status?.last_ingest ? `${ago(status.last_ingest, now).toUpperCase()} AGO` : "—"}</span>
      <span className={cn(delayed && toneClass.warn)}>{delayed ?? "—"} DELAYED SERIES</span>
      <span className="ml-auto">
        TRIALS LOGGED <b className="font-normal text-text-strong">{int(status?.trials_logged)}</b>
      </span>
      <span>
        SPEND{" "}
        {spend ? `${usd(spend.used, { decimals: usedDecimals })} / ${usd(spend.budget, { decimals: 2 })}` : "—"}
      </span>
    </div>
  );
}
