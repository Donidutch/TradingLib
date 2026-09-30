"""The Agent Center attention queue.

The Overview inbox is the one place a person is expected to look to find out
what needs them. The reference groups that queue into five kinds, and each one
has a real source in TradingLib:

============  ============================================================
Kind          Where the items come from
============  ============================================================
Approval      pending human-approval gates - a run is blocked on a person
Validation    validation reports whose gates did not pass
Experiment    experiment runs that failed, and backtests whose engine raised
              a data warning (look-ahead, unadjusted corporate action, ...)
Data          open data-quality incidents
Agent         unacknowledged agent findings, and failed/killed pipeline runs
============  ============================================================

Every item carries where it came from and where clicking it goes, so nothing in
the queue is a dead end. Sources that are not installed in this process are
skipped silently - the queue degrades to the kinds that *are* available rather
than failing whole.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from tradinglib.data.base import utcnow

logger = logging.getLogger(__name__)

#: The item kinds the Overview's *Type* filter offers, in the reference's order.
INBOX_TYPES: Final[tuple[str, ...]] = (
    "Approval",
    "Validation",
    "Experiment",
    "Data",
    "Agent",
)

#: Sort weight per urgency band; the queue is urgent-first, then oldest-first.
_URGENCY_RANK: Final[dict[str, int]] = {
    "Critical": 0,
    "High": 1,
    "Medium": 2,
    "Normal": 3,
    "Low": 4,
}

#: Quality severities that put an incident in front of a person.
_SEVERE: Final[frozenset[str]] = frozenset({"error", "critical"})


@dataclass(slots=True)
class InboxSources:
    """The registries the queue is composed from. Any of them may be ``None``.

    Attributes:
        approvals: the human-approval gate.
        findings: the agent findings store.
        validation: the Strategy Lab validation engine.
        experiments: the experiment engine.
        quality: the data-quality engine.
        runner: the pipeline runner.
    """

    approvals: Any | None = None
    findings: Any | None = None
    validation: Any | None = None
    experiments: Any | None = None
    quality: Any | None = None
    runner: Any | None = None


@dataclass(slots=True)
class InboxItem:
    """One row of the attention queue."""

    id: str
    type: str
    title: str
    reason: str
    urgency: str = "Medium"
    status: str = "Needs attention"
    symbol: str = "—"
    source: str | None = None
    owner: str | None = None
    action: str = "Open"
    route: str = "inbox"
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    age_seconds: float | None = None
    confidence_pct: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        payload: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "reason": self.reason,
            "urgency": self.urgency,
            "status": self.status,
            "symbol": self.symbol,
            "source": self.source,
            "owner": self.owner,
            "action": self.action,
            "route": self.route,
            "context": dict(self.context),
            "created_at": self.created_at,
            "age_seconds": self.age_seconds,
            "confidence_pct": self.confidence_pct,
        }
        payload.update(self.extra)
        return payload


def _age(moment: datetime | None) -> tuple[str | None, float | None]:
    """Return ``(iso, seconds_old)`` for a timestamp that may be missing."""
    if moment is None:
        return None, None
    now = utcnow()
    stamp = moment if moment.tzinfo else moment.replace(tzinfo=now.tzinfo)
    return stamp.isoformat(), max(0.0, (now - stamp).total_seconds())


class InboxComposer:
    """Builds the merged attention queue from the registries it is given."""

    def __init__(self, sources: InboxSources) -> None:
        self._sources = sources

    @property
    def sources(self) -> InboxSources:
        """The registries this composer reads."""
        return self._sources

    def build(self, *, limit: int = 25) -> dict[str, Any]:
        """Return the merged queue plus the per-kind counts.

        Args:
            limit: how many items to return, and the per-source fetch cap.
        """
        items: list[InboxItem] = []
        available: dict[str, bool] = {}
        for kind, builder in (
            ("Approval", self._approvals),
            ("Validation", self._validation),
            ("Experiment", self._experiments),
            ("Data", self._data),
            ("Agent", self._agent),
        ):
            try:
                produced = builder(limit)
            except Exception:  # noqa: BLE001 - one dead registry must not empty the queue
                logger.warning("inbox source %s failed", kind, exc_info=True)
                available[kind] = False
                continue
            available[kind] = produced is not None
            items.extend(produced or [])
        items.sort(
            key=lambda item: (
                _URGENCY_RANK.get(item.urgency, 5),
                -(item.age_seconds or 0.0),
            )
        )
        counts = {kind: sum(1 for item in items if item.type == kind) for kind in INBOX_TYPES}
        # The Overview's "FINDINGS PENDING" metric counts findings specifically,
        # not everything filed under the Agent kind (which also holds failed runs).
        counts["Finding"] = sum(1 for item in items if item.extra.get("kind") == "finding")
        shown = items[:limit]
        return {
            "items": [item.as_dict() for item in shown],
            "count": len(shown),
            "total": len(items),
            "types": list(INBOX_TYPES),
            "counts": counts,
            "sources_available": available,
            "pending_approvals": counts.get("Approval", 0),
        }

    # --- per-kind builders ------------------------------------------------
    def _approvals(self, limit: int) -> list[InboxItem] | None:
        """Pending human-approval gates: a run is blocked until someone acts."""
        gate = self._sources.approvals
        if gate is None:
            return None
        out: list[InboxItem] = []
        for request in gate.queue(limit=limit):
            payload = request.as_dict()
            created, age = _age(request.requested_at)
            out.append(
                InboxItem(
                    id=request.id,
                    type="Approval",
                    title=request.title,
                    reason=request.summary,
                    urgency="Critical",
                    status="Needs attention",
                    symbol=str((request.context or {}).get("symbol") or "Run"),
                    source=request.run_id,
                    owner=request.agent_id,
                    action="Review candidate",
                    route="approval",
                    context={"approvalId": request.id, "runId": request.run_id},
                    created_at=created or payload.get("requested_at"),
                    age_seconds=age if age is not None else payload.get("age_seconds"),
                    extra={"stage_id": request.stage_id, "agent_id": request.agent_id},
                )
            )
        return out

    def _validation(self, limit: int) -> list[InboxItem] | None:
        """Validation reports whose gates did not pass."""
        engine = self._sources.validation
        if engine is None:
            return None
        out: list[InboxItem] = []
        for report in engine.reports(limit=max(limit * 2, 20)):
            if report.passed or report.promoted:
                continue
            failed = report.failed_gates
            if not failed:
                continue
            created, age = _age(report.evaluated_at)
            names = ", ".join(getattr(gate, "name", "gate") for gate in failed[:3])
            hard = any(getattr(gate, "hard", False) for gate in failed)
            out.append(
                InboxItem(
                    id=report.id,
                    type="Validation",
                    title=f"{len(failed)} gate(s) failed on {report.backtest_id}",
                    reason=(
                        f"{names} did not pass; the strategy cannot be promoted until they do "
                        f"({report.verdict})."
                    ),
                    urgency="High" if hard else "Medium",
                    status="Needs attention",
                    symbol=report.hypothesis_id or "Strategy",
                    source=report.backtest_id,
                    owner="Validation",
                    action="Inspect failed gate",
                    route="lab",
                    context={
                        "reportId": report.id,
                        "backtestId": report.backtest_id,
                        "hypothesisId": report.hypothesis_id,
                    },
                    created_at=created,
                    age_seconds=age,
                    confidence_pct=round(report.score, 1),
                    extra={"verdict": report.verdict, "failed_gates": len(failed)},
                )
            )
            if len(out) >= limit:
                break
        return out

    def _experiments(self, limit: int) -> list[InboxItem] | None:
        """Experiment runs that failed - the run log has the reason."""
        engine = self._sources.experiments
        if engine is None:
            return None
        out: list[InboxItem] = []
        for run in engine.all_runs(limit=max(limit * 2, 20)):
            if str(run.status) != "failed":
                continue
            created, age = _age(run.finished_at or run.started_at)
            out.append(
                InboxItem(
                    id=run.id,
                    type="Experiment",
                    title=f"Experiment run {run.id} failed",
                    reason=run.error or "the run ended without recording a reason",
                    urgency="High",
                    status="Needs attention",
                    symbol=run.hypothesis_id or "Experiment",
                    source=run.experiment_id,
                    owner="Strategy Lab",
                    action="Open run detail",
                    route="lab",
                    context={
                        "experimentId": run.experiment_id,
                        "runId": run.id,
                        "backtestId": run.backtest_id,
                    },
                    created_at=created,
                    age_seconds=age,
                )
            )
            if len(out) >= limit:
                break
        return out

    def _data(self, limit: int) -> list[InboxItem] | None:
        """Open data-quality incidents."""
        engine = self._sources.quality
        if engine is None:
            return None
        out: list[InboxItem] = []
        for incident in engine.incidents(limit=max(limit * 2, 20)):
            created, age = _age(incident.opened_at)
            severity = str(incident.severity)
            out.append(
                InboxItem(
                    id=incident.id,
                    type="Data",
                    title=f"{incident.check.replace('_', ' ')} on {incident.dataset}",
                    reason=incident.message,
                    urgency="High" if severity in _SEVERE else "Medium",
                    status="Needs attention",
                    symbol=incident.dataset.split(":")[1]
                    if ":" in incident.dataset
                    else incident.dataset,
                    source=incident.dataset,
                    owner="Data quality",
                    action="Trace source",
                    route="data",
                    context={"incidentId": incident.id, "dataset": incident.dataset},
                    created_at=created,
                    age_seconds=age,
                    extra={"severity": severity, "check": incident.check},
                )
            )
            if len(out) >= limit:
                break
        return out

    def _agent(self, limit: int) -> list[InboxItem] | None:
        """Unacknowledged agent findings, plus runs that failed outright."""
        out: list[InboxItem] = []
        store = self._sources.findings
        if store is not None:
            for row in store.inbox(limit=limit):
                out.append(
                    InboxItem(
                        id=str(row.get("id")),
                        type="Agent",
                        title=str(row.get("title") or ""),
                        reason=str(row.get("reason") or ""),
                        urgency=str(row.get("urgency") or "Medium"),
                        status=str(row.get("status") or "new"),
                        symbol=str(row.get("symbol") or "—"),
                        source=row.get("source"),
                        owner=row.get("agent_id"),
                        action=str(row.get("action") or "Review finding"),
                        route=str(row.get("route") or "findings"),
                        context=dict(row.get("context") or {}),
                        created_at=row.get("created_at"),
                        age_seconds=row.get("age_seconds"),
                        confidence_pct=row.get("confidence_pct"),
                        extra={
                            "kind": "finding",
                            "evidence_count": row.get("evidence_count"),
                            "agent_id": row.get("agent_id"),
                            "stage_id": row.get("stage_id"),
                        },
                    )
                )
        runner = self._sources.runner
        if runner is not None:
            for run in runner.list(limit=max(limit, 20)):
                if str(run.status) not in {"failed", "killed"}:
                    continue
                created, age = _age(run.finished_at or run.started_at or run.created_at)
                out.append(
                    InboxItem(
                        id=run.id,
                        type="Agent",
                        title=f"Pipeline run {run.id} {run.status}",
                        reason=run.error or f"{run.pipeline_id} ended as {run.status}",
                        urgency="High" if str(run.status) == "failed" else "Medium",
                        status="Needs attention",
                        symbol=run.pipeline_id,
                        source=run.id,
                        owner=run.trigger,
                        action="Open run detail",
                        route="pipeline",
                        context={"runId": run.id, "pipelineId": run.pipeline_id},
                        created_at=created,
                        age_seconds=age,
                        extra={"kind": "run", "run_status": str(run.status)},
                    )
                )
        if store is None and runner is None:
            return None
        return out[:limit]
