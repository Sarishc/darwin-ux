"""evaluate_candidate: provenance -> harness (once) -> categories -> policy -> immutable record.

    CandidateNotFoundError      nothing to record against (raised)
    ProvenanceError             recorded: status provenance_failed, recommendation reject,
                                no harness call
    HarnessError                recorded: status evaluator_error, recommendation human_review
    otherwise                   recorded: status completed, recommendation from policy

Never fails open: only a completed evaluation whose every gate passed and whose
UX intent is aligned can be `pass` (and the database CHECKs agree). Nothing is
deployed, promoted or written to the repository; the harness works on temp files.

Logs: ids, versions, category statuses, recommendation, duration, error type —
never spec JSON, hypothesis text or form values.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from darwin.db.models import CandidateEvaluationRun, MutationRun
from darwin.hypotheses.service import SessionFactory
from darwin.mutations.apply import content_hash

from .harness import HARNESS_VERSION, HarnessError, HarnessRunner, NodeHarnessRunner
from .policy import (
    CATEGORY_ORDER,
    EVALUATOR_VERSION,
    CategoryResult,
    aggregate,
    evaluate_facts,
)
from .provenance import ProvenanceError, load_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluationOutcome:
    evaluation_run_id: uuid.UUID
    candidate_spec_id: uuid.UUID
    mutation_run_id: uuid.UUID | None
    status: str
    recommendation: str
    reason_codes: tuple[str, ...]
    categories: dict[str, dict[str, Any]]
    error_type: str | None
    harness_called: bool


def _errors(why: str) -> dict[str, CategoryResult]:
    return {n: CategoryResult(n, "deterministic", "error", note=why) for n in CATEGORY_ORDER}


def evaluate_candidate(
    session_factory: SessionFactory,
    candidate_spec_id: uuid.UUID,
    runner: HarnessRunner | None = None,
    mutation_run_id: uuid.UUID | None = None,
) -> EvaluationOutcome:
    runner = runner or NodeHarnessRunner()
    started = time.perf_counter()
    harness_called = False
    harness_version: str | None = None
    mutation_id: uuid.UUID | None = mutation_run_id
    with session_factory() as session:
        try:
            context = load_context(session, candidate_spec_id, mutation_run_id)
            provenance_error: ProvenanceError | None = None
        except ProvenanceError as error:
            provenance_error = error
        session.rollback()

    if provenance_error is not None:
        status, error_type = "provenance_failed", provenance_error.code
        categories = {
            n: CategoryResult(n, "deterministic", "skipped", note="provenance failed")
            for n in CATEGORY_ORDER
        }
        recommendation, reasons = "reject", ["provenance_invalid"]
        if mutation_id is not None:
            mutation_id = mutation_id if _exists(session_factory, mutation_id) else None
    else:
        mutation_id = context.mutation_run_id
        source_key, candidate_key = content_hash(context.source), content_hash(context.candidate)
        try:
            harness_called = True
            facts = runner.run({source_key: context.source, candidate_key: context.candidate})
            harness_version = HARNESS_VERSION
            categories = evaluate_facts(context, facts[source_key], facts[candidate_key])
            status, error_type = "completed", None
            recommendation, reasons = aggregate(categories)
        except HarnessError as error:
            status, error_type = "evaluator_error", error.code
            categories = _errors(f"harness: {error.code}")
            recommendation, reasons = "human_review", ["evaluator_error"]
        except Exception as error:  # an evaluator bug must fail closed too
            status, error_type = "evaluator_error", f"evaluator_bug:{type(error).__name__}"[:64]
            categories = _errors("evaluator raised")
            recommendation, reasons = "human_review", ["evaluator_error"]

    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    results = {name: categories[name].as_dict() for name in CATEGORY_ORDER}
    run_id = uuid.uuid4()
    with session_factory() as session:
        session.add(
            CandidateEvaluationRun(
                id=run_id,
                candidate_spec_id=candidate_spec_id,
                mutation_run_id=mutation_id,
                evaluator_version=EVALUATOR_VERSION,
                harness_version=harness_version,
                status=status,
                recommendation=recommendation,
                reason_codes=reasons,
                category_results=results,
                error_type=error_type,
                duration_ms=duration_ms,
            )
        )
        session.commit()
    logger.info(
        "candidate evaluated",
        extra={
            "context": {
                "evaluation_run_id": str(run_id),
                "candidate_spec_id": str(candidate_spec_id),
                "mutation_run_id": str(mutation_id) if mutation_id else None,
                "evaluator_version": EVALUATOR_VERSION,
                "categories": {n: results[n]["status"] for n in CATEGORY_ORDER},
                "recommendation": recommendation,
                "reason_codes": reasons,
                "status": status,
                "error_type": error_type,
                "duration_ms": duration_ms,
            }
        },
    )
    return EvaluationOutcome(
        evaluation_run_id=run_id,
        candidate_spec_id=candidate_spec_id,
        mutation_run_id=mutation_id,
        status=status,
        recommendation=recommendation,
        reason_codes=tuple(reasons),
        categories=results,
        error_type=error_type,
        harness_called=harness_called,
    )


def _exists(session_factory: SessionFactory, mutation_run_id: uuid.UUID) -> bool:
    with session_factory() as session:
        return session.get(MutationRun, mutation_run_id) is not None
