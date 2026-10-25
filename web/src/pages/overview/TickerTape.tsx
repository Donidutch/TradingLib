import { num, pct } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import type { TapeNotice } from "../../api/schemas/stream";
import { TAPE_SYMBOLS, type Quotes } from "./useTape";

/** Panel 2: the scope's first symbols with live prices from the tape. */
export function TickerTape({
  label,
  error,
  members,
  names,
  quotes,
  notice,
}: {
  label: string | null;
  error: string | null;
  members: readonly string[];
  names: ReadonlyMap<string, string>;
  quotes: Quotes;
  notice: TapeNotice | null;
}) {
  const title = !notice
    ? undefined
    : notice.event === "tape_error"
      ? `tape degraded: ${notice.detail ?? ""}`
      : `${notice.delayed === false ? "live feed" : "delayed feed"} · ${notice.feed ?? ""} · ${notice.symbols?.length ?? 0} symbols`;
  return (
    <div
      aria-label="Ticker tape"
      className="mb-[11px] flex h-[35px] items-center overflow-hidden rounded-[5px] border border-border-subtle bg-surface-3 text-[11px]"
    >
      <span
        className="flex h-full items-center whitespace-nowrap border-r border-border-subtle px-3 text-up"
        title={error ?? title}
      >
        {error ? "● UNIVERSE UNAVAILABLE" : `● ${(label ?? "…").toUpperCase()}`}
      </span>
      {label && !members.length && <span className="px-[18px] text-text-muted">no universe members</span>}
      {members.slice(0, TAPE_SYMBOLS).map((symbol) => {
        const quote = quotes.get(symbol);
        const change = quote?.change_pct;
        return (
          <span
            key={symbol}
            data-symbol={symbol}
            title={names.get(symbol) ?? symbol}
            className="flex h-full items-center gap-[7px] whitespace-nowrap border-r border-border px-[18px]"
          >
            <b className="font-medium text-text-strong">{symbol}</b>
            <span>{quote ? num(quote.price) : "—"}</span>
            <i
              className={cn("not-italic", change == null ? undefined : change >= 0 ? "text-up" : "text-down")}
              title={
                !quote
                  ? undefined
                  : change == null
                    ? "the quote carries no previous close"
                    : `${quote.delayed ? "delayed" : "live"} · ${quote.source}`
              }
            >
              {!quote ? "…" : change == null ? "—" : `${change >= 0 ? "▲" : "▼"} ${pct(Math.abs(change))}`}
            </i>
          </span>
        );
      })}
    </div>
  );
}
