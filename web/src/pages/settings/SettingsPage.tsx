/* Settings, ported from ui/js/pages/settings.js (map: docs/UI_PORT.md, S1).
 * Every control saves only its own field and the page shows what the server
 * stored. API keys are write-only: a key is sent, never shown. */
import { useState } from "react";
import { useSaveSettings, useSettings } from "../../api/hooks";
import type { KeyStatus, Settings, SettingsPatch } from "../../api/schemas/settings";
import { useToast } from "../../shell/toast-context";
import { etDate, usd } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Button } from "../../ui/primitives/button";
import { Panel } from "../../ui/primitives/card";
import { Input, Select } from "../../ui/primitives/field";
import { errorReason } from "../../api/errors";

type Save = (patch: SettingsPatch, description: string) => void;

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border-subtle py-2 last:border-b-0">
      <span>{label}</span>
      <div className="w-56">{children}</div>
    </div>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return <p className="pt-2 text-[11px] text-text-muted">{children}</p>;
}

function Appearance({ settings, save, busy }: { settings: Settings; save: Save; busy: boolean }) {
  const light = settings.theme === "light";
  const capitalise = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);
  return (
    <Panel title="Appearance" accent={false}>
      <Row label="Theme">
        <Button size="sm" disabled={busy} onClick={() => save({ theme: light ? "dark" : "light" }, light ? "Dark theme enabled" : "Light theme enabled")}>
          {light ? "Switch to dark theme" : "Switch to light theme"}
        </Button>
      </Row>
      <Row label="Density">
        <Select aria-label="Density" size="sm" disabled={busy} value={settings.density} onChange={(e) => save({ density: e.target.value }, `Density: ${e.target.value}`)}>
          {settings.options.densities.map((value) => (
            <option key={value} value={value}>
              {capitalise(value)}
            </option>
          ))}
        </Select>
      </Row>
      <Row label="Timezone">
        <Select aria-label="Timezone" size="sm" disabled={busy} value={settings.timezone} onChange={(e) => save({ timezone: e.target.value }, `Timezone: ${e.target.value}`)}>
          {settings.options.timezones.map((zone) => (
            <option key={zone} value={zone}>
              {zone}
            </option>
          ))}
        </Select>
      </Row>
      <Note>The timezone drives the topbar clock and the timestamps in the workspaces. Market data itself is always stored in UTC.</Note>
    </Panel>
  );
}

function Defaults({ settings, save, busy }: { settings: Settings; save: Save; busy: boolean }) {
  const benchmarks = settings.options.benchmarks.map((b) => ({ value: b.symbol, label: b.label }));
  if (!benchmarks.some((b) => b.value === settings.benchmark)) benchmarks.unshift({ value: settings.benchmark, label: settings.benchmark });
  const universes = settings.options.universes.length
    ? settings.options.universes.map((u) => ({ value: u.id, label: u.name }))
    : [{ value: settings.default_universe ?? "", label: settings.default_universe ?? "—" }];
  return (
    <Panel title="Research defaults" accent={false}>
      <Row label="Primary benchmark">
        <Select aria-label="Primary benchmark" size="sm" disabled={busy} value={settings.benchmark} onChange={(e) => save({ benchmark: e.target.value }, `Benchmark: ${e.target.value}`)}>
          {benchmarks.map((b) => (
            <option key={b.value} value={b.value}>
              {b.label}
            </option>
          ))}
        </Select>
      </Row>
      <Row label="Default universe">
        <Select
          aria-label="Default universe"
          size="sm"
          disabled={busy}
          value={settings.default_universe ?? ""}
          onChange={(e) => save({ default_universe: e.target.value }, `Default universe: ${e.target.value}`)}
        >
          {universes.map((u) => (
            <option key={u.value} value={u.value}>
              {u.label}
            </option>
          ))}
        </Select>
      </Row>
      <Note>
        The default universe is the active universe: it scopes the tape, the screener and every agent run. The benchmark is the comparison series new backtests use.
      </Note>
    </Panel>
  );
}

function KeyRow({ entry, save, busy }: { entry: KeyStatus; save: Save; busy: boolean }) {
  const toast = useToast();
  const [value, setValue] = useState("");
  const status = entry.configured
    ? `configured · ${entry.source ?? "unknown source"}${entry.updated_at ? ` · saved ${etDate(entry.updated_at)}` : ""}`
    : `not configured · reads ${entry.env}`;
  return (
    <div className="border-b border-border-subtle py-2 last:border-b-0">
      <b className="block font-medium text-text-strong">{entry.label}</b>
      <span className={cn("text-[11px]", entry.configured ? "text-up" : "text-text-muted")}>{status}</span>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <Input
          type="password"
          size="sm"
          autoComplete="off"
          aria-label={entry.label}
          placeholder={entry.configured ? "configured — type to replace" : "not configured"}
          className="flex-[1_1_180px]"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <Button
          size="sm"
          variant="primary"
          disabled={busy}
          onClick={() => {
            if (!value) {
              toast.warn("Type a key first, or use Clear to remove the stored one");
              return;
            }
            // Clear the field before sending: the key is not kept in the page.
            setValue("");
            save({ keys: { [entry.provider]: value } }, `${entry.label} saved`);
          }}
        >
          Save
        </Button>
        <Button
          size="sm"
          disabled={busy || !entry.configured || entry.source !== "stored"}
          onClick={() => save({ keys: { [entry.provider]: "" } }, `${entry.label} cleared`)}
        >
          Clear
        </Button>
      </div>
    </div>
  );
}

function Keys({ settings, save, busy }: { settings: Settings; save: Save; busy: boolean }) {
  const configured = settings.keys.filter((k) => k.configured).length;
  return (
    <Panel title="API keys" accent={false} actions={<Badge>{`${configured} / ${settings.keys.length} configured`}</Badge>}>
      {settings.keys.map((entry) => (
        <KeyRow key={entry.provider} entry={entry} save={save} busy={busy} />
      ))}
      <Note>{settings.secret_storage}</Note>
    </Panel>
  );
}

function Providers({ settings }: { settings: Settings }) {
  const b = settings.budgets;
  return (
    <Panel title="Provider status" accent={false}>
      {settings.providers.map((p) => (
        <div key={p.provider} className="border-b border-border-subtle py-2 last:border-b-0">
          <div className="flex justify-between">
            <span>{p.provider}</span>
            <b className={p.configured ? "text-up" : "text-warn"}>{p.configured ? "configured" : "not configured"}</b>
          </div>
          {p.note && <Note>{p.note}</Note>}
        </div>
      ))}
      <div className="pt-2">
        <b className="block font-medium text-text-strong">LLM budgets</b>
        <span className="text-text-muted">
          per run {usd(b.per_run_usd)} · daily {usd(b.daily_usd)} · monthly {usd(b.monthly_usd)}
          {settings.config_path ? ` · from ${settings.config_path}` : ""}
        </span>
      </div>
    </Panel>
  );
}

export function SettingsPage() {
  const settings = useSettings();
  const saving = useSaveSettings();
  const save: Save = (patch, description) => saving.mutate({ patch, description });
  const busy = saving.isPending;

  return (
    <div>
      <header className="mb-3">
        <h1 className="text-[14px] tracking-[1.5px] text-text-strong">SETTINGS</h1>
        <p className="text-text-muted">Application preferences</p>
      </header>
      {settings.isPending ? (
        <p className="text-text-muted">Loading settings…</p>
      ) : settings.isError ? (
        <p role="alert" className="text-down">
          Settings could not be loaded: {errorReason(settings.error)}
        </p>
      ) : (
        <div className="grid gap-[11px]">
          <div className="grid grid-cols-2 gap-[11px]">
            <Appearance settings={settings.data} save={save} busy={busy} />
            <Defaults settings={settings.data} save={save} busy={busy} />
          </div>
          <div className="grid grid-cols-2 gap-[11px]">
            <Keys settings={settings.data} save={save} busy={busy} />
            <Providers settings={settings.data} />
          </div>
        </div>
      )}
    </div>
  );
}
