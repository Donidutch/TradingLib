/* ⌘K command palette, ported from ui/js/palette.js: workspaces, any typed
 * ticker, and /api/search results. A search failure is shown in the list,
 * not swallowed. */
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { useApi } from "../api/context";
import { ApiError } from "../api/client";
import { useSearch } from "../api/hooks";
import { Kbd } from "../ui/primitives/kbd";
import { cn } from "../ui/lib/cn";
import { openLegacy } from "./legacy";
import { targetForHash, WORKSPACES, type Target } from "./pages";
import { useToast } from "./toast-context";

const SEARCH_DEBOUNCE_MS = 160;
const TICKER = /^[A-Za-z.-]{1,10}$/;

interface Entry {
  id: string;
  label: string;
  hint: string;
  run: (() => void | Promise<void>) | null;
}

function useDebounced(value: string, ms: number): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

export function Palette({ open, onClose }: { open: boolean; onClose: () => void }) {
  // Mounted fresh on each open, so the query and selection reset.
  return open ? <PaletteDialog onClose={onClose} /> : null;
}

function PaletteDialog({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const api = useApi();
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [rawSel, setRawSel] = useState(0);
  const text = query.trim();
  const search = useSearch(useDebounced(text, SEARCH_DEBOUNCE_MS));

  const go = (target: Target) => () => {
    if (target.kind === "legacy") openLegacy(target.href);
    else if (target.to === "/stock/$symbol") void navigate({ to: target.to, params: { symbol: target.symbol } });
    else if (target.to === "/docs/$article") void navigate({ to: target.to, params: { article: target.article } });
    else if (target.to === "/") void navigate({ to: "/", search: target.symbol ? { symbol: target.symbol } : {} });
    else void navigate({ to: target.to });
  };

  // "Active" = running first, then queued, then the most recent.
  const openActiveBacktest = async () => {
    const rank: Record<string, number> = { running: 0, queued: 1, pending: 1, complete: 2, completed: 2, finished: 2, failed: 3 };
    let rows;
    try {
      rows = (await api.lab.backtests({ limit: 50 })).backtests;
    } catch (err) {
      toast.error(`Could not load backtests: ${err instanceof Error ? err.message : String(err)}`);
      openLegacy("/#tl/lab/backtests");
      return;
    }
    const active = [...rows].sort(
      (a, b) =>
        (rank[a.status.toLowerCase()] ?? 9) - (rank[b.status.toLowerCase()] ?? 9) ||
        Date.parse(b.created_at) - Date.parse(a.created_at),
    )[0];
    if (!active) toast.warn("No backtests yet — opening the backtests tab");
    openLegacy(active ? `/#tl/lab/backtests?id=${encodeURIComponent(active.id)}` : "/#tl/lab/backtests");
  };

  const entries = ((): Entry[] => {
    const out: Entry[] = [];
    if (text && TICKER.test(text)) {
      const symbol = text.toUpperCase();
      out.push({
        id: `ticker-${symbol}`,
        label: `Research ${symbol}`,
        hint: "Stock Research",
        run: go({ kind: "app", to: "/stock/$symbol", symbol }),
      });
    }
    if (text) {
      for (const result of (search.data?.results ?? []).slice(0, 24)) {
        out.push({
          id: `result-${result.type}-${result.hash}`,
          label: result.title,
          hint: result.subtitle ?? result.type,
          run: go(targetForHash(result.hash)),
        });
      }
    }
    for (const w of WORKSPACES) {
      const label = w.key === "overview" ? `Go to ${w.label}` : `Open ${w.label}`;
      if (text && !label.toLowerCase().includes(text.toLowerCase())) continue;
      out.push({
        id: `page-${w.key}`,
        label,
        hint: w.path ? w.glyph : "current console ↗",
        run: go(w.path ? { kind: "app", to: w.path } : { kind: "legacy", href: `/${w.legacyHash ?? ""}` }),
      });
      if (w.key === "lab" && (!text || "open active backtest".includes(text.toLowerCase()))) {
        out.push({ id: "active-backtest", label: "Open active backtest", hint: "Strategy Lab ↗", run: openActiveBacktest });
      }
    }
    if (text && search.isError) {
      const err = search.error;
      out.push({
        id: "search-error",
        label:
          err instanceof ApiError && err.missing
            ? "Search API not available in this build"
            : `Search failed: ${err.message}`,
        hint: "GET /api/search",
        run: null,
      });
    }
    if (!out.length) out.push({ id: "none", label: `No matches for “${text}”`, hint: "", run: null });
    return out;
  })();

  const runnable = entries.map((e, i) => (e.run ? i : -1)).filter((i) => i >= 0);
  const sel = runnable.includes(rawSel) ? rawSel : (runnable[0] ?? -1);

  const move = (delta: 1 | -1) => {
    if (!runnable.length) return;
    const at = runnable.indexOf(sel);
    setRawSel(runnable[(at + delta + runnable.length) % runnable.length] ?? -1);
  };

  const pick = (entry: Entry | undefined) => {
    if (!entry?.run) return;
    onClose();
    Promise.resolve()
      .then(entry.run)
      .catch((err: unknown) => toast.error(err instanceof Error ? err.message : String(err)));
  };

  return (
    <div className="fixed inset-0 z-[150] flex items-start justify-center pt-[12vh]" role="presentation">
      <div className="absolute inset-0 bg-backdrop" onClick={onClose} aria-hidden />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="animate-in relative w-[min(560px,92vw)] overflow-hidden rounded-[12px] border border-border bg-surface shadow-lg"
      >
        <input
          autoFocus
          aria-label="Command palette search"
          placeholder="Search or jump to…"
          spellCheck={false}
          autoComplete="off"
          value={query}
          className="w-full border-b border-border-subtle bg-transparent px-3.5 py-3 text-[13px] text-text-strong outline-none placeholder:text-text-subtle"
          onChange={(event) => {
            setQuery(event.target.value);
            setRawSel(0);
          }}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              move(1);
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              move(-1);
            } else if (event.key === "Enter") {
              event.preventDefault();
              pick(entries[sel]);
            } else if (event.key === "Escape") {
              event.preventDefault();
              onClose();
            }
          }}
        />
        <div role="listbox" aria-label="Results" className="max-h-[50vh] overflow-y-auto py-1">
          {entries.map((entry, i) => (
            <button
              key={entry.id}
              type="button"
              role="option"
              aria-selected={i === sel}
              disabled={!entry.run}
              onMouseEnter={() => entry.run && setRawSel(i)}
              onClick={() => pick(entry)}
              className={cn(
                "flex w-full items-center justify-between gap-3 px-3.5 py-1.5 text-left text-text",
                i === sel && "bg-surface-2",
                !entry.run && "opacity-60",
              )}
            >
              <span className="truncate">{entry.label}</span>
              <span className="shrink-0 text-[11px] text-text-subtle">{entry.hint}</span>
            </button>
          ))}
        </div>
        <div className="flex gap-3 border-t border-border-subtle px-3.5 py-1.5 text-[10px] text-text-subtle">
          <span>
            <Kbd>↑</Kbd> <Kbd>↓</Kbd> navigate
          </span>
          <span>
            <Kbd>↵</Kbd> open
          </span>
          <span>
            <Kbd>esc</Kbd> close
          </span>
        </div>
      </div>
    </div>
  );
}
