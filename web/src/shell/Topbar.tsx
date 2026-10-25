import { Kbd } from "../ui/primitives/kbd";
import { Clock } from "./Clock";
import { EngineIndicator } from "./EngineIndicator";
import type { Indicator } from "./health";
import { Pick } from "./Pick";
import { UniverseSelect } from "./UniverseSelect";

export function Topbar({
  engine,
  onSearch,
  main,
}: {
  engine: Indicator;
  onSearch: () => void;
  main: HTMLElement | null;
}) {
  return (
    <header className="sticky top-0 z-10 flex items-center gap-3.5 border-b border-border-subtle bg-surface-2 px-4 py-2.5">
      <button
        type="button"
        onClick={onSearch}
        aria-label="Search the research app"
        className="flex max-w-[420px] flex-1 cursor-text items-center gap-2 rounded-[var(--ds-radius-ctrl)] border border-border bg-inset px-3 py-1.5 text-left text-text-subtle"
      >
        <span aria-hidden>⌕</span>
        <span>Search the entire research app…</span>
        <Kbd className="ml-auto">⌘K</Kbd>
      </button>
      <div className="ml-auto flex items-center gap-4 whitespace-nowrap text-text-subtle">
        <Pick main={main} />
        <UniverseSelect />
        <EngineIndicator engine={engine} />
        <Clock />
      </div>
    </header>
  );
}
