"""promotion_policy.v1: is this analysed experiment ELIGIBLE FOR HUMAN APPROVAL?

Eligible never means "the candidate won". It means every hard gate holds, so a
human may decide. There is no override: every blocking reason is a hard stop.

The review re-derives everything from persisted records, starting from one
ExperimentAnalysis:

  analysis   exists, experiment_analysis.v1, completed, report_hash still matches the
             stored report, report names this experiment and its two spec hashes,
             assessment evidence_ready with reasons exactly [evidence_ready],
             data sufficient, guardrails ok, zero integrity flags (fallbacks,
             refused / mismatched / out-of-window exposures), cut off at or after the
             experiment's completion, and no newer analysis of the experiment reports
             different evidence
  experiment completed (not stopped/paused/running); stored spec hashes still equal
             the content of both spec rows; no other experiment on the candidate is
             active or was stopped for a guardrail or candidate concern; no experiment
             is active on the page (a promotion would change its control)
  candidate  Step 14 eligibility re-derived (Step 13 pass, no newer non-pass
             evaluation, provenance chain intact: MutationRun -> DecisionRun ->
             ResearchRun -> BehaviorSignal, hashes re-computed) and equal to the
             experiment's candidate
  generation the page has an active-generation pointer; the candidate's parent IS the
             active generation (stale source otherwise); the target generation is
             computed here — max(generation on the page) + 1 — never chosen by a caller

The evidence hash binds an approval to exactly these facts; promotion recomputes it
under a lock and refuses on any difference (TOCTOU).
"""

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darwin.db.models import Experiment, ExperimentAnalysis, UISpecVersion
from darwin.experiments.eligibility import ExperimentRefused, check_eligibility
from darwin.experiments.service import report_hash
from darwin.experiments.vocabulary import ANALYSIS_VERSION
from darwin.mutations.apply import content_hash

from .active import active_pointer
from .vocabulary import POLICY_VERSION


class AnalysisNotFoundError(LookupError):
    pass


class PointerMissingError(RuntimeError):
    """No active-generation pointer for the page: run `make generation-bootstrap`."""


@dataclass(frozen=True)
class PromotionEvidence:
    """Everything an approval is bound to. Aggregates and ids only — no raw events."""

    policy_version: str
    page_id: str
    candidate_spec_id: str
    candidate_spec_hash: str
    candidate_evaluation_run_id: str
    experiment_id: str
    experiment_key: str
    experiment_status: str
    experiment_stopped_at: str | None
    traffic_source: str
    control_spec_hash: str
    experiment_analysis_id: str
    analysis_version: str
    analysis_report_hash: str
    analysis_as_of: str
    assessment: str
    source_spec_id: str
    source_spec_hash: str
    source_generation: int
    target_generation: int

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def hash(self) -> str:
        return hashlib.sha256(self.canonical().encode("ascii")).hexdigest()


@dataclass(frozen=True)
class Review:
    evidence: PromotionEvidence
    blocking: tuple[str, ...]
    # What a human reads: the aggregate report's evidence, no winner label.
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return not self.blocking


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def next_generation(session: Session, page_id: str) -> int:
    """max(generation of any baseline/promoted row on the page) + 1: monotonic, never reused."""
    current = session.scalar(
        select(func.max(UISpecVersion.generation)).where(
            UISpecVersion.page_id == page_id,
            UISpecVersion.status.in_(("baseline", "promoted")),
        )
    )
    return (current if current is not None else -1) + 1


def review(session: Session, experiment_analysis_id: uuid.UUID) -> Review:
    analysis = session.get(ExperimentAnalysis, experiment_analysis_id)
    if analysis is None:
        raise AnalysisNotFoundError(f"no experiment analysis {experiment_analysis_id}")
    experiment = session.get(Experiment, analysis.experiment_id)
    assert experiment is not None  # FK
    candidate = session.get(UISpecVersion, experiment.candidate_spec_id)
    control = session.get(UISpecVersion, experiment.control_spec_id)
    assert candidate is not None and control is not None  # FK
    pointer = active_pointer(session, experiment.page_id)
    if pointer is None:
        raise PointerMissingError(f"no active generation for page {experiment.page_id}")
    source = session.get(UISpecVersion, pointer.ui_spec_version_id)
    assert source is not None  # FK
    blocking: list[str] = []
    report = analysis.report or {}

    # ---- the analysis
    if analysis.analysis_version != ANALYSIS_VERSION:
        blocking.append("analysis_version_unknown")
    if analysis.status != "completed":
        blocking.append("analysis_error")
    if report_hash(report) != analysis.report_hash:
        blocking.append("analysis_report_tampered")
    if (
        report.get("experiment_key") != experiment.experiment_key
        or report.get("candidate_spec_hash") != experiment.candidate_spec_hash
        or report.get("control_spec_hash") != experiment.control_spec_hash
    ):
        blocking.append("analysis_experiment_mismatch")
    if analysis.assessment != "evidence_ready":
        blocking.append(f"analysis_{analysis.assessment}")
    if list(analysis.reason_codes) != ["evidence_ready"]:
        blocking.append("analysis_reasons_not_clean")
    if analysis.data_sufficiency != "sufficient":
        blocking.append("analysis_insufficient_samples")
    if analysis.guardrail_status != "ok":
        blocking.append("guardrails_not_ok")
    integrity = report.get("integrity", {})
    fallbacks = integrity.get("fallback_sessions", {})
    if (
        any(v for v in fallbacks.values())
        or integrity.get("rejected_exposure_sessions")
        or integrity.get("assignment_mismatches")
        or integrity.get("exposures_outside_windows")
    ):
        blocking.append("analysis_integrity_flag")
    if experiment.stopped_at is None or analysis.as_of < experiment.stopped_at:
        blocking.append("analysis_before_completion")
    # Newer evidence: another analysis of this experiment, created at or after this one
    # (ties fail closed), whose report differs. A rerun with the same report is not new
    # evidence.
    newer = session.scalar(
        select(ExperimentAnalysis.id)
        .where(
            ExperimentAnalysis.experiment_id == experiment.id,
            ExperimentAnalysis.id != analysis.id,
            ExperimentAnalysis.created_at >= analysis.created_at,
            ExperimentAnalysis.report_hash != analysis.report_hash,
        )
        .limit(1)
    )
    if newer is not None:
        blocking.append("analysis_not_latest")

    # ---- the experiment
    if experiment.status != "completed":
        blocking.append(f"experiment_{experiment.status}")
    if (
        content_hash(candidate.spec) != experiment.candidate_spec_hash
        or content_hash(control.spec) != experiment.control_spec_hash
    ):
        blocking.append("experiment_spec_hash_mismatch")
    conflicting = session.scalar(
        select(Experiment.id)
        .where(
            Experiment.candidate_spec_id == candidate.id,
            Experiment.id != experiment.id,
            Experiment.status.in_(("running", "paused"))
            | (
                (Experiment.status == "stopped")
                & Experiment.stop_reason.in_(("guardrail_concern", "candidate_issue"))
            ),
        )
        .limit(1)
    )
    if conflicting is not None:
        blocking.append("conflicting_experiment")
    active_on_page = session.scalar(
        select(Experiment.id)
        .where(
            Experiment.page_id == experiment.page_id, Experiment.status.in_(("running", "paused"))
        )
        .limit(1)
    )
    if active_on_page is not None:
        blocking.append("experiment_active_on_page")

    # ---- the candidate (Step 13/14 re-derived) and the source generation
    try:
        eligible = check_eligibility(session, experiment.candidate_evaluation_run_id)
        if (
            eligible.candidate_spec_id != candidate.id
            or eligible.candidate_spec_hash != experiment.candidate_spec_hash
        ):
            blocking.append("candidate_mismatch")
    except ExperimentRefused as refused:
        blocking.extend(
            "stale_source_generation" if code == "control_not_active_generation" else code
            for code in refused.codes
        )
        if refused.detail:
            blocking.append(refused.detail)
    if candidate.parent_id != source.id or experiment.control_spec_id != source.id:
        blocking.append("stale_source_generation")
    if candidate.status != "candidate":
        blocking.append("not_a_candidate")

    evidence = PromotionEvidence(
        policy_version=POLICY_VERSION,
        page_id=experiment.page_id,
        candidate_spec_id=str(candidate.id),
        candidate_spec_hash=candidate.content_hash,
        candidate_evaluation_run_id=str(experiment.candidate_evaluation_run_id),
        experiment_id=str(experiment.id),
        experiment_key=experiment.experiment_key,
        experiment_status=experiment.status,
        experiment_stopped_at=_iso(experiment.stopped_at),
        traffic_source=experiment.traffic_source,
        control_spec_hash=experiment.control_spec_hash,
        experiment_analysis_id=str(analysis.id),
        analysis_version=analysis.analysis_version,
        analysis_report_hash=analysis.report_hash,
        analysis_as_of=analysis.as_of.isoformat(),
        assessment=analysis.assessment,
        source_spec_id=str(source.id),
        source_spec_hash=source.content_hash,
        source_generation=pointer.generation,
        target_generation=next_generation(session, experiment.page_id),
    )
    summary = {
        "experiment_key": experiment.experiment_key,
        "traffic_source": experiment.traffic_source,
        "assessment": analysis.assessment,
        "exposed_sessions": report.get("exposed_sessions"),
        "primary": {
            k: report.get("primary", {}).get(k)
            for k in ("metric", "direction", "absolute_difference", "difference_95")
        },
        "guardrails": [
            {k: g.get(k) for k in ("metric", "status", "difference_95")}
            for g in report.get("guardrails", [])
        ],
        "report_hash": analysis.report_hash,
    }
    return Review(evidence, tuple(dict.fromkeys(blocking)), summary)
