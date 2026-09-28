"""The closed vocabulary of experiments: variants, lifecycle, allocations, metrics.

Everything a human (or a future model) can configure is chosen from these
lists. There are no free-form metric names, percentages or statuses; the
database CHECKs (migration 0010) repeat the same lists.

Metrics are defined only on telemetry DarwinUX already collects (Generation 0
emits page_view, button_click and form_error; the worker derives rage_click
and error_burst signals). Every metric is a SESSION-level binary outcome: did
an exposed session show the behaviour at least once, after its exposure?
There is no completion event today, so "task success" is deliberately absent.
"""

from dataclasses import dataclass
from typing import Literal, get_args

Variant = Literal["control", "candidate"]
VARIANTS: tuple[Variant, ...] = get_args(Variant)

ExperimentStatus = Literal["draft", "running", "paused", "stopped", "completed"]
EXPERIMENT_STATUSES: tuple[ExperimentStatus, ...] = get_args(ExperimentStatus)
ACTIVE_STATUSES: tuple[ExperimentStatus, ...] = ("running", "paused")
TERMINAL_STATUSES: tuple[ExperimentStatus, ...] = ("stopped", "completed")
# Every allowed lifecycle move. Enforced here AND by a database trigger.
TRANSITIONS: frozenset[tuple[ExperimentStatus, ExperimentStatus]] = frozenset(
    {
        ("draft", "running"),  # start (start gate + explicit human command)
        ("running", "paused"),
        ("paused", "running"),  # resume (start gate again)
        ("draft", "stopped"),  # cancel a draft
        ("running", "stopped"),
        ("paused", "stopped"),
        ("running", "completed"),
        ("paused", "completed"),
    }
)

StopReason = Literal["human_decision", "guardrail_concern", "candidate_issue", "planned_end"]
STOP_REASONS: tuple[StopReason, ...] = get_args(StopReason)

TrafficSource = Literal["simulated", "real"]
TRAFFIC_SOURCES: tuple[TrafficSource, ...] = get_args(TrafficSource)

# ---- allocation: integer basis points out of 10 000 buckets -----------------------------------

TOTAL_BUCKETS = 10_000
CandidateAllocation = Literal[100, 500, 1000, 2500, 5000]  # 1%, 5%, 10%, 25%, 50%
CANDIDATE_ALLOCATIONS_BP: tuple[int, ...] = get_args(CandidateAllocation)

# ---- sample floor ---------------------------------------------------------------------------

# An OPERATIONAL floor per variant, not a power calculation.
MIN_SAMPLE_FLOOR = 100
MIN_SAMPLE_CEILING = 100_000

EXPERIMENT_KEY_PATTERN = r"^[a-z][a-z0-9_]{2,63}$"
SPEC_HASH_PATTERN = r"^[0-9a-f]{64}$"

# ---- metrics --------------------------------------------------------------------------------

MetricName = Literal[
    "rage_click_session_rate",
    "error_burst_session_rate",
    "form_error_session_rate",
    "signup_submit_session_rate",
]
METRIC_NAMES: tuple[MetricName, ...] = get_args(MetricName)
Direction = Literal["lower_is_better", "higher_is_better"]

# The Generation 0 component the form metrics read. The start gate proves both
# variants still contain it (ids are protected, so a candidate cannot rename it).
SIGNUP_FORM_ID = "signup_form"
SIGNUP_SUBMIT_COMPONENT = f"{SIGNUP_FORM_ID}_submit"  # SignupForm tracks `${id}_submit`


@dataclass(frozen=True)
class MetricDefinition:
    name: MetricName
    direction: Direction
    source: Literal["signal", "event"]
    signal_type: str | None = None
    event_type: str | None = None
    component: str | None = None
    description: str = ""


METRICS: dict[MetricName, MetricDefinition] = {
    m.name: m
    for m in (
        MetricDefinition(
            "rage_click_session_rate",
            "lower_is_better",
            "signal",
            signal_type="rage_click",
            description="Share of exposed sessions with >= 1 canonical rage_click signal "
            "(4 clicks on one control within 2 s) after exposure.",
        ),
        MetricDefinition(
            "error_burst_session_rate",
            "lower_is_better",
            "signal",
            signal_type="error_burst",
            description="Share of exposed sessions with >= 1 canonical error_burst signal "
            "(3 form/client errors within 10 s) after exposure.",
        ),
        MetricDefinition(
            "form_error_session_rate",
            "lower_is_better",
            "event",
            event_type="form_error",
            component=SIGNUP_FORM_ID,
            description="Share of exposed sessions with >= 1 form_error on the signup form "
            "after exposure.",
        ),
        MetricDefinition(
            "signup_submit_session_rate",
            "higher_is_better",
            "event",
            event_type="button_click",
            component=SIGNUP_SUBMIT_COMPONENT,
            description="Share of exposed sessions that pressed the signup form's submit "
            "button at least once after exposure (an attempt, not a completion).",
        ),
    )
}

# Guardrail rule: `watch` when the point estimate is worse by more than this
# (absolute rate), `breach` when the whole 95% difference interval is worse.
GUARDRAIL_WATCH_TOLERANCE = 0.02
MAX_GUARDRAILS = len(METRIC_NAMES) - 1

# ---- telemetry event types this layer adds --------------------------------------------------

EXPOSURE_EVENT = "experiment_exposure"
FALLBACK_EVENT = "experiment_fallback"
FallbackReason = Literal[
    "spec_invalid", "render_error", "spec_unavailable", "spec_hash_mismatch", "assignment_error"
]
FALLBACK_REASONS: tuple[FallbackReason, ...] = get_args(FallbackReason)

# ---- analysis ---------------------------------------------------------------------------------

ANALYSIS_VERSION = "experiment_analysis.v1"
Assessment = Literal["insufficient_data", "evidence_ready", "needs_review", "stop_recommended"]
ASSESSMENTS: tuple[Assessment, ...] = get_args(Assessment)
AnalysisStatus = Literal["completed", "analysis_error"]
ANALYSIS_STATUSES: tuple[AnalysisStatus, ...] = get_args(AnalysisStatus)


def sql_in(column: str, values: tuple[object, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"
