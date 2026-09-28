"""Provenance re-check before any evaluator runs, and the bounded evaluation context.

Checked, in order (the first failure ends the evaluation as `reject`,
status provenance_failed, with no harness call):

  candidate row is a candidate; its parent exists and is a generation (a baseline
  or, since Step 15, a promoted generation);
  both stored content hashes match their content (nothing was altered);
  candidate_for_generation = parent.generation + 1 = candidate.spec.generation;
  a SUCCEEDED MutationRun points at this candidate from this parent (the one
  named, or else the latest such run);
  that run's decision still says proceed.

The context passed on to the checks is bounded: the two specs, the
MutationSpec operations, the signal type and the hypothesis's affected
component. No hypothesis text, critique text, Product Memory, telemetry or ids
of sessions and events.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from darwin.db.models import (
    BehaviorSignal,
    DecisionRun,
    Hypothesis,
    MutationRun,
    ResearchRun,
    UISpecVersion,
)
from darwin.mutations.apply import content_hash


class CandidateNotFoundError(LookupError):
    pass


class ProvenanceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EvaluationContext:
    candidate_id: uuid.UUID
    parent_id: uuid.UUID
    mutation_run_id: uuid.UUID
    source: dict[str, Any]
    candidate: dict[str, Any]
    operations: tuple[dict[str, Any], ...]  # from the stored MutationSpec (may be empty)
    signal_type: str
    affected_component: str | None


def load_context(
    session: Session, candidate_id: uuid.UUID, mutation_run_id: uuid.UUID | None
) -> EvaluationContext:
    candidate = session.get(UISpecVersion, candidate_id)
    if candidate is None:
        raise CandidateNotFoundError(f"no UI Spec version {candidate_id}")
    if candidate.status != "candidate" or candidate.parent_id is None:
        raise ProvenanceError("not_a_candidate")
    parent = session.get(UISpecVersion, candidate.parent_id)
    if parent is None or parent.status not in ("baseline", "promoted") or parent.generation is None:
        raise ProvenanceError("parent_not_a_baseline")
    if content_hash(candidate.spec) != candidate.content_hash:
        raise ProvenanceError("candidate_hash_mismatch")
    if content_hash(parent.spec) != parent.content_hash:
        raise ProvenanceError("source_hash_mismatch")
    expected_generation = parent.generation + 1
    if (
        candidate.candidate_for_generation != expected_generation
        or candidate.spec.get("generation") != expected_generation
    ):
        raise ProvenanceError("generation_mismatch")

    if mutation_run_id is not None:
        run = session.get(MutationRun, mutation_run_id)
        if run is None:
            raise ProvenanceError("mutation_run_not_found")
    else:
        run = session.scalar(
            select(MutationRun)
            .where(MutationRun.candidate_spec_id == candidate.id, MutationRun.status == "succeeded")
            .order_by(MutationRun.created_at.desc(), MutationRun.id)
            .limit(1)
        )
        if run is None:
            raise ProvenanceError("no_succeeded_mutation_run")
    if run.status != "succeeded":
        raise ProvenanceError("mutation_run_not_succeeded")
    if run.candidate_spec_id != candidate.id:
        raise ProvenanceError("mutation_run_candidate_mismatch")
    if run.source_spec_id != parent.id:
        raise ProvenanceError("mutation_run_source_mismatch")

    decision = session.get(DecisionRun, run.decision_run_id)
    if decision is None or decision.decision != "proceed" or decision.status != "decided":
        raise ProvenanceError("decision_not_proceed")
    hypothesis = session.get(Hypothesis, decision.hypothesis_id)
    research = session.get(ResearchRun, decision.research_run_id)
    signal = (
        session.scalar(select(BehaviorSignal).where(BehaviorSignal.signal_id == research.signal_id))
        if research
        else None
    )
    if hypothesis is None or signal is None:
        raise ProvenanceError("decision_chain_broken")

    operations = tuple((run.mutation_spec or {}).get("operations", []))
    return EvaluationContext(
        candidate_id=candidate.id,
        parent_id=parent.id,
        mutation_run_id=run.id,
        source=dict(parent.spec),
        candidate=dict(candidate.spec),
        operations=operations,
        signal_type=signal.signal_type,
        affected_component=hypothesis.affected_component,
    )
