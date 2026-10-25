import type { Status } from "../../api/schemas/status";
import { ago, int, pct, statusText } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Panel } from "../../ui/primitives/card";

interface Cell {
  text: string;
  tone?: "up" | "warn";
  title?: string;
}

function fundamentals(status: Status): Cell {
  const coverage = status.fundamentals_coverage;
  if (!coverage) {
    return {
      text: "MEASURING",
      title:
        "Coverage across the active universe is measured in the background; the first result appears once every member has been resolved.",
    };
  }
  if (!coverage.total) return { text: "NO UNIVERSE", tone: "warn", title: "No active universe has members to measure." };
  const extra = coverage.missing_total - coverage.missing.length;
  return {
    text: `${coverage.covered} / ${coverage.total}`,
    tone: coverage.covered < coverage.total ? "warn" : "up",
    title:
      `${coverage.universe_name || coverage.universe_id}: ${pct(coverage.pct)} of members have a market cap and trailing revenue` +
      (coverage.missing.length
        ? `. Missing: ${coverage.missing.join(", ")}${extra > 0 ? ` (+${extra} more)` : ""}`
        : "") +
      `. Measured ${ago(coverage.measured_at)} ago.`,
  };
}

/** Panel 4: engine, price feed, fundamentals coverage and delayed series. */
export function HealthPanel({ status, live }: { status: Status | undefined; live: boolean }) {
  const rows: [string, Cell][] = status
    ? [
        [
          "Research engine",
          {
            text: statusText(status.engine_health),
            tone: status.engine_health === "healthy" ? "up" : "warn",
            title: status.engine_detail ?? "",
          },
        ],
        [
          "Market prices",
          {
            text: status.market_stream.running ? "LIVE" : "DELAYED",
            tone: status.market_stream.running ? "up" : "warn",
            title:
              status.market_stream.detail ??
              "quotes are polled from the delayed vendor feed; no push stream is configured",
          },
        ],
        ["Fundamentals", fundamentals(status)],
        [
          "Delayed series",
          { text: status.delayed_series ? int(status.delayed_series) : "0", tone: status.delayed_series ? "warn" : "up" },
        ],
      ]
    : ["Research engine", "Market prices", "Fundamentals", "Delayed series"].map((label) => [label, { text: "—" }]);

  return (
    <Panel
      title="System & data health"
      accent={false}
      bodyClassName="p-0"
      actions={<Badge tone={live ? "up" : "down"}>{live ? "live" : "offline"}</Badge>}
    >
      <dl>
        {rows.map(([label, cell]) => (
          <div key={label} className="flex justify-between border-b border-border px-2.5 py-2 text-[10px] last:border-b-0">
            <dt>{label}</dt>
            <dd
              className={cn("font-bold", cell.tone === "up" && "text-up", cell.tone === "warn" && "text-warn")}
              title={cell.title}
            >
              {cell.text}
            </dd>
          </div>
        ))}
      </dl>
    </Panel>
  );
}
