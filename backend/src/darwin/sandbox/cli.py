"""Developer CLI: evaluate one candidate UI Spec in the sandbox.

    python -m darwin.sandbox.cli [--candidate-spec-id <uuid>] [--mutation-run-id <uuid>]
    make candidate-eval [CANDIDATE_SPEC_ID=<uuid>] [MUTATION_RUN_ID=<uuid>]

Without an id, the most recent candidate is evaluated. The evaluator is fixed
(candidate_eval.v1); no evaluator code is accepted from the command line.
Category outcomes are printed separately; `pass` means eligible for FUTURE
human approval only.
"""

import argparse
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import UISpecVersion
from darwin.logging_config import configure_logging

from .policy import CATEGORY_ORDER, EVALUATOR_VERSION
from .provenance import CandidateNotFoundError
from .service import evaluate_candidate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate one candidate UI Spec (sandbox).")
    parser.add_argument("--candidate-spec-id", type=uuid.UUID)
    parser.add_argument("--mutation-run-id", type=uuid.UUID)
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(str(settings.database_url))
    factory = sessionmaker(engine)
    try:
        candidate_id = args.candidate_spec_id
        if candidate_id is None:
            with factory() as session:
                candidate_id = session.scalar(
                    select(UISpecVersion.id)
                    .where(UISpecVersion.status == "candidate")
                    .order_by(UISpecVersion.created_at.desc(), UISpecVersion.id)
                    .limit(1)
                )
            if candidate_id is None:
                print("No candidate UI Spec. Run `make mutation-generate` first.")
                return 1
        try:
            outcome = evaluate_candidate(
                factory, candidate_id, mutation_run_id=args.mutation_run_id
            )
        except CandidateNotFoundError as error:
            print(f"Cannot evaluate: {error}")
            return 1
    finally:
        engine.dispose()
    print(f"candidate      {outcome.candidate_spec_id}")
    print(f"mutation run   {outcome.mutation_run_id}")
    print(f"evaluation     {outcome.evaluation_run_id}  ({EVALUATOR_VERSION})")
    print(
        f"status         {outcome.status}"
        + (f"  ({outcome.error_type})" if outcome.error_type else "")
    )
    for name in CATEGORY_ORDER:
        result = outcome.categories[name]
        failed = [f"{c['name']}:{c['reason']}" for c in result.get("checks", []) if not c["ok"]]
        suffix = f"  [{', '.join(failed)}]" if failed else ""
        print(f"  {name:<14} {result['status']:<8} {result['kind']}{suffix}")
    print(f"recommendation {outcome.recommendation}   reasons: {', '.join(outcome.reason_codes)}")
    if outcome.recommendation == "pass":
        print("note           pass = eligible for future human approval; nothing was deployed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
