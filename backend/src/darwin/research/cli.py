"""Developer CLI for the research workflow (FakeLLMProvider only in Step 10).

    python -m darwin.research.cli run [--signal-id <uuid> | --type rage_click]
                                      [--fake-mode grounded] [--critique-mode accept]
    python -m darwin.research.cli resume --run-id <uuid> --decision approve|reject

    make research-run [SIGNAL_ID=<uuid>] [SIGNAL_TYPE=rage_click] [CRITIQUE_MODE=human_review]
    make research-resume RUN_ID=<uuid> DECISION=approve

Prints the trajectory and results to stdout for the developer. The decision
is an allowlisted word, never free text; logs stay content-free.
"""

import argparse
import uuid
from collections.abc import Sequence
from typing import cast

from sqlalchemy.orm import sessionmaker

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import Hypothesis
from darwin.db.models.research import HUMAN_DECISIONS
from darwin.hypotheses.generate import latest_signal_id
from darwin.hypotheses.service import SessionFactory, SignalNotFoundError
from darwin.llm.fake import CRITIQUE_MODES, FAKE_MODES, CritiqueMode, FakeLLMProvider, FakeMode
from darwin.logging_config import configure_logging
from darwin.memory.embeddings import HashingEmbeddingProvider
from darwin.observability import setup_observability

from .service import (
    InvalidDecisionError,
    ResearchOutcome,
    ResearchRunNotFoundError,
    ResumeNotAllowedError,
    resume_research,
    run_research,
)


def print_outcome(factory: SessionFactory, outcome: ResearchOutcome) -> None:
    print(f"research run  {outcome.run_id}")
    print(f"status        {outcome.status}  ({outcome.stop_reason})")
    print(f"trajectory    {' -> '.join(outcome.trajectory)}")
    print(
        f"budget used   retrievals={outcome.retrieval_attempts} llm_calls={outcome.llm_calls} "
        f"steps={outcome.steps}  tokens in={outcome.input_tokens} out={outcome.output_tokens} "
        f"(fake estimates)  elapsed_ms={outcome.elapsed_ms}"
    )
    for i, query in enumerate(outcome.queries, start=1):
        print(f"query {i}       {query}")
    if outcome.hypothesis_id is not None:
        with factory() as session:
            h = session.get(Hypothesis, outcome.hypothesis_id)
            assert h is not None
            print(f"hypothesis    {h.id}  [{h.status}, {h.confidence}, {h.affected_component}]")
            print(f"  statement   {h.statement}")
            for ref in h.evidence_references:
                print(f"  cites       {ref['source_key']}  §{ref['section'][:60]}")
    if outcome.critique:
        c = outcome.critique
        print(f"critique      {c['verdict']}: {c['summary']}")
        for label in ("issues", "unsupported_claims", "missing_evidence"):
            for item in c[label]:
                print(f"  {label:<18}{item}")
    if outcome.status == "waiting_for_human":
        print(f"\nHuman review needed ({outcome.review_reason}). Decide with:")
        print(f"  make research-resume RUN_ID={outcome.run_id} DECISION=approve   # or reject")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded research workflow (fake LLM).")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--signal-id", type=uuid.UUID)
    run.add_argument("--type", dest="signal_type")
    run.add_argument("--fake-mode", choices=FAKE_MODES, default="grounded")
    run.add_argument("--critique-mode", choices=CRITIQUE_MODES, default="accept")
    resume = commands.add_parser("resume")
    resume.add_argument("--run-id", type=uuid.UUID, required=True)
    resume.add_argument("--decision", choices=HUMAN_DECISIONS, required=True)
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level)
    setup_observability(settings, "darwin-cli")
    engine = create_db_engine(str(settings.database_url))
    factory = sessionmaker(engine)
    try:
        if args.command == "resume":
            outcome = resume_research(factory, args.run_id, args.decision)
        else:
            signal_id = args.signal_id
            if signal_id is None:
                with factory() as session:
                    signal_id = latest_signal_id(session, args.signal_type)
                if signal_id is None:
                    print("No canonical signal found. Generate one with the /demo page first.")
                    return 1
            llm = FakeLLMProvider(
                cast(FakeMode, args.fake_mode), cast(CritiqueMode, args.critique_mode)
            )
            outcome = run_research(factory, signal_id, llm, HashingEmbeddingProvider())
        print_outcome(factory, outcome)
    except (
        SignalNotFoundError,
        ResearchRunNotFoundError,
        ResumeNotAllowedError,
        InvalidDecisionError,
    ) as error:
        print(f"Cannot continue: {error}")
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
