/* The screener (COVERAGE F.2): saved screens, a rule builder with real
 * per-rule funnel counts, run, results and profile, the backtest hand-off,
 * save as universe, and pinned membership alerts. */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useAction } from "../../api/actions";
import { useApi } from "../../api/context";
import { errorReason } from "../../api/errors";
import { useSettings } from "../../api/hooks";
import type { MetricDef, Screen, ScreenRun } from "../../api/schemas/screens";
import { Notification } from "../../api/schemas/stream";
import { useChannel } from "../../live/context";
import { openLegacy } from "../../shell/legacy";
import { useToast } from "../../shell/toast-context";
import { ago, int, num, pct, usd } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Button } from "../../ui/primitives/button";
import { Panel } from "../../ui/primitives/card";
import { Input, Select } from "../../ui/primitives/field";
import { dataKeys, useAcknowledgeAlert, useCollections, usePreviewFunnel, useScreenerData } from "./hooks";
import {
  apiRules,
  defaultRules,
  editableRules,
  inputValue,
  isPercentile,
  isSet,
  operatorLabel,
  ranked,
  unitFor,
  withField,
  withOperator,
  withValue,
  type EditableRule,
} from "./rules";

const FUNNEL_DEBOUNCE_MS = 350;
const SHOWN_RESULTS = 12;

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

function RuleRow({
  rule,
  index,
  metrics,
  operators,
  scopes,
  funnel,
  funnelFailed,
  onChange,
  onDrop,
}: {
  rule: EditableRule;
  index: number;
  metrics: readonly MetricDef[];
  operators: readonly string[];
  scopes: readonly string[];
  funnel: { before: number; after: number } | null;
  funnelFailed: boolean;
  onChange: (rule: EditableRule) => void;
  onDrop: () => void;
}) {
  const def = metrics.find((m) => m.key === rule.field);
  const unit = unitFor(rule, def);
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border-subtle py-1.5" aria-label={`Rule ${index + 1}`} role="group">
      <div className="flex flex-wrap items-center gap-1.5">
        <Select size="sm" aria-label="Metric" wrapperClassName="w-36" value={rule.field} onChange={(e) => onChange(withField(rule, e.target.value, metrics.find((m) => m.key === e.target.value)))}>
          {metrics.map((m) => (
            <option key={m.key} value={m.key} title={m.description ?? ""}>
              {m.key}
            </option>
          ))}
        </Select>
        <Select size="sm" aria-label="Operator" wrapperClassName="w-28" value={rule.operator} onChange={(e) => onChange(withOperator(rule, e.target.value))}>
          {operators.map((op) => (
            <option key={op} value={op}>
              {operatorLabel(op)}
            </option>
          ))}
        </Select>
        <Input size="sm" aria-label="Value" className={isSet(rule.operator) ? "w-32" : "w-20"} value={inputValue(rule, def)} onChange={(e) => onChange(withValue(rule, e.target.value, def))} />
        {rule.operator === "between" && (
          <>
            <span className="text-[10px] text-text-subtle">and</span>
            <Input size="sm" aria-label="Upper value" className="w-20" value={String(rule.value2 ?? "")} onChange={(e) => onChange({ ...rule, value2: Number(e.target.value) })} />
          </>
        )}
        {unit && <span className="text-[10px] text-text-subtle">{unit}</span>}
        {isPercentile(rule.operator) && (
          <Select size="sm" aria-label="Scope" wrapperClassName="w-32" value={rule.scope ?? "all"} onChange={(e) => onChange({ ...rule, scope: e.target.value })}>
            {scopes.map((s) => (
              <option key={s} value={s}>
                {s === "sector" ? "within sector" : "overall"}
              </option>
            ))}
          </Select>
        )}
        {def && <span className="text-[10px] text-text-subtle">← {def.source}</span>}
      </div>
      <span className="flex items-center gap-1.5">
        <span className={cn("mono text-[11px]", funnelFailed && "text-down")} aria-label="Funnel">
          {funnelFailed ? "preview failed" : funnel ? (
            <>
              {int(funnel.before)} → <b className="text-text-strong">{int(funnel.after)}</b>
            </>
          ) : (
            "…"
          )}
        </span>
        <button type="button" aria-label="Remove rule" className="px-1 text-text-subtle hover:text-down" onClick={onDrop}>
          ×
        </button>
      </span>
    </div>
  );
}

export function ScreenerTab() {
  const api = useApi();
  const toast = useToast();
  const client = useQueryClient();
  const settings = useSettings();
  const collections = useCollections();
  const { metrics, screens, alerts } = useScreenerData();
  const acknowledge = useAcknowledgeAlert();

  const universes = collections.data?.universes.universes ?? [];
  const [universeId, setUniverseId] = useState<string | null>(null);
  const baseUniverse = universeId ?? collections.data?.universes.active ?? universes[0]?.id ?? "";
  const [rules, setRules] = useState<EditableRule[] | null>(null);
  const [rankBy, setRankBy] = useState("composite");
  const [current, setCurrent] = useState<Screen | null>(null);
  const [lastRun, setLastRun] = useState<ScreenRun | null>(null);
  const [showQuery, setShowQuery] = useState(false);
  // A run's own stage counts are authoritative until the rules change again.
  const [stagesFresh, setStagesFresh] = useState(false);

  const catalogue = metrics.data;
  const activeRules = useMemo(() => rules ?? (catalogue ? defaultRules(catalogue.metrics) : []), [rules, catalogue]);
  const request = useDebounced(
    catalogue && baseUniverse ? { universe_id: baseUniverse, rules: apiRules(activeRules), rank_by: rankBy } : null,
    FUNNEL_DEBOUNCE_MS,
  );
  const funnel = usePreviewFunnel(request);
  const funnelError = useRef<unknown>(null);
  useEffect(() => {
    if (funnel.isError && funnel.error !== funnelError.current) {
      funnelError.current = funnel.error;
      toast.error(`Rule preview failed: ${errorReason(funnel.error)}`);
    }
  }, [funnel.isError, funnel.error, toast]);

  useChannel("notifications", Notification, (note) => {
    if (note.event === "screen_alert") void client.invalidateQueries({ queryKey: dataKeys.alerts });
  });

  const edit = (next: EditableRule[]) => {
    setRules(next);
    setStagesFresh(false);
  };

  /** Save the rules as the current screen, creating one when needed. */
  const saveCurrent = async (): Promise<Screen> => {
    const body = { rules: apiRules(activeRules), rank_by: rankBy, universe_id: baseUniverse };
    const saved = current
      ? await api.screens.update(current.id, body)
      : await api.screens.create({
          name: `Screen ${new Date().toISOString().slice(0, 16).replace("T", " ")}`,
          ...body,
          description: "Created from the Market & Data screener.",
        });
    setCurrent(saved);
    void client.invalidateQueries({ queryKey: dataKeys.screens });
    return saved;
  };

  const runScreen = useAction({
    run: async () => {
      const screen = await saveCurrent();
      return api.screens.run(screen.id);
    },
    apply: (_c, run) => {
      setLastRun(run);
      setStagesFresh(true);
      for (const warning of run.warnings) toast.warn(warning);
    },
    done: (run) => `Screen executed · ${int(run.pass_count)} of ${int(run.base_count)} names pass`,
    failed: () => "Could not run the screen",
  });

  const benchmark = settings.data?.benchmark ?? "SPY";
  const backtest = useAction({
    run: async () => {
      let run = lastRun;
      let screen = current;
      if (!run || !screen) {
        screen = await saveCurrent();
        run = await api.screens.run(screen.id);
        setLastRun(run);
      }
      return { queued: await api.screens.queueBacktest(screen.id, { run_id: run.id, benchmark }), run };
    },
    done: ({ queued, run }) => `Backtest ${queued.backtest_id ?? "queued"} · ${int(queued.request?.symbol_count ?? run.pass_count)} names vs ${benchmark}`,
    apply: (_c, { queued }) => {
      // The Strategy Lab is ported in S5; until then the run opens there.
      if (queued.backtest_id) openLegacy(`/#tl/lab/backtests?id=${encodeURIComponent(queued.backtest_id)}`);
      else if (queued.detail) toast.warn(queued.detail);
    },
    failed: () => "Could not queue the backtest",
  });

  const saveAsUniverse = useAction({
    run: (run: ScreenRun) =>
      api.universes.create({
        name: `Screen ${new Date().toISOString().slice(0, 10)} (${run.pass_count})`,
        symbols: run.symbols.join(", "),
        description: `Survivors of ${current ? current.name : "an unsaved screen"} run ${run.id}`,
        construction_method: "Screener result",
      }),
    invalidate: () => [dataKeys.collections],
    done: (u) => `Saved universe “${u.name}” · ${u.symbol_count} symbols`,
    failed: () => "Could not save the universe",
  });

  const pin = useAction({
    run: async () => {
      const screen = await saveCurrent();
      const tags = Array.from(new Set([...screen.tags, "pinned", "alert"]));
      const pinned = await api.screens.update(screen.id, { tags });
      setCurrent(pinned);
      // Record the baseline now rather than waiting a monitor cycle.
      await api.screens.checkAlerts();
      return { pinned, alerts: await client.fetchQuery({ queryKey: dataKeys.alerts, queryFn: () => api.client.optional(api.screens.alerts()) }) };
    },
    invalidate: () => [dataKeys.screens],
    done: ({ pinned, alerts }) => {
      const seconds = alerts?.monitor?.interval_seconds;
      const every = seconds ? `${Math.round(seconds / 60)} min` : "the configured interval";
      return `“${pinned.name}” pinned and watched — it is re-run every ${every}, and any symbol entering or leaving raises an alert.`;
    },
    failed: () => "Could not pin the screen",
  });

  if (metrics.isPending || collections.isPending) return <p className="text-text-muted">loading the metric catalogue…</p>;
  if (metrics.isError || collections.isError) {
    return (
      <p role="alert" className="text-down">
        Screener: {errorReason(metrics.error ?? collections.error)}
      </p>
    );
  }
  const { metrics: defs, operators, scopes } = metrics.data;
  const numeric = defs.filter((m) => !m.categorical);
  const counts = funnel.data ?? [];
  const stageFor = (i: number) => {
    const stage = lastRun?.stages[i];
    if (stage && stagesFresh) return { before: stage.input_count, after: stage.output_count };
    const entry = counts[i];
    if (!entry) return null;
    return { before: i === 0 ? entry.base : (counts[i - 1]?.count ?? entry.base), after: entry.count };
  };
  const baseInfo = universes.find((u) => u.id === baseUniverse);
  const baseCount = baseInfo ? baseInfo.member_count || baseInfo.symbol_count : 0;
  const survivors = lastRun ? ranked(lastRun.survivors, rankBy) : [];
  const stats = lastRun?.stats;
  const alertData = alerts.data;
  const watched = alertData?.monitor?.watched ?? [];

  return (
    <div className="grid grid-cols-[240px_minmax(0,1fr)] gap-[11px]">
      <div className="flex flex-col gap-[11px]">
        <Panel title="Saved screens" accent={false} bodyClassName="p-0">
          {!screens.data?.screens.length ? (
            <p className="px-2.5 py-2 text-[11px] text-text-subtle">No saved screens yet. Running a screen saves it.</p>
          ) : (
            screens.data.screens.map((screen) => (
              <button
                key={screen.id}
                type="button"
                aria-current={current?.id === screen.id ? "true" : undefined}
                className={cn("block w-full border-t border-border px-2.5 py-1.5 text-left first:border-t-0 hover:bg-surface-2", current?.id === screen.id && "bg-active")}
                onClick={() => {
                  setCurrent(screen);
                  setRules(editableRules(screen.rules));
                  setRankBy(screen.rank_by || "composite");
                  if (screen.universe_id) setUniverseId(screen.universe_id);
                  setLastRun(null);
                  toast.show(`Loaded screen “${screen.name}”`);
                }}
              >
                {screen.name}
                <span className="block text-[10px] text-text-subtle">
                  {screen.active_rule_count} active rules · {screen.universe_id ?? "—"}
                </span>
              </button>
            ))
          )}
          <button
            type="button"
            className="w-full border-t border-border px-2.5 py-1.5 text-left text-[10px] text-text-subtle hover:text-text"
            onClick={() => {
              setCurrent(null);
              setRules(defaultRules(defs));
              setLastRun(null);
              toast.show("New screen — save it by running it");
            }}
          >
            + NEW SCREEN
          </button>
        </Panel>
        <Panel title="Base universe" accent={false}>
          <Select aria-label="Base universe" size="sm" value={baseUniverse} onChange={(e) => setUniverseId(e.target.value)}>
            {universes.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name}
              </option>
            ))}
          </Select>
          <p className="mt-1.5 text-[11px] text-text-subtle">
            {baseInfo ? (baseCount ? `${baseCount} names · ${baseInfo.builtin ? "built-in" : "user universe"}` : "no members stored yet — refresh it on the Universes tab") : ""}
          </p>
        </Panel>
      </div>

      <div className="flex min-w-0 flex-col gap-[11px]">
        <Panel title="Screen criteria" accent={false} actions={<span className="text-[10px] text-text-subtle">Pass funnel</span>}>
          {activeRules.map((rule, i) => (
            <RuleRow
              key={i}
              rule={rule}
              index={i}
              metrics={defs}
              operators={operators}
              scopes={scopes}
              funnel={stageFor(i)}
              funnelFailed={funnel.isError}
              onChange={(next) => edit(activeRules.map((r, j) => (j === i ? next : r)))}
              onDrop={() => {
                if (activeRules.length === 1) {
                  toast.warn("A screen needs at least one rule");
                  return;
                }
                edit(activeRules.filter((_, j) => j !== i));
              }}
            />
          ))}
          <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-border-subtle pt-2">
            <Button
              size="sm"
              onClick={() => {
                edit([...activeRules, { field: numeric[0]?.key ?? "quality", operator: ">", value: 0 }]);
                toast.show("Rule added");
              }}
            >
              + Add rule
            </Button>
            <span className="text-[10px] text-text-subtle">Rank by</span>
            <Select size="sm" aria-label="Rank by" wrapperClassName="w-36" value={rankBy} onChange={(e) => setRankBy(e.target.value)}>
              {numeric.map((m) => (
                <option key={m.key} value={m.key}>
                  {m.key}
                </option>
              ))}
            </Select>
            <Button size="sm" variant="primary" className="ml-auto" disabled={runScreen.isPending} onClick={() => runScreen.mutate()}>
              Run screen
            </Button>
            <button type="button" className="text-accent" aria-expanded={showQuery} onClick={() => setShowQuery((v) => !v)}>
              View as query {showQuery ? "⌃" : "⌄"}
            </button>
          </div>
          {showQuery && <pre className="mono mt-2 whitespace-pre-wrap text-[11px] text-text-muted" aria-label="Query">{counts.at(-1)?.query ?? ""}</pre>}
        </Panel>

        <div className="grid grid-cols-[minmax(0,1fr)_300px] gap-[11px]">
          <Panel title="Results" accent={false} bodyClassName="p-0" actions={<Badge>{lastRun ? `${int(lastRun.pass_count)} symbols` : "not run"}</Badge>}>
            <table className="w-full" aria-label="Results">
              <thead className="text-[9px] text-text-muted">
                <tr>
                  {["#", "Symbol", "Composite", "ROIC", "Mom 12-1"].map((h, i) => (
                    <th key={h} className={cn("px-2.5 py-1.5 font-normal", i > 1 ? "text-right" : "text-left")}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {!lastRun && (
                  <tr>
                    <td colSpan={5} className="px-2.5 py-2 text-text-muted">
                      Run the screen to see the names that pass.
                    </td>
                  </tr>
                )}
                {lastRun && !survivors.length && (
                  <tr>
                    <td colSpan={5} className="px-2.5 py-2 text-text-muted">
                      No names pass this screen — loosen a rule.
                    </td>
                  </tr>
                )}
                {survivors.slice(0, SHOWN_RESULTS).map((row, i) => {
                  const composite = Number(row.composite);
                  const pctl = row.mom_12_1_pctl;
                  return (
                    <tr key={String(row.symbol)} className="border-t border-border">
                      <td className="px-2.5 py-1.5 text-text-muted">{i + 1}</td>
                      <td className="px-2.5 py-1.5">{String(row.symbol)}</td>
                      <td className={cn("mono px-2.5 py-1.5 text-right", composite >= 90 && "text-up")}>{num(Number.isFinite(composite) ? composite : null, 1)}</td>
                      <td className="mono px-2.5 py-1.5 text-right">{pct(typeof row.roic === "number" ? row.roic : null, { decimals: 1 })}</td>
                      <td className="mono px-2.5 py-1.5 text-right">{pctl == null ? "—" : `p${Math.round(Number(pctl) * 100)}`}</td>
                    </tr>
                  );
                })}
                {survivors.length > SHOWN_RESULTS && (
                  <tr>
                    <td colSpan={5} className="px-2.5 py-1.5 text-text-muted">
                      … {survivors.length - SHOWN_RESULTS} more
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </Panel>

          <div className="flex flex-col gap-[11px]">
            <Panel title="Result profile" accent={false} actions={<Badge>{stats ? `${int(stats.count)} names` : "—"}</Badge>}>
              <dl>
                {(
                  [
                    ["Sector mix", stats?.sector_mix?.length ? stats.sector_mix.slice(0, 3).map((e) => `${e.sector} ${Math.round(e.share * 100)}%`).join(" · ") : "—", null],
                    ["Median mkt cap", stats?.median_market_cap ? usd(stats.median_market_cap, { compact: true, decimals: 1 }) : "—", null],
                    [
                      "Turnover vs last run",
                      !stats ? "—" : stats.turnover ? `${stats.turnover.entered_count} in / ${stats.turnover.exited_count} out (vs ${stats.turnover.compared_with_run})` : "first run — nothing to compare against",
                      "text-warn",
                    ],
                    ["Overlap w/ live book", !stats ? "—" : stats.live_book_overlap?.source ? `${stats.live_book_overlap.count} names (${stats.live_book_overlap.source})` : (stats.live_book_overlap?.reason ?? "—"), null],
                  ] as const
                ).map(([label, value, className]) => (
                  <div key={label} className="flex justify-between gap-2 border-b border-border-subtle py-1.5 text-[11px] last:border-b-0">
                    <dt>{label}</dt>
                    <dd className={cn("text-right", className)}>{value}</dd>
                  </div>
                ))}
              </dl>
            </Panel>
            <Panel title="Act on this screen" accent={false}>
              <div className="flex flex-col gap-2">
                <Button size="sm" variant="primary" disabled={backtest.isPending} onClick={() => backtest.mutate()}>
                  → Backtest this screen
                </Button>
                <Button
                  size="sm"
                  disabled={saveAsUniverse.isPending}
                  onClick={() => (lastRun ? saveAsUniverse.mutate(lastRun) : toast.warn("Run the screen first — there are no survivors to save yet"))}
                >
                  Save as universe
                </Button>
                <Button size="sm" disabled={pin.isPending} onClick={() => pin.mutate()}>
                  Pin + alert on changes
                </Button>
              </div>
            </Panel>
            <Panel title="Membership alerts" accent={false} bodyClassName="p-0" actions={<Badge>{watched.length ? `${watched.length} screen${watched.length === 1 ? "" : "s"} watched` : "no screen pinned"}</Badge>}>
              {alertData?.monitor?.running === false && (
                <p className="px-2.5 py-2 text-warn">The alert monitor is not running in this process, so pinned screens are not being re-run.</p>
              )}
              {!alertData?.alerts.length ? (
                <p className="px-2.5 py-2 text-[11px]">
                  {watched.length
                    ? `No membership change since the baseline was recorded${alertData?.monitor?.last_cycle_at ? ` (last checked ${ago(alertData.monitor.last_cycle_at)} ago).` : "."}`
                    : "Pin a screen to watch its membership. Nothing is watched yet."}
                </p>
              ) : (
                alertData.alerts.map((alert) => (
                  <div key={alert.id} className="border-t border-border px-2.5 py-2 first:border-t-0">
                    <b className="block font-medium text-text-strong">{alert.headline}</b>
                    <span className="text-[10px] text-text-muted">
                      {ago(alert.detected_at)} ago · {alert.previous_count} → {alert.pass_count}
                    </span>
                    {!alert.acknowledged_at && (
                      <Button size="sm" className="ml-2" disabled={acknowledge.isPending} onClick={() => acknowledge.mutate(alert.id)}>
                        Acknowledge
                      </Button>
                    )}
                  </div>
                ))
              )}
            </Panel>
          </div>
        </div>
      </div>
    </div>
  );
}
