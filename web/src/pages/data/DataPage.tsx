/* Market & Data, ported from ui/js/pages/data.js (map: docs/UI_PORT.md, S2).
 * Collections and the screener are here; Source Catalog, Data Ingestion and
 * Data Quality follow in S3 and open in the current console until then. */
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import type { Universe } from "../../api/schemas/universes";
import type { Watchlist } from "../../api/schemas/watchlists";
import { openLegacy } from "../../shell/legacy";
import { Button } from "../../ui/primitives/button";
import { Tabs } from "../../ui/primitives/tabs";
import { CollectionDialog, type DialogTarget } from "./CollectionDialog";
import { CollectionsTab } from "./CollectionsTab";
import { ScreenerTab } from "./ScreenerTab";
import type { CollectionKind } from "./rules";

const DATA_TABS = [
  { value: "collections", label: "Universes & Watchlists", ported: true },
  { value: "screener", label: "Screener", ported: true },
  { value: "source-catalog", label: "Source Catalog", ported: false },
  { value: "ingestion", label: "Data Ingestion", ported: false },
  { value: "quality", label: "Data Quality", ported: false },
] as const;

export type DataTab = (typeof DATA_TABS)[number]["value"];

function symbolsOf(record: Universe | Watchlist): string {
  if ("members" in record && Array.isArray(record.members)) return record.members.map((m) => m.symbol).join(", ");
  return Array.isArray(record.symbols) ? record.symbols.join(", ") : "";
}

export function DataPage({ tab }: { tab: DataTab }) {
  const navigate = useNavigate();
  const [dialog, setDialog] = useState<DialogTarget | null>(null);

  const open = (next: string) => {
    const target = DATA_TABS.find((t) => t.value === next);
    if (!target) return;
    if (target.ported) void navigate({ to: "/data/$tab", params: { tab: target.value } });
    else openLegacy(`/#data/${target.value}`);
  };

  const edit = (kind: CollectionKind, record: Universe | Watchlist) =>
    setDialog({
      kind,
      editing: { id: record.id, name: record.name, description: typeof record.description === "string" ? record.description : null, symbols: symbolsOf(record) },
    });

  return (
    <div>
      <header className="mb-3 flex items-end justify-between">
        <div>
          <small className="text-[10px] tracking-[1px] text-text-subtle">MARKET &amp; DATA</small>
          <h1 className="text-[14px] text-text-strong">Collections, screening, ingestion, and data quality</h1>
        </div>
        <div className="flex gap-1.5">
          <Button size="sm" onClick={() => setDialog({ kind: "Watchlist", editing: null })}>
            + New watchlist
          </Button>
          <Button size="sm" variant="primary" onClick={() => setDialog({ kind: "Universe", editing: null })}>
            + New universe
          </Button>
        </div>
      </header>
      <Tabs tabs={DATA_TABS.map((t) => ({ value: t.value, label: t.ported ? t.label : `${t.label} ↗` }))} value={tab} onChange={open} className="mb-[11px]" />
      <div role="tabpanel" aria-label={DATA_TABS.find((t) => t.value === tab)?.label}>
        {tab === "screener" ? <ScreenerTab /> : <CollectionsTab onEdit={edit} />}
      </div>
      {dialog && (
        <CollectionDialog
          key={`${dialog.kind}-${dialog.editing?.id ?? "new"}`}
          target={dialog}
          onClose={() => setDialog(null)}
          onSaved={() => {
            if (tab !== "collections") void navigate({ to: "/data/$tab", params: { tab: "collections" } });
          }}
        />
      )}
    </div>
  );
}
