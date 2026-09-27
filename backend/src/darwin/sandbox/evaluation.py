"""Golden sandbox evaluation (make sandbox-eval): does candidate_eval.v1 decide correctly?

    python -m darwin.sandbox.evaluation

Two kinds of case, all inside one rolled-back transaction (a savepoint each):

- chain: the REAL Step 12 path — research artifact -> rules.v1 proceed decision
  -> fixture or LLM-baseline mutation -> candidate. (e.g. the harmful-but-safe
  LLM candidate that sets feedback back to "delayed".)
- synthetic: a candidate row written directly from edits to Generation 0 plus a
  matching succeeded MutationRun, BYPASSING Step 12's validation on purpose —
  so Step 13 is tested as an independent layer (id renames, removed fields,
  visibility changes, schema-invalid tokens, corrupted provenance).

Harness faults (unavailable, malformed output, an evaluator bug, and a
simulated render crash — no schema-valid spec crashes today's registry) test
fail-closed behaviour. The real harness runs once, over every distinct spec.

Expected properties per case: recommendation, optionally status, reason codes
that must appear, and tags for the detection metrics. Never prose.
"""

import argparse
import copy
import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import MutationRun, UISpecVersion
from darwin.decisions.evaluation import Artifact, write_artifact
from darwin.decisions.rules import RulesDecider
from darwin.decisions.service import decide_research_run
from darwin.hypotheses.evaluation import GOLDEN_DIR
from darwin.llm.fake import FakeLLMProvider, MutationMode
from darwin.logging_config import configure_logging
from darwin.memory.corpus import REPO_ROOT
from darwin.mutations.apply import content_hash, diff_paths
from darwin.mutations.evaluation import mutable_paths
from darwin.mutations.fixture import FixtureMode, FixtureMutationGenerator
from darwin.mutations.llm import LLMMutationGenerator
from darwin.mutations.service import generate_candidate
from darwin.mutations.specs import import_generation_zero, load_generation_zero

from .harness import HarnessError, NodeHarnessRunner, SpecFacts, parse_output
from .service import EvaluationOutcome, evaluate_candidate

GOLDEN_PATH = GOLDEN_DIR / "sandbox.json"
REPORT_PATH = REPO_ROOT / "artifacts" / "sandbox-eval.json"
NAMESPACE = uuid.UUID("2b7e3c19-4d8f-4a61-9e02-6c5b1d8f3a47")

Recommendation = Literal["pass", "human_review", "reject"]
Corrupt = Literal[
    "none",
    "wrong_hash",
    "generation_mismatch",
    "failed_mutation_run",
    "source_mismatch",
    "not_a_candidate",
    "malformed_spec",
]
HarnessFault = Literal["none", "unavailable", "malformed", "render_crash", "raises"]
Tag = Literal[
    "harmful_safe",
    "ux_intent",
    "accessibility_regression",
    "functional_regression",
    "provenance",
    "evaluator_failure",
    "irrelevant_safe",
]


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: tuple[str | int, ...]
    value: Any = None
    remove: bool = False  # remove a list element instead of replacing


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    recommendation: Recommendation
    status: Literal["completed", "provenance_failed", "evaluator_error"] = "completed"
    reasons_include: tuple[str, ...] = ()
    ux_intent: Literal["pass", "warn", "fail"] | None = None


class SandboxCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    description: str = Field(min_length=10, max_length=300)
    mode: Literal["chain", "synthetic"]
    artifact: Artifact = Field(default_factory=Artifact)
    generator: Literal["fixture", "llm"] = "fixture"
    fixture_mode: FixtureMode = "auto"
    llm_mode: MutationMode = "first_enum"
    edits: tuple[Edit, ...] = ()
    source_edits: tuple[Edit, ...] = ()  # a separate baseline (pre-existing issues)
    corrupt: Corrupt = "none"
    harness_fault: HarnessFault = "none"
    tags: tuple[Tag, ...] = ()
    expected: Expected


class SandboxDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: int = Field(ge=1)
    cases: tuple[SandboxCase, ...] = Field(min_length=1)

    @field_validator("cases")
    @classmethod
    def _unique_ids(cls, cases: tuple[SandboxCase, ...]) -> tuple[SandboxCase, ...]:
        ids = [c.id for c in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("golden case ids must be unique")
        return cases


def load_dataset(path: Path = GOLDEN_PATH) -> SandboxDataset:
    return SandboxDataset.model_validate_json(path.read_text(encoding="utf-8"))


# ---- building one case's candidate ---------------------------------------------------------


def apply_edits(spec: dict[str, Any], edits: Sequence[Edit]) -> dict[str, Any]:
    out = copy.deepcopy(spec)
    for edit in edits:
        node: Any = out
        for part in edit.path[:-1]:
            node = node[part]
        last = edit.path[-1]
        if edit.remove:
            del node[last]
        elif isinstance(node, list) and last == len(node):
            node.append(edit.value)  # index == length appends (e.g. a duplicated field)
        else:
            node[last] = edit.value
    return out


def _operations(source: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """MutationSpec-style operations for the differing mutable leaves (as Step 12 stores them)."""
    ops = []
    for path in diff_paths(source, candidate):
        if path == ("generation",) or path not in mutable_paths(source):
            continue
        node: Any = source
        for part in path[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        value: Any = candidate
        for part in path:
            value = value[int(part)] if isinstance(value, list) else value[part]
        ops.append(
            {"op": "replace", "component_id": node["id"], "property": path[-1], "value": value}
        )
    return ops


@dataclass
class Built:
    candidate_id: uuid.UUID
    mutation_run_id: uuid.UUID | None
    specs: dict[str, dict[str, Any]]  # every spec the harness will need, by content hash


def _uid(case: SandboxCase, part: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{case.id}:{part}")


def build_case(factory: Callable[[], Session], case: SandboxCase) -> Built:
    with factory() as session:
        import_generation_zero(session)
        research_run_id = write_artifact(session, f"sandbox:{case.id}", case.artifact)
    decision = decide_research_run(factory, research_run_id, RulesDecider())
    if case.mode == "chain":
        generator = (
            FixtureMutationGenerator(case.fixture_mode)
            if case.generator == "fixture"
            else LLMMutationGenerator(FakeLLMProvider(mutation_mode=case.llm_mode))
        )
        outcome = generate_candidate(factory, decision.decision_run_id, generator)
        assert outcome.candidate_spec_id is not None, f"{case.id}: chain produced no candidate"
        with factory() as session:
            row = session.get(UISpecVersion, outcome.candidate_spec_id)
            parent = session.get(UISpecVersion, outcome.source_spec_id)
            assert row is not None and parent is not None
            specs = {row.content_hash: dict(row.spec), parent.content_hash: dict(parent.spec)}
        return Built(outcome.candidate_spec_id, outcome.mutation_run_id, specs)

    with factory() as session:
        # Every synthetic case gets its own baseline row (a copy of Generation 0, or an
        # edited source), so it never collides with real candidates in the database.
        source_spec = apply_edits(load_generation_zero(), case.source_edits)
        parent = UISpecVersion(
            id=_uid(case, "source"),
            page_id=f"sandbox_{case.id}"[:64],
            status="baseline",
            generation=0,
            schema_version=1,
            spec=source_spec,
            content_hash=content_hash(source_spec),
            source="sandbox-eval-fixture",
        )
        session.add(parent)
        session.flush()
        source = dict(parent.spec)
        candidate = apply_edits({**source, "generation": 1}, case.edits)
        if case.corrupt == "malformed_spec":
            candidate = {"version": 1, "generation": 1, "page": {"id": "broken"}}
        if case.corrupt == "generation_mismatch":
            candidate["generation"] = 7
        digest = content_hash(candidate)
        row = UISpecVersion(
            id=_uid(case, "candidate"),
            page_id=parent.page_id,
            status="candidate",
            candidate_for_generation=1,
            parent_id=parent.id,
            schema_version=1,
            spec=candidate,
            content_hash=hashlib.sha256(b"tampered").hexdigest()
            if case.corrupt == "wrong_hash"
            else digest,
            source="sandbox-eval-fixture",
        )
        session.add(row)
        session.flush()
        ops = _operations(source, candidate) if case.corrupt != "malformed_spec" else []
        run_source = parent.id
        if case.corrupt == "source_mismatch":
            other = UISpecVersion(
                id=_uid(case, "other"),
                page_id=parent.page_id,
                status="candidate",
                candidate_for_generation=1,
                parent_id=parent.id,
                schema_version=1,
                spec={**source, "generation": 1, "note": "other"},
                content_hash=content_hash({**source, "generation": 1, "note": "other"}),
                source="sandbox-eval-fixture",
            )
            session.add(other)
            session.flush()
            run_source = other.id
        run = MutationRun(
            id=_uid(case, "mutation-run"),
            decision_run_id=decision.decision_run_id,
            source_spec_id=run_source,
            candidate_spec_id=row.id,
            generator="fixture",
            generator_version="sandbox-eval-synthetic",
            request_version="mutation_request.v1",
            request_hash=hashlib.sha256(case.id.encode()).hexdigest(),
            status="succeeded",
            error_type=None,
            validation_errors=[],
            mutation_spec={"version": 1, "operations": ops} if ops else None,
            operation_count=len(ops) if 1 <= len(ops) <= 5 else None,
        )
        session.add(run)
        chosen_run: uuid.UUID | None = run.id
        if case.corrupt == "failed_mutation_run":
            failed = MutationRun(
                id=_uid(case, "failed-run"),
                decision_run_id=decision.decision_run_id,
                source_spec_id=parent.id,
                candidate_spec_id=None,
                generator="fixture",
                generator_version="sandbox-eval-synthetic",
                request_version="mutation_request.v1",
                request_hash=hashlib.sha256(b"failed").hexdigest(),
                status="validation_failed",
                error_type="protected_property",
                validation_errors=[],
            )
            session.add(failed)
            chosen_run = failed.id
        session.commit()
        specs = {digest: candidate, content_hash(source): source}
        target_id = parent.id if case.corrupt == "not_a_candidate" else row.id
        return Built(
            target_id, chosen_run if case.corrupt == "failed_mutation_run" else None, specs
        )


# ---- harness wrappers ----------------------------------------------------------------------


class CachedRunner:
    """Facts computed once for every distinct spec; optionally a simulated fault."""

    def __init__(self, facts: Mapping[str, SpecFacts], fault: HarnessFault = "none") -> None:
        self.facts, self.fault, self.calls = facts, fault, 0

    def run(self, specs: Mapping[str, dict[str, Any]]) -> dict[str, SpecFacts]:
        self.calls += 1
        if self.fault == "unavailable":
            raise HarnessError("harness_unavailable")
        if self.fault == "malformed":
            return parse_output(
                {"harness_version": "sandbox_harness.v1", "facts": {"x": {}}}, set(specs)
            )
        if self.fault == "raises":
            raise RuntimeError("simulated evaluator bug")
        out = {key: self.facts[key] for key in specs}
        if self.fault == "render_crash":
            keys = sorted(specs)
            candidate_key = next(
                k for k in keys if specs[k].get("generation") != 0
            )  # the candidate is generation 1
            broken = out[candidate_key].model_copy(
                update={
                    "render": out[candidate_key].render.model_copy(
                        update={"ok": False, "error": "TypeError"}
                    )
                }
            )
            out[candidate_key] = broken
        return out


# ---- running and scoring -------------------------------------------------------------------


@dataclass
class CaseResult:
    id: str
    expected: str
    recommendation: str
    status: str
    reason_codes: list[str]
    categories: dict[str, str]
    tags: list[str]
    harness_called: bool
    correct: bool
    fail_open: bool
    false_reject: bool
    failed_checks: list[str] = field(default_factory=list)


def _savepointed(connection: Connection, work: Callable[[], Any]) -> Any:
    savepoint = connection.begin_nested()
    try:
        return work()
    finally:
        savepoint.rollback()


def run_evaluation(engine: Engine, dataset: SandboxDataset) -> list[CaseResult]:
    with engine.connect() as connection:
        transaction = connection.begin()
        try:

            def factory() -> Session:
                return Session(bind=connection, join_transaction_mode="create_savepoint")

            # Phase 1: every distinct spec, for one real harness run.
            specs: dict[str, dict[str, Any]] = {}
            for case in dataset.cases:

                def build(case: SandboxCase = case) -> Built:
                    return build_case(factory, case)

                specs.update(_savepointed(connection, build).specs)
            facts = NodeHarnessRunner().run(specs)
            # Phase 2: rebuild each case (deterministic ids/content) and evaluate it.
            results = []
            for case in dataset.cases:

                def work(case: SandboxCase = case) -> CaseResult:
                    built = build_case(factory, case)
                    runner = CachedRunner(facts, case.harness_fault)
                    outcome = evaluate_candidate(
                        factory, built.candidate_id, runner, built.mutation_run_id
                    )
                    return score(case, outcome)

                results.append(_savepointed(connection, work))
            return results
        finally:
            transaction.rollback()


def score(case: SandboxCase, outcome: EvaluationOutcome) -> CaseResult:
    expected = case.expected
    failed = []
    if outcome.recommendation != expected.recommendation:
        failed.append("recommendation")
    if outcome.status != expected.status:
        failed.append("status")
    if not set(expected.reasons_include) <= set(outcome.reason_codes):
        failed.append("reasons")
    ux = outcome.categories["ux_intent"]["status"]
    if expected.ux_intent is not None and ux != expected.ux_intent:
        failed.append("ux_intent")
    if outcome.status == "provenance_failed" and outcome.harness_called:
        failed.append("harness_called_on_provenance_failure")
    return CaseResult(
        id=case.id,
        expected=expected.recommendation,
        recommendation=outcome.recommendation,
        status=outcome.status,
        reason_codes=list(outcome.reason_codes),
        categories={name: result["status"] for name, result in outcome.categories.items()},
        tags=list(case.tags),
        harness_called=outcome.harness_called,
        correct=outcome.recommendation == expected.recommendation,
        fail_open=outcome.recommendation == "pass" and expected.recommendation != "pass",
        false_reject=outcome.recommendation == "reject" and expected.recommendation != "reject",
        failed_checks=failed,
    )


def _rate(values: Sequence[bool]) -> dict[str, Any]:
    passed, of = sum(values), len(values)
    return {"passed": passed, "of": of, "rate": round(passed / of, 4) if of else None}


def metrics(results: Sequence[CaseResult]) -> dict[str, Any]:
    labels = ("pass", "human_review", "reject")
    per_class = {}
    for label in labels:
        predicted = [r for r in results if r.recommendation == label]
        actual = [r for r in results if r.expected == label]
        hits = sum(1 for r in predicted if r.expected == label)
        per_class[label] = {
            "precision": round(hits / len(predicted), 4) if predicted else None,
            "recall": round(hits / len(actual), 4) if actual else None,
            "support": len(actual),
        }

    def tagged(tag: str) -> list[CaseResult]:
        return [r for r in results if tag in r.tags]

    return {
        "cases_meeting_all_expectations": _rate([not r.failed_checks for r in results]),
        "terminal_recommendation_accuracy": _rate([r.correct for r in results]),
        "per_class": per_class,
        "harmful_safe_rejection_rate": _rate(
            [r.recommendation == "reject" for r in tagged("harmful_safe")]
        ),
        "ux_intent_accuracy": _rate(
            ["ux_intent" not in r.failed_checks for r in tagged("ux_intent")]
        ),
        "irrelevant_safe_not_passed": _rate(
            [r.recommendation != "pass" for r in tagged("irrelevant_safe")]
        ),
        "accessibility_regression_detection": _rate(
            [
                r.categories["accessibility"] in ("fail", "warn") and r.recommendation != "pass"
                for r in tagged("accessibility_regression")
            ]
        ),
        "functional_regression_detection": _rate(
            [
                (r.categories["functional"] == "fail" or r.categories["regression"] == "fail")
                and r.recommendation == "reject"
                for r in tagged("functional_regression")
            ]
        ),
        "provenance_failure_containment": _rate(
            [r.status == "provenance_failed" and not r.harness_called for r in tagged("provenance")]
        ),
        "evaluator_failure_containment": _rate(
            [r.recommendation != "pass" for r in tagged("evaluator_failure")]
        ),
        "fail_open_count": sum(r.fail_open for r in results),
        "false_reject_count": sum(r.false_reject for r in results),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Golden sandbox evaluation (candidate_eval.v1).")
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    configure_logging("WARNING")
    dataset = load_dataset()
    engine = create_db_engine(str(Settings().database_url))
    try:
        results = run_evaluation(engine, dataset)
    except HarnessError as error:
        print(f"Cannot run the sandbox harness: {error.code}")
        return 1
    finally:
        engine.dispose()
    m = metrics(results)
    data = {
        "schema": "darwinux.sandbox-eval.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "path": str(GOLDEN_PATH.relative_to(REPO_ROOT)),
            "sha256": hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest(),
            "cases": len(dataset.cases),
        },
        "evaluator_version": "candidate_eval.v1",
        "metrics": m,
        "cases": [asdict(r) for r in results],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    print(f"golden: {len(dataset.cases)} cases   candidate_eval.v1")
    for r in results:
        mark = "ok  " if not r.failed_checks else "FAIL"
        cats = " ".join(f"{k[:4]}={v}" for k, v in r.categories.items())
        extra = f"  failed: {','.join(r.failed_checks)}" if r.failed_checks else ""
        print(f"  {mark} {r.id:<34} {r.recommendation:<12} {cats}{extra}")
    for key in (
        "cases_meeting_all_expectations",
        "terminal_recommendation_accuracy",
        "harmful_safe_rejection_rate",
        "ux_intent_accuracy",
        "irrelevant_safe_not_passed",
        "accessibility_regression_detection",
        "functional_regression_detection",
        "provenance_failure_containment",
        "evaluator_failure_containment",
    ):
        v = m[key]
        print(f"{key:<38} {v['passed']}/{v['of']}  ({v['rate']})")
    for label, pc in m["per_class"].items():
        support = pc["support"]
        print(
            f"{label:<14} precision {pc['precision']}  recall {pc['recall']}  (support {support})"
        )
    print(f"{'FAIL_OPEN_COUNT':<38} {m['fail_open_count']}")
    print(f"{'false_reject_count':<38} {m['false_reject_count']}")
    print(f"report: {args.output.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
