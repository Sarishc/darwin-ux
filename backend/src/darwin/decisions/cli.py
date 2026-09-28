"""Developer CLI for the decision gate.

    python -m darwin.decisions.cli [--run-id <research run>] [--decider rules|fake|llm|jev]
                                   [--fake-mode proceed] [--llm-mode cautious] [--show-request]

    make decision-run [RUN_ID=<uuid>] [DECIDER=rules|fake|fake_jev|llm|jev]

Without --run-id, the latest research run that ended succeeded/rejected is used.
`fake_jev` is accepted as an alias of `fake` (the test double); it is recorded
as "fake", never as Jev. DECIDER=jev needs DARWIN_JEV_API_KEY; without it the
command stops with a clear message and records nothing — there is no fallback.
"""

import argparse
import json
import uuid
from collections.abc import Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import ResearchRun
from darwin.llm.fake import DECISION_MODES, DecisionMode, FakeLLMProvider
from darwin.logging_config import configure_logging
from darwin.observability import setup_observability

from .fake import FAKE_DECIDER_MODES, FakeDecider, FakeDeciderMode
from .jev import JevAdapter, JevNotConfiguredError
from .llm import LLMDecider
from .port import Decider
from .request import ELIGIBLE_RESEARCH_STATUSES, DecisionInputError
from .rules import RulesDecider
from .service import decide_research_run

DECIDER_CHOICES = ("rules", "fake", "fake_jev", "llm", "jev")


def make_decider(
    name: str,
    settings: Settings,
    fake_mode: FakeDeciderMode = "proceed",
    llm_mode: DecisionMode = "cautious",
) -> Decider:
    if name == "rules":
        return RulesDecider()
    if name in ("fake", "fake_jev"):
        return FakeDecider(fake_mode)
    if name == "llm":  # only the FakeLLMProvider exists (OPEN_QUESTIONS N1)
        return LLMDecider(FakeLLMProvider(decision_mode=llm_mode))
    if name == "jev":
        key = settings.jev_api_key.get_secret_value() if settings.jev_api_key else None
        return JevAdapter(key, model=settings.jev_model)  # raises JevNotConfiguredError
    raise ValueError(f"unknown decider {name!r}")


def latest_decidable_run(session: Session) -> uuid.UUID | None:
    return session.scalar(
        select(ResearchRun.id)
        .where(ResearchRun.status.in_(ELIGIBLE_RESEARCH_STATUSES))
        .order_by(ResearchRun.completed_at.desc(), ResearchRun.id)
        .limit(1)
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decide one finished research run.")
    parser.add_argument("--run-id", type=uuid.UUID)
    parser.add_argument("--decider", choices=DECIDER_CHOICES, default="rules")
    parser.add_argument("--fake-mode", choices=FAKE_DECIDER_MODES, default="proceed")
    parser.add_argument("--llm-mode", choices=DECISION_MODES, default="cautious")
    parser.add_argument("--show-request", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level)
    setup_observability(settings, "darwin-cli")
    try:
        decider = make_decider(
            args.decider,
            settings,
            cast(FakeDeciderMode, args.fake_mode),
            cast(DecisionMode, args.llm_mode),
        )
    except JevNotConfiguredError as error:
        print(f"Cannot decide: {error}")
        return 1
    engine = create_db_engine(str(settings.database_url))
    factory = sessionmaker(engine)
    try:
        run_id = args.run_id
        if run_id is None:
            with factory() as session:
                run_id = latest_decidable_run(session)
            if run_id is None:
                print("No finished research run to decide. Run `make research-run` first.")
                return 1
        try:
            outcome = decide_research_run(factory, run_id, decider)
        except DecisionInputError as error:
            print(f"Not decidable ({error.code}): {error}")
            return 1
    finally:
        engine.dispose()

    print(f"research run   {outcome.research_run_id}")
    print(f"decision run   {outcome.decision_run_id}")
    print(f"decider        {outcome.decider}  ({outcome.decider_version})")
    print(f"request        {outcome.request_version}  sha256={outcome.request_hash[:16]}…")
    print(f"decision       {outcome.decision}   [status {outcome.status}]")
    if outcome.decider_decision and outcome.decider_decision != outcome.decision:
        print(f"decider said   {outcome.decider_decision} (overridden by policy)")
    print(f"confidence     {outcome.confidence}  (qualitative, uncalibrated)")
    print(f"reason codes   {', '.join(outcome.reason_codes)}")
    if outcome.error_type:
        print(f"error type     {outcome.error_type}")
    if outcome.decision == "proceed":
        print("note           proceed = eligible for a future mutation stage; nothing was created")
    if args.show_request:
        print(json.dumps(outcome.request.model_dump(mode="json"), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
