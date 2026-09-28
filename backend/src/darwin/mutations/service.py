"""generate_candidate: the Step 12 pipeline, one explicit generator call.

    decision -> provenance re-check (stale? record stale_provenance; never call the generator)
      -> MutationRequest -> generator.generate (once)
      -> MutationSpec checks -> pure apply -> protected diff
      -> MutationRun (always) + CandidateUISpec (only on success; deduplicated by content)

Nothing is written outside the database. Candidate specs are never written to
frontend/src/ui-spec/, never rendered and never deployed here.

Logs: ids, generator and version, operation count, status, error type,
latency, tokens. Never the spec, the MutationSpec (it has text), prompts or
model output.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from darwin.db.models import MutationRun, UISpecVersion
from darwin.hypotheses.service import SessionFactory
from darwin.observability import stage

from .apply import Change, content_hash
from .port import (
    GeneratorReply,
    GeneratorTimeoutError,
    GeneratorUnavailableError,
    MutationGenerator,
)
from .request import (
    DEMO_PAGE_ID,
    MUTATION_REQUEST_VERSION,
    MutationRequest,
    StaleProvenanceError,
    build_mutation_request,
    load_proceed_context,
)
from .spec import MutationCheck, check_mutation
from .specs import current_baseline

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MutationOutcome:
    mutation_run_id: uuid.UUID
    decision_run_id: uuid.UUID
    source_spec_id: uuid.UUID
    candidate_spec_id: uuid.UUID | None
    generator: str
    generator_version: str
    status: str
    error_type: str | None
    changes: tuple[Change, ...]
    request: MutationRequest | None
    candidate: dict[str, Any] | None
    generator_called: bool


def _call(
    generator: MutationGenerator, request: MutationRequest
) -> tuple[GeneratorReply | None, str | None, str | None]:
    try:
        return generator.generate(request), None, None
    except GeneratorUnavailableError:
        return None, "generator_unavailable", "unavailable"
    except GeneratorTimeoutError:
        return None, "generator_error", "timeout"
    except Exception as error:  # GeneratorFailureError, or a generator bug
        return None, "generator_error", f"error:{type(error).__name__}"[:64]


def generate_candidate(
    session_factory: SessionFactory,
    decision_run_id: uuid.UUID,
    generator: MutationGenerator,
    source_spec_id: uuid.UUID | None = None,
) -> MutationOutcome:
    """Traced as `mutation.generate`: generator, version, status, operation count —
    never the MutationSpec, its values or the candidate spec."""
    with stage("mutation.generate", "mutation", {"darwin.decision_run.id": decision_run_id}) as s:
        outcome = _generate_candidate(session_factory, decision_run_id, generator, source_spec_id)
        s.set(
            **{
                "darwin.mutation_run.id": outcome.mutation_run_id,
                "darwin.generator.type": outcome.generator,
                "darwin.generator.version": outcome.generator_version,
                "darwin.status": outcome.status,
                "darwin.error.type": outcome.error_type,
                "darwin.mutation.operation_count": len(outcome.changes),
            }
        )
        s.outcome = outcome.status
        return outcome


def _generate_candidate(
    session_factory: SessionFactory,
    decision_run_id: uuid.UUID,
    generator: MutationGenerator,
    source_spec_id: uuid.UUID | None = None,
) -> MutationOutcome:
    stale: StaleProvenanceError | None = None
    request: MutationRequest | None = None
    with session_factory() as session:
        try:
            context = load_proceed_context(session, decision_run_id, source_spec_id)
            request = build_mutation_request(session, context)
            source_id, source_spec = context.source.id, dict(context.source.spec)
        except StaleProvenanceError as error:
            stale = error
            source_id = source_spec_id or _baseline_id(session)
            source_spec = {}
        session.rollback()

    reply: GeneratorReply | None = None
    check: MutationCheck | None = None
    latency_ms: float | None = None
    error_type: str | None
    if stale is not None:
        status, error_type = "stale_provenance", stale.code
        errors = [{"loc": "provenance", "type": stale.code}]
    else:
        assert request is not None
        started = time.perf_counter()
        reply, failed_status, failure = _call(generator, request)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        if reply is None:
            status, error_type = failed_status or "generator_error", failure
            errors = [{"loc": "generator", "type": failure or "error"}]
        else:
            check = check_mutation(reply.output, request, source_spec)
            status = "succeeded" if check.status == "valid" else check.status
            error_type, errors = check.error_type, check.errors

    run_id = uuid.uuid4()
    candidate_id: uuid.UUID | None = None
    with session_factory() as session:
        if status == "succeeded" and check is not None and check.candidate is not None:
            candidate_id = _store_candidate(session, source_id, check.candidate, request)
        session.add(
            MutationRun(
                id=run_id,
                decision_run_id=decision_run_id,
                source_spec_id=source_id,
                candidate_spec_id=candidate_id,
                generator=generator.name,
                generator_version=(reply.generator_version if reply else generator.version)[:128],
                request_version=MUTATION_REQUEST_VERSION,
                request_hash=request.request_hash() if request is not None and not stale else None,
                status=status,
                error_type=error_type,
                validation_errors=errors,
                mutation_spec=check.spec.model_dump(mode="json") if check and check.spec else None,
                operation_count=len(check.spec.operations) if check and check.spec else None,
                input_tokens=reply.input_tokens if reply else None,
                output_tokens=reply.output_tokens if reply else None,
                latency_ms=latency_ms,
            )
        )
        session.commit()

    outcome = MutationOutcome(
        mutation_run_id=run_id,
        decision_run_id=decision_run_id,
        source_spec_id=source_id,
        candidate_spec_id=candidate_id,
        generator=generator.name,
        generator_version=reply.generator_version if reply else generator.version,
        status=status,
        error_type=error_type,
        changes=check.changes if check else (),
        request=request if not stale else None,
        candidate=check.candidate if check and status == "succeeded" else None,
        generator_called=stale is None,
    )
    logger.info(
        "mutation run finished",
        extra={
            "context": {
                "mutation_run_id": str(run_id),
                "decision_run_id": str(decision_run_id),
                "source_spec_id": str(source_id),
                "candidate_spec_id": str(candidate_id) if candidate_id else None,
                "generator": generator.name,
                "generator_version": outcome.generator_version,
                "operation_count": len(check.spec.operations) if check and check.spec else None,
                "status": status,
                "error_type": error_type,
                "latency_ms": latency_ms,
                "input_tokens": reply.input_tokens if reply else None,
                "output_tokens": reply.output_tokens if reply else None,
            }
        },
    )
    return outcome


def _baseline_id(session: Any) -> uuid.UUID:
    baseline = current_baseline(session, DEMO_PAGE_ID)
    assert baseline is not None  # load_proceed_context refuses a missing baseline first
    return baseline.id


def _store_candidate(
    session: Any, parent_id: uuid.UUID, candidate: dict[str, Any], request: MutationRequest | None
) -> uuid.UUID:
    """Content-addressed: the same change to the same parent reuses one immutable row."""
    digest = content_hash(candidate)
    existing = session.scalar(
        select(UISpecVersion.id).where(
            UISpecVersion.parent_id == parent_id, UISpecVersion.content_hash == digest
        )
    )
    if existing is not None:
        return uuid.UUID(str(existing))
    assert request is not None
    row = UISpecVersion(
        id=uuid.uuid4(),
        page_id=request.source_spec.page_id,
        status="candidate",
        generation=None,
        candidate_for_generation=request.source_spec.generation + 1,
        parent_id=parent_id,
        schema_version=int(candidate["version"]),
        spec=candidate,
        content_hash=digest,
        source="mutation_run",
    )
    session.add(row)
    session.flush()
    return row.id
