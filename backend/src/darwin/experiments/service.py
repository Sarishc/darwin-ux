"""Experiment lifecycle and analysis: the database orchestration around the pure parts.

    create_experiment   eligibility (re-derived) + allowlisted config -> draft row
    start_experiment    start gate -> running (explicit human command only)
    pause / stop / complete_experiment   lifecycle moves (explicit human commands)
    analyze_experiment  counts -> experiment_analysis.v1 -> immutable ExperimentAnalysis

Nothing here promotes, deploys, changes Generation 0, or changes traffic based
on results. There is deliberately no HTTP route for any of these: they are
reached only through the CLI (python -m darwin.experiments.cli / make targets).

Logs: ids, keys, statuses, reason codes, counts — never session ids, spec
content, events or report bodies.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from darwin.db.models import Experiment, ExperimentAnalysis
from darwin.observability import stage

from .analysis import collect
from .eligibility import ExperimentRefused, check_eligibility, start_gate, validate_config
from .policy import AnalysisConfig, assess
from .vocabulary import (
    ANALYSIS_VERSION,
    STOP_REASONS,
    TOTAL_BUCKETS,
    TRANSITIONS,
    MetricName,
)

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]


class ExperimentNotFoundError(LookupError):
    pass


def _log(message: str, **context: object) -> None:
    logger.info(message, extra={"context": {k: str(v) for k, v in context.items()}})


# ---- create ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateOutcome:
    experiment_id: uuid.UUID | None
    reasons: tuple[str, ...]  # empty when created
    detail: str | None = None

    @property
    def created(self) -> bool:
        return self.experiment_id is not None


def create_experiment(
    factory: SessionFactory,
    *,
    experiment_key: str,
    candidate_evaluation_run_id: uuid.UUID,
    candidate_allocation_bp: int,
    primary_metric: str,
    guardrail_metrics: Sequence[str],
    minimum_sample_per_variant: int,
    traffic_source: str,
    at: datetime | None = None,  # tests/eval only; the CLI always uses "now"
) -> CreateOutcome:
    """A DRAFT experiment, or the reasons it cannot exist. Never starts anything."""
    problems = validate_config(
        experiment_key=experiment_key,
        candidate_allocation_bp=candidate_allocation_bp,
        primary_metric=primary_metric,
        guardrail_metrics=list(guardrail_metrics),
        minimum_sample_per_variant=minimum_sample_per_variant,
        traffic_source=traffic_source,
    )
    if problems:
        _log("experiment refused", key=experiment_key, reasons=",".join(problems))
        return CreateOutcome(None, tuple(problems))
    with factory() as session:
        try:
            eligible = check_eligibility(session, candidate_evaluation_run_id)
        except ExperimentRefused as refused:
            _log("experiment refused", key=experiment_key, reasons=",".join(refused.codes))
            return CreateOutcome(None, refused.codes, refused.detail)
        if session.scalar(select(Experiment.id).where(Experiment.experiment_key == experiment_key)):
            return CreateOutcome(None, ("experiment_key_taken",))
        experiment = Experiment(
            experiment_key=experiment_key,
            page_id=eligible.page_id,
            candidate_evaluation_run_id=eligible.evaluation_run_id,
            mutation_run_id=eligible.mutation_run_id,
            hypothesis_id=eligible.hypothesis_id,
            control_spec_id=eligible.control_spec_id,
            candidate_spec_id=eligible.candidate_spec_id,
            control_spec_hash=eligible.control_spec_hash,
            candidate_spec_hash=eligible.candidate_spec_hash,
            control_allocation_bp=TOTAL_BUCKETS - candidate_allocation_bp,
            candidate_allocation_bp=candidate_allocation_bp,
            primary_metric=primary_metric,
            guardrail_metrics=list(guardrail_metrics),
            minimum_sample_per_variant=minimum_sample_per_variant,
            traffic_source=traffic_source,
            status="draft",
            status_changed_at=_when(at),
        )
        session.add(experiment)
        session.commit()
        _log("experiment created", experiment_id=experiment.id, key=experiment_key)
        return CreateOutcome(experiment.id, ())


# ---- lifecycle --------------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionOutcome:
    experiment_id: uuid.UUID
    status: str  # the status after the call (unchanged when refused)
    changed: bool
    reasons: tuple[str, ...]


def _when(at: datetime | None) -> datetime:
    """The transition time (server clock). Explicit times exist for tests and the golden set."""
    if at is None:
        return datetime.now(UTC)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("transition time must be timezone-aware")
    return at


def _load(session: Session, experiment_id: uuid.UUID) -> Experiment:
    experiment = session.get(Experiment, experiment_id, with_for_update=True)
    if experiment is None:
        raise ExperimentNotFoundError(f"no experiment {experiment_id}")
    return experiment


def start_experiment(
    factory: SessionFactory, experiment_id: uuid.UUID, at: datetime | None = None
) -> TransitionOutcome:
    with stage(
        "experiment.transition",
        "experiment_transition",
        {"darwin.experiment.id": experiment_id, "darwin.experiment.transition": "start"},
    ) as s:
        result = _start_experiment(factory, experiment_id, at)
        s.set(**{"darwin.experiment.status": result.status})
        s.outcome = "changed" if result.changed else "refused"
        return result


def _start_experiment(
    factory: SessionFactory, experiment_id: uuid.UUID, at: datetime | None = None
) -> TransitionOutcome:
    """draft|paused -> running, only if every start-gate check passes. Opens a collection window."""
    with factory() as session:
        experiment = _load(session, experiment_id)
        before = experiment.status
        problems = start_gate(session, experiment)
        if problems:
            session.rollback()
            _log("experiment start refused", experiment_id=experiment_id, reasons=problems)
            return TransitionOutcome(experiment_id, experiment.status, False, tuple(problems))
        now = _when(at)
        if now <= experiment.status_changed_at:
            session.rollback()
            return TransitionOutcome(experiment_id, before, False, ("transition_time_not_after",))
        experiment.status = "running"
        experiment.status_changed_at = now
        if experiment.started_at is None:
            experiment.started_at = now
        try:
            session.commit()
        except IntegrityError:  # the one-active-per-page index (a concurrent start)
            session.rollback()
            return TransitionOutcome(experiment_id, before, False, ("another_experiment_active",))
        _log("experiment started", experiment_id=experiment_id)
        return TransitionOutcome(experiment_id, "running", True, ())


def _move(
    factory: SessionFactory,
    experiment_id: uuid.UUID,
    target: str,
    stop_reason: str | None = None,
    at: datetime | None = None,
) -> TransitionOutcome:
    with stage(
        "experiment.transition",
        "experiment_transition",
        {"darwin.experiment.id": experiment_id, "darwin.experiment.transition": target},
    ) as s:
        result = _move_now(factory, experiment_id, target, stop_reason, at)
        s.set(**{"darwin.experiment.status": result.status})
        s.outcome = "changed" if result.changed else "refused"
        return result


def _move_now(
    factory: SessionFactory,
    experiment_id: uuid.UUID,
    target: str,
    stop_reason: str | None = None,
    at: datetime | None = None,
) -> TransitionOutcome:
    with factory() as session:
        experiment = _load(session, experiment_id)
        current = experiment.status
        if (current, target) not in TRANSITIONS:
            session.rollback()
            return TransitionOutcome(experiment_id, current, False, ("transition_not_allowed",))
        now = _when(at)
        if now <= experiment.status_changed_at:
            session.rollback()
            return TransitionOutcome(experiment_id, current, False, ("transition_time_not_after",))
        experiment.status = target
        experiment.status_changed_at = now
        if target == "paused":
            experiment.paused_at = now
        if target in ("stopped", "completed"):
            experiment.stopped_at = now
            experiment.stop_reason = stop_reason
        session.commit()
        _log("experiment status changed", experiment_id=experiment_id, frm=current, to=target)
        return TransitionOutcome(experiment_id, target, True, ())


def pause_experiment(
    factory: SessionFactory, experiment_id: uuid.UUID, at: datetime | None = None
) -> TransitionOutcome:
    """running -> paused: closes the collection window; Generation 0 is served meanwhile."""
    return _move(factory, experiment_id, "paused", at=at)


def stop_experiment(
    factory: SessionFactory, experiment_id: uuid.UUID, reason: str, at: datetime | None = None
) -> TransitionOutcome:
    if reason not in STOP_REASONS:
        return TransitionOutcome(experiment_id, "unknown", False, ("stop_reason_unknown",))
    return _move(factory, experiment_id, "stopped", reason, at=at)


def complete_experiment(
    factory: SessionFactory, experiment_id: uuid.UUID, at: datetime | None = None
) -> TransitionOutcome:
    """Ends data collection. It does NOT promote, deploy or pick a variant."""
    return _move(factory, experiment_id, "completed", "planned_end", at=at)


# ---- analysis ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisOutcome:
    analysis_id: uuid.UUID
    experiment_id: uuid.UUID
    status: str
    assessment: str
    reason_codes: tuple[str, ...]
    report: dict[str, Any]
    report_hash: str


def report_hash(report: dict[str, Any]) -> str:
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _config(experiment: Experiment) -> AnalysisConfig:
    metrics: list[MetricName] = [experiment.primary_metric, *experiment.guardrail_metrics]  # type: ignore[list-item]
    return AnalysisConfig(
        experiment_key=experiment.experiment_key,
        primary_metric=metrics[0],
        guardrail_metrics=tuple(metrics[1:]),
        minimum_sample_per_variant=experiment.minimum_sample_per_variant,
        control_allocation_bp=experiment.control_allocation_bp,
        candidate_allocation_bp=experiment.candidate_allocation_bp,
        control_spec_hash=experiment.control_spec_hash,
        candidate_spec_hash=experiment.candidate_spec_hash,
        traffic_source=experiment.traffic_source,
    )


def analyze_experiment(
    factory: SessionFactory,
    experiment_id: uuid.UUID,
    as_of: datetime | None = None,
    *,
    fault: Callable[[], None] | None = None,  # tests/eval: simulate an analysis failure
) -> AnalysisOutcome:
    """Compute and persist one immutable analysis. Errors are recorded, never hidden.

    Traced as `experiment.analyze`: status, assessment — never the report, counts per
    session or ids of sessions.
    """
    with stage(
        "experiment.analyze", "experiment_analysis", {"darwin.experiment.id": experiment_id}
    ) as s:
        outcome = _analyze_experiment(factory, experiment_id, as_of, fault=fault)
        s.set(
            **{
                "darwin.status": outcome.status,
                "darwin.experiment.assessment": outcome.assessment,
            }
        )
        s.outcome = outcome.assessment
        return outcome


def _analyze_experiment(
    factory: SessionFactory,
    experiment_id: uuid.UUID,
    as_of: datetime | None = None,
    *,
    fault: Callable[[], None] | None = None,
) -> AnalysisOutcome:
    as_of = (as_of or datetime.now(UTC)).astimezone(UTC)
    with factory() as session:
        experiment = session.get(Experiment, experiment_id)
        if experiment is None:
            raise ExperimentNotFoundError(f"no experiment {experiment_id}")
        key = experiment.experiment_key
        try:
            if fault is not None:
                fault()
            config = _config(experiment)
            control, candidate, integrity, windows, boundary = collect(
                session,
                experiment,
                (config.primary_metric, *config.guardrail_metrics),
                as_of,
            )
            assessed = assess(
                config,
                control,
                candidate,
                integrity,
                as_of.isoformat(),
                [w.as_list() for w in windows],
                boundary,
            )
            row = ExperimentAnalysis(
                experiment_id=experiment.id,
                analysis_version=ANALYSIS_VERSION,
                as_of=as_of,
                status="completed",
                assessment=assessed.assessment,
                data_sufficiency=assessed.data_sufficiency,
                guardrail_status=assessed.guardrail_status,
                control_exposures=control.exposed,
                candidate_exposures=candidate.exposed,
                reason_codes=list(assessed.reason_codes),
                report=assessed.report,
                report_hash=report_hash(assessed.report),
                error_type=None,
            )
        except Exception as error:  # noqa: BLE001 — no conclusion; recorded as needs_review
            session.rollback()
            report = {
                "analysis_version": ANALYSIS_VERSION,
                "experiment_key": key,
                "as_of": as_of.isoformat(),
                "assessment": "needs_review",
                "reason_codes": ["analysis_error"],
                "error_type": type(error).__name__,
                "note": "Analysis failed; no conclusion can be drawn.",
            }
            row = ExperimentAnalysis(
                experiment_id=experiment_id,
                analysis_version=ANALYSIS_VERSION,
                as_of=as_of,
                status="analysis_error",
                assessment="needs_review",
                data_sufficiency=None,
                guardrail_status=None,
                control_exposures=0,
                candidate_exposures=0,
                reason_codes=["analysis_error"],
                report=report,
                report_hash=report_hash(report),
                error_type=type(error).__name__[:64],
            )
        session.add(row)
        session.commit()
        _log(
            "experiment analyzed",
            experiment_id=experiment_id,
            analysis_id=row.id,
            status=row.status,
            assessment=row.assessment,
            control=row.control_exposures,
            candidate=row.candidate_exposures,
        )
        return AnalysisOutcome(
            row.id,
            experiment_id,
            row.status,
            row.assessment,
            tuple(row.reason_codes),
            dict(row.report),
            row.report_hash,
        )
