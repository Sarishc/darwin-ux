"""Bootstrap, human approval, atomic promotion and rollback. CLI-only; no HTTP route.

    bootstrap_active(page)          pointer -> Generation 0 (idempotent)
    decide(analysis, approve|reject) immutable PromotionApproval (approve only if eligible)
    promote(approval)               ONE transaction: lock pointer -> re-review -> same
                                    evidence hash? -> new promoted UISpecVersion ->
                                    GenerationPromotion -> pointer update -> commit
    rollback(page)                  ONE transaction: lock pointer -> known-good earlier
                                    generation -> GenerationRollback -> pointer update

Database triggers (migration 0011) independently refuse a pointer move without a
matching record, a record without the pointer move, a promoted row that differs from
its candidate, a replayed approval and duplicate generation numbers. The deferred
checks are forced before commit (SET CONSTRAINTS ALL IMMEDIATE) so they also run in
savepoint-based tests.

Reviewer strings are SELF-ASSERTED labels, not authenticated identities.
Logs: ids, page, generations, reviewer, result — never specs, reports or reasons.
"""

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from darwin.db.models import (
    ActiveGeneration,
    GenerationPromotion,
    GenerationRollback,
    PromotionApproval,
    UISpecVersion,
)
from darwin.mutations.apply import content_hash
from darwin.observability import stage

from .eligibility import PointerMissingError, review
from .vocabulary import POLICY_VERSION, REASON_MAX, REVIEWER_PATTERN, Decision

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], Session]
_REVIEWER = re.compile(REVIEWER_PATTERN)


class GenerationInputError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _log(message: str, **context: object) -> None:
    logger.info(message, extra={"context": {k: str(v) for k, v in context.items()}})


def validate_reviewer(reviewer: object) -> str:
    if not isinstance(reviewer, str) or not _REVIEWER.fullmatch(reviewer):
        raise GenerationInputError("reviewer_invalid")
    return reviewer


def validate_reason(reason: object) -> str:
    if not isinstance(reason, str):
        raise GenerationInputError("reason_invalid")
    cleaned = reason.strip()
    if not 1 <= len(cleaned) <= REASON_MAX or any(ord(c) < 32 and c not in "\t" for c in cleaned):
        raise GenerationInputError("reason_invalid")
    return cleaned


# ---- bootstrap ------------------------------------------------------------------------------


def bootstrap_active(factory: SessionFactory, page_id: str) -> tuple[str, int]:
    """Point the page at its Generation 0 baseline. Idempotent: an existing pointer is kept."""
    with factory() as session:
        pointer = session.get(ActiveGeneration, page_id)
        if pointer is not None:
            return "unchanged", pointer.generation
        gen0 = session.scalar(
            select(UISpecVersion).where(
                UISpecVersion.page_id == page_id,
                UISpecVersion.status == "baseline",
                UISpecVersion.generation == 0,
            )
        )
        if gen0 is None:
            raise GenerationInputError("generation_zero_missing")
        session.add(
            ActiveGeneration(
                page_id=page_id,
                ui_spec_version_id=gen0.id,
                generation=0,
                change_kind="bootstrap",
                change_id=None,
            )
        )
        try:
            session.commit()
        except IntegrityError:  # a concurrent bootstrap won; that is fine
            session.rollback()
            return "unchanged", 0
        _log("active generation bootstrapped", page=page_id, generation=0)
        return "created", 0


# ---- approval ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionOutcome:
    approval_id: uuid.UUID | None
    decision: str
    blocking: tuple[str, ...]
    evidence_hash: str | None
    target_generation: int | None

    @property
    def recorded(self) -> bool:
        return self.approval_id is not None


def decide(
    factory: SessionFactory,
    experiment_analysis_id: uuid.UUID,
    decision: Decision,
    reviewer: str,
    reason: str,
) -> DecisionOutcome:
    """Record a human decision. `approve` only when the gate has no blocking reason.

    Traced as `promotion.decide`: decision, eligibility, target generation. The
    reviewer and the reason are audit data and never reach traces or metrics.
    """
    with stage("promotion.decide", "approval", {"darwin.approval.decision": decision}) as s:
        outcome = _decide(factory, experiment_analysis_id, decision, reviewer, reason)
        s.set(
            **{
                "darwin.promotion.eligible": not outcome.blocking,
                "darwin.generation.to": outcome.target_generation,
                "darwin.promotion.reason": outcome.blocking[0] if outcome.blocking else None,
            }
        )
        s.outcome = "recorded" if outcome.recorded else "refused"
        return outcome


def _decide(
    factory: SessionFactory,
    experiment_analysis_id: uuid.UUID,
    decision: Decision,
    reviewer: str,
    reason: str,
) -> DecisionOutcome:
    reviewer, reason = validate_reviewer(reviewer), validate_reason(reason)
    if decision not in ("approve", "reject"):
        raise GenerationInputError("decision_invalid")
    with factory() as session:
        result = review(session, experiment_analysis_id)
        ev = result.evidence
        if decision == "approve" and result.blocking:
            session.rollback()
            _log("approval refused", analysis=experiment_analysis_id, reasons=result.blocking)
            return DecisionOutcome(None, decision, result.blocking, ev.hash(), None)
        row = PromotionApproval(
            page_id=ev.page_id,
            candidate_spec_id=uuid.UUID(ev.candidate_spec_id),
            candidate_evaluation_run_id=uuid.UUID(ev.candidate_evaluation_run_id),
            experiment_id=uuid.UUID(ev.experiment_id),
            experiment_analysis_id=experiment_analysis_id,
            source_spec_id=uuid.UUID(ev.source_spec_id),
            source_generation=ev.source_generation,
            target_generation=ev.target_generation,
            decision=decision,
            reviewer=reviewer,
            reason=reason,
            policy_version=POLICY_VERSION,
            evidence_hash=ev.hash(),
            blocking_reasons=list(result.blocking),
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:  # uq_promotion_approval_evidence: already approved
            session.rollback()
            return DecisionOutcome(None, decision, ("duplicate_approval",), ev.hash(), None)
        _log(
            "promotion decision recorded",
            approval=row.id,
            decision=decision,
            page=ev.page_id,
            target=ev.target_generation,
            reviewer=reviewer,
        )
        return DecisionOutcome(row.id, decision, result.blocking, ev.hash(), ev.target_generation)


# ---- promotion ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeOutcome:
    record_id: uuid.UUID | None  # the promotion or rollback record
    page_id: str
    from_generation: int | None
    to_generation: int | None
    to_spec_id: uuid.UUID | None
    reasons: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.record_id is not None


def _refuse(
    page: str, reasons: list[str] | tuple[str, ...], frm: int | None = None
) -> ChangeOutcome:
    _log("generation change refused", page=page, reasons=",".join(reasons))
    return ChangeOutcome(None, page, frm, None, None, tuple(reasons))


def _lock_pointer(session: Session, page_id: str) -> ActiveGeneration | None:
    # Commit-time checks run at the end of THIS operation, whatever mode the surrounding
    # transaction was left in; they are forced (IMMEDIATE) just before commit.
    session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    # Row lock: concurrent promotions/rollbacks of one page are serialised here.
    return session.get(ActiveGeneration, page_id, with_for_update=True)


def expected_confirmation(page_id: str, generation: int) -> str:
    return f"{page_id}:{generation}"


def promote(
    factory: SessionFactory,
    approval_id: uuid.UUID,
    reviewer: str,
    confirm: str,
    *,
    before_commit: Callable[[Session], Any] | None = None,  # tests: inject a failure
) -> ChangeOutcome:
    """Traced as `generation.promote`: page, from/to generation, result, first refusal
    code. No reviewer, no reason, no spec."""
    with stage("generation.promote", "promotion") as s:
        result = _promote(factory, approval_id, reviewer, confirm, before_commit=before_commit)
        _describe_change(s, result)
        return result


def _describe_change(s: Any, result: "ChangeOutcome") -> None:
    s.set(
        **{
            "darwin.page": result.page_id,
            "darwin.generation.from": result.from_generation,
            "darwin.generation.to": result.to_generation,
            "darwin.promotion.reason": result.reasons[0] if result.reasons else None,
        }
    )
    s.outcome = "changed" if result.changed else "refused"


def _promote(
    factory: SessionFactory,
    approval_id: uuid.UUID,
    reviewer: str,
    confirm: str,
    *,
    before_commit: Callable[[Session], Any] | None = None,
) -> ChangeOutcome:
    reviewer = validate_reviewer(reviewer)
    with factory() as session:
        approval = session.get(PromotionApproval, approval_id)
        if approval is None:
            return _refuse("?", ["approval_not_found"])
        page = approval.page_id
        pointer = _lock_pointer(session, page)
        if pointer is None:
            session.rollback()
            return _refuse(page, ["active_generation_missing"])
        problems: list[str] = []
        if approval.decision != "approve":
            problems.append("approval_is_not_approve")
        if approval.policy_version != POLICY_VERSION:
            problems.append("policy_version_changed")
        if session.scalar(
            select(GenerationPromotion.id).where(GenerationPromotion.approval_id == approval.id)
        ):
            problems.append("approval_already_used")
        later_reject = session.scalar(
            select(PromotionApproval.id).where(
                PromotionApproval.evidence_hash == approval.evidence_hash,
                PromotionApproval.decision == "reject",
                PromotionApproval.created_at >= approval.created_at,
                PromotionApproval.id != approval.id,
            )
        )
        if later_reject is not None:
            problems.append("rejected_after_approval")
        if problems:
            session.rollback()
            return _refuse(page, problems, pointer.generation)
        try:
            current = review(session, approval.experiment_analysis_id)  # TOCTOU: re-derive
        except PointerMissingError:
            session.rollback()
            return _refuse(page, ["active_generation_missing"])
        if current.blocking:
            session.rollback()
            return _refuse(page, current.blocking, pointer.generation)
        ev = current.evidence
        if ev.hash() != approval.evidence_hash:
            session.rollback()
            return _refuse(page, ["evidence_changed"], pointer.generation)
        if confirm != expected_confirmation(page, ev.target_generation):
            session.rollback()
            return _refuse(page, ["confirmation_mismatch"], pointer.generation)

        candidate = session.get(UISpecVersion, uuid.UUID(ev.candidate_spec_id))
        assert candidate is not None
        spec = {**candidate.spec, "generation": ev.target_generation}
        promoted = UISpecVersion(
            id=uuid.uuid4(),
            page_id=page,
            status="promoted",
            generation=ev.target_generation,
            candidate_for_generation=None,
            parent_id=candidate.id,
            schema_version=candidate.schema_version,
            spec=spec,
            content_hash=content_hash(spec),
            source="promotion",
        )
        record = GenerationPromotion(
            id=uuid.uuid4(),
            approval_id=approval.id,
            page_id=page,
            candidate_spec_id=candidate.id,
            promoted_spec_id=promoted.id,
            from_spec_id=pointer.ui_spec_version_id,
            from_generation=pointer.generation,
            to_generation=ev.target_generation,
            reviewer=reviewer,
            policy_version=POLICY_VERSION,
            evidence_hash=ev.hash(),
        )
        from_generation = pointer.generation
        try:
            session.add(promoted)
            session.flush()
            session.add(record)
            session.flush()
            pointer.ui_spec_version_id = promoted.id
            pointer.generation = ev.target_generation
            pointer.change_kind = "promotion"
            pointer.change_id = record.id
            session.flush()
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))  # run the deferred checks
            if before_commit is not None:
                before_commit(session)
            session.commit()
        except IntegrityError as error:
            session.rollback()
            diag = getattr(error.orig, "diag", None)
            refused_by = (
                getattr(diag, "constraint_name", None)
                or str(getattr(diag, "message_primary", "") or "").split(" (")[0]
            )
            _log("promotion refused by the database", page=page, refused_by=refused_by)
            return _refuse(page, ["concurrent_or_duplicate_promotion"], from_generation)
        _log(
            "generation promoted",
            promotion=record.id,
            approval=approval.id,
            page=page,
            frm=from_generation,
            to=ev.target_generation,
            reviewer=reviewer,
        )
        return ChangeOutcome(
            record.id, page, from_generation, ev.target_generation, promoted.id, ()
        )


# ---- rollback -------------------------------------------------------------------------------


def rollback_target(session: Session, pointer: ActiveGeneration) -> UISpecVersion | None:
    """Default target: the generation the current one was promoted FROM (never a candidate)."""
    promotion = session.scalar(
        select(GenerationPromotion).where(
            GenerationPromotion.promoted_spec_id == pointer.ui_spec_version_id
        )
    )
    if promotion is None:
        return None  # Generation 0 (or an unpromoted baseline): nothing earlier to return to
    return session.get(UISpecVersion, promotion.from_spec_id)


def rollback(
    factory: SessionFactory,
    page_id: str,
    reviewer: str,
    reason: str,
    confirm: str,
    to_generation: int | None = None,
) -> ChangeOutcome:
    """Traced as `generation.rollback`: page, from/to generation, result. No reviewer/reason."""
    with stage("generation.rollback", "rollback") as s:
        result = _rollback(factory, page_id, reviewer, reason, confirm, to_generation)
        _describe_change(s, result)
        return result


def _rollback(
    factory: SessionFactory,
    page_id: str,
    reviewer: str,
    reason: str,
    confirm: str,
    to_generation: int | None = None,
) -> ChangeOutcome:
    reviewer, reason = validate_reviewer(reviewer), validate_reason(reason)
    with factory() as session:
        pointer = _lock_pointer(session, page_id)
        if pointer is None:
            session.rollback()
            return _refuse(page_id, ["active_generation_missing"])
        if to_generation is None:
            target = rollback_target(session, pointer)
            if target is None:
                session.rollback()
                return _refuse(page_id, ["no_earlier_generation"], pointer.generation)
        else:
            if type(to_generation) is not int:
                session.rollback()
                return _refuse(page_id, ["target_invalid"], pointer.generation)
            target = session.scalar(
                select(UISpecVersion).where(
                    UISpecVersion.page_id == page_id,
                    UISpecVersion.generation == to_generation,
                    UISpecVersion.status.in_(("baseline", "promoted")),
                )
            )
            if target is None:
                session.rollback()
                return _refuse(page_id, ["target_not_a_generation"], pointer.generation)
        assert target.generation is not None
        problems: list[str] = []
        if target.status not in ("baseline", "promoted"):
            problems.append("target_not_a_generation")  # never a candidate
        if target.generation >= pointer.generation:
            problems.append("target_not_earlier")
        if content_hash(target.spec) != target.content_hash:
            problems.append("target_hash_mismatch")  # known-good means unaltered
        if confirm != expected_confirmation(page_id, target.generation):
            problems.append("confirmation_mismatch")
        if problems:
            session.rollback()
            return _refuse(page_id, problems, pointer.generation)
        record = GenerationRollback(
            id=uuid.uuid4(),
            page_id=page_id,
            from_spec_id=pointer.ui_spec_version_id,
            from_generation=pointer.generation,
            to_spec_id=target.id,
            to_generation=target.generation,
            reviewer=reviewer,
            reason=reason,
        )
        from_generation = pointer.generation
        session.add(record)
        session.flush()
        pointer.ui_spec_version_id = target.id
        pointer.generation = target.generation
        pointer.change_kind = "rollback"
        pointer.change_id = record.id
        session.flush()
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.commit()
        _log(
            "generation rolled back",
            rollback=record.id,
            page=page_id,
            frm=from_generation,
            to=target.generation,
            reviewer=reviewer,
        )
        return ChangeOutcome(record.id, page_id, from_generation, target.generation, target.id, ())
