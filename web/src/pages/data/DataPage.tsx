/* Market & Data, ported from ui/js/pages/data.js (map: docs/UI_PORT.md, S2
 * and S3): collections, the screener, the source catalog, ingestion and
 * data quality. */
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import type { Source } from "../../api/schemas/ingest";
import type { Universe } from "../../api/schemas/universes";
import type { Watchlist } from "../../api/schemas/watchlists";
import { useToast } from "../../shell/toast-context";
import { Button } from "../../ui/primitives/button";
import { Tabs } from "../../ui/primitives/tabs";
import { CollectionDialog, type DialogTarget } from "./CollectionDialog";
import { CatalogTab } from "./CatalogTab";
import { CollectionsTab } from "./CollectionsTab";
import { IngestionTab } from "./IngestionTab";
import { QualityTab } from "./QualityTab";
import { ScreenerTab } from "./ScreenerTab";
import type { CollectionKind } from "./rules";
import { DATA_TABS, isDataTab, type DataTab } from "./tabs";


function symbolsOf(record: Universe | Watchlist): string {
  if ("members" in record && Array.isArray(record.members)) return record.members.map((m) => m.symbol).join(", ");
  return Array.isArray(record.symbols) ? record.symbols.join(", ") : "";
}

export type { DataTab };

export function DataPage({ tab }: { tab: DataTab }) {
  const navigate = useNavigate();
  const toast = useToast();
  const [dialog, setDialog] = useState<DialogTarget | null>(null);
  // What the catalog hands the ingestion form: a source to copy, or a blank form.
  const [draft, setDraft] = useState<{ source: Source | null; key: number }>({ source: null, key: 0 });

  const open = (next: string) => {
    if (isDataTab(next)) void navigate({ to: "/data/$tab", params: { tab: next } });
  };
  const toIngestion = (source: Source | null) => {
    setDraft({ source, key: Date.now() });
    open("ingestion");
    toast.show(source ? `${source.name} copied into the ingestion form` : "Describe the new source in the ingestion form and test it");
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
      <Tabs tabs={DATA_TABS.map((t) => ({ value: t.value, label: t.label }))} value={tab} onChange={open} className="mb-[11px]" />
      <div role="tabpanel" aria-label={DATA_TABS.find((t) => t.value === tab)?.label}>
        {tab === "screener" ? (
          <ScreenerTab />
        ) : tab === "source-catalog" ? (
          <CatalogTab onRequest={() => toIngestion(null)} onCustom={toIngestion} />
        ) : tab === "ingestion" ? (
          <IngestionTab key={draft.key} draft={draft} />
        ) : tab === "quality" ? (
          <QualityTab />
        ) : (
          <CollectionsTab onEdit={edit} />
        )}
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
