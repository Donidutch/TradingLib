/* The identity head: any ticker, name, price (live when quoted), market
 * cap, next earnings and data freshness. */
import { useState, type FormEvent } from "react";
import { useCompany } from "../../api/hooks";
import type { TapeQuote } from "../../api/schemas/stream";
import { etDate, metricText, usd } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Button } from "../../ui/primitives/button";
import { freshnessSummary, reason } from "./rules";

export function Head({ symbol, live, onSubmit }: { symbol: string; live: TapeQuote | undefined; onSubmit: (symbol: string) => void }) {
  const company = useCompany(symbol);
  const [draft, setDraft] = useState(symbol);
  const data = company.data;
  const quote = live?.price != null ? live : data?.quote;
  const failed = company.isError;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const next = draft.trim().toUpperCase();
    if (next) onSubmit(next);
  };

  const name = failed ? `${symbol} could not be loaded` : data ? (data.profile.name || symbol).toUpperCase() : "Loading…";
  const meta = failed
    ? reason(company.error)
    : data
      ? data.profile.meta_line || [data.profile.exchange, data.profile.sector, data.profile.region].filter(Boolean).join(" · ") || "—"
      : "";
  const cap = metricText(data?.metrics.market_cap);
  const next = data?.next_earnings;
  const fresh = freshnessSummary(failed ? undefined : data?.freshness);

  return (
    <div className="grid grid-cols-[1fr_auto] items-center rounded-[5px] border border-border-subtle bg-surface-3 px-3.5 py-[13px]">
      <form className="flex items-center gap-3" onSubmit={submit}>
        <input
          aria-label="Research ticker"
          autoComplete="off"
          spellCheck={false}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          className="h-[35px] w-[92px] rounded-[var(--ds-radius-ctrl)] border border-border bg-inset px-2 text-[18px] uppercase text-text-strong"
        />
        <Button type="submit" size="sm">
          Load
        </Button>
        <div className="text-[10px]">
          <h1 className="font-medium text-text-strong">{name}</h1>
          <small className={cn("mt-1 block text-[10px]", failed ? "text-down" : "text-text-muted")}>{meta}</small>
        </div>
        <span
          aria-label="Price"
          className={cn("text-[20px]", quote?.change_pct != null && quote.change_pct < 0 ? "text-down" : "text-up")}
          title={
            !quote || failed
              ? undefined
              : live?.price != null
                ? `live quote ${live.ts ?? ""} · ${live.source} (${live.delayed ? "delayed" : "realtime"})`
                : `quote ${quote.ts ?? ""} · ${quote.source}`
          }
        >
          {failed || quote?.price == null ? "—" : usd(quote.price, { decimals: 2 })}
        </span>
      </form>
      <dl className="flex gap-[22px]">
        {(
          [
            ["MARKET CAP", failed ? "—" : cap.text, failed ? undefined : cap.title, null],
            [
              "NEXT EARNINGS",
              next ? `${next.days_away} ${next.days_away === 1 ? "DAY" : "DAYS"}` : "—",
              next ? `${etDate(next.date)} · ${next.basis || next.source}` : data ? "the vendor published no upcoming reporting date" : undefined,
              null,
            ],
            ["DATA FRESHNESS", fresh.text, fresh.title, fresh.tone],
          ] as const
        ).map(([label, value, title, tone]) => (
          <div key={label}>
            <dt className="mb-[5px] text-[9px] text-text-muted">{label}</dt>
            <dd className={cn("font-bold", tone === "up" && "text-up", tone === "warn" && "text-warn")} title={title}>
              {value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
