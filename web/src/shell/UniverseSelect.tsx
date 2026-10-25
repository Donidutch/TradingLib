/* The scope picker: universes and watchlists, either can be the active scope
 * the tape, screens and scanner use. Selecting one sets it server-side. */
import { useActiveScope, useSetActiveScope, useUniverses, useWatchlists } from "../api/hooks";
import { useToast } from "./toast-context";

export function UniverseSelect() {
  const toast = useToast();
  const universes = useUniverses();
  const watchlists = useWatchlists();
  const active = useActiveScope();
  const setActive = useSetActiveScope();

  const scope = active.data?.scope;
  const current = scope ? `${scope.kind}:${scope.id}` : "";
  const failed = universes.isError || active.isError;
  const loading = universes.isPending || active.isPending;
  const universeRows = universes.data?.universes ?? [];
  // A failed watchlist listing narrows the selector rather than breaking it.
  const watchlistRows = watchlists.data?.watchlists ?? [];
  const empty = !loading && !failed && universeRows.length === 0 && watchlistRows.length === 0;

  const choose = async (ref: string) => {
    if (!ref || ref === current) return;
    try {
      const result = await setActive.mutateAsync(ref);
      toast.ok(
        `Active ${result.scope.kind === "watchlist" ? "watchlist" : "universe"}: ${result.scope.name}`,
      );
    } catch (err) {
      toast.error(`Could not switch scope: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  return (
    <label className="flex items-center gap-2">
      UNIVERSE
      <select
        aria-label="Universe"
        className="rounded-[var(--ds-radius-ctrl)] border border-border bg-inset px-1.5 py-0.5 text-text"
        value={failed || loading || empty ? "" : current}
        disabled={failed || loading || empty || setActive.isPending}
        onChange={(event) => void choose(event.target.value)}
      >
        {loading && <option value="">loading…</option>}
        {failed && <option value="">unavailable</option>}
        {empty && <option value="">no collections</option>}
        {!loading &&
          !failed &&
          universeRows.map((u) => (
            <option key={u.id} value={`universe:${u.id}`}>
              {u.name}
              {u.symbol_count || u.member_count ? ` (${u.symbol_count || u.member_count})` : ""}
            </option>
          ))}
        {!loading &&
          !failed &&
          watchlistRows.map((w) => (
            <option key={w.id} value={`watchlist:${w.id}`}>
              Watchlist: {w.name}
              {w.symbol_count ? ` (${w.symbol_count})` : ""}
            </option>
          ))}
      </select>
    </label>
  );
}
