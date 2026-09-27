"""generate_hypothesis: the Step 9 pipeline, synchronous, one bounded LLM call.

    load canonical signal -> retrieval plan -> retrieve (Step 8) -> EvidenceBundle
      -> [no excerpts? record insufficient_evidence, never call the model]
      -> build hypothesis.v1 request -> provider.generate_structured (once)
      -> parse + schema + grounding checks
      -> persist HypothesisRun (always) + Hypothesis (only if valid), one transaction

`generate_from_bundle` is the part after retrieval. The Step 10 research
graph calls it with an EvidenceBundle it assembled itself (possibly after a
refined second retrieval), so the request, schema, grounding and persistence
logic exist exactly once.

No database transaction is held open during the provider call: reads happen
in one session, writes in another. Every call creates a new run — repeated
generation is never deduplicated, because model output is not deterministic
and each call has a cost.

Logs carry ids, counts, outcome, latency and token counts only — never the
prompt, excerpt text or model output.
"""

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from darwin.db.models import BehaviorSignal, Hypothesis, HypothesisRun
from darwin.llm.port import (
    LLMProvider,
    ProviderFailureError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    StructuredGenerationRequest,
    StructuredGenerationResult,
)
from darwin.memory.embeddings import EmbeddingProvider
from darwin.memory.retrieval import retrieve

from .evidence import EvidenceBundle, build_evidence_bundle, generation_zero_components
from .prompt import REQUEST_VERSION, build_request, evidence_hash
from .queries import DEFAULT_TOP_K, build_retrieval_plan
from .schema import OutputCheck, check_output

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]


class SignalNotFoundError(LookupError):
    """No canonical (non-superseded) signal has this signal_id."""


@dataclass(frozen=True)
class GenerationOutcome:
    run_id: uuid.UUID
    status: str  # one of darwin.db.models.hypothesis.RUN_STATUSES
    error_type: str | None
    hypothesis_id: uuid.UUID | None
    bundle: EvidenceBundle
    request: StructuredGenerationRequest | None  # None when the model was not called
    latency_ms: float | None = None  # None when the model was not called
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def provider_called(self) -> bool:
        return self.request is not None


def call_provider(
    llm: LLMProvider, request: StructuredGenerationRequest
) -> tuple[StructuredGenerationResult | None, str | None, str | None, float]:
    """One provider call. Returns (result, failed_status, error_type, latency_ms).

    Provider errors become (None, status, error_type) — never exceptions, never
    error messages (which could contain anything). Shared with the Step 10 critique.
    """
    started = time.perf_counter()
    try:
        result = llm.generate_structured(request)
    except ProviderUnavailableError:
        return None, "provider_unavailable", "unavailable", _since(started)
    except ProviderTimeoutError:
        return None, "provider_error", "timeout", _since(started)
    except ProviderFailureError:
        return None, "provider_error", "failure", _since(started)
    except Exception as error:  # an adapter bug must still leave an audited run
        return None, "provider_error", f"unexpected:{type(error).__name__}"[:64], _since(started)
    return result, None, None, _since(started)


def _since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def generate_hypothesis(
    session_factory: SessionFactory,
    signal_id: uuid.UUID,
    llm: LLMProvider,
    embedder: EmbeddingProvider,
    *,
    top_k: int = DEFAULT_TOP_K,
    known_components: Sequence[str] | None = None,
) -> GenerationOutcome:
    components = generation_zero_components() if known_components is None else known_components

    # ---- read: signal + retrieval (then the transaction ends) ---------------------------
    with session_factory() as session:
        signal = session.scalar(
            select(BehaviorSignal).where(
                BehaviorSignal.signal_id == signal_id, BehaviorSignal.superseded_at.is_(None)
            )
        )
        if signal is None:
            raise SignalNotFoundError(f"no canonical signal {signal_id}")
        plan = build_retrieval_plan(signal, top_k)
        chunks = retrieve(session, embedder, plan.query, plan.top_k)
        bundle = build_evidence_bundle(signal, plan, chunks, embedder.name, components)
        session.rollback()  # read-only: nothing to keep, and no transaction held during the call
    return generate_from_bundle(session_factory, signal_id, bundle, llm)


def generate_from_bundle(
    session_factory: SessionFactory,
    signal_id: uuid.UUID,
    bundle: EvidenceBundle,
    llm: LLMProvider,
) -> GenerationOutcome:
    """Request -> one provider call -> checks -> HypothesisRun (+ Hypothesis)."""
    request = build_request(bundle)
    result: StructuredGenerationResult | None = None
    check: OutputCheck | None = None
    latency_ms: float | None = None
    error_type: str | None
    if not bundle.excerpts:
        status = "insufficient_evidence"
        error_type = "no_context" if bundle.retrieved == 0 else "low_relevance"
        sent: StructuredGenerationRequest | None = None
    else:
        sent = request
        result, status_or_none, error_type, latency_ms = call_provider(llm, request)
        if result is not None:
            check = check_output(result.output_text, bundle)
            status = "succeeded" if check.status == "valid" else check.status
            error_type = check.error_type
        else:
            assert status_or_none is not None
            status = status_or_none

    # ---- write: run (+ hypothesis) in one transaction ---------------------------------
    run_id = uuid.uuid4()
    usage = result.usage if result else None
    run = HypothesisRun(
        id=run_id,
        signal_id=signal_id,
        signal_type=bundle.signal.signal_type,
        request_version=REQUEST_VERSION,
        provider=result.provider if result else llm.name,
        model=result.model if result else llm.model,
        embedding_model=bundle.embedding_model,
        retrieval_query=bundle.retrieval_query[:1000],
        evidence_chunk_ids=list(bundle.chunk_ids),
        evidence_hash=evidence_hash(request),
        status=status,
        error_type=error_type,
        validation_errors=list(check.errors) if check else [],
        output=check.draft.model_dump() if check and check.draft else None,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        latency_ms=latency_ms,
    )
    hypothesis_id: uuid.UUID | None = None
    with session_factory() as session:
        session.add(run)
        session.flush()  # the run row must exist before the hypothesis references it
        if status == "succeeded" and check is not None and check.draft is not None:
            draft = check.draft
            by_id = {str(e.chunk_id): e for e in bundle.excerpts}
            hypothesis = Hypothesis(
                id=uuid.uuid4(),
                run_id=run_id,
                signal_id=signal_id,
                statement=draft.statement,
                rationale=draft.rationale,
                affected_component=draft.affected_component,
                confidence=draft.confidence,
                evidence_references=[
                    {
                        "chunk_id": ref,
                        "source_key": by_id[ref].source_key,
                        "section": by_id[ref].section,
                    }
                    for ref in draft.evidence_chunk_ids
                ],
                limitations=list(draft.limitations),
                status="proposed",
            )
            session.add(hypothesis)
            hypothesis_id = hypothesis.id
        session.commit()

    logger.info(
        "hypothesis run finished",
        extra={
            "context": {
                "run_id": str(run_id),
                "signal_id": str(signal_id),
                "signal_type": bundle.signal.signal_type,
                "provider": result.provider if result else llm.name,
                "model": result.model if result else llm.model,
                "request_version": REQUEST_VERSION,
                "evidence_chunks": len(bundle.excerpts),
                "status": status,
                "error_type": error_type,
                "latency_ms": latency_ms,
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
            }
        },
    )
    return GenerationOutcome(
        run_id,
        status,
        error_type,
        hypothesis_id,
        bundle,
        sent,
        latency_ms=latency_ms,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
    )
