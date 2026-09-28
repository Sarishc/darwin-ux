"""Step 15 unit tests: evidence hash, policy version, input validation, confirmations,
signal attribution, message versions, CLI/API boundaries, golden scoring. No database."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from darwin.config import Settings
from darwin.generations import cli
from darwin.generations.eligibility import PromotionEvidence
from darwin.generations.evaluation import SCENARIOS, PromotionCase, load_dataset, score
from darwin.generations.service import (
    GenerationInputError,
    expected_confirmation,
    validate_reason,
    validate_reviewer,
)
from darwin.generations.vocabulary import POLICY_VERSION
from darwin.main import create_app
from darwin.signals.service import ui_attribution
from darwin.telemetry.messages import TelemetryMessageV1, TelemetryMessageV2, parse_message
from darwin.telemetry.schemas import TelemetryEvent

EVIDENCE = PromotionEvidence(
    policy_version=POLICY_VERSION,
    page_id="pricing_signup",
    candidate_spec_id=str(uuid.UUID(int=1)),
    candidate_spec_hash="a" * 64,
    candidate_evaluation_run_id=str(uuid.UUID(int=2)),
    experiment_id=str(uuid.UUID(int=3)),
    experiment_key="rage_fix_pricing",
    experiment_status="completed",
    experiment_stopped_at="2026-09-01T13:30:00+00:00",
    traffic_source="simulated",
    control_spec_hash="b" * 64,
    experiment_analysis_id=str(uuid.UUID(int=4)),
    analysis_version="experiment_analysis.v1",
    analysis_report_hash="c" * 64,
    analysis_as_of="2100-01-01T00:00:00+00:00",
    assessment="evidence_ready",
    source_spec_id=str(uuid.UUID(int=5)),
    source_spec_hash="d" * 64,
    source_generation=0,
    target_generation=1,
)


# ---- evidence hash / policy version -------------------------------------------------------------


def test_evidence_hash_is_stable_and_canonical() -> None:
    assert EVIDENCE.hash() == replace(EVIDENCE).hash()
    assert len(EVIDENCE.hash()) == 64
    assert '"policy_version":"promotion_policy.v1"' in EVIDENCE.canonical()
    assert EVIDENCE.canonical().startswith('{"analysis_as_of"')  # sorted keys


@pytest.mark.parametrize(
    "change",
    [
        {"candidate_spec_hash": "e" * 64},
        {"analysis_report_hash": "e" * 64},
        {"experiment_analysis_id": str(uuid.UUID(int=9))},
        {"source_spec_id": str(uuid.UUID(int=9))},
        {"source_generation": 1},
        {"target_generation": 2},
        {"policy_version": "promotion_policy.v2"},
        {"experiment_status": "stopped"},
        {"traffic_source": "real"},
    ],
)
def test_any_evidence_change_changes_the_hash(change: dict[str, Any]) -> None:
    assert replace(EVIDENCE, **change).hash() != EVIDENCE.hash()


def test_evidence_contains_no_raw_data() -> None:
    fields = set(EVIDENCE.__dataclass_fields__)
    assert not fields & {"spec", "report", "events", "sessions", "reviewer", "reason"}


# ---- operator input ---------------------------------------------------------------------------


@pytest.mark.parametrize("reviewer", ["sarish", "a", "ops.team-1", "A_b.c"])
def test_valid_reviewers(reviewer: str) -> None:
    assert validate_reviewer(reviewer) == reviewer


@pytest.mark.parametrize(
    "reviewer", ["", " sarish", "sarish ok", "me@example.com", "x" * 65, "-lead", None, 7]
)
def test_invalid_reviewers(reviewer: object) -> None:
    with pytest.raises(GenerationInputError):
        validate_reviewer(reviewer)


def test_reasons_are_bounded_plain_text() -> None:
    assert validate_reason("  Reduced rage clicks; guardrails ok.  ") == (
        "Reduced rage clicks; guardrails ok."
    )
    for bad in ("", "   ", "x" * 501, "line\x00break", "bell\x07", None):
        with pytest.raises(GenerationInputError):
            validate_reason(bad)


def test_confirmation_names_page_and_generation() -> None:
    assert expected_confirmation("pricing_signup", 1) == "pricing_signup:1"


# ---- signal attribution --------------------------------------------------------------------------

A, B = uuid.uuid4(), uuid.uuid4()


@pytest.mark.parametrize(
    ("specs", "expected"),
    [
        ([A, A, A, A], ("single", A)),
        ([A, A, B, B], ("mixed", None)),
        ([A, None, A, A], ("unknown", None)),
        ([None, None, None, None], ("unknown", None)),
        ([None, A, B, B], ("unknown", None)),
        ([], ("unknown", None)),
    ],
)
def test_signal_attribution_is_proven_by_every_evidence_event(
    specs: list[uuid.UUID | None], expected: tuple[str, uuid.UUID | None]
) -> None:
    ids = [f"e{i}" for i in range(len(specs))]
    assert ui_attribution(ids, dict(zip(ids, specs, strict=True))) == expected


def test_evidence_event_missing_from_history_is_unknown() -> None:
    assert ui_attribution(["e1", "missing"], {"e1": A}) == ("unknown", None)


# ---- telemetry contract versions ---------------------------------------------------------------


def _event(**extra: Any) -> TelemetryEvent:
    return TelemetryEvent(
        event_id=uuid.uuid4(),
        event_type="button_click",
        session_id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        payload={"component": "plan_team_pro_cta"},
        **extra,
    )


def test_v2_carries_attribution_and_v1_stays_valid_without_it() -> None:
    event = _event(ui_generation=1, ui_spec_hash="a" * 64, ui_spec_version_id=A)
    v2 = parse_message(TelemetryMessageV2.from_event(event).to_body())
    assert (v2.ui_generation, v2.ui_spec_hash, v2.ui_spec_version_id) == (1, "a" * 64, A)
    v1 = parse_message(TelemetryMessageV1.from_event(event).to_body())
    assert (v1.ui_generation, v1.ui_spec_hash, v1.ui_spec_version_id) == (None, None, None)


@pytest.mark.parametrize(
    "extra",
    [
        {"ui_generation": -1},
        {"ui_generation": 100_001},
        {"ui_spec_hash": "ABC"},
        {"ui_spec_hash": "a" * 63},
    ],
)
def test_invalid_attribution_claims_are_rejected(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _event(**extra)


def test_attribution_claims_are_optional_legacy_clients_still_work() -> None:
    event = _event()
    assert (event.ui_generation, event.ui_spec_hash, event.ui_spec_version_id) == (None, None, None)


# ---- CLI / API boundaries ----------------------------------------------------------------------


def test_cli_has_no_automatic_or_model_path() -> None:
    subparsers: Any = cli._parser()._subparsers
    commands = set(subparsers._group_actions[0].choices)
    assert commands == {"bootstrap", "show", "review", "approve", "reject", "promote", "rollback"}
    for argv in (
        ["promote", "--approval-id", str(A), "--reviewer", "x"],  # no --confirm
        ["rollback", "--reviewer", "x", "--reason", "r"],  # no --confirm
        ["approve", "--analysis-id", str(A), "--reviewer", "x"],  # no --reason
    ):
        with pytest.raises(SystemExit):
            cli._parser().parse_args(argv)


def test_api_has_no_promotion_or_rollback_route() -> None:
    paths = create_app(Settings(env="test", log_level="WARNING")).openapi()["paths"]
    generation_routes = {
        (method.upper(), path)
        for path, ops in paths.items()
        if any(w in path for w in ("generation", "promot", "rollback", "approv"))
        for method in ops
    }
    assert generation_routes == {("GET", "/api/v1/generations/active")}


# ---- golden dataset and scoring -----------------------------------------------------------------


def test_golden_promotion_dataset_covers_the_required_cases() -> None:
    dataset = load_dataset()
    assert len(dataset.cases) >= 30
    assert {c.scenario for c in dataset.cases} <= set(SCENARIOS)
    groups = {c.group for c in dataset.cases}
    assert groups == {
        "eligible",
        "ineligible",
        "toctou",
        "rollback",
        "forgery",
        "telemetry",
        "failure",
    }
    ids = {c.id for c in dataset.cases}
    for required in (
        "approve_clean",
        "promote_clean",
        "sandbox_human_review",
        "sandbox_reject",
        "experiment_running",
        "experiment_paused",
        "insufficient_data",
        "needs_review",
        "stop_recommended",
        "analysis_error",
        "candidate_hash_mismatch",
        "newer_reject_after_approval",
        "wrong_candidate",
        "stale_source_generation",
        "duplicate_approval",
        "replay_promotion",
        "rollback_to_generation_0",
        "candidate_as_rollback_target",
        "rollback_unknown_target",
        "telemetry_generation_1",
        "telemetry_mixed_signal",
        "telemetry_legacy_unknown",
        "active_api_failure_falls_back",
    ):
        assert required in ids, required


def _case(group: str, **expected: Any) -> PromotionCase:
    return PromotionCase.model_validate(
        {
            "id": "unit_case",
            "group": group,
            "scenario": "approve_clean",
            "description": "a unit scoring case",
            "expected": expected,
        }
    )


def test_score_counts_unauthorized_promotions() -> None:
    case = _case("ineligible", approved=False, promoted=False, active_generation=0, promoted_rows=0)
    for observed in (
        {"approved": False, "promoted": True, "active_generation": 0, "promoted_rows": 0},
        {"approved": False, "active_generation": 1, "promoted_rows": 0},
        {"approved": False, "active_generation": 0, "promoted_rows": 1},
    ):
        result = score(case, observed)
        assert not result.correct and result.unauthorized_promotion


def test_score_counts_invalid_rollbacks() -> None:
    case = _case("rollback", rolled_back=False, active_generation=1)
    result = score(case, {"rolled_back": True, "active_generation": 0})
    assert not result.correct and result.invalid_rollback
