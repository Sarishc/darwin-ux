"""Generation CLI — the ONLY way to approve, promote or roll back (human commands).

    python -m darwin.generations.cli bootstrap [--page pricing_signup]
    python -m darwin.generations.cli show      [--page pricing_signup]
    python -m darwin.generations.cli review    --analysis-id <uuid>
    python -m darwin.generations.cli approve   --analysis-id <uuid> --reviewer <name> --reason "..."
    python -m darwin.generations.cli reject    --analysis-id <uuid> --reviewer <name> --reason "..."
    python -m darwin.generations.cli promote   --approval-id <uuid> --reviewer <name> \\
                                               --confirm <page>:<target generation>
    python -m darwin.generations.cli rollback  --page <page> --reviewer <name> \\
        --reason "..." --confirm <page>:<target generation> [--to-generation N]

`--reviewer` is a SELF-ASSERTED operator label (there is no authentication yet).
Confirmations name the exact page and generation that will become active, so a
command cannot be run "blind". Output shows ids, hashes, generations and aggregate
evidence — never full specs, raw events or session ids. No command declares a winner.
"""

import argparse
import json
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import (
    GenerationPromotion,
    GenerationRollback,
    UISpecVersion,
)
from darwin.logging_config import configure_logging
from darwin.observability import setup_observability

from .active import active_pointer
from .eligibility import AnalysisNotFoundError, PointerMissingError, review
from .service import (
    GenerationInputError,
    SessionFactory,
    bootstrap_active,
    decide,
    promote,
    rollback,
)

DEFAULT_PAGE = "pricing_signup"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DarwinUX generations (human commands only).")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap", "show"):
        sub.add_parser(name).add_argument("--page", default=DEFAULT_PAGE)
    sub.add_parser("review").add_argument("--analysis-id", type=uuid.UUID, required=True)
    for name in ("approve", "reject"):
        command = sub.add_parser(name)
        command.add_argument("--analysis-id", type=uuid.UUID, required=True)
        command.add_argument("--reviewer", required=True)
        command.add_argument("--reason", required=True)
    command = sub.add_parser("promote")
    command.add_argument("--approval-id", type=uuid.UUID, required=True)
    command.add_argument("--reviewer", required=True)
    command.add_argument("--confirm", required=True, help="<page>:<target generation>")
    command = sub.add_parser("rollback")
    command.add_argument("--page", default=DEFAULT_PAGE)
    command.add_argument("--reviewer", required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--confirm", required=True, help="<page>:<target generation>")
    command.add_argument("--to-generation", type=int)
    return parser


def _show(factory: SessionFactory, page: str) -> int:
    with factory() as session:
        pointer = active_pointer(session, page)
        if pointer is None:
            print(f"page            {page}\nactive          none (run `make generation-bootstrap`)")
            return 1
        spec = session.get(UISpecVersion, pointer.ui_spec_version_id)
        assert spec is not None
        print(f"page            {page}")
        print(f"active          generation {pointer.generation}  ({spec.status})")
        print(f"spec            {spec.id}  sha256={spec.content_hash}")
        print(
            f"last change     {pointer.change_kind}  {pointer.change_id or '-'}  "
            f"at {pointer.updated_at}"
        )
        promotion = session.scalar(
            select(GenerationPromotion).where(GenerationPromotion.promoted_spec_id == spec.id)
        )
        if promotion is not None:
            print(f"promoted by     {promotion.id}  (approval {promotion.approval_id})")
            print(f"previous        generation {promotion.from_generation}")
        rows = session.scalars(
            select(UISpecVersion)
            .where(
                UISpecVersion.page_id == page,
                UISpecVersion.status.in_(("baseline", "promoted")),
            )
            .order_by(UISpecVersion.generation)
        ).all()
        print("generations     " + ", ".join(f"{r.generation} ({r.status})" for r in rows))
        rollbacks = session.scalars(
            select(GenerationRollback).where(GenerationRollback.page_id == page)
        ).all()
        print(f"rollbacks       {len(rollbacks)}")
    return 0


def run(argv: Sequence[str] | None, factory: SessionFactory) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "bootstrap":
            status, generation = bootstrap_active(factory, args.page)
            print(f"active generation for {args.page}: {generation} ({status})")
            return 0
        if args.command == "show":
            return _show(factory, args.page)
        if args.command == "review":
            with factory() as session:
                found = review(session, args.analysis_id)
            ev = found.evidence
            print(f"eligible        {found.eligible}   (policy {ev.policy_version})")
            print(f"blocking        {', '.join(found.blocking) or '-'}")
            print(f"source -> target generation {ev.source_generation} -> {ev.target_generation}")
            print(f"evidence_hash   {ev.hash()}")
            print(json.dumps(found.summary, indent=2, sort_keys=True))
            print("note            evidence for a HUMAN decision; no winner is declared")
            return 0 if found.eligible else 1
        if args.command in ("approve", "reject"):
            outcome = decide(factory, args.analysis_id, args.command, args.reviewer, args.reason)
            if not outcome.recorded:
                print(f"REFUSED: {', '.join(outcome.blocking)}. Nothing was recorded.")
                return 1
            print(f"approval        {outcome.approval_id}  decision={outcome.decision}")
            print(f"evidence_hash   {outcome.evidence_hash}")
            if outcome.decision == "approve":
                print(
                    f"next            make generation-promote APPROVAL_ID={outcome.approval_id} "
                    f"REVIEWER=... CONFIRM=<page>:{outcome.target_generation}"
                )
            return 0
        if args.command == "promote":
            result = promote(factory, args.approval_id, args.reviewer, args.confirm)
        else:
            result = rollback(
                factory, args.page, args.reviewer, args.reason, args.confirm, args.to_generation
            )
    except (AnalysisNotFoundError, PointerMissingError) as error:
        print(f"REFUSED: {error}")
        return 1
    except GenerationInputError as error:
        print(f"REFUSED: {error.code}")
        return 1
    if not result.changed:
        print(f"REFUSED: {', '.join(result.reasons)}. The active generation did not change.")
        return 1
    print(
        f"{args.command:<15} {result.record_id}  {result.page_id}: generation "
        f"{result.from_generation} -> {result.to_generation}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    setup_observability(settings, "darwin-cli")
    engine = create_db_engine(str(settings.database_url))
    try:
        return run(argv, sessionmaker(engine))
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
