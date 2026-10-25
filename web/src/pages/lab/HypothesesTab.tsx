/* Hypotheses (COVERAGE D): the list, one hypothesis with its falsifier,
 * provenance, evidence, experiments and status history, the create and edit
 * form, and the Agent Center's activity beside them. */
import { useState } from "react";
import { errorReason } from "../../api/errors";
import { useActiveScope, useUniverses } from "../../api/hooks";
import type { Hypothesis } from "../../api/schemas/lab";
import { openLegacy } from "../../shell/legacy";
import { useToast } from "../../shell/toast-context";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Button } from "../../ui/primitives/button";
import { Panel } from "../../ui/primitives/card";
import { ConfirmDialog } from "../../ui/primitives/confirm";
import { Field, Input, Select, Textarea } from "../../ui/primitives/field";
import { useAgentActivity, useHypothesisActions, useLabData } from "./hooks";
import { EmptyBox, Foot, Kv, Pill, Ref, Section } from "./parts";
import {
  activityActor,
  activityBadge,
  activitySentence,
  EVIDENCE_KINDS,
  HYPOTHESIS_STATES,
  judgedWindow,
  newestByExperiment,
  ratio,
  reportsByBacktest,
  statusTone,
  toneText,
  when,
} from "./rules";
import { useLab } from "./state";

function AgentActivity() {
  const { runs, definitions } = useAgentActivity();
  const list = runs.data?.runs ?? [];
  const badge = activityBadge(list);
  return (
    <Panel title="Agent activity" actions={<Badge tone={badge.live ? "warn" : "neutral"}>{badge.text}</Badge>} className="mt-3">
      <div className="flex flex-col gap-1.5 text-[11px]" aria-label="Agent activity">
        {runs.isError ? (
          <Step title="Agent Center unreachable" body={errorReason(runs.error)} />
        ) : runs.isPending ? (
          <Foot>Loading…</Foot>
        ) : !list.length ? (
          <Step title="No agent runs yet" body="Start a pipeline in the Agent Center and its stages appear here." />
        ) : (
          <>
            {list.slice(0, 6).map((run) => (
              <button
                key={run.id}
                type="button"
                className="text-left hover:bg-surface-2"
                onClick={() => openLegacy(`/#agents/runs?run=${encodeURIComponent(run.id)}`)}
              >
                <Step title={activityActor(run, definitions.data?.agents ?? [])} body={activitySentence(run)} />
              </button>
            ))}
            <Step title="Where this comes from" body="GET /api/agents/runs — the same ledger the Agent Center's run table reads." />
          </>
        )}
      </div>
    </Panel>
  );
}

const Step = ({ title, body }: { title: string; body: string }) => (
  <div className="border-b border-border-subtle py-1 last:border-b-0">
    <b className="block text-text-strong">{title}</b>
    <span className="text-text-muted">{body}</span>
  </div>
);

function HypothesisList({ selected }: { selected: string | null }) {
  const lab = useLab();
  const { hypotheses, experiments } = useLabData();
  if (hypotheses.isError) return <p className="text-warn">Hypotheses could not be loaded: {errorReason(hypotheses.error)}</p>;
  if (hypotheses.isPending) return <Foot>Loading hypotheses…</Foot>;
  const items = hypotheses.data.hypotheses;
  if (!items.length) {
    return (
      <EmptyBox
        headline="No hypotheses yet"
        detail="A hypothesis is a falsifiable statement; every experiment hangs off one."
        action={
          <Button size="sm" variant="primary" onClick={() => lab.setHypothesisMode("new")}>
            + NEW HYPOTHESIS
          </Button>
        }
      />
    );
  }
  const all = experiments.data?.experiments ?? [];
  return (
    <nav aria-label="Hypotheses" className="flex flex-col gap-1">
      {items.map((h) => {
        const count = all.filter((e) => e.hypothesis_id === h.id).length;
        const on = h.id === selected && lab.hypothesisMode === "view";
        return (
          <button
            key={h.id}
            type="button"
            aria-current={on ? "true" : undefined}
            onClick={() => {
              lab.setHypothesisMode("view");
              lab.go("hypotheses", h.id);
            }}
            className={cn("rounded-[4px] border p-2 text-left", on ? "border-accent bg-accent-dim" : "border-border-subtle hover:border-border")}
          >
            <div className="flex items-center gap-2">
              <span className="text-[12px] text-text-strong">
                {h.id} · {h.title}
              </span>
              <Pill tone={(h.owner ?? "").toLowerCase() === "agent" ? "accent" : null} className="ml-auto">
                {h.owner || h.origin || "unassigned"}
              </Pill>
            </div>
            <Foot className="mt-0.5">
              {count} experiments · {h.evidence_count} evidence · <span className={toneText(statusTone(h.status))}>{h.status.toUpperCase()}</span>
            </Foot>
          </button>
        );
      })}
    </nav>
  );
}

function EvidenceForm({ hypothesis }: { hypothesis: Hypothesis }) {
  const toast = useToast();
  const { link } = useHypothesisActions();
  const [kind, setKind] = useState("run");
  const [ref, setRef] = useState("");
  const [label, setLabel] = useState("");
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      <Select aria-label="Evidence kind" value={kind} onChange={(e) => setKind(e.target.value)} wrapperClassName="w-[110px]">
        {EVIDENCE_KINDS.map((k) => (
          <option key={k}>{k}</option>
        ))}
      </Select>
      <Input aria-label="Evidence reference" placeholder="bt-… / k-… / url" value={ref} onChange={(e) => setRef(e.target.value)} className="w-36" />
      <Input aria-label="Evidence label" placeholder="label (optional)" value={label} onChange={(e) => setLabel(e.target.value)} className="w-36" />
      <Button
        size="sm"
        disabled={link.isPending}
        onClick={() => {
          if (!ref.trim()) return toast.warn("Evidence needs a reference.");
          link.mutate(
            { id: hypothesis.id, body: { kind, ref: ref.trim(), label: label.trim() || null } },
            {
              onSuccess: () => {
                setRef("");
                setLabel("");
              },
            },
          );
        }}
      >
        + LINK
      </Button>
    </div>
  );
}

function HypothesisDetail({ hypothesis }: { hypothesis: Hypothesis }) {
  const lab = useLab();
  const { experiments, backtests, reports } = useLabData();
  const universes = useUniverses();
  const actions = useHypothesisActions();
  const [deleting, setDeleting] = useState(false);
  const mine = (experiments.data?.experiments ?? []).filter((e) => e.hypothesis_id === hypothesis.id);
  const newest = newestByExperiment(backtests.data?.backtests ?? []);
  const byBacktest = reportsByBacktest(reports.data?.reports ?? []);
  const universeName = (id: string | null) => (id ? (universes.data?.universes.find((u) => u.id === id)?.name ?? id) : "—");
  return (
    <Panel title="Hypothesis" accent={false}>
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="text-[14px] font-semibold text-text-strong">{hypothesis.id}</span>
        <span className="text-text-muted">{hypothesis.title}</span>
        <span className={cn("ml-auto", toneText(statusTone(hypothesis.status)))}>{hypothesis.status.toUpperCase()}</span>
      </div>
      <div className="my-2.5 rounded-[4px] border border-accent/40 bg-accent-dim p-2.5">
        <h4 className="mb-1 text-[9px] tracking-[1px] text-accent">WOULD FALSIFY THIS</h4>
        <span className="text-text-strong">{hypothesis.falsifier}</span>
      </div>
      <Section label="Statement">
        <p className="text-[13px]">{hypothesis.statement}</p>
      </Section>
      {hypothesis.rationale && (
        <Section label="Rationale">
          <p className="text-[13px]">{hypothesis.rationale}</p>
        </Section>
      )}
      <div className="grid grid-cols-2 gap-2.5 max-[900px]:grid-cols-1">
        <Section label="Provenance" className="mb-0">
          <Kv k="Origin">{hypothesis.origin || "—"}</Kv>
          <Kv k="Owner">{hypothesis.owner || "—"}</Kv>
          <Kv k="Universe">{universeName(hypothesis.universe_id)}</Kv>
          <Kv k="Experiments">{String(mine.length)}</Kv>
          <Kv k="Created">{when(hypothesis.created_at)}</Kv>
          <Kv k="Updated">{when(hypothesis.updated_at)}</Kv>
          {hypothesis.tags.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {hypothesis.tags.map((tag) => (
                <Badge key={tag} size="sm">
                  {tag}
                </Badge>
              ))}
            </div>
          )}
        </Section>
        <Section label="Linked evidence" className="mb-0">
          {!hypothesis.evidence?.length ? (
            <Foot>Nothing linked yet.</Foot>
          ) : (
            hypothesis.evidence.map((link) => (
              <Kv key={String(link.id)} k={link.kind}>
                {link.url ? (
                  <a href={link.url} target="_blank" rel="noreferrer noopener" className="text-accent">
                    {link.label || link.ref}
                  </a>
                ) : (
                  <span>{link.label || link.ref}</span>
                )}
                <button
                  type="button"
                  className="ml-2 text-text-subtle hover:text-down"
                  aria-label={`Detach ${link.label || link.ref}`}
                  title="detach"
                  onClick={() => actions.unlink.mutate({ id: hypothesis.id, evidence: link.id })}
                >
                  ✕
                </button>
              </Kv>
            ))
          )}
          <EvidenceForm hypothesis={hypothesis} />
        </Section>
      </div>
      <Section label="Experiments under this hypothesis" className="mt-2.5">
        {!mine.length ? (
          <Foot className="py-1.5">No experiments yet. This hypothesis has never been tested.</Foot>
        ) : (
          mine.map((exp) => {
            const record = newest[exp.id];
            const report = record ? byBacktest[record.id] : undefined;
            return (
              <Kv key={exp.id} k={<Ref onClick={() => lab.go("experiments", exp.id)}>{exp.name}</Ref>}>
                {record ? (
                  `${exp.stage.toUpperCase()} · Sharpe ${ratio(judgedWindow(record)?.sharpe)}${report ? ` · ${report.gates_passed}/${report.gates_total} gates` : ""}`
                ) : (
                  <span className="text-text-subtle">no completed run</span>
                )}
              </Kv>
            );
          })
        )}
        <div className="mt-2 flex flex-wrap gap-1.5">
          <Button
            size="sm"
            variant="primary"
            onClick={() => {
              lab.setComparing(false);
              lab.setDraft({ clone: null, hypothesis: hypothesis.id });
              lab.go("experiments");
            }}
          >
            + NEW EXPERIMENT
          </Button>
          <Button size="sm" onClick={() => lab.setHypothesisMode("edit")}>
            EDIT
          </Button>
          {HYPOTHESIS_STATES.filter((s) => s !== hypothesis.status).map((status) => (
            <Button key={status} size="sm" disabled={actions.setStatus.isPending} onClick={() => actions.setStatus.mutate({ id: hypothesis.id, status })}>
              MARK {status.toUpperCase()}
            </Button>
          ))}
          <Button size="sm" variant="danger" onClick={() => setDeleting(true)}>
            DELETE
          </Button>
        </div>
      </Section>
      {hypothesis.status_history && hypothesis.status_history.length > 0 && (
        <Section label="Status history">
          {[...hypothesis.status_history].reverse().map((entry, i) => (
            <div key={i} className="text-[11px]">
              <span className="text-text-subtle">{when(entry.at || entry.ts)}</span>{" "}
              {entry.actor ? `${entry.actor} ` : ""}→ {String(entry.to || entry.status || "").toUpperCase()}
              {entry.note ? ` · ${entry.note}` : ""}
            </div>
          ))}
        </Section>
      )}
      <ConfirmDialog
        open={deleting}
        title="Delete hypothesis"
        confirmLabel="Delete"
        onCancel={() => setDeleting(false)}
        onConfirm={async () => {
          await actions.remove.mutateAsync(hypothesis.id).catch(() => undefined);
          setDeleting(false);
          lab.go("hypotheses");
        }}
      >
        Delete {hypothesis.id}? Its experiments keep their reference.
      </ConfirmDialog>
    </Panel>
  );
}

function HypothesisForm({ existing }: { existing: Hypothesis | null }) {
  const lab = useLab();
  const toast = useToast();
  const universes = useUniverses();
  const scope = useActiveScope();
  const { create, update } = useHypothesisActions();
  const [form, setForm] = useState({
    title: existing?.title ?? "",
    statement: existing?.statement ?? "",
    falsifier: existing?.falsifier ?? "",
    rationale: existing?.rationale ?? "",
    owner: existing?.owner ?? "",
    origin: existing?.origin ?? "",
    universe: existing ? (existing.universe_id ?? "") : null,
    tags: existing?.tags.join(", ") ?? "",
    status: existing?.status ?? "active",
  });
  // A new hypothesis is scoped to the active universe until one is picked.
  const activeId = scope.data?.universe?.id ?? "";
  const universe = form.universe ?? activeId;
  const set = (key: keyof typeof form) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const save = () => {
    const body = {
      title: form.title.trim(),
      statement: form.statement.trim(),
      falsifier: form.falsifier.trim(),
      rationale: form.rationale.trim() || null,
      owner: form.owner.trim() || null,
      origin: form.origin.trim() || null,
      universe_id: universe || null,
      tags: form.tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
    };
    if (!body.title || !body.statement || !body.falsifier) return toast.warn("Title, statement and falsifier are all required.");
    if (existing) update.mutate({ id: existing.id, body }, { onSuccess: () => lab.setHypothesisMode("view") });
    else
      create.mutate(
        { ...body, status: form.status },
        {
          onSuccess: (created) => {
            lab.setHypothesisMode("view");
            lab.go("hypotheses", created.id);
          },
        },
      );
  };
  return (
    <Panel title={existing ? "Edit hypothesis" : "New hypothesis"} actions={<span className="text-text-subtle">{existing ? existing.id : "draft"}</span>}>
      <form
        aria-label={existing ? "Edit hypothesis" : "New hypothesis"}
        className="flex flex-col gap-2.5"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <Field label="TITLE" htmlFor="h-title">
          <Input id="h-title" placeholder="Momentum decay is crowding-conditioned" value={form.title} onChange={set("title")} />
        </Field>
        <Field label="STATEMENT" htmlFor="h-statement">
          <Textarea id="h-statement" rows={2} placeholder="Conditioning 12-1 momentum on low crowding slows alpha decay." value={form.statement} onChange={set("statement")} />
        </Field>
        <div className="rounded-[4px] border border-accent/40 bg-accent-dim p-2.5">
          <Field label="FALSIFIER · REQUIRED" htmlFor="h-falsifier" hint="What evidence would kill this? The backend refuses a hypothesis without one.">
            <Input id="h-falsifier" placeholder="crowding percentile adds no incremental IC over plain momentum" value={form.falsifier} onChange={set("falsifier")} />
          </Field>
        </div>
        <Field label="RATIONALE" htmlFor="h-rationale">
          <Textarea id="h-rationale" rows={2} placeholder="Why this is worth testing (optional)." value={form.rationale} onChange={set("rationale")} />
        </Field>
        <div className="grid grid-cols-2 gap-2">
          <Field label="OWNER" htmlFor="h-owner">
            <Input id="h-owner" placeholder="owner" value={form.owner} onChange={set("owner")} />
          </Field>
          <Field label="ORIGIN" htmlFor="h-origin">
            <Input id="h-origin" placeholder="manual · lit scan · agent" value={form.origin} onChange={set("origin")} />
          </Field>
          <Field label="UNIVERSE SCOPE" htmlFor="h-universe">
            <Select id="h-universe" value={universe} onChange={set("universe")}>
              <option value="">— none —</option>
              {(universes.data?.universes ?? []).map((u) => (
                <option key={u.id} value={u.id}>
                  {u.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="TAGS" htmlFor="h-tags">
            <Input id="h-tags" placeholder="momentum, crowding" value={form.tags} onChange={set("tags")} />
          </Field>
        </div>
        {!existing && (
          <Field label="STATUS" htmlFor="h-status">
            <Select id="h-status" value={form.status} onChange={set("status")}>
              {HYPOTHESIS_STATES.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </Select>
          </Field>
        )}
        <div className="flex gap-2">
          <Button size="sm" type="submit" variant="primary" disabled={create.isPending || update.isPending}>
            {existing ? "SAVE CHANGES" : "SAVE AND OPEN"}
          </Button>
          <Button size="sm" type="button" onClick={() => lab.setHypothesisMode("view")}>
            CANCEL
          </Button>
        </div>
      </form>
    </Panel>
  );
}

export function HypothesesTab({ id }: { id: string | undefined }) {
  const lab = useLab();
  const { hypotheses } = useLabData();
  const list = hypotheses.data?.hypotheses ?? [];
  const hypothesis = list.find((h) => h.id === id) ?? (id ? undefined : list[0]);
  return (
    <>
      <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-3 max-[1000px]:grid-cols-1">
        <div className="flex flex-col gap-2">
          <HypothesisList selected={hypothesis?.id ?? null} />
          <Button size="sm" variant="ghost" onClick={() => lab.setHypothesisMode("new")}>
            + NEW HYPOTHESIS
          </Button>
        </div>
        <div>
          {lab.hypothesisMode === "new" ? (
            <HypothesisForm key="new" existing={null} />
          ) : lab.hypothesisMode === "edit" && hypothesis ? (
            <HypothesisForm key={hypothesis.id} existing={hypothesis} />
          ) : hypothesis ? (
            <HypothesisDetail hypothesis={hypothesis} />
          ) : (
            <Panel title="Hypothesis" accent={false}>
              <Foot>Select a hypothesis on the left, or create one.</Foot>
            </Panel>
          )}
        </div>
      </div>
      <AgentActivity />
    </>
  );
}
