/* New / edit collection, the old "New collection" dialog. The kind of an
 * existing collection is fixed; the server reports fields it ignored. */
import { useState, type FormEvent } from "react";
import { useToast } from "../../shell/toast-context";
import { Button } from "../../ui/primitives/button";
import { Field, Input, Select, Textarea } from "../../ui/primitives/field";
import { Modal } from "../../ui/primitives/modal";
import { cn } from "../../ui/lib/cn";
import { useCollectionActions, type CollectionForm } from "./hooks";
import type { CollectionKind } from "./rules";

export interface DialogTarget {
  kind: CollectionKind;
  editing: { id: string; name: string; description: string | null; symbols: string } | null;
}

const CONSTRUCTION = ["Manual symbols", "Screener result", "Rule-based", "Agent findings", "Earnings calendar"];
const REFRESH = ["Manual", "Live", "Hourly", "Daily", "Weekly"];

export function CollectionDialog({ target, onClose, onSaved }: { target: DialogTarget; onClose: () => void; onSaved: (kind: CollectionKind, id: string) => void }) {
  const toast = useToast();
  const { save } = useCollectionActions();
  const [form, setForm] = useState<CollectionForm>({
    kind: target.kind,
    editing: target.editing?.id ?? null,
    name: target.editing?.name ?? "",
    description: target.editing?.description ?? null,
    construction: CONSTRUCTION[0] as string,
    refresh: REFRESH[0] as string,
    symbols: target.editing?.symbols ?? "",
    makeDefault: false,
  });
  const set = <K extends keyof CollectionForm>(key: K, value: CollectionForm[K]) => setForm((f) => ({ ...f, [key]: value }));
  const editing = form.editing !== null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!form.name.trim()) return;
    save.mutate(
      { ...form, name: form.name.trim(), description: form.description?.trim() || null },
      {
        onSuccess: (saved) => {
          const ignored = (saved as { ignored_detail?: string; ignored_fields?: string[] }).ignored_detail;
          if (ignored) toast.warn(ignored);
          onSaved(form.kind, saved.id);
          onClose();
        },
      },
    );
  };

  return (
    <Modal open onClose={onClose} title={`${editing ? "Edit" : "New"} ${form.kind.toLowerCase()}`} className="max-w-lg">
      <form className="flex flex-col gap-3" onSubmit={submit} aria-label="Collection">
        <div role="radiogroup" aria-label="Collection type" className="flex gap-1">
          {(["Watchlist", "Universe"] as const).map((kind) => (
            <button
              key={kind}
              type="button"
              role="radio"
              aria-checked={form.kind === kind}
              disabled={editing}
              onClick={() => set("kind", kind)}
              className={cn("rounded-[var(--ds-radius-ctrl)] border border-border px-3 py-1", form.kind === kind && "bg-active text-accent")}
            >
              {kind}
            </button>
          ))}
        </div>
        <Field label="Name" htmlFor="collectionName">
          <Input id="collectionName" required autoFocus placeholder="e.g. Semiconductor research" value={form.name} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <Field label="Description" htmlFor="collectionDescription">
          <Textarea id="collectionDescription" placeholder="What this collection represents" value={form.description ?? ""} onChange={(e) => set("description", e.target.value)} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Construction method" htmlFor="constructionMethod">
            <Select id="constructionMethod" value={form.construction} onChange={(e) => set("construction", e.target.value)}>
              {CONSTRUCTION.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </Select>
          </Field>
          <Field label="Refresh" htmlFor="collectionRefresh">
            <Select id="collectionRefresh" value={form.refresh} onChange={(e) => set("refresh", e.target.value)}>
              {REFRESH.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </Select>
          </Field>
        </div>
        <Field label="Symbols or rules" htmlFor="collectionSymbols">
          <Textarea id="collectionSymbols" placeholder="GRAB, UBER, SEA — or describe the rule" value={form.symbols} onChange={(e) => set("symbols", e.target.value)} />
        </Field>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={form.makeDefault} onChange={(e) => set("makeDefault", e.target.checked)} /> Make this the default global collection
        </label>
        <div className="flex justify-end gap-2">
          <Button size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button size="sm" variant="primary" type="submit" disabled={save.isPending}>
            {editing ? "Save changes" : "Create collection"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
