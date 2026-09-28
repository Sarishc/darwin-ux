"""Step 14 unit tests: assignment, allocation, vocabulary, statistics, policy, serving,
exposure validation, CLI and API boundaries. No database."""

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from darwin.api.experiments import get_session_factory
from darwin.config import Settings
from darwin.experiments import cli
from darwin.experiments.assignment import (
    AllocationError,
    assign,
    bucket,
    validate_allocation,
    validate_key,
)
from darwin.experiments.eligibility import validate_config
from darwin.experiments.evaluation import ExperimentCase, load_dataset, score
from darwin.experiments.exposure import record_exposure
from darwin.experiments.policy import (
    REASON_CODES,
    AnalysisConfig,
    ArmCounts,
    Integrity,
    assess,
    guardrail_status,
)
from darwin.experiments.serving import resolve_variant
from darwin.experiments.stats import Z_95, arm, compare, newcombe_difference, wilson
from darwin.experiments.vocabulary import (
    CANDIDATE_ALLOCATIONS_BP,
    METRIC_NAMES,
    METRICS,
    TOTAL_BUCKETS,
    TRANSITIONS,
    MetricName,
)
from darwin.main import create_app
from darwin.telemetry.schemas import TelemetryEvent

BACKEND_DIR = Path(__file__).resolve().parents[1]
MIGRATION = BACKEND_DIR / "alembic" / "versions" / "0010_create_experiments.py"
KEY = "rage_fix_pricing"
SESSIONS = [uuid.uuid5(uuid.NAMESPACE_URL, f"s{i}") for i in range(20_000)]

# ---- assignment -------------------------------------------------------------------------------

# Pinned: sha256("<key>:<uuid>")[:8] big-endian % 10000. If these change, every running
# experiment would silently reshuffle its users — so they must never change.
PINNED = [
    ("00000000-0000-4000-8000-000000000001", 8384, 9905),
    ("5b2f0c8e-4c1a-4a5e-9d6f-0a1b2c3d4e5f", 640, 8897),
    ("9c8b7a6f-5e4d-4c3b-8a29-1f0e9d8c7b6a", 3954, 7679),
]


@pytest.mark.parametrize(("session_id", "bucket_a", "bucket_b"), PINNED)
def test_buckets_are_pinned(session_id: str, bucket_a: int, bucket_b: int) -> None:
    sid = uuid.UUID(session_id)
    assert bucket(KEY, sid) == bucket_a
    assert bucket("other_experiment", sid) == bucket_b  # salted by experiment key


def test_assignment_is_stable_across_processes_with_different_hash_seeds() -> None:
    code = (
        "import uuid;from darwin.experiments.assignment import bucket;"
        f"print(bucket('{KEY}', uuid.UUID('{PINNED[1][0]}')))"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in ("0", "1", "12345")
    }
    assert outputs == {str(PINNED[1][1])}


def test_same_session_same_variant_and_boundaries_are_exact() -> None:
    for bp in CANDIDATE_ALLOCATIONS_BP:
        for sid in SESSIONS[:3000]:
            b = bucket(KEY, sid)
            assert 0 <= b < TOTAL_BUCKETS
            assert assign(KEY, sid, bp) == ("candidate" if b < bp else "control")
            assert assign(KEY, sid, bp) == assign(KEY, sid, bp)


@pytest.mark.parametrize("bp", CANDIDATE_ALLOCATIONS_BP)
def test_candidate_share_matches_allocation(bp: int) -> None:
    share = sum(assign(KEY, s, bp) == "candidate" for s in SESSIONS) / len(SESSIONS)
    assert abs(share - bp / TOTAL_BUCKETS) < 0.01


def test_allocation_is_monotonic_a_candidate_stays_candidate_when_traffic_grows() -> None:
    small = {s for s in SESSIONS if assign(KEY, s, 1000) == "candidate"}
    large = {s for s in SESSIONS if assign(KEY, s, 2500) == "candidate"}
    assert small <= large


def test_experiments_bucket_independently() -> None:
    a = [assign("experiment_alpha", s, 5000) for s in SESSIONS[:10_000]]
    b = [assign("experiment_beta", s, 5000) for s in SESSIONS[:10_000]]
    assert 0.47 < sum(x == y for x, y in zip(a, b, strict=True)) / len(a) < 0.53


@pytest.mark.parametrize("bp", CANDIDATE_ALLOCATIONS_BP)
def test_allowlisted_allocations_accepted(bp: int) -> None:
    assert validate_allocation(bp) == bp


@pytest.mark.parametrize(
    "value", [0, 10_000, 5100, 9999, -100, 99, 101, 1000.0, 0.1, True, False, "1000", None, [1000]]
)
def test_other_allocations_refused(value: object) -> None:
    with pytest.raises(AllocationError):
        validate_allocation(value)
    with pytest.raises(AllocationError):
        assign(KEY, SESSIONS[0], value)  # type: ignore[arg-type]


@pytest.mark.parametrize("key", ["", "ab", "Rage", "rage-fix", "1rage", "rage fix", "x" * 65, None])
def test_invalid_keys_refused(key: object) -> None:
    with pytest.raises(AllocationError):
        validate_key(key)


def test_session_must_be_a_uuid() -> None:
    with pytest.raises(AllocationError):
        bucket(KEY, "5b2f0c8e-4c1a-4a5e-9d6f-0a1b2c3d4e5f")  # type: ignore[arg-type]


# ---- vocabulary and configuration ---------------------------------------------------------------


def test_metric_vocabulary_is_closed_and_grounded_in_existing_telemetry() -> None:
    assert set(METRICS) == set(METRIC_NAMES) and len(METRICS) == 4
    for metric in METRICS.values():
        assert metric.source in ("signal", "event")
        if metric.source == "signal":
            assert metric.signal_type in ("rage_click", "error_burst")  # Step 5 detectors
        else:
            assert metric.event_type in ("form_error", "button_click")  # Generation 0 events
    assert METRICS["signup_submit_session_rate"].direction == "higher_is_better"


def test_migration_repeats_the_vocabulary() -> None:
    source = MIGRATION.read_text()
    for name in METRIC_NAMES:
        assert name in source
    assert "IN (100, 500, 1000, 2500, 5000)" in source
    for old, new in TRANSITIONS:
        assert f"('{old}','{new}')" in source


def _config(**overrides: Any) -> list[str]:
    values: dict[str, Any] = {
        "experiment_key": KEY,
        "candidate_allocation_bp": 1000,
        "primary_metric": "rage_click_session_rate",
        "guardrail_metrics": ["form_error_session_rate"],
        "minimum_sample_per_variant": 100,
        "traffic_source": "simulated",
    } | overrides
    return validate_config(**values)


def test_valid_configuration_has_no_problems() -> None:
    assert _config() == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"primary_metric": "conversion_rate"}, "primary_metric_unknown"),
        ({"guardrail_metrics": []}, "guardrails_missing_or_too_many"),
        ({"guardrail_metrics": ["revenue"]}, "guardrail_metric_unknown"),
        ({"guardrail_metrics": ["form_error_session_rate"] * 2}, "guardrail_metric_duplicated"),
        ({"guardrail_metrics": ["rage_click_session_rate"]}, "primary_metric_is_guardrail"),
        ({"minimum_sample_per_variant": 99}, "minimum_sample_invalid"),
        ({"minimum_sample_per_variant": 100.0}, "minimum_sample_invalid"),
        ({"candidate_allocation_bp": 7500}, "allocation_not_allowlisted"),
        ({"traffic_source": "production"}, "traffic_source_unknown"),
        ({"experiment_key": "Bad Key"}, "experiment_key_invalid"),
    ],
)
def test_configuration_problems_are_reported(overrides: dict[str, Any], code: str) -> None:
    assert code in _config(**overrides)


# ---- statistics ---------------------------------------------------------------------------------


def test_wilson_reference_values() -> None:
    assert Z_95 == pytest.approx(1.959963984540054)
    zero, full, half = wilson(0, 10), wilson(10, 10), wilson(50, 100)
    assert zero and full and half
    assert (zero.lower, zero.upper) == pytest.approx((0.0, 0.277533), abs=1e-6)
    assert (full.lower, full.upper) == pytest.approx((0.722467, 1.0), abs=1e-6)
    assert (half.lower, half.upper) == pytest.approx((0.403832, 0.596168), abs=1e-6)


def test_newcombe_matches_the_published_example() -> None:
    # Newcombe (1998), method 10: 56/70 − 48/80 = 0.200, 95% CI 0.0524 to 0.3339.
    interval = newcombe_difference(48, 80, 56, 70)
    assert interval is not None
    assert (interval.lower, interval.upper) == pytest.approx((0.0524, 0.3339), abs=1e-4)


def test_zero_denominators_give_no_rate_rather_than_a_crash() -> None:
    assert wilson(0, 0) is None
    assert newcombe_difference(0, 0, 3, 10) is None
    summary = compare(0, 0, 3, 10).as_dict()
    assert summary["absolute_difference"] is None and summary["difference_95"] is None
    assert arm(0, 0).as_dict()["rate"] is None


def test_zero_and_full_success_edge_cases_keep_intervals_defined() -> None:
    zeros = compare(0, 150, 0, 150).as_dict()
    fulls = compare(150, 150, 150, 150).as_dict()
    for summary in (zeros, fulls):
        assert summary["absolute_difference"] == 0.0
        interval: Any = summary["difference_95"]
        low, high = interval
        assert low < 0 < high  # never a zero-width interval
        assert summary["interval_excludes_zero"] is False
    assert zeros["relative_difference"] is None  # undefined when the control rate is 0
    assert fulls["relative_difference"] == 0.0


def test_intervals_stay_in_range_and_contain_the_estimate() -> None:
    for x1, n1, x2, n2 in [(0, 5, 5, 5), (5, 5, 0, 5), (1, 1000, 999, 1000), (3, 7, 4, 9)]:
        c = compare(x1, n1, x2, n2)
        assert c.interval and c.absolute_difference is not None
        assert -1 <= c.interval.lower <= c.absolute_difference <= c.interval.upper <= 1


@pytest.mark.parametrize(("x", "n"), [(-1, 10), (11, 10), (1, -1)])
def test_impossible_counts_are_refused(x: int, n: int) -> None:
    with pytest.raises(ValueError):
        wilson(x, n)


def test_float_counts_are_refused() -> None:
    with pytest.raises(TypeError):
        wilson(1.0, 10)  # type: ignore[arg-type]


# ---- policy -------------------------------------------------------------------------------------

CONFIG = AnalysisConfig(
    experiment_key=KEY,
    primary_metric="rage_click_session_rate",
    guardrail_metrics=("form_error_session_rate", "signup_submit_session_rate"),
    minimum_sample_per_variant=100,
    control_allocation_bp=5000,
    candidate_allocation_bp=5000,
    control_spec_hash="a" * 64,
    candidate_spec_hash="b" * 64,
    traffic_source="simulated",
)
AS_OF = "2026-09-27T00:00:00+00:00"


def _arm(n: int, rage: int = 0, form: int = 0, submit: int = 0) -> ArmCounts:
    return ArmCounts(
        n,
        {
            "rage_click_session_rate": rage,
            "form_error_session_rate": form,
            "signup_submit_session_rate": submit,
        },
    )


def test_numerically_better_candidate_is_only_evidence_never_a_winner() -> None:
    result = assess(CONFIG, _arm(200, rage=80), _arm(200, rage=20), Integrity(), AS_OF)
    assert result.assessment == "evidence_ready"
    assert result.report["primary"]["interval_excludes_zero"] is True
    fields = json.dumps({k: v for k, v in result.report.items() if k != "note"}).lower()
    for forbidden in ("winner", "promot", "significant", "deploy", "best", "decision"):
        assert forbidden not in fields
    assert "does not declare winners" in result.report["note"]


def test_insufficient_data_is_not_a_conclusion() -> None:
    result = assess(CONFIG, _arm(99, rage=50), _arm(500, rage=0), Integrity(), AS_OF)
    assert result.assessment == "insufficient_data"
    assert result.data_sufficiency == "insufficient_data"


def test_guardrail_breach_flags_stop_even_with_little_data() -> None:
    result = assess(CONFIG, _arm(50, form=1), _arm(50, form=30), Integrity(), AS_OF)
    assert result.assessment == "stop_recommended"
    assert {"guardrail_breach", "insufficient_samples"} <= set(result.reason_codes)


def test_higher_is_better_guardrail_breaches_when_it_drops() -> None:
    comparison = compare(100, 200, 40, 200)
    assert guardrail_status("signup_submit_session_rate", comparison) == "breach"
    assert guardrail_status("form_error_session_rate", comparison) == "ok"  # fewer errors = fine


def test_watch_needs_review_only_with_enough_data() -> None:
    enough = assess(CONFIG, _arm(150, form=15), _arm(150, form=22), Integrity(), AS_OF)
    little = assess(CONFIG, _arm(40, form=4), _arm(40, form=7), Integrity(), AS_OF)
    assert enough.assessment == "needs_review" and "guardrail_watch" in enough.reason_codes
    assert little.assessment == "insufficient_data"


@pytest.mark.parametrize(
    ("integrity", "code"),
    [
        (Integrity(fallback_sessions={"control": 0, "candidate": 3}), "candidate_fallbacks"),
        (Integrity(rejected_exposure_sessions=1), "rejected_exposures"),
        (Integrity(assignment_mismatches=1), "assignment_mismatch"),
    ],
)
def test_integrity_problems_need_review(integrity: Integrity, code: str) -> None:
    result = assess(CONFIG, _arm(200, rage=40), _arm(200, rage=10), integrity, AS_OF)
    assert result.assessment == "needs_review" and code in result.reason_codes


def test_reason_codes_are_closed_and_counts_validated() -> None:
    result = assess(CONFIG, _arm(10), _arm(10), Integrity(), AS_OF)
    assert set(result.reason_codes) <= set(REASON_CODES)
    with pytest.raises(ValueError):
        assess(CONFIG, _arm(10, rage=11), _arm(10), Integrity(), AS_OF)


def test_report_is_aggregate_only_and_deterministic() -> None:
    a = assess(CONFIG, _arm(120, 30, 10, 40), _arm(120, 12, 10, 44), Integrity(), AS_OF)
    b = assess(CONFIG, _arm(120, 30, 10, 40), _arm(120, 12, 10, 44), Integrity(), AS_OF)
    assert a.report == b.report
    flat = json.dumps(a.report)
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", flat)  # no session (or any) UUIDs
    assert a.report["integrity"]["rejected_exposure_sessions"] == 0  # counts, not ids


# ---- serving and exposure validation (stubs, no database) --------------------------------------


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)

    def __getattr__(self, name: str) -> Any:  # typing only; real attributes come from kw
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        self.__dict__[name] = value


class _StubSession:
    def __init__(self, experiment: Any, spec_row: Any) -> None:
        self.experiment, self.spec_row = experiment, spec_row

    def scalar(self, _statement: Any) -> Any:
        return self.experiment

    def get(self, _model: Any, _id: Any) -> Any:
        return self.spec_row


def _experiment(**kw: Any) -> _Row:
    return _Row(
        experiment_key=KEY,
        candidate_allocation_bp=5000,
        control_spec_id=uuid.uuid4(),
        candidate_spec_id=uuid.uuid4(),
        control_spec_hash="a" * 64,
        candidate_spec_hash="b" * 64,
        **kw,
    )


def test_no_running_experiment_serves_nothing() -> None:
    served = resolve_variant(_StubSession(None, None), SESSIONS[0], "pricing_signup")  # type: ignore[arg-type]
    assert served.status == "none" and served.spec is None


def test_missing_spec_row_falls_back_never_serves() -> None:
    served = resolve_variant(_StubSession(_experiment(), None), SESSIONS[0], "pricing_signup")  # type: ignore[arg-type]
    assert (served.status, served.reason, served.spec) == ("fallback", "spec_unavailable", None)


def test_spec_whose_content_does_not_match_the_hash_falls_back() -> None:
    row = _Row(content_hash="b" * 64, spec={"generation": 1})  # stored hash claims b, content isn't
    for sid in SESSIONS[:40]:
        served = resolve_variant(_StubSession(_experiment(), row), sid, "pricing_signup")  # type: ignore[arg-type]
        assert served.status == "fallback" and served.spec is None


def test_broken_allocation_serves_fallback() -> None:
    broken = _experiment()
    broken.candidate_allocation_bp = 3333
    served = resolve_variant(
        _StubSession(broken, None),  # type: ignore[arg-type]
        SESSIONS[0],
        "pricing_signup",
    )
    assert (served.status, served.reason) == ("fallback", "assignment_error")


class _NoDatabase:
    def begin(self) -> Any:
        raise AssertionError("an invalid exposure must be refused before touching the database")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"experiment": KEY, "variant": "candidate", "spec_hash": "b" * 64},  # no generation
        {"experiment": KEY, "variant": "treatment", "spec_hash": "b" * 64, "generation": 1},
        {"experiment": "Bad!", "variant": "control", "spec_hash": "a" * 64, "generation": 0},
        {"experiment": KEY, "variant": "control", "spec_hash": "xyz", "generation": 0},
        {"experiment": KEY, "variant": "control", "spec_hash": "a" * 64, "generation": 0, "e": 1},
    ],
)
def test_malformed_exposures_are_rejected_without_database_access(payload: dict[str, Any]) -> None:
    event = TelemetryEvent(
        event_id=uuid.uuid4(),
        event_type="experiment_exposure",
        session_id=SESSIONS[0],
        occurred_at=datetime.now(UTC),
        payload=payload,
    )
    result = record_exposure(_NoDatabase(), event)  # type: ignore[arg-type]
    assert (result.status, result.reason) == ("rejected", "payload_invalid")


# ---- CLI and API boundaries ---------------------------------------------------------------------


def test_cli_has_no_promotion_or_deploy_command_and_start_needs_confirmation() -> None:
    parser = cli._parser()
    subparsers: Any = parser._subparsers
    commands = subparsers._group_actions[0].choices
    assert set(commands) == {
        "create",
        "start",
        "pause",
        "stop",
        "complete",
        "analyze",
        "show",
        "assign",
    }
    with pytest.raises(SystemExit):
        parser.parse_args(["start", "--experiment-id", str(uuid.uuid4())])  # no --confirm
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "create",
                "--key",
                KEY,
                "--allocation-bp",
                "10.5",
                "--primary",
                "rage_click_session_rate",
                "--guardrails",
                "form_error_session_rate",
            ]
        )


def test_api_exposes_only_the_read_only_assignment_route() -> None:
    app = create_app(Settings(env="test", log_level="WARNING"))
    paths = app.openapi()["paths"]
    experiment_routes = {
        (method.upper(), path)
        for path, ops in paths.items()
        if "experiment" in path
        for method in ops
    }
    assert experiment_routes == {("POST", "/api/v1/experiments/assignment")}


def _raising_factory() -> Any:
    raise RuntimeError("database down")


def test_assignment_endpoint_fails_closed_and_validates_input() -> None:
    app = create_app(Settings(env="test", log_level="WARNING"))
    app.dependency_overrides[get_session_factory] = lambda: _raising_factory
    with TestClient(app) as client:
        ok = client.post(
            "/api/v1/experiments/assignment",
            json={"session_id": str(SESSIONS[0]), "page": "pricing_signup"},
        )
        assert ok.status_code == 200 and ok.json() == {"status": "none"}
        for bad in (
            {"session_id": "not-a-uuid", "page": "pricing_signup"},
            {"session_id": str(SESSIONS[0]), "page": "../etc"},
            {"session_id": str(SESSIONS[0]), "page": "pricing_signup", "variant": "candidate"},
        ):
            assert client.post("/api/v1/experiments/assignment", json=bad).status_code == 422


# ---- golden dataset and fail-open scoring -------------------------------------------------------


def test_golden_dataset_covers_every_required_case() -> None:
    dataset = load_dataset()
    ids = {c.id for c in dataset.cases}
    kinds = {c.kind for c in dataset.cases}
    assert len(dataset.cases) >= 20
    assert kinds == {
        "eligibility",
        "allocation",
        "exposure",
        "analysis",
        "window",
        "serving",
        "db_constraint",
    }
    for required in (
        "elig_pass_eligible",
        "elig_reject_refused",
        "elig_human_review_refused",
        "elig_missing_evaluation",
        "elig_candidate_hash_mismatch",
        "alloc_deterministic",
        "alloc_same_session",
        "alloc_independent_experiments",
        "alloc_invalid_values",
        "alloc_boundary",
        "exp_assignment_only",
        "exp_counts_after_render",
        "exp_duplicate_idempotent",
        "ana_insufficient",
        "ana_equal",
        "ana_candidate_better",
        "ana_candidate_worse",
        "ana_guardrail_breach",
        "ana_zero_successes",
        "ana_all_successes",
        "ana_failure",
        "fail_invalid_metric",
        "fail_candidate_render_failure",
        "fail_candidate_unavailable",
        "paused_exposure_refused",
        "paused_outcome_not_attributed",
        "resumed_outcome_attributed",
        "terminal_outcome_not_attributed",
        "multiple_collection_windows_correct",
    ):
        assert required in ids, required
    for case in dataset.cases:  # outcome counts must be possible
        for spec in (case.control, case.candidate):
            if spec is not None:
                assert all(v <= spec.exposed for v in spec.outcomes.values())
                burst = spec.outcomes.get("error_burst_session_rate", 0)
                assert spec.outcomes.get("form_error_session_rate", burst) >= burst


def _case(kind: str, **expected: Any) -> ExperimentCase:
    return ExperimentCase.model_validate(
        {
            "id": "unit_case",
            "kind": kind,
            "description": "a unit scoring case",
            "expected": expected,
        }
    )


@pytest.mark.parametrize(
    ("case", "observed"),
    [
        (_case("eligibility", created=False, started=False), {"created": True, "started": True}),
        (
            _case("exposure", exposures={"control": 0, "candidate": 0}),
            {"exposures": {"control": 1, "candidate": 0}},
        ),
        (_case("serving", served="fallback"), {"served": "assigned"}),
        (_case("db_constraint", accepted=False), {"accepted": True}),
    ],
)
def test_score_counts_permissive_mistakes_as_fail_open(
    case: ExperimentCase, observed: dict[str, Any]
) -> None:
    result = score(case, observed)
    assert not result.correct and result.fail_open


def test_score_treats_a_more_conclusive_assessment_as_fail_open() -> None:
    case = ExperimentCase.model_validate(
        {
            "id": "unit_analysis",
            "kind": "analysis",
            "description": "a unit scoring case",
            "control": {"exposed": 10},
            "candidate": {"exposed": 10},
            "expected": {"assessment": "insufficient_data"},
        }
    )
    observed = {"assessment": "evidence_ready", "status_after": "running", "baselines_after": 1}
    assert score(case, observed).fail_open
    promoted = {
        "assessment": "insufficient_data",
        "status_after": "completed",
        "baselines_after": 2,
    }
    assert score(case, promoted).fail_open  # any promotion side effect is a fail-open


def test_metric_names_type_matches_vocabulary() -> None:
    names: tuple[MetricName, ...] = METRIC_NAMES
    assert all(isinstance(n, str) for n in names)


# ---- collection windows (pause integrity) -------------------------------------------------------

from darwin.experiments.windows import (  # noqa: E402
    LifecycleHistoryError,
    LifecycleStep,
    collection_windows,
    validate_history,
    window_containing,
)

D = datetime(2026, 9, 28, tzinfo=UTC)


def _t(hh: int, mm: int) -> datetime:
    return D.replace(hour=hh, minute=mm)


HISTORY = [
    LifecycleStep(0, None, "draft", _t(9, 50)),
    LifecycleStep(1, "draft", "running", _t(10, 0)),
    LifecycleStep(2, "running", "paused", _t(10, 30)),
    LifecycleStep(3, "paused", "running", _t(10, 45)),
    LifecycleStep(4, "running", "completed", _t(11, 30)),
]


def test_windows_follow_the_running_intervals() -> None:
    windows = collection_windows(HISTORY, _t(23, 0))
    assert [(w.opened_at, w.closed_at) for w in windows] == [
        (_t(10, 0), _t(10, 30)),
        (_t(10, 45), _t(11, 30)),
    ]
    # The worked example: a session exposed at 10:10.
    for moment, counts in [
        (_t(10, 20), True),
        (_t(10, 35), False),  # paused
        (_t(10, 50), True),  # resumed
        (_t(11, 40), False),  # completed
        (_t(10, 30), False),  # windows are half-open: the pause instant is outside
        (_t(10, 45), True),
    ]:
        assert (window_containing(windows, moment) is not None) is counts, moment


def test_an_open_window_closes_at_as_of_and_later_windows_are_cut() -> None:
    running = HISTORY[:4]  # resumed at 10:45, still running
    assert collection_windows(running, _t(11, 0))[-1].closed_at == _t(11, 0)
    assert collection_windows(HISTORY, _t(10, 40)) == collection_windows(HISTORY[:3], _t(10, 40))
    assert collection_windows(HISTORY[:2], _t(9, 55)) == []  # as_of before the start
    assert collection_windows(HISTORY[:1], _t(23, 0)) == []  # a draft collects nothing


@pytest.mark.parametrize(
    "history",
    [
        [],
        [LifecycleStep(0, None, "running", _t(10, 0))],  # not born as draft
        [HISTORY[0], HISTORY[2]],  # sequence gap / broken chain
        [HISTORY[0], LifecycleStep(1, "draft", "paused", _t(10, 0))],  # disallowed move
        [HISTORY[0], LifecycleStep(1, "draft", "running", _t(9, 50))],  # time not increasing
        [HISTORY[0], HISTORY[1], LifecycleStep(2, "running", "running", _t(10, 5))],  # duplicate
    ],
)
def test_invalid_histories_fail_closed(history: list[LifecycleStep]) -> None:
    with pytest.raises(LifecycleHistoryError):
        validate_history(history)
    with pytest.raises(LifecycleHistoryError):
        collection_windows(history, _t(23, 0))


def test_out_of_window_exposures_need_review() -> None:
    result = assess(CONFIG, _arm(200), _arm(200), Integrity(exposures_outside_windows=1), AS_OF)
    assert result.assessment == "needs_review"
    assert "exposure_outside_window" in result.reason_codes


def test_report_lists_its_collection_windows_and_excluded_boundary_signals() -> None:
    windows = [w.as_list() for w in collection_windows(HISTORY, _t(23, 0))]
    result = assess(CONFIG, _arm(200), _arm(200), Integrity(), AS_OF, windows, 3)
    assert result.report["collection_windows"] == windows
    assert result.report["integrity"]["boundary_signals_excluded"] == 3
