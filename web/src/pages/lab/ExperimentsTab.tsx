/* Experiments (COVERAGE D): the filtered list with compare checkboxes, one
 * experiment's figures, strategy, criteria, backtests, analyses and runs with
 * the live log, the compare view, and the screens that feed the lab. */
import { useEffect, useRef, useState } from "react";
import { errorReason } from "../../api/errors";
import { useActiveScope, useUniverses } from "../../api/hooks";
import type { Backtest, Experiment } from "../../api/schemas/lab";
import { openLegacy } from "../../shell/legacy";
import { useToast } from "../../shell/toast-context";
import { etClock, int, num } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Button } from "../../ui/primitives/button";
import { Panel } from "../../ui/primitives/card";
import { ConfirmDialog } from "../../ui/primitives/confirm";
import { Input, Select } from "../../ui/primitives/field";
import { ExperimentForm } from "./ExperimentForm";
import { useAnalyses, useExperimentActions, useExperimentDetail, useLabData, useScreenActions, useScreening, useStartRun } from "./hooks";
import { EmptyBox, Foot, Kv, Metric, Pill, Ref, RuleRow, Section } from "./parts";
import {
  compareRows,
  configDiff,
  deflatedOf,
  filterExperiments,
  judgedWindow,
  newestByExperiment,
  originOf,
  percent,
  ratio,
  reportsByBacktest,
  runTone,
  stageTone,
  toneText,
  strategyRules,
  when,
  type CompareItem,
  type Origin,
  type Tone,
} from "./rules";
import { useLab } from "./state";
import { legacyLab } from "./tabs";

const MAX_COMPARED = 4;

function useUniverseName() {
  const universes = useUniverses();
  return (id: string | null | undefined) => (id ? (universes.data?.universes.find((u) => u.id === id)?.name ?? id) : "—");
}

function ExperimentList({ selected, onNew, onCompare }: { selected: string | null; onNew: () => void; onCompare: () => void }) {
  const lab = useLab();
  const { experiments, backtests, reports } = useLabData();
  const [query, setQuery] = useState("");
  const [origin, setOrigin] = useState<"all" | Origin>("all");
  const all = experiments.data?.experiments ?? [];
  const runs = backtests.data?.backtests ?? [];
  const rows = filterExperiments(all, runs, { stage: lab.stage, query, origin });
  const newest = newestByExperiment(runs);
  const byBacktest = reportsByBacktest(reports.data?.reports ?? []);
  return (
    <Panel
      title="Experiments"
      actions={<span className="text-[11px] text-text-muted">{experiments.isError ? "—" : `${rows.length} of ${all.length}`}</span>}
      bodyClassName="flex flex-col gap-2"
    >
      {lab.stage && (
        <div className="flex items-center justify-between">
          <button type="button" className="rounded-[4px] border border-accent bg-accent-dim px-2 py-0.5 text-[11px] text-accent" onClick={() => lab.setStage(null)}>
            reached {lab.stage} ✕
          </button>
          <Foot>click again to clear</Foot>
        </div>
      )}
      <div className="flex gap-2">
        <Input aria-label="Filter experiments" placeholder="Filter by name, hypothesis, universe…" value={query} onChange={(e) => setQuery(e.target.value)} />
        <Select aria-label="Origin" value={origin} onChange={(e) => setOrigin(e.target.value as "all" | Origin)} wrapperClassName="w-[118px] shrink-0">
          <option value="all">All origins</option>
          <option value="manual">Manual</option>
          <option value="agent">Agent</option>
          <option value="screen">Screen</option>
        </Select>
      </div>
      {experiments.isError ? (
        <p className="text-warn">Experiments could not be loaded: {errorReason(experiments.error)}</p>
      ) : experiments.isPending ? (
        <Foot>Loading experiments…</Foot>
      ) : !rows.length ? (
        <EmptyBox
          headline={all.length ? "Nothing at this filter" : "No experiments yet"}
          detail={all.length ? "Experiments arrive here once they exist at this stage." : "An experiment pins a reproducible backtest configuration to a hypothesis."}
          action={
            <Button size="sm" variant="primary" onClick={onNew}>
              + NEW EXPERIMENT
            </Button>
          }
        />
      ) : (
        <ul aria-label="Experiment list" className="flex flex-col gap-1">
          {rows.map((exp) => {
            const record = newest[exp.id];
            const report = record ? byBacktest[record.id] : undefined;
            const from = originOf(exp, runs);
            const on = exp.id === selected && !lab.draft && !lab.comparing;
            return (
              <li key={exp.id} className={cn("flex items-start gap-2 rounded-[4px] border p-2", on ? "border-accent bg-accent-dim" : "border-border-subtle")}>
                <input
                  type="checkbox"
                  className="mt-1"
                  aria-label={`Compare ${exp.name}`}
                  checked={lab.compare.has(exp.id)}
                  onChange={(e) => lab.setCompared(exp.id, e.target.checked)}
                />
                <button
                  type="button"
                  className="min-w-0 flex-1 text-left"
                  aria-current={on ? "true" : undefined}
                  onClick={() => {
                    lab.setDraft(null);
                    lab.setComparing(false);
                    lab.go("experiments", exp.id);
                  }}
                >
                  <div className="flex items-center gap-2">
                    <span className="text-[12px] text-text-strong">{exp.name}</span>
                    <Pill tone={stageTone(exp.stage)} className="ml-auto">
                      {exp.stage.toUpperCase()}
                    </Pill>
                  </div>
                  <Foot className="mt-0.5">
                    <Pill tone={from === "agent" ? "accent" : null}>{from.toUpperCase()}</Pill> {exp.hypothesis_id || "no hypothesis"} · Sharpe{" "}
                    {ratio(judgedWindow(record)?.sharpe)} · gates {report ? `${report.gates_passed}/${report.gates_total}` : "—"}
                  </Foot>
                </button>
              </li>
            );
          })}
        </ul>
      )}
      <div className="flex items-center gap-1.5">
        <Button size="sm" onClick={onCompare}>
          COMPARE SELECTED ({lab.compare.size})
        </Button>
        <Foot className="ml-auto">select 2–4</Foot>
      </div>
      <Button size="sm" variant="ghost" onClick={onNew}>
        + NEW EXPERIMENT
      </Button>
    </Panel>
  );
}

function StrategyPanel({ exp }: { exp: Experiment }) {
  const [view, setView] = useState<"rules" | "code">("rules");
  const chip = (value: "rules" | "code", label: string) => (
    <button
      type="button"
      aria-pressed={view === value}
      onClick={() => setView(value)}
      className={cn("rounded-[3px] border px-1.5 text-[9px]", view === value ? "border-accent text-accent" : "border-border-subtle text-text-muted")}
    >
      {label}
    </button>
  );
  return (
    <Section label="Strategy" actions={<>{chip("rules", "RULES")}{chip("code", "CODE")}</>}>
      {view === "rules" ? (
        <>
          {strategyRules(exp.config).map((rule, i) => (
            <RuleRow key={i} parts={rule} />
          ))}
          <Foot>Rules are read from the persisted configuration — this is exactly what the run executed.</Foot>
        </>
      ) : (
        <>
          <pre className="max-h-72 overflow-auto rounded-[4px] bg-inset p-2 text-[10px]">{JSON.stringify(exp.config, null, 2)}</pre>
          <Foot className="mt-1.5">The stored BacktestConfig, verbatim. Re-running this JSON reproduces the run.</Foot>
        </>
      )}
    </Section>
  );
}

function BacktestsPanel({ exp, runs }: { exp: Experiment; runs: Backtest[] }) {
  const lab = useLab();
  const { reports } = useLabData();
  const { backtest } = useExperimentActions();
  const byBacktest = reportsByBacktest(reports.data?.reports ?? []);
  return (
    <Section label="Backtests">
      {!runs.length ? (
        <Foot className="py-1.5">No backtest has been run for this experiment yet.</Foot>
      ) : (
        <table className="w-full text-[11px]" aria-label="Backtests">
          <thead className="text-[9px] text-text-muted">
            <tr>
              {["RUN", "DATE", "SHARPE", "DEFL", "MAX DD", "STATUS"].map((h, i) => (
                <th key={h} className={cn("px-1 py-1 font-normal", i < 2 ? "text-left" : "text-right")}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {runs.map((row) => (
              <tr key={row.id} className="border-t border-border-subtle">
                <td className="px-1 py-1">
                  <Ref onClick={() => lab.go("backtests", row.id)}>{row.id}</Ref>
                </td>
                <td className="px-1 py-1 text-text-subtle">{when(row.finished_at || row.created_at)}</td>
                <td className="px-1 py-1 text-right">{ratio(judgedWindow(row)?.sharpe)}</td>
                <td className="px-1 py-1 text-right">{ratio(deflatedOf(byBacktest[row.id]))}</td>
                <td className="px-1 py-1 text-right">{row.stats ? percent(row.stats.max_drawdown) : "—"}</td>
                <td className={cn("px-1 py-1 text-right", toneText(runTone(row.status)))}>{row.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Button size="sm" variant="ghost" className="mt-1.5" disabled={backtest.isPending} onClick={() => backtest.mutate(exp)}>
        {backtest.isPending ? "▷ RUNNING BACKTEST…" : "▷ RUN BACKTEST WITH CURRENT CONFIG"}
      </Button>
    </Section>
  );
}

/** The analysis payload names its chip tone in the old console's classes. */
const ANALYSIS_TONE: Record<string, Tone> = { ok: "up", grn: "up", bad: "down", red: "down", warn: "warn", blu: "accent" };
const ANALYSIS_LABEL: Record<string, string> = { sweep: "SWEEP", walk_forward: "WALK-FORWARD", monte_carlo: "MONTE CARLO" };

/** Analyses open and start in the current console until S5. */
function AnalysesPanel({ exp, record, runs }: { exp: Experiment; record: Backtest | undefined; runs: Backtest[] }) {
  const analyses = useAnalyses();
  const open = () => openLegacy(legacyLab("experiments", exp.id));
  if (analyses.isError) {
    return (
      <Section label="Analyses">
        <p className="text-warn">Analyses could not be loaded: {errorReason(analyses.error)}</p>
      </Section>
    );
  }
  const mine = (analyses.data?.analyses ?? []).filter((a) => a.experiment_id === exp.id || (a.backtest_id && runs.some((b) => b.id === a.backtest_id)));
  return (
    <Section label="Analyses">
      <div className="mt-1 flex flex-wrap gap-1.5">
        {!mine.length && <Foot>No analyses yet — a single run is one observation.</Foot>}
        {mine.map((a) => (
          <button key={a.id} type="button" className={cn("rounded-[4px] border border-border-subtle px-2 py-0.5 text-[10px]", toneText(ANALYSIS_TONE[a.tone ?? ""]))} onClick={open}>
            {ANALYSIS_LABEL[a.kind] ?? a.kind.toUpperCase()} {a.chip ?? ""} ↗
          </button>
        ))}
      </div>
      {!record ? (
        <p className="mt-2 rounded-[4px] border border-border-subtle p-2 text-[11px]">Run a backtest first — every analysis re-uses the run's configuration and its price panel.</p>
      ) : (
        <>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {["+ SWEEP", "+ MONTE CARLO", "+ WALK-FORWARD"].map((label) => (
              <Button key={label} size="sm" onClick={open}>
                {label} ↗
              </Button>
            ))}
          </div>
          <Foot className="mt-1.5">
            Sweep and walk-forward re-run real backtests over {record.id}'s price panel; Monte Carlo block-bootstraps its return series. Sweep cells count as trials in the
            deflation ledger — resamples do not.
          </Foot>
        </>
      )}
    </Section>
  );
}

function RunLog({ experimentId }: { experimentId: string }) {
  const { log } = useLab();
  const box = useRef<HTMLDivElement>(null);
  const lines = log.experimentId === experimentId ? log.lines : [];
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines.length]);
  return (
    <>
      <h4 className="mt-1 text-[9px] tracking-[1px] text-text-subtle">RUN LOG · LIVE</h4>
      <div ref={box} role="log" aria-label="Run log" className="max-h-[190px] overflow-auto rounded-[4px] bg-inset p-2 text-[10px] whitespace-pre-wrap">
        {!lines.length ? (
          <span className="text-text-subtle">No run log yet. Running the experiment streams its log here live.</span>
        ) : (
          lines.map((line, i) => {
            const time = new Date(line.ts);
            return (
              <div key={i}>
                <span className={line.level === "error" ? "text-down" : line.level === "warn" ? "text-warn" : "text-text-subtle"}>
                  {Number.isNaN(time.getTime()) ? "--:--:--" : etClock(time)}{" "}
                </span>
                <span>{line.message}</span>
              </div>
            );
          })
        )}
      </div>
    </>
  );
}

function RunsPanel({ exp }: { exp: Experiment }) {
  const detail = useExperimentDetail(exp.id);
  const runner = useStartRun();
  const running = runner.pending(exp.id);
  const start = () => runner.start(exp);
  return (
    <Section label="Experiment runs" actions={<Foot>backtest + information coefficient</Foot>}>
      {detail.isError ? (
        <p className="text-warn">Runs could not be loaded: {errorReason(detail.error)}</p>
      ) : detail.isPending ? (
        <Foot>Loading runs…</Foot>
      ) : !detail.data.runs.length ? (
        <Foot className="py-1">Never run.</Foot>
      ) : (
        <table className="w-full text-[11px]" aria-label="Experiment runs">
          <thead className="text-[9px] text-text-muted">
            <tr>
              {["RUN", "STARTED", "IC", "SECONDS", "ACCEPTED", "STATUS"].map((h, i) => (
                <th key={h} className={cn("px-1 py-1 font-normal", i < 2 ? "text-left" : "text-right")}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {detail.data.runs.map((row) => {
              const ic = row.results?.ic?.mean_ic;
              const passed = row.results?.passed_gates;
              return (
                <tr key={row.id} className="border-t border-border-subtle">
                  <td className="px-1 py-1 text-text-strong">{row.id}</td>
                  <td className="px-1 py-1 text-text-subtle">{when(row.started_at)}</td>
                  <td className="px-1 py-1 text-right">{ic === null || ic === undefined ? "—" : num(ic, 4)}</td>
                  <td className="px-1 py-1 text-right">{row.duration_ms ? num(row.duration_ms / 1000, 2) : "—"}</td>
                  <td className={cn("px-1 py-1 text-right", passed === true ? "text-up" : passed === false ? "text-warn" : undefined)}>
                    {passed === null || passed === undefined ? "—" : passed ? "PASS" : "NOT MET"}
                  </td>
                  <td className={cn("px-1 py-1 text-right", toneText(runTone(row.status)))}>{row.status}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <div className="my-2 flex items-center gap-2">
        <Button size="sm" variant="primary" disabled={running} onClick={start}>
          {running ? "▷ RUNNING…" : "▷ RUN EXPERIMENT"}
        </Button>
        <Foot className="ml-auto">Runs a real backtest over stored bars and a real IC study; the log streams below.</Foot>
      </div>
      <RunLog experimentId={exp.id} />
    </Section>
  );
}

function ExperimentDetail({ exp }: { exp: Experiment }) {
  const lab = useLab();
  const toast = useToast();
  const universeName = useUniverseName();
  const { backtests, reports } = useLabData();
  const { remove } = useExperimentActions();
  const [deleting, setDeleting] = useState(false);
  const all = backtests.data?.backtests ?? [];
  const runs = all.filter((b) => b.experiment_id === exp.id);
  const record = newestByExperiment(all)[exp.id];
  const report = record ? reportsByBacktest(reports.data?.reports ?? [])[record.id] : undefined;
  const window = judgedWindow(record);
  const stats = record?.stats;
  const symbols = Array.isArray(exp.config.symbols) ? exp.config.symbols.length : 0;
  const criteria = exp.criteria ?? {};
  return (
    <Panel title="Experiment" accent={false}>
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="text-[15px] font-semibold text-text-strong">{exp.name}</span>
        <Pill tone={stageTone(exp.stage)}>{exp.stage.toUpperCase()}</Pill>
        <Foot className="ml-auto">
          hypothesis{" "}
          {exp.hypothesis_id ? (
            <Ref onClick={() => lab.go("hypotheses", exp.hypothesis_id)}>{exp.hypothesis_id}</Ref>
          ) : (
            "none"
          )}
        </Foot>
      </div>
      <Foot className="mt-0.5 mb-2.5">
        {exp.id} · {exp.signal || "no signal"} · {exp.universe_id ? universeName(exp.universe_id) : `${symbols} symbols`} · created {when(exp.created_at)}
      </Foot>
      <div className="mb-2.5 flex flex-wrap gap-5">
        <Metric label="OOS SHARPE" value={window ? ratio(window.sharpe) : "—"} tone={window ? "up" : "muted"} />
        <Metric label="DEFLATED" value={report ? ratio(deflatedOf(report)) : "—"} tone={report ? null : "muted"} />
        <Metric label="MAX DD" value={stats ? percent(stats.max_drawdown) : "—"} tone={stats ? "down" : "muted"} />
        <Metric label="TRADES" value={stats ? int(stats.trades as number) : "—"} tone={stats ? "accent" : "muted"} />
        <Metric label="UNIVERSE" value={exp.universe_id ? universeName(exp.universe_id) : "explicit"} />
      </div>
      {!record && <p className="mb-2.5 rounded-[4px] border border-border-subtle p-2 text-[11px]">No completed backtest yet — every figure above stays empty until this experiment runs.</p>}
      <StrategyPanel exp={exp} />
      <Section label="Acceptance criteria · applied to every run">
        <Kv k="Min OOS Sharpe">{ratio(criteria.min_oos_sharpe)}</Kv>
        <Kv k="Max drawdown">{percent(criteria.max_drawdown)}</Kv>
        <Kv k="Min trades">{int(criteria.min_trades ?? null)}</Kv>
        <Kv k="IC horizon">{`${exp.horizon} bars`}</Kv>
        <Kv k="Kind">{exp.kind.toUpperCase()}</Kv>
      </Section>
      <BacktestsPanel exp={exp} runs={runs} />
      <AnalysesPanel exp={exp} record={record} runs={runs} />
      <RunsPanel exp={exp} />
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="primary" disabled={!record} onClick={() => lab.go("pipeline", record?.id)}>
          ⑂ SEND TO PIPELINE ↗
        </Button>
        <Button size="sm" onClick={() => lab.setDraft({ clone: exp, hypothesis: null })}>
          CLONE &amp; EDIT
        </Button>
        <Button
          size="sm"
          onClick={() => {
            lab.setCompared(exp.id, true);
            const size = lab.compare.has(exp.id) ? lab.compare.size : lab.compare.size + 1;
            if (size < 2) return toast.warn("Tick a second experiment in the list to compare.");
            lab.setComparing(true);
          }}
        >
          COMPARE WITH…
        </Button>
        <Button size="sm" variant="danger" onClick={() => setDeleting(true)}>
          DELETE
        </Button>
      </div>
      <ConfirmDialog
        open={deleting}
        title="Delete experiment"
        confirmLabel="Delete"
        onCancel={() => setDeleting(false)}
        onConfirm={async () => {
          await remove.mutateAsync(exp).catch(() => undefined);
          setDeleting(false);
          lab.go("experiments");
        }}
      >
        Delete experiment {exp.name} and its runs?
      </ConfirmDialog>
    </Panel>
  );
}

function CompareView() {
  const lab = useLab();
  const { experiments, backtests, reports } = useLabData();
  const all = experiments.data?.experiments ?? [];
  const newest = newestByExperiment(backtests.data?.backtests ?? []);
  const byBacktest = reportsByBacktest(reports.data?.reports ?? []);
  const items: CompareItem[] = [...lab.compare]
    .slice(0, MAX_COMPARED)
    .flatMap((id) => {
      const experiment = all.find((e) => e.id === id);
      if (!experiment) return [];
      const record = newest[id] ?? null;
      return [{ experiment, record, report: record ? (byBacktest[record.id] ?? null) : null }];
    });
  const rows = compareRows(items);
  const diff = configDiff(items);
  const head = (
    <tr>
      <th className="w-[22%] px-1 py-1 text-left font-normal">METRIC</th>
      {items.map((i) => (
        <th key={i.experiment.id} className="px-1 py-1 text-right font-normal">
          {i.experiment.name}
        </th>
      ))}
    </tr>
  );
  return (
    <div>
      <div className="mb-2.5 flex flex-wrap items-center gap-2.5">
        <Button size="sm" onClick={() => lab.setComparing(false)}>
          ← BACK TO EXPERIMENTS
        </Button>
        <span className="text-[15px] font-semibold text-text-strong">Comparing {items.length} experiments</span>
        <Foot className="ml-auto">values are each experiment's newest completed backtest</Foot>
      </div>
      <div className="mb-3 grid grid-cols-[minmax(0,3fr)_minmax(0,2fr)] gap-3 max-[1000px]:grid-cols-1">
        <Panel title="Metrics">
          <table className="w-full text-[11px]" aria-label="Compared metrics">
            <thead className="text-[9px] text-text-muted">{head}</thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.label} className="border-t border-border-subtle">
                  <td className="px-1 py-1 text-text-muted">{row.label}</td>
                  {row.text.map((t, i) => (
                    <td key={i} className={cn("px-1 py-1 text-right", row.bestIndex.includes(i) && "text-up")}>
                      {t}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
        <Panel title="Config diff">
          <table className="w-full text-[11px]" aria-label="Config diff">
            <thead className="text-[9px] text-text-muted">
              <tr>
                <th className="w-[22%] px-1 py-1 text-left font-normal">PARAM</th>
                {items.map((i) => (
                  <th key={i.experiment.id} className="px-1 py-1 text-right font-normal">
                    {i.experiment.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {diff.keys.map((key) => (
                <tr key={key} className="border-t border-border-subtle">
                  <td className="px-1 py-1 text-text-muted">{key}</td>
                  {diff.values.map((map, i) => (
                    <td key={i} className="px-1 py-1 text-right text-text-strong">
                      {map[key] === undefined ? "—" : String(map[key])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <Foot className="mt-1.5">{diff.identical} identical fields hidden.</Foot>
        </Panel>
      </div>
      <Panel title="Verdict · gates passed">
        {items.map((i) => (
          <Kv key={i.experiment.id} k={<span className="text-text-strong">{i.experiment.name}</span>}>
            {i.report ? (
              <span className={i.report.passed ? "text-up" : "text-warn"}>
                {i.report.gates_passed} / {i.report.gates_total} · {i.report.verdict.toUpperCase()}
              </span>
            ) : (
              <span className="text-text-subtle">never validated</span>
            )}
          </Kv>
        ))}
        <Foot className="mt-1.5 border-t border-border-subtle pt-1.5">Gate counts come from each run's stored validation report.</Foot>
      </Panel>
    </div>
  );
}

function Screening() {
  const lab = useLab();
  const scope = useActiveScope();
  const universeName = useUniverseName();
  const { screens, runs } = useScreening();
  const actions = useScreenActions();
  const latest: Record<string, { ran_at: string | null; pass_count: number; base_count: number }> = {};
  for (const run of runs.data?.runs ?? []) if (run.screen_id && !latest[run.screen_id]) latest[run.screen_id] = run;
  return (
    <Panel title="Screening · survivors feeding the lab" actions={<Foot>universe {scope.data?.universe?.name ?? "—"}</Foot>} className="mt-3">
      {screens.isError ? (
        <p className="text-warn">Screens could not be loaded: {errorReason(screens.error)}</p>
      ) : screens.isPending ? (
        <Foot>Loading screens…</Foot>
      ) : !screens.data.screens.length ? (
        <Foot>No screens saved yet. Build one in Market &amp; Data → Screener; its survivors can be queued as a lab backtest.</Foot>
      ) : (
        <>
          <table className="w-full text-[11px]" aria-label="Screens">
            <thead className="text-[9px] text-text-muted">
              <tr>
                {["SCREEN", "UNIVERSE", "RULES", "LAST RUN", "SURVIVORS", ""].map((h, i) => (
                  <th key={i} className={cn("px-1 py-1 font-normal", i === 2 || i === 4 ? "text-right" : "text-left")}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {screens.data.screens.map((screen) => {
                const run = latest[screen.id];
                const count = (screen as { active_rule_count?: number }).active_rule_count;
                return (
                  <tr key={screen.id} className="border-t border-border-subtle">
                    <td className="px-1 py-1 text-text-strong">{screen.name}</td>
                    <td className="px-1 py-1 text-text-subtle">{universeName(screen.universe_id)}</td>
                    <td className="px-1 py-1 text-right">{count ?? "—"}</td>
                    <td className="px-1 py-1 text-text-subtle">{run ? when(run.ran_at) : "never"}</td>
                    <td className="px-1 py-1 text-right text-text-strong">{run ? `${run.pass_count} of ${run.base_count}` : "—"}</td>
                    <td className="px-1 py-1">
                      <div className="flex justify-end gap-1.5">
                        <Button size="sm" disabled={actions.run.isPending} onClick={() => actions.run.mutate(screen)}>
                          RUN
                        </Button>
                        <Button
                          size="sm"
                          disabled={actions.queue.isPending}
                          onClick={() =>
                            actions.queue.mutate(screen, {
                              onSuccess: (request) => {
                                if (request.backtest_id) lab.go("backtests", request.backtest_id);
                              },
                            })
                          }
                        >
                          → BACKTEST ↗
                        </Button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <Foot className="mt-1.5">Counts are the survivor counts of the screen's last persisted run — never an estimate.</Foot>
        </>
      )}
    </Panel>
  );
}

export function ExperimentsTab({ id }: { id: string | undefined }) {
  const lab = useLab();
  const toast = useToast();
  const { experiments } = useLabData();
  const list = experiments.data?.experiments ?? [];
  const exp = list.find((e) => e.id === id) ?? (id ? undefined : list[0]);
  const startNew = () => {
    lab.setComparing(false);
    lab.setDraft({ clone: null, hypothesis: null });
  };
  const compare = () => {
    if (lab.compare.size < 2) return toast.warn("Select 2–4 experiments to compare.");
    lab.setComparing(true);
    window.scrollTo?.(0, 0);
  };
  return (
    <>
      {lab.comparing ? (
        <CompareView />
      ) : (
        <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-3 max-[1000px]:grid-cols-1">
          <ExperimentList selected={exp?.id ?? null} onNew={startNew} onCompare={compare} />
          {lab.draft ? (
            <ExperimentForm key={`${lab.draft.clone?.id ?? "new"}-${lab.draft.hypothesis ?? ""}`} seed={lab.draft} />
          ) : exp ? (
            <ExperimentDetail exp={exp} />
          ) : (
            <Panel title="Experiment" accent={false}>
              <Foot>Select an experiment on the left, or create one.</Foot>
            </Panel>
          )}
        </div>
      )}
      <Screening />
    </>
  );
}
