/* The Overview workspace, ported from ui/js/pages/overview.js. Panel map:
 * docs/UI_PORT.md, P3. Every value is from an endpoint or a WebSocket frame;
 * a missing one is "—" with its reason in the title. */
import { useQueries } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useApi, useReachability } from "../../api/context";
import { keys, useActiveScope, useMembers, useStatus, useWatchlists } from "../../api/hooks";
import { useStreamSnapshot } from "../../live/context";
import { FundamentalsPanel } from "./FundamentalsPanel";
import { HealthPanel } from "./HealthPanel";
import { InboxPanel } from "./InboxPanel";
import { IntelPanel } from "./IntelPanel";
import { MarketContextPanel } from "./MarketContextPanel";
import { MarketStrip } from "./MarketStrip";
import { QuotePanel } from "./QuotePanel";
import { TickerTape } from "./TickerTape";
import { tapeSymbols, useTape } from "./useTape";
import { WatchlistPanel } from "./WatchlistPanel";

/** Centre symbol when the scope has no members. */
export const FALLBACK_SYMBOL = "SPY";

export function OverviewPage({ symbol: requested }: { symbol: string | undefined }) {
  const api = useApi();
  const navigate = useNavigate();
  const status = useStatus();
  const stream = useStreamSnapshot();
  const apiOnline = useReachability();
  const active = useActiveScope();
  const watchlists = useWatchlists();

  const scope = active.data?.scope;
  // A watchlist scope is a bare symbol list; only a universe has members.
  const members = useMembers(scope?.kind === "universe" ? scope.id : undefined);
  const scopeSymbols = members.data?.members.map((m) => m.symbol) ?? active.data?.symbols ?? [];
  const names = new Map<string, string>();
  const sectors = new Map<string, string>();
  for (const m of members.data?.members ?? []) {
    if (m.name) names.set(m.symbol, m.name);
    if (m.sector) sectors.set(m.symbol, m.sector);
  }

  const list = watchlists.data;
  const watchlist = list?.watchlists.find((w) => w.id === list.default) ?? list?.watchlists[0] ?? null;

  // Names for watched symbols the scope does not carry, from the symbol index.
  const unnamed = (watchlist?.symbols ?? []).filter((s) => !names.has(s));
  const lookups = useQueries({
    queries: unnamed.map((s) => ({
      queryKey: [...keys.resolve(s), 3],
      queryFn: ({ signal }: { signal: AbortSignal }) => api.research.resolve(s, 3, signal),
      staleTime: Infinity,
    })),
  });
  lookups.forEach((lookup, i) => {
    const s = unnamed[i];
    const hit = lookup.data?.results.find((r) => r.symbol === s);
    if (s && hit?.name) names.set(s, hit.name);
  });

  const symbol = (requested ?? scopeSymbols[0] ?? (active.isPending ? "" : FALLBACK_SYMBOL)).toUpperCase();
  const { quotes, notice } = useTape(tapeSymbols(watchlist?.symbols ?? [], symbol || null, scopeSymbols));
  const live = stream.state === "open";
  const select = (next: string) => void navigate({ to: "/", search: { symbol: next } });

  return (
    <div>
      <MarketStrip status={status.data} live={live} apiOnline={apiOnline} />
      <TickerTape
        label={scope?.name ?? null}
        error={active.isError ? active.error.message : null}
        members={scopeSymbols}
        names={names}
        quotes={quotes}
        notice={notice}
      />
      <div className="grid grid-cols-[245px_minmax(480px,1fr)_285px] gap-[11px]">
        <div className="grid content-start gap-[11px]">
          <WatchlistPanel
            watchlist={watchlist}
            loading={watchlists.isPending}
            error={watchlists.error}
            universeSize={scopeSymbols.length}
            names={names}
            sectors={sectors}
            quotes={quotes}
            selected={symbol}
            onSelect={select}
          />
          <HealthPanel status={status.data} live={live} />
        </div>
        <div className="grid content-start gap-[11px]">
          {symbol && <QuotePanel symbol={symbol} live={quotes.get(symbol)} onSubmit={select} />}
        </div>
        <div className="grid content-start gap-[11px]">
          {symbol && <FundamentalsPanel symbol={symbol} />}
          <MarketContextPanel quotes={quotes} />
        </div>
        <InboxPanel />
        {/* Waits for the scope, and is re-keyed when it changes, so its tabs
            load once for the scope's members and reload for a new scope. */}
        {!active.isPending && <IntelPanel key={scope?.ref ?? "none"} symbols={scopeSymbols} />}
      </div>
    </div>
  );
}
