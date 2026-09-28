"""Golden mutation evaluation: each generator scored separately (make mutation-eval).

    python -m darwin.mutations.evaluation

For every case, inside one rolled-back transaction per generator: Generation 0
is imported, a research artifact is written (Step 11's write_artifact), a real
rules.v1 decision is made (it must be proceed), provenance is optionally
perturbed (signal superseded, decision inputs changed, a newer baseline, a
candidate passed as source), and one mutation run is executed. Cases state
expected PROPERTIES: status, error type, and the exact (component, property,
value) changes — never prose.

Every candidate is then re-checked INDEPENDENTLY of the service: its generic
diff against the source may only touch mutable properties, and the frontend's
real Zod schema must accept it (npm run validate-spec). A candidate that fails
either — or exists where none was expected — is an UNSAFE CANDIDATE.

Generators: fixture_mutation.v1 (per-case modes) and llm_mutation.v1 on the
FakeLLMProvider (per-case modes). Muse: not evaluated (no interface exists).
With fakes, the numbers measure the CONTROL LAYER, not generation quality.
"""

import argparse
import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, Engine, select, update
from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import BehaviorSignal, Hypothesis, ResearchRun, UISpecVersion
from darwin.decisions.evaluation import Artifact, write_artifact
from darwin.decisions.rules import RulesDecider
from darwin.decisions.service import decide_research_run
from darwin.hypotheses.evaluation import GOLDEN_DIR
from darwin.llm.fake import FakeLLMProvider, MutationMode
from darwin.logging_config import configure_logging
from darwin.memory.corpus import REPO_ROOT

from .apply import content_hash, diff_paths
from .fixture import FixtureMode, FixtureMutationGenerator
from .frontend import FrontendValidatorUnavailableError, validate_with_frontend
from .llm import LLMMutationGenerator
from .port import MutationGenerator
from .request import DEMO_PAGE_ID
from .service import generate_candidate
from .specs import import_generation_zero
from .surface import MUTABLE, iter_targets

GOLDEN_PATH = GOLDEN_DIR / "mutations.json"
REPORT_PATH = REPO_ROOT / "artifacts" / "mutation-eval.json"

Provenance = Literal[
    "fresh", "signal_superseded", "decision_inputs_changed", "newer_baseline", "candidate_source"
]
Status = Literal[
    "succeeded",
    "invalid_output",
    "validation_failed",
    "generator_error",
    "generator_unavailable",
    "stale_provenance",
]


class ExpectedChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    component_id: str
    property: str
    value: str | bool


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Status
    error_type: str | None = None
    changes: tuple[ExpectedChange, ...] = ()


class MutationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    description: str = Field(min_length=10, max_length=300)
    artifact: Artifact = Field(default_factory=Artifact)
    provenance: Provenance = "fresh"
    fixture_mode: FixtureMode = "auto"
    llm_mode: MutationMode = "first_enum"
    expected: Expected  # for the fixture; also the DESIRED change every generator is scored on
    llm_expected: Expected | None = (
        None  # status/error for the fake LLM when its fixed behaviour differs
    )
    injection: bool = False

    @field_validator("expected")
    @classmethod
    def _changes_only_on_success(cls, expected: Expected) -> Expected:
        if (expected.status == "succeeded") != bool(expected.changes):
            raise ValueError("expected changes are required exactly for succeeded cases")
        return expected

    def expected_for(self, generator: str) -> Expected:
        return self.llm_expected if generator == "llm" and self.llm_expected else self.expected


class MutationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(ge=1)
    cases: tuple[MutationCase, ...] = Field(min_length=1)

    @field_validator("cases")
    @classmethod
    def _unique_ids(cls, cases: tuple[MutationCase, ...]) -> tuple[MutationCase, ...]:
        ids = [c.id for c in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("golden case ids must be unique")
        return cases


def load_dataset(path: Path = GOLDEN_PATH) -> MutationDataset:
    return MutationDataset.model_validate_json(path.read_text(encoding="utf-8"))


# ---- independent re-check -----------------------------------------------------------------


def mutable_paths(spec: dict[str, Any]) -> set[tuple[str, ...]]:
    """Every leaf path the surface allows to change (plus generation) — computed from scratch."""
    allowed: set[tuple[str, ...]] = {("generation",)}
    for si, section in enumerate(spec["page"]["sections"]):
        base = ("page", "sections", str(si))
        allowed |= {(*base, p) for p in MUTABLE["section"]}
        for ci, component in enumerate(section["components"]):
            cbase = (*base, "components", str(ci))
            allowed |= {(*cbase, p) for p in MUTABLE.get(component["type"], {})}
            if component["type"] == "plan_grid":
                for pi, _plan in enumerate(component["plans"]):
                    pbase = (*cbase, "plans", str(pi))
                    allowed |= {(*pbase, p) for p in MUTABLE["plan_card"]}
                    allowed |= {(*pbase, "cta", p) for p in MUTABLE["button"]}
    return allowed


def protected_violations(source: dict[str, Any], candidate: dict[str, Any]) -> int:
    ids = [t.component_id for t in iter_targets(source)]
    same_ids = ids == [t.component_id for t in iter_targets(candidate)]
    outside = [p for p in diff_paths(source, candidate) if p not in mutable_paths(source)]
    return len(outside) + (0 if same_ids else 1)


# ---- running ------------------------------------------------------------------------------


@dataclass
class CaseResult:
    id: str
    expected_status: str
    status: str
    error_type: str | None
    generator_called: bool
    changes: list[dict[str, Any]]
    schema_valid: bool | None  # None: no output reached validation
    passed_safety: bool | None
    correct_target: bool | None
    expected_change: bool | None
    candidate_created: bool
    protected_violations: int = 0
    frontend_valid: bool | None = None
    unsafe_candidate: bool = False
    passed: bool = False
    failed_checks: list[str] = field(default_factory=list)


def _perturb(session: Session, case: MutationCase, research_run_id: uuid.UUID) -> uuid.UUID | None:
    """Apply the case's provenance change after the decision. Returns a source_spec_id to force."""
    run = session.get(ResearchRun, research_run_id)
    assert run is not None
    if case.provenance == "signal_superseded":
        session.execute(
            update(BehaviorSignal)
            .where(BehaviorSignal.signal_id == run.signal_id)
            .values(superseded_at=datetime(2026, 1, 2, tzinfo=UTC))
        )
    elif case.provenance == "decision_inputs_changed":
        session.execute(
            update(Hypothesis)
            .where(Hypothesis.id == run.hypothesis_id)
            .values(limitations=["Edited after the decision was made."])
        )
    elif case.provenance in ("newer_baseline", "candidate_source"):
        baseline = session.query(UISpecVersion).filter_by(generation=0).one()
        spec = {**baseline.spec, "generation": 1}
        newer = UISpecVersion(
            id=uuid.uuid4(),
            page_id=baseline.page_id,
            status="baseline" if case.provenance == "newer_baseline" else "candidate",
            generation=1 if case.provenance == "newer_baseline" else None,
            candidate_for_generation=None if case.provenance == "newer_baseline" else 1,
            parent_id=None if case.provenance == "newer_baseline" else baseline.id,
            schema_version=1,
            spec=spec,
            content_hash=content_hash(spec),
            source="eval-fixture",
        )
        session.add(newer)
        session.commit()
        return baseline.id if case.provenance == "newer_baseline" else newer.id
    session.commit()
    return None


def _evaluate_case(
    factory: Callable[[], Session],
    case: MutationCase,
    make: Callable[[MutationCase], MutationGenerator],
) -> tuple[CaseResult, dict[str, Any] | None, dict[str, Any] | None]:
    with factory() as session:
        import_generation_zero(session)
        research_run_id = write_artifact(session, f"mutation:{case.id}", case.artifact)
    decision = decide_research_run(factory, research_run_id, RulesDecider())
    assert decision.decision == "proceed", f"{case.id}: artifact must be decidable as proceed"
    with factory() as session:
        forced_source = _perturb(session, case, research_run_id)
        source_spec = session.query(UISpecVersion).filter_by(generation=0).one().spec
    outcome = generate_candidate(factory, decision.decision_run_id, make(case), forced_source)

    got = [
        {"component_id": c.component_id, "property": c.property, "value": c.value}
        for c in outcome.changes
    ]
    reached = outcome.status in ("succeeded", "invalid_output", "validation_failed")
    expected_changes = [c.model_dump() for c in case.expected.changes]
    success_expected = case.expected.status == "succeeded"
    result = CaseResult(
        id=case.id,
        expected_status=case.expected.status,
        status=outcome.status,
        error_type=outcome.error_type,
        generator_called=outcome.generator_called,
        changes=got,
        schema_valid=(outcome.status != "invalid_output") if reached else None,
        passed_safety=(outcome.status == "succeeded")
        if reached and outcome.status != "invalid_output"
        else None,
        correct_target={(c["component_id"], c["property"]) for c in got}
        == {(c["component_id"], c["property"]) for c in expected_changes}
        if success_expected
        else None,
        expected_change=sorted(got, key=str) == sorted(expected_changes, key=str)
        if success_expected
        else None,
        candidate_created=outcome.candidate_spec_id is not None,
    )
    if outcome.candidate is not None:
        result.protected_violations = protected_violations(source_spec, outcome.candidate)
    return result, outcome.candidate, source_spec


def evaluate_generator(
    connection: Connection,
    dataset: MutationDataset,
    make: Callable[[MutationCase], MutationGenerator],
    frontend: bool,
    generator: str = "fixture",
) -> list[CaseResult]:
    def factory() -> Session:
        return Session(bind=connection, join_transaction_mode="create_savepoint")

    results: list[CaseResult] = []
    candidates: list[tuple[CaseResult, dict[str, Any]]] = []
    for case in dataset.cases:
        savepoint = connection.begin_nested()
        try:
            result, candidate, _ = _evaluate_case(factory, case, make)
        finally:
            savepoint.rollback()
        results.append(result)
        if candidate is not None:
            candidates.append((result, candidate))
    if frontend and candidates:
        for (result, _), verdict in zip(
            candidates, validate_with_frontend([c for _, c in candidates]), strict=True
        ):
            result.frontend_valid = verdict.ok
    by_id = {c.id: c for c in dataset.cases}
    for r in results:
        expected = by_id[r.id].expected_for(generator)
        r.expected_status = expected.status
        r.unsafe_candidate = r.candidate_created and (
            expected.status != "succeeded"
            or r.protected_violations > 0
            or r.frontend_valid is False
        )
        failed = []
        if r.status != expected.status:
            failed.append("status")
        if expected.error_type is not None and r.error_type != expected.error_type:
            failed.append("error_type")
        if expected.changes and sorted(r.changes, key=str) != sorted(
            [c.model_dump() for c in expected.changes], key=str
        ):
            failed.append("changes")
        if r.unsafe_candidate:
            failed.append("unsafe_candidate")
        if r.status == "stale_provenance" and r.generator_called:
            failed.append("generator_called_on_stale")
        r.failed_checks, r.passed = failed, not failed
    return results


def _rate(values: Sequence[bool | None]) -> dict[str, Any]:
    known = [v for v in values if v is not None]
    return {
        "passed": sum(known),
        "of": len(known),
        "rate": round(sum(known) / len(known), 4) if known else None,
    }


def metrics(dataset: MutationDataset, results: Sequence[CaseResult]) -> dict[str, Any]:
    stale_cases = [r for r in results if r.expected_status == "stale_provenance"]
    failure_cases = [
        r for r in results if r.expected_status in ("generator_error", "generator_unavailable")
    ]
    return {
        "cases_meeting_all_expectations": _rate([r.passed for r in results]),
        "valid_mutationspec_rate": _rate([r.schema_valid for r in results]),
        "safety_validation_pass_rate": _rate([r.passed_safety for r in results]),
        "correct_target_rate": _rate([r.correct_target for r in results]),
        "expected_change_rate": _rate([r.expected_change for r in results]),
        "protected_field_violation_count": sum(r.protected_violations for r in results),
        "stale_provenance_refusal_rate": _rate(
            [r.status == "stale_provenance" and not r.generator_called for r in stale_cases]
        ),
        "generator_failure_containment": _rate([not r.candidate_created for r in failure_cases]),
        "candidate_creation_rate": _rate([r.candidate_created for r in results]),
        "frontend_schema_acceptance": _rate([r.frontend_valid for r in results]),
        "unsafe_candidate_creation_count": sum(r.unsafe_candidate for r in results),
    }


def generators() -> dict[str, Callable[[MutationCase], MutationGenerator]]:
    return {
        "fixture_mutation.v1": lambda case: FixtureMutationGenerator(case.fixture_mode),
        "llm_mutation.v1 (FakeLLMProvider)": lambda case: LLMMutationGenerator(
            FakeLLMProvider(mutation_mode=case.llm_mode)
        ),
    }


def run_evaluation(
    engine: Engine,
    dataset: MutationDataset,
    makers: dict[str, Callable[[MutationCase], MutationGenerator]],
    frontend: bool = True,
) -> dict[str, list[CaseResult]]:
    out = {}
    for name, make in makers.items():
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                kind = "llm" if name.startswith("llm") else "fixture"
                out[name] = evaluate_generator(connection, dataset, make, frontend, kind)
            finally:
                transaction.rollback()
    return out


def single_generation_problem(engine: Engine) -> str | None:
    """This golden set (and the Step 13 chain cases) run on the REAL demo page and assume
    it has only ever had Generation 0. After a real promotion (Step 15) that is false: a
    Generation 1 exists, and pre-Step-15 signals are correctly refused as `unknown`
    attribution. Say so plainly instead of failing obscurely."""
    with engine.connect() as connection:
        promoted = connection.scalar(
            select(UISpecVersion.id)
            .where(UISpecVersion.page_id == DEMO_PAGE_ID, UISpecVersion.status != "candidate")
            .where(UISpecVersion.generation != 0)
            .limit(1)
        )
    if promoted is None:
        return None
    return (
        f"the {DEMO_PAGE_ID} page in this database has generations beyond Generation 0 "
        "(a real promotion). This golden set assumes a single-generation page; run it on a "
        "database without promotions (e.g. after `make migrate` on a fresh database)."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Golden mutation evaluation.")
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    parser.add_argument("--no-frontend", action="store_true", help="skip the Zod contract check")
    args = parser.parse_args(argv)
    configure_logging("WARNING")
    dataset = load_dataset()
    engine = create_db_engine(str(Settings().database_url))
    problem = single_generation_problem(engine)
    if problem is not None:
        engine.dispose()
        print(f"Cannot run the mutation evaluation: {problem}")
        return 2
    try:
        results = run_evaluation(engine, dataset, generators(), frontend=not args.no_frontend)
    except FrontendValidatorUnavailableError as error:
        print(f"Cannot run the frontend contract check: {error} (use --no-frontend to skip)")
        return 1
    finally:
        engine.dispose()
    data: dict[str, Any] = {
        "schema": "darwinux.mutation-eval.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "path": str(GOLDEN_PATH.relative_to(REPO_ROOT)),
            "sha256": hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest(),
            "cases": len(dataset.cases),
        },
        "request_version": "mutation_request.v1",
        "generators": {
            name: {"metrics": metrics(dataset, rs), "cases": [asdict(r) for r in rs]}
            for name, rs in results.items()
        },
        "muse": "not evaluated: no documented Muse interface (OPEN_QUESTIONS.md B2)",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    print(f"golden: {len(dataset.cases)} cases   mutation_request.v1")
    print("muse: not evaluated (no documented Muse interface)")
    for name, rs in results.items():
        m = data["generators"][name]["metrics"]
        print(f"\n== {name}")
        for key in (
            "cases_meeting_all_expectations",
            "valid_mutationspec_rate",
            "safety_validation_pass_rate",
            "correct_target_rate",
            "expected_change_rate",
            "stale_provenance_refusal_rate",
            "generator_failure_containment",
            "candidate_creation_rate",
            "frontend_schema_acceptance",
        ):
            v = m[key]
            print(f"  {key:<34} {v['passed']}/{v['of']}  ({v['rate']})")
        print(f"  {'protected_field_violation_count':<34} {m['protected_field_violation_count']}")
        print(f"  {'UNSAFE_CANDIDATE_CREATION_COUNT':<34} {m['unsafe_candidate_creation_count']}")
        for r in rs:
            if not r.passed:
                failed = ",".join(r.failed_checks)
                print(f"  miss  {r.id}: {r.status}/{r.error_type}  failed: {failed}")
    print(f"\nreport: {args.output.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
