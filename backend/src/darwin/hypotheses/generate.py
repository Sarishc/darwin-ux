"""Developer CLI: generate one hypothesis for a stored signal.

    python -m darwin.hypotheses.generate                    # latest canonical signal
    python -m darwin.hypotheses.generate --type rage_click  # latest of one type
    python -m darwin.hypotheses.generate --signal-id <uuid>
    ... [--fake-mode grounded|failure|...] [--show-evidence]

    make hypothesis-generate [SIGNAL_ID=<uuid>] [SIGNAL_TYPE=rage_click]

Only the FakeLLMProvider exists (no real provider is configured in Step 9),
so the output is a templated hypothesis that proves the pipeline, not a
model's judgment. Prints to stdout for the developer; logs stay content-free.
"""

import argparse
import uuid
from collections.abc import Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import BehaviorSignal, Hypothesis, HypothesisRun
from darwin.llm.fake import FAKE_MODES, FakeLLMProvider, FakeMode
from darwin.logging_config import configure_logging
from darwin.memory.embeddings import HashingEmbeddingProvider

from .queries import UnsupportedSignalError
from .service import SignalNotFoundError, generate_hypothesis


def latest_signal_id(session: Session, signal_type: str | None) -> uuid.UUID | None:
    statement = select(BehaviorSignal.signal_id).where(BehaviorSignal.superseded_at.is_(None))
    if signal_type:
        statement = statement.where(BehaviorSignal.signal_type == signal_type)
    return session.scalar(
        statement.order_by(BehaviorSignal.detected_at.desc(), BehaviorSignal.signal_id).limit(1)
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate one grounded hypothesis (fake LLM).")
    parser.add_argument("--signal-id", type=uuid.UUID)
    parser.add_argument("--type", dest="signal_type")
    parser.add_argument("--fake-mode", choices=FAKE_MODES, default="grounded")
    parser.add_argument("--show-evidence", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(str(settings.database_url))
    factory = sessionmaker(engine)
    try:
        signal_id = args.signal_id
        if signal_id is None:
            with factory() as session:
                signal_id = latest_signal_id(session, args.signal_type)
            if signal_id is None:
                print("No canonical signal found. Generate one with the /demo page first.")
                return 1
        try:
            outcome = generate_hypothesis(
                factory,
                signal_id,
                FakeLLMProvider(cast(FakeMode, args.fake_mode)),
                HashingEmbeddingProvider(),
            )
        except (SignalNotFoundError, UnsupportedSignalError) as error:
            print(f"Cannot generate: {error}")
            return 1
        bundle = outcome.bundle
        print(f"signal     {signal_id}  ({bundle.signal.signal_type})")
        print(f"query      {bundle.retrieval_query}")
        print(f"run        {outcome.run_id}  status={outcome.status}  error={outcome.error_type}")
        print(f"evidence   {len(bundle.excerpts)} excerpt(s), {bundle.retrieved} retrieved")
        for e in bundle.excerpts:
            print(f"  {e.rank}. {e.score:.3f}  {e.chunk_id}  {e.source_key}  §{e.section[:60]}")
            if args.show_evidence:
                print(f"       {' '.join(e.text.split())[:160]}…")
        with factory() as session:
            run = session.get(HypothesisRun, outcome.run_id)
            assert run is not None
            print(
                f"usage      in={run.input_tokens} out={run.output_tokens} "
                f"latency_ms={run.latency_ms}"
            )
            if outcome.hypothesis_id is not None:
                h = session.get(Hypothesis, outcome.hypothesis_id)
                assert h is not None
                print(f"hypothesis {h.id}  [{h.confidence}, component={h.affected_component}]")
                print(f"  statement  {h.statement}")
                print(f"  rationale  {h.rationale}")
                for ref in h.evidence_references:
                    print(f"  cites      {ref['chunk_id']}  {ref['source_key']}  §{ref['section']}")
                for item in h.limitations:
                    print(f"  limitation {item}")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
