/* The new-experiment form: name, hypothesis, kind, horizon, owner, notes,
 * the BacktestConfig editor, acceptance criteria and what the run will be
 * judged by. Seeded by CLONE & EDIT or by a hypothesis. */
import { useState } from "react";
import { useActiveScope, useUniverses } from "../../api/hooks";
import { useToast } from "../../shell/toast-context";
import { num } from "../../ui/format";
import { Button } from "../../ui/primitives/button";
import { Panel } from "../../ui/primitives/card";
import { Field, Input, Select, Textarea } from "../../ui/primitives/field";
import { useExperimentActions, useJudgement, useLabData, useStartRun } from "./hooks";
import { Foot } from "./parts";
import { configForm, readConfig, REBALANCES, SIGNAL_KINDS, WEIGHTINGS, type ConfigForm } from "./rules";
import { useLab, type ExperimentSeed } from "./state";

function ConfigEditor({ form, set }: { form: ConfigForm; set: <K extends keyof ConfigForm>(key: K, value: ConfigForm[K]) => void }) {
  const universes = useUniverses();
  const input = (key: keyof ConfigForm, label: string, props: { type?: string; placeholder?: string; step?: string } = {}) => (
    <Field label={label} htmlFor={`cfg-${key}`}>
      <Input id={`cfg-${key}`} value={String(form[key])} onChange={(e) => set(key, e.target.value)} {...props} />
    </Field>
  );
  const select = (key: keyof ConfigForm, label: string, options: readonly string[]) => (
    <Field label={label} htmlFor={`cfg-${key}`}>
      <Select id={`cfg-${key}`} value={String(form[key])} onChange={(e) => set(key, e.target.value)}>
        {options.map((o) => (
          <option key={o}>{o}</option>
        ))}
      </Select>
    </Field>
  );
  return (
    <div className="grid grid-cols-3 gap-3 max-[1100px]:grid-cols-1">
      <fieldset className="flex flex-col gap-2 rounded-[4px] border border-border-subtle p-2.5">
        <legend className="px-1 text-[9px] tracking-[1px] text-text-subtle">PERIOD &amp; DATA</legend>
        {input("name", "NAME", { placeholder: "mom_12_1_top10" })}
        {input("start", "START", { type: "date" })}
        {input("end", "END", { type: "date" })}
        {input("oosFraction", "OOS FRACTION", { type: "number", step: "0.05" })}
        <Foot>Empty dates mean the whole stored history. The store's coverage decides what is loadable.</Foot>
      </fieldset>
      <fieldset className="flex flex-col gap-2 rounded-[4px] border border-border-subtle p-2.5">
        <legend className="px-1 text-[9px] tracking-[1px] text-text-subtle">PORTFOLIO</legend>
        <Field label="UNIVERSE" htmlFor="cfg-universe">
          <Select id="cfg-universe" value={form.universe} onChange={(e) => set("universe", e.target.value)}>
            <option value="">— explicit symbols —</option>
            {(universes.data?.universes ?? []).map((u) => (
              <option key={u.id} value={u.id}>
                {u.name}
              </option>
            ))}
          </Select>
        </Field>
        {input("symbols", "SYMBOLS (when no universe)", { placeholder: "AAPL, MSFT, JPM" })}
        {input("benchmark", "BENCHMARK", { placeholder: "SPY" })}
        {select("rebalance", "REBALANCE", REBALANCES)}
        {input("capital", "INITIAL CAPITAL", { type: "number" })}
        {input("maxPositions", "MAX POSITIONS", { type: "number", placeholder: "no cap" })}
        <label className="flex items-center gap-1.5 text-[12px]">
          <input type="checkbox" checked={form.longOnly} onChange={(e) => set("longOnly", e.target.checked)} /> long only
        </label>
      </fieldset>
      <fieldset className="flex flex-col gap-2 rounded-[4px] border border-border-subtle p-2.5">
        <legend className="px-1 text-[9px] tracking-[1px] text-text-subtle">SIGNAL &amp; COSTS</legend>
        {select("kind", "SIGNAL", SIGNAL_KINDS)}
        {input("lookback", "LOOKBACK (bars)", { type: "number" })}
        {input("skip", "SKIP (bars)", { type: "number" })}
        <div className="grid grid-cols-2 gap-1.5">
          {input("fast", "SMA FAST", { type: "number" })}
          {input("slow", "SMA SLOW", { type: "number" })}
        </div>
        {input("topN", "TOP N", { type: "number" })}
        {select("weighting", "WEIGHTING", WEIGHTINGS)}
        {input("expression", "EXPRESSION", { placeholder: "rank(mom_12_1) (expression signals only)" })}
        {input("filterExpression", "FILTER EXPRESSION", { placeholder: "optional filter expression" })}
        <div className="grid grid-cols-3 gap-1.5">
          {input("commission", "COMMISSION BPS", { type: "number" })}
          {input("slippage", "SLIPPAGE BPS", { type: "number" })}
          {input("borrow", "BORROW BPS", { type: "number" })}
        </div>
      </fieldset>
    </div>
  );
}

/** What the run will be judged by, and on what data — both read live. */
function JudgementNote() {
  const { gates, quality } = useJudgement();
  const scope = useActiveScope();
  const q = quality.data;
  const parts: string[] = [];
  const active = scope.data?.universe;
  if (active) parts.push(`${active.name} · ${active.member_count || active.symbol_count || 0} members`);
  if (q) {
    parts.push(`store quality ${q.overall_quality_pct === null ? "not measured" : `${num(q.overall_quality_pct, 1)}%`}`);
    parts.push(`${q.stale_series ?? 0} stale series`);
    parts.push(`${q.open_incidents ?? 0} open incidents`);
    parts.push(`${q.datasets_in_store ?? 0} datasets in store`);
  }
  const names = gates.data?.gates.map((g) => g.name) ?? [];
  return (
    <div className="my-2.5 rounded-[4px] border border-accent/40 bg-accent-dim p-2.5" aria-label="Judged by">
      <h4 className="mb-1 text-[9px] tracking-[1px] text-accent">THIS RUN WILL BE JUDGED BY</h4>
      <span>{names.length ? names.join(" · ") : "the gate suite could not be loaded"}</span>
      <Foot className="mt-1">{parts.join(" · ") || "no data-quality figures available"}</Foot>
    </div>
  );
}

export function ExperimentForm({ seed }: { seed: ExperimentSeed }) {
  const lab = useLab();
  const toast = useToast();
  const scope = useActiveScope();
  const { hypotheses } = useLabData();
  const { create } = useExperimentActions();
  const runner = useStartRun();
  const clone = seed.clone;
  const activeUniverse = scope.data?.universe?.id ?? "";
  const [config, setConfig] = useState<ConfigForm | null>(null);
  // Until touched, the editor follows the active universe as it loads.
  const form = config ?? configForm(clone ? clone.config : null, activeUniverse);
  const set = <K extends keyof ConfigForm>(key: K, value: ConfigForm[K]) => setConfig({ ...form, [key]: value });
  const [meta, setMeta] = useState({
    name: clone ? `${clone.name} (clone)` : "",
    hypothesis: seed.hypothesis ?? clone?.hypothesis_id ?? "",
    kind: clone?.kind ?? "combined",
    horizon: String(clone?.horizon ?? 21),
    owner: clone?.owner ?? "",
    notes: clone?.notes ?? "",
    minSharpe: String(clone?.criteria?.min_oos_sharpe ?? 0.8),
    maxDd: String(clone?.criteria?.max_drawdown ?? 0.15),
    minTrades: String(clone?.criteria?.min_trades ?? 30),
  });
  const m = (key: keyof typeof meta) => (e: { target: { value: string } }) => setMeta((v) => ({ ...v, [key]: e.target.value }));

  const submit = (runAfter: boolean) => {
    const cfg = readConfig(form);
    const name = meta.name.trim() || form.name.trim();
    if (!name) return toast.warn("The experiment needs a name.");
    cfg.name = name;
    if (!cfg.universe_id && !(cfg.symbols as string[]).length) return toast.warn("Choose a universe or list explicit symbols.");
    create.mutate(
      {
        name,
        config: cfg,
        hypothesis_id: meta.hypothesis || null,
        kind: meta.kind,
        horizon: Number(meta.horizon) || 21,
        owner: meta.owner.trim() || null,
        notes: meta.notes.trim() || null,
        criteria: { min_oos_sharpe: Number(meta.minSharpe), max_drawdown: Number(meta.maxDd), min_trades: Number(meta.minTrades) },
      },
      {
        onSuccess: (created) => {
          lab.setDraft(null);
          lab.go("experiments", created.id);
          if (runAfter) runner.start(created);
        },
      },
    );
  };

  return (
    <Panel title="New experiment" actions={<span className="text-text-subtle">draft</span>}>
      <form
        aria-label="New experiment"
        onSubmit={(e) => {
          e.preventDefault();
          submit(true);
        }}
      >
        <div className="grid grid-cols-2 gap-2">
          <Field label="NAME · REQUIRED" htmlFor="x-name">
            <Input id="x-name" placeholder="mom_crowd_cond_v2" value={meta.name} onChange={m("name")} />
          </Field>
          <Field label="HYPOTHESIS" htmlFor="x-hypothesis">
            <Select id="x-hypothesis" value={meta.hypothesis} onChange={m("hypothesis")}>
              <option value="">— none —</option>
              {(hypotheses.data?.hypotheses ?? []).map((h) => (
                <option key={h.id} value={h.id}>
                  {h.id} · {h.title}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="KIND" htmlFor="x-kind">
            <Select id="x-kind" value={meta.kind} onChange={m("kind")}>
              {["combined", "backtest", "signal"].map((k) => (
                <option key={k}>{k}</option>
              ))}
            </Select>
          </Field>
          <Field label="IC HORIZON (bars)" htmlFor="x-horizon">
            <Input id="x-horizon" type="number" value={meta.horizon} onChange={m("horizon")} />
          </Field>
          <Field label="OWNER" htmlFor="x-owner">
            <Input id="x-owner" placeholder="owner" value={meta.owner} onChange={m("owner")} />
          </Field>
        </div>
        <Field label="NOTES" htmlFor="x-notes" className="mt-2">
          <Textarea id="x-notes" rows={2} placeholder="What is this experiment testing?" value={meta.notes} onChange={m("notes")} />
        </Field>
        <h4 className="mt-2.5 mb-1.5 text-[9px] tracking-[1px] text-text-subtle">CONFIGURATION · PINNED WITH THE EXPERIMENT</h4>
        <ConfigEditor form={form} set={set} />
        <div className="mt-2.5 grid grid-cols-3 gap-2">
          <Field label="ACCEPT · MIN OOS SHARPE" htmlFor="x-sharpe">
            <Input id="x-sharpe" type="number" step="0.1" value={meta.minSharpe} onChange={m("minSharpe")} />
          </Field>
          <Field label="ACCEPT · MAX DRAWDOWN" htmlFor="x-dd">
            <Input id="x-dd" type="number" step="0.01" value={meta.maxDd} onChange={m("maxDd")} />
          </Field>
          <Field label="ACCEPT · MIN TRADES" htmlFor="x-trades">
            <Input id="x-trades" type="number" value={meta.minTrades} onChange={m("minTrades")} />
          </Field>
        </div>
        <JudgementNote />
        <div className="flex gap-2">
          <Button size="sm" type="submit" variant="primary" disabled={create.isPending}>
            {create.isPending ? "▷ SAVING…" : "▷ SAVE AND RUN"}
          </Button>
          <Button size="sm" type="button" disabled={create.isPending} onClick={() => submit(false)}>
            SAVE EXPERIMENT
          </Button>
          <Button size="sm" type="button" variant="ghost" className="ml-auto" onClick={() => lab.setDraft(null)}>
            CANCEL
          </Button>
        </div>
        <Foot className="mt-1.5">Running executes a real backtest over stored bars plus an information-coefficient study; nothing is simulated.</Foot>
      </form>
    </Panel>
  );
}
