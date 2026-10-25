/* Panel 5: ticker form, price, newest bar and a year of closes. */
import { useState, type FormEvent } from "react";
import { useCompany, usePrice } from "../../api/hooks";
import type { TapeQuote } from "../../api/schemas/stream";
import { compact, etDate, num } from "../../ui/format";
import { AreaChart } from "../../ui/charts/AreaChart";
import { Button } from "../../ui/primitives/button";
import { Card } from "../../ui/primitives/card";

function reason(err: Error | null): string {
  if (!err) return "unknown error";
  return "status" in err && err.status === 404 ? "not found" : err.message;
}

export function QuotePanel({
  symbol,
  live,
  onSubmit,
}: {
  symbol: string;
  live: TapeQuote | undefined;
  onSubmit: (symbol: string) => void;
}) {
  const company = useCompany(symbol);
  const price = usePrice(symbol, "1y");
  const [draft, setDraft] = useState<string | null>(null);

  const points = price.data?.points ?? [];
  const last = points.at(-1);
  const quote = company.data?.quote;
  const shown = live?.price ?? quote?.price ?? null;
  const priceTitle = live?.price != null
    ? `live quote ${live.ts ?? ""} · ${live.source}`
    : quote
      ? `${company.data?.profile.name ? `${company.data.profile.name} · ` : ""}quote ${quote.ts ?? ""} · ${quote.source}`
      : undefined;

  const notes: string[] = [];
  if (company.isError) notes.push(`${symbol}: ${reason(company.error)}`);
  if (price.isError) notes.push(`price history for ${symbol}: ${reason(price.error)}`);
  if (price.data?.error) notes.push(price.data.error);
  if (price.data && !points.length) notes.push(`no price history for ${symbol}`);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const next = (draft ?? symbol).trim().toUpperCase();
    if (next) onSubmit(next);
    setDraft(null);
  };

  return (
    <Card data-panel="" aria-label="Quote">
      <div className="flex items-center justify-between gap-3 border-b border-border-subtle px-[11px] py-2.5">
        <form className="flex items-center gap-1.5" onSubmit={submit}>
          <span className="text-text-muted">⌕ TICKER</span>
          <input
            aria-label="Chart ticker"
            autoComplete="off"
            spellCheck={false}
            className="w-24 rounded-[var(--ds-radius-ctrl)] border border-border bg-inset px-2 py-1 text-text"
            value={draft ?? symbol}
            onChange={(event) => setDraft(event.target.value)}
          />
          <Button type="submit" size="sm">
            Load
          </Button>
        </form>
        <span className="text-[18px] text-up" aria-label="Price" title={priceTitle}>
          {company.isError ? "—" : num(shown)}
        </span>
        <dl className="flex gap-[18px]">
          {(
            [
              ["OPEN", num(last?.open)],
              ["HIGH", num(last?.high)],
              ["LOW", num(last?.low)],
              ["VOL", compact(last?.volume, 1)],
            ] as const
          ).map(([label, value]) => (
            <div key={label}>
              <dt className="mb-1 text-[9px] text-text-muted">{label}</dt>
              <dd
                className="text-[11px] font-bold"
                title={label === "VOL" && last ? `session of ${etDate(last.time)} · source ${price.data?.source ?? "?"}` : undefined}
              >
                {value}
              </dd>
            </div>
          ))}
        </dl>
      </div>
      <div className="relative h-[285px]">
        {points.length > 0 && <AreaChart className="absolute inset-[13px]" points={points} live={live} />}
      </div>
      {notes.length > 0 && (
        <p role="alert" className="border-t border-border px-2.5 py-2 text-down">
          {notes.join(" · ")}
        </p>
      )}
    </Card>
  );
}
