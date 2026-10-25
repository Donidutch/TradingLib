/* PICK mode, ported from ui/js/components/pick.js: mark panels across the
 * workspaces and export the selection as JSON, so a layout discussion can
 * name panels unambiguously. A mark is keyed by position on the page
 * ("stock.NVDA.p2" is the second panel on Stock Research for NVDA), survives
 * reloads in localStorage, and is re-applied whenever the page re-renders. */
import { useRouterState } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import { Button } from "../ui/primitives/button";
import { Modal } from "../ui/primitives/modal";
import { cn } from "../ui/lib/cn";
import { useToast } from "./toast-context";

/** Separate from the vanilla console's key: panel positions differ. */
export const PICK_STORE_KEY = "tradinglib-app-picks";
const PANEL = "[data-panel]";

interface PickEntry {
  page: string;
  sub: string;
  id: string;
  title: string;
}

type Picks = Record<string, PickEntry>;

function loadPicks(): Picks {
  try {
    return JSON.parse(localStorage.getItem(PICK_STORE_KEY) ?? "{}") as Picks;
  } catch {
    return {};
  }
}

function savePicks(picks: Picks): void {
  try {
    localStorage.setItem(PICK_STORE_KEY, JSON.stringify(picks));
  } catch (err) {
    console.warn("pick mode: cannot persist picks", err);
  }
}

/** "/" → overview, "/stock/NVDA" → stock + NVDA. */
function contextFor(pathname: string): { page: string; sub: string } {
  const [page = "", sub = ""] = pathname.replace(/^\//, "").split("/");
  return { page: page || "overview", sub };
}

function titleOf(panel: Element): string {
  const clean = (s: string | null) => (s ?? "").trim().replace(/\s+/g, " ");
  const heading = panel.querySelector("h2, h3");
  return heading ? clean(heading.textContent).slice(0, 60) : `(untitled) ${clean(panel.textContent).slice(0, 44)}`;
}

export function Pick({ main }: { main: HTMLElement | null }) {
  const toast = useToast();
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const [active, setActive] = useState(false);
  const [picks, setPicks] = useState<Picks>(loadPicks);
  const [exporting, setExporting] = useState(false);
  const ctx = contextFor(pathname);
  const idFor = useCallback(
    (index: number) => [ctx.page, ctx.sub, `p${index}`].filter(Boolean).join("."),
    [ctx.page, ctx.sub],
  );

  // Paint marks on whatever is on screen, again after every re-render.
  useEffect(() => {
    if (!main) return;
    const ids = Object.keys(picks);
    const paint = () => {
      main.querySelectorAll(PANEL).forEach((panel, index) => {
        const id = idFor(index);
        const n = ids.indexOf(id);
        if (n >= 0) panel.setAttribute("data-picked", String(n + 1));
        else panel.removeAttribute("data-picked");
      });
    };
    paint();
    const observer = new MutationObserver((records) => {
      if (records.every((r) => r.type === "attributes" && r.attributeName === "data-picked")) return;
      paint();
    });
    observer.observe(main, { subtree: true, childList: true });
    return () => observer.disconnect();
  }, [main, picks, idFor]);

  // Capture phase: while picking, a click on a panel marks it instead of
  // running the page's own handler.
  useEffect(() => {
    if (!active || !main) return;
    const onClick = (event: MouseEvent) => {
      const panel = (event.target as Element | null)?.closest(PANEL);
      if (!panel || !main.contains(panel)) return;
      event.preventDefault();
      event.stopPropagation();
      const index = [...main.querySelectorAll(PANEL)].indexOf(panel);
      const id = idFor(index);
      setPicks((current) => {
        const next: Picks = current[id]
          ? Object.fromEntries(Object.entries(current).filter(([key]) => key !== id))
          : { ...current, [id]: { page: ctx.page, sub: ctx.sub, id, title: titleOf(panel) } };
        savePicks(next);
        return next;
      });
    };
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, [active, main, idFor, ctx.page, ctx.sub]);

  useEffect(() => {
    document.body.classList.toggle("pickmode", active);
  }, [active]);

  const manifest = JSON.stringify(Object.values(picks), null, 2);
  const count = Object.keys(picks).length;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(manifest);
      toast.ok("Manifest copied");
    } catch {
      toast.error("Could not copy to the clipboard");
    }
  };

  const download = () => {
    const url = URL.createObjectURL(new Blob([manifest], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "tradinglib-picks.json";
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <span className="flex items-center gap-2">
      <Button
        size="sm"
        variant="ghost"
        aria-pressed={active}
        className={cn("border border-border", active && "border-accent bg-active text-accent")}
        onClick={() => setActive((on) => !on)}
      >
        PICK
      </Button>
      <span className="text-up" title="panels selected" aria-label="Panels selected">
        {count}
      </span>
      <Button size="sm" variant="ghost" className="border border-border" onClick={() => setExporting(true)}>
        Export
      </Button>
      <Modal
        open={exporting}
        onClose={() => setExporting(false)}
        title="Selected panels"
        className="max-w-[760px]"
        footer={
          <>
            <Button
              size="sm"
              onClick={() => {
                setPicks({});
                savePicks({});
              }}
            >
              Clear all
            </Button>
            <Button size="sm" onClick={() => void copy()}>
              Copy
            </Button>
            <Button size="sm" onClick={download}>
              Download .json
            </Button>
          </>
        }
      >
        <textarea
          readOnly
          aria-label="Picked panels as JSON"
          spellCheck={false}
          value={manifest}
          className="mono h-[44vh] w-full resize-y rounded-[8px] border border-border bg-inset p-2.5 text-[11px] text-text"
        />
      </Modal>
    </span>
  );
}
