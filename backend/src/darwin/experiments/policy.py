"""experiment_analysis.v1: aggregate counts -> evidence report + assessment. Pure.

The assessment is a statement about the EVIDENCE, never about the candidate:

  stop_recommended   a guardrail's whole 95% difference interval is on the harmful side
  needs_review       integrity problems (candidate render fallbacks, rejected,
                     mismatched or out-of-window exposures), a guardrail `watch`
                     with enough data, or an analysis error
  insufficient_data  a variant has fewer exposed sessions than the configured floor
  evidence_ready     enough data, no guardrail concern: a human reads the numbers

Priority: stop_recommended > needs_review(integrity) > insufficient_data >
needs_review(watch) > evidence_ready. Every triggered condition is listed in
reason_codes, so the highest-priority one never hides the others.

There is no "winner", "promote" or "significant" field. `interval_excludes_zero`
is reported as a plain fact next to the interval. A guardrail breach is a
FLAG: nothing is paused, stopped or rolled back automatically in Step 14.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .stats import Comparison, compare
from .vocabulary import (
    ANALYSIS_VERSION,
    GUARDRAIL_WATCH_TOLERANCE,
    METRICS,
    Assessment,
    MetricName,
    Variant,
)

REASON_CODES = (
    "evidence_ready",
    "insufficient_samples",
    "guardrail_breach",
    "guardrail_watch",
    "candidate_fallbacks",
    "rejected_exposures",
    "assignment_mismatch",
    "exposure_outside_window",
    "analysis_error",
)
NOTE = (
    "Aggregate evidence for human review. DarwinUX does not declare winners, promote "
    "candidates or change traffic from this report."
)


@dataclass(frozen=True)
class ArmCounts:
    exposed: int
    successes: dict[MetricName, int]


@dataclass(frozen=True)
class Integrity:
    fallback_sessions: dict[Variant, int] = field(
        default_factory=lambda: {"control": 0, "candidate": 0}
    )
    rejected_exposure_sessions: int = 0
    assignment_mismatches: int = 0
    exposures_outside_windows: int = 0


@dataclass(frozen=True)
class AnalysisConfig:
    experiment_key: str
    primary_metric: MetricName
    guardrail_metrics: tuple[MetricName, ...]
    minimum_sample_per_variant: int
    control_allocation_bp: int
    candidate_allocation_bp: int
    control_spec_hash: str
    candidate_spec_hash: str
    traffic_source: str


def guardrail_status(metric: MetricName, comparison: Comparison) -> str:
    interval, d = comparison.interval, comparison.absolute_difference
    if interval is None or d is None:
        return "not_evaluable"
    lower_is_better = METRICS[metric].direction == "lower_is_better"
    harm = d if lower_is_better else -d
    clearly_harmful = interval.lower > 0 if lower_is_better else interval.upper < 0
    if clearly_harmful:
        return "breach"
    if harm > GUARDRAIL_WATCH_TOLERANCE:
        return "watch"
    return "ok"


def _metric_block(metric: MetricName, control: ArmCounts, candidate: ArmCounts) -> Comparison:
    return compare(
        control.successes[metric],
        control.exposed,
        candidate.successes[metric],
        candidate.exposed,
    )


@dataclass(frozen=True)
class Assessed:
    assessment: Assessment
    reason_codes: tuple[str, ...]
    data_sufficiency: str
    guardrail_status: str
    report: dict[str, Any]


def assess(
    config: AnalysisConfig,
    control: ArmCounts,
    candidate: ArmCounts,
    integrity: Integrity,
    as_of: str,
    collection_windows: Sequence[Sequence[str]] = (),
    boundary_signals_excluded: int = 0,
) -> Assessed:
    for arm_counts in (control, candidate):
        for name, value in arm_counts.successes.items():
            if not 0 <= value <= arm_counts.exposed:
                raise ValueError(f"{name}: successes outside [0, exposed]")

    sufficient = min(control.exposed, candidate.exposed) >= config.minimum_sample_per_variant
    primary = _metric_block(config.primary_metric, control, candidate)
    guardrails = []
    statuses = []
    for metric in config.guardrail_metrics:
        comparison = _metric_block(metric, control, candidate)
        status = guardrail_status(metric, comparison)
        statuses.append(status)
        guardrails.append(
            {
                "metric": metric,
                "direction": METRICS[metric].direction,
                "status": status,
                **comparison.as_dict(),
            }
        )

    reasons: list[str] = []
    if "breach" in statuses:
        reasons.append("guardrail_breach")
    integrity_issue = False
    if integrity.fallback_sessions.get("candidate", 0) or integrity.fallback_sessions.get(
        "control", 0
    ):
        reasons.append("candidate_fallbacks")
        integrity_issue = True
    if integrity.rejected_exposure_sessions:
        reasons.append("rejected_exposures")
        integrity_issue = True
    if integrity.assignment_mismatches:
        reasons.append("assignment_mismatch")
        integrity_issue = True
    if integrity.exposures_outside_windows:
        reasons.append("exposure_outside_window")
        integrity_issue = True
    if not sufficient:
        reasons.append("insufficient_samples")
    if "watch" in statuses:
        reasons.append("guardrail_watch")

    assessment: Assessment
    if "guardrail_breach" in reasons:
        assessment = "stop_recommended"
    elif integrity_issue:
        assessment = "needs_review"
    elif not sufficient:
        assessment = "insufficient_data"
    elif "guardrail_watch" in reasons:
        assessment = "needs_review"
    else:
        assessment = "evidence_ready"
        reasons.append("evidence_ready")

    overall_guardrail = (
        "breach"
        if "breach" in statuses
        else "watch"
        if "watch" in statuses
        else "not_evaluable"
        if "not_evaluable" in statuses
        else "ok"
    )
    data_sufficiency = "sufficient" if sufficient else "insufficient_data"
    report: dict[str, Any] = {
        "analysis_version": ANALYSIS_VERSION,
        "experiment_key": config.experiment_key,
        "as_of": as_of,
        "traffic_source": config.traffic_source,
        "control_spec_hash": config.control_spec_hash,
        "candidate_spec_hash": config.candidate_spec_hash,
        "allocation_bp": {
            "control": config.control_allocation_bp,
            "candidate": config.candidate_allocation_bp,
        },
        "minimum_sample_per_variant": config.minimum_sample_per_variant,
        "exposed_sessions": {"control": control.exposed, "candidate": candidate.exposed},
        "data_sufficiency": data_sufficiency,
        "primary": {
            "metric": config.primary_metric,
            "direction": METRICS[config.primary_metric].direction,
            **primary.as_dict(),
        },
        "guardrails": guardrails,
        "guardrail_status": overall_guardrail,
        "integrity": {
            "fallback_sessions": dict(integrity.fallback_sessions),
            "rejected_exposure_sessions": integrity.rejected_exposure_sessions,
            "assignment_mismatches": integrity.assignment_mismatches,
            "exposures_outside_windows": integrity.exposures_outside_windows,
            "boundary_signals_excluded": boundary_signals_excluded,
        },
        # The running intervals whose evidence this report counts, [opened, closed).
        "collection_windows": [list(w) for w in collection_windows],
        "assessment": assessment,
        "reason_codes": reasons,
        "note": NOTE,
    }
    return Assessed(assessment, tuple(reasons), data_sufficiency, overall_guardrail, report)
