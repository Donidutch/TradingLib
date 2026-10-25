/* Panel 3: the default watchlist with live prices, a prefix filter, add and
 * remove. Adding to no watchlist creates "Primary research". */
import { useState, type FormEvent } from "react";
import type { Watchlist } from "../../api/schemas/watchlists";
import { useAddWatchSymbol, useCreateWatchlist, useRemoveWatchSymbol } from "../../api/hooks";
import { useToast } from "../../shell/toast-context";
import { num, pct } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Button } from "../../ui/primitives/button";
import { Panel } from "../../ui/primitives/card";
import { sublabel } from "./names";
import type { Quotes } from "./useTape";

const input =
  "w-full rounded-[var(--ds-radius-ctrl)] border border-border bg-inset px-2 py-1 text-text placeholder:text-text-subtle";

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export function WatchlistPanel({
  watchlist,
  loading,
  error,
  universeSize,
  names,
  sectors,
  quotes,
  selected,
  onSelect,
}: {
  watchlist: Watchlist | null;
  loading: boolean;
  error: Error | null;
  universeSize: number;
  names: ReadonlyMap<string, string>;
  sectors: ReadonlyMap<string, string>;
  quotes: Quotes;
  selected: string | null;
  onSelect: (symbol: string) => void;
}) {
  const toast = useToast();
  const [filter, setFilter] = useState("");
  const [adding, setAdding] = useState("");
  const create = useCreateWatchlist();
  const add = useAddWatchSymbol();
  const remove = useRemoveWatchSymbol();

  const symbols = watchlist?.symbols ?? [];
  const prefix = filter.trim().toUpperCase();
  const shown = symbols.filter((symbol) => !prefix || symbol.startsWith(prefix));

  const badge = error
    ? "error"
    : !watchlist
      ? loading
        ? "—"
        : "none"
      : universeSize
        ? `${symbols.length} / ${universeSize}`
        : String(symbols.length);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const symbol = adding.trim().toUpperCase();
    if (!symbol) return;
    try {
      const changed = watchlist
        ? await add.mutateAsync({ id: watchlist.id, symbol })
        : await create.mutateAsync({ name: "Primary research", symbols: [symbol], is_default: true });
      setAdding("");
      toast.ok(`${symbol} added to ${changed.name}`);
    } catch (err) {
      toast.error(`Could not add ${symbol}: ${errorText(err)}`);
    }
  };

  const drop = async (symbol: string) => {
    if (!watchlist) return;
    try {
      const changed = await remove.mutateAsync({ id: watchlist.id, symbol });
      toast.show(`${symbol} removed from ${changed.name}`);
    } catch (err) {
      toast.error(`Could not remove ${symbol}: ${errorText(err)}`);
    }
  };

  return (
    <Panel
      title="Watchlist"
      accent={false}
      bodyClassName="p-0"
      actions={
        <Badge
          tone={error ? "down" : "neutral"}
          title={
            watchlist
              ? `${watchlist.name}${universeSize ? ` · ${symbols.length} watched of ${universeSize} in the active universe` : ""}`
              : "no watchlist exists yet"
          }
        >
          {badge}
        </Badge>
      }
    >
      <div className="flex flex-col gap-2 p-2.5">
        <input
          className={input}
          placeholder="Filter symbols…"
          aria-label="Filter watchlist symbols"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
        <form className="flex gap-1.5" onSubmit={(event) => void submit(event)}>
          <input
            className={input}
            placeholder="TICKER"
            aria-label="Symbol to add to the watchlist"
            value={adding}
            onChange={(event) => setAdding(event.target.value)}
          />
          <Button type="submit" size="sm" disabled={add.isPending || create.isPending}>
            Add
          </Button>
        </form>
      </div>
      <div role="list" aria-label="Watched symbols">
        {error ? (
          <p className="border-t border-border px-2.5 py-2.5 text-down">{error.message}</p>
        ) : !watchlist ? (
          !loading && (
            <div className="border-t border-border px-2.5 py-2.5">
              <b className="block font-medium text-text-strong">No watchlist yet</b>
              <span className="text-text-muted">Add a ticker above and TradingLib creates “Primary research” for it.</span>
            </div>
          )
        ) : !shown.length ? (
          <p className="border-t border-border px-2.5 py-2.5 text-text-muted">
            {symbols.length ? `No symbol matches “${prefix}”.` : "This watchlist is empty."}
          </p>
        ) : (
          shown.map((symbol) => {
            const quote = quotes.get(symbol);
            const change = quote?.change_pct;
            const [label, title] = sublabel(names.get(symbol), sectors.get(symbol));
            return (
              <div
                key={symbol}
                role="listitem"
                className={cn(
                  "grid cursor-pointer grid-cols-[1fr_auto] gap-2 border-t border-border px-[11px] py-2.5 hover:bg-surface-2",
                  symbol === selected && "bg-warn-dim shadow-[inset_2px_0_var(--ds-warn)]",
                )}
                onClick={() => onSelect(symbol)}
              >
                <button
                  type="button"
                  className="text-left"
                  aria-label={`Open ${symbol}`}
                  aria-current={symbol === selected ? "true" : undefined}
                >
                  <b className="font-medium text-text-strong">{symbol}</b>
                  <small className="mt-1 block text-[10px] text-text-muted" title={title}>
                    {label}
                  </small>
                </button>
                <span className="text-right">
                  {quote ? num(quote.price) : "—"}{" "}
                  <button
                    type="button"
                    aria-label={`Remove ${symbol}`}
                    title={`Remove ${symbol} from ${watchlist.name}`}
                    className="pl-2 text-[11px] text-text-subtle hover:text-down"
                    onClick={(event) => {
                      event.stopPropagation();
                      void drop(symbol);
                    }}
                  >
                    ✕
                  </button>
                  <span
                    className={cn("block", change == null ? undefined : change >= 0 ? "text-up" : "text-down")}
                  >
                    {!quote ? "…" : change == null ? "—" : pct(change, { sign: true })}
                  </span>
                </span>
              </div>
            );
          })
        )}
      </div>
    </Panel>
  );
}
