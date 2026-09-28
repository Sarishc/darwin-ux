"""Developer CLI: generate one candidate UI Spec from a proceed decision.

    python -m darwin.mutations.cli [--decision-run-id <uuid>] [--generator fixture|llm|muse]
                                   [--fixture-mode auto] [--llm-mode first_enum] [--show-request]

    make mutation-generate [DECISION_RUN_ID=<uuid>] [GENERATOR=fixture] [FIXTURE_MODE=...]

Without an id, the latest proceed decision is used. A successful candidate is
also checked with the frontend's real Zod schema (npm run validate-spec) and
the result printed; nothing is written to frontend/src/ui-spec/.
GENERATOR=muse stops with a clear message: no documented Muse interface exists.
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
from darwin.db.models import DecisionRun, UISpecVersion
from darwin.llm.fake import MUTATION_MODES, FakeLLMProvider, MutationMode
from darwin.logging_config import configure_logging
from darwin.observability import setup_observability

from .fixture import FIXTURE_MODES, FixtureMode, FixtureMutationGenerator
from .frontend import FrontendValidatorUnavailableError, validate_with_frontend
from .llm import LLMMutationGenerator
from .muse import MuseAdapter, MuseNotConfiguredError
from .port import MutationGenerator
from .request import MutationInputError
from .service import generate_candidate
from .surface import index_targets

GENERATOR_CHOICES = ("fixture", "llm", "muse")


def make_generator(
    name: str, fixture_mode: FixtureMode = "auto", llm_mode: MutationMode = "first_enum"
) -> MutationGenerator:
    if name == "fixture":
        return FixtureMutationGenerator(fixture_mode)
    if name == "llm":  # only the FakeLLMProvider exists (OPEN_QUESTIONS N1)
        return LLMMutationGenerator(FakeLLMProvider(mutation_mode=llm_mode))
    if name == "muse":
        return MuseAdapter()  # raises MuseNotConfiguredError
    raise ValueError(f"unknown generator {name!r}")


def latest_proceed_decision(session: Session) -> uuid.UUID | None:
    return session.scalar(
        select(DecisionRun.id)
        .where(DecisionRun.decision == "proceed", DecisionRun.status == "decided")
        .order_by(DecisionRun.created_at.desc(), DecisionRun.id)
        .limit(1)
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate one candidate UI Spec.")
    parser.add_argument("--decision-run-id", type=uuid.UUID)
    parser.add_argument("--source-spec-id", type=uuid.UUID)
    parser.add_argument("--generator", choices=GENERATOR_CHOICES, default="fixture")
    parser.add_argument("--fixture-mode", choices=FIXTURE_MODES, default="auto")
    parser.add_argument("--llm-mode", choices=MUTATION_MODES, default="first_enum")
    parser.add_argument("--show-request", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level)
    setup_observability(settings, "darwin-cli")
    try:
        generator = make_generator(
            args.generator, cast(FixtureMode, args.fixture_mode), cast(MutationMode, args.llm_mode)
        )
    except MuseNotConfiguredError as error:
        print(f"Cannot generate: {error}")
        return 1
    engine = create_db_engine(str(settings.database_url))
    factory = sessionmaker(engine)
    try:
        decision_id = args.decision_run_id
        if decision_id is None:
            with factory() as session:
                decision_id = latest_proceed_decision(session)
            if decision_id is None:
                print("No proceed decision. Run `make decision-run` first.")
                return 1
        try:
            outcome = generate_candidate(factory, decision_id, generator, args.source_spec_id)
        except MutationInputError as error:
            print(f"Not eligible ({error.code}): {error}")
            return 1
        with factory() as session:
            source = session.get(UISpecVersion, outcome.source_spec_id)
            candidate = (
                session.get(UISpecVersion, outcome.candidate_spec_id)
                if outcome.candidate_spec_id
                else None
            )
            print(f"decision run   {outcome.decision_run_id}")
            print(f"mutation run   {outcome.mutation_run_id}")
            print(f"generator      {outcome.generator}  ({outcome.generator_version})")
            if source is not None:
                print(
                    f"source spec    {source.id}  generation {source.generation}  "
                    f"sha256={source.content_hash[:16]}…"
                )
            print(
                f"status         {outcome.status}"
                + (f"  ({outcome.error_type})" if outcome.error_type else "")
            )
            for change in outcome.changes:
                before = _current(
                    source.spec if source else {}, change.component_id, change.property
                )
                target = f"{change.component_id}.{change.property}"
                print(f"change         {target}: {before!r} -> {change.value!r}")
            if candidate is not None:
                print(
                    f"candidate      {candidate.id}  status {candidate.status}  "
                    f"candidate for generation {candidate.candidate_for_generation}  "
                    f"sha256={candidate.content_hash[:16]}…"
                )
                try:
                    [verdict] = validate_with_frontend([candidate.spec])
                    result = "valid" if verdict.ok else "INVALID: " + "; ".join(verdict.issues)
                    print(f"frontend Zod   {result}")
                except FrontendValidatorUnavailableError as error:
                    print(f"frontend Zod   not run ({error})")
            else:
                print("candidate      none")
        if args.show_request and outcome.request is not None:
            print(json.dumps(outcome.request.model_dump(mode="json"), indent=1))
    finally:
        engine.dispose()
    return 0


def _current(spec: dict[str, object], component_id: str, prop: str) -> object:
    target = index_targets(spec).get(component_id) if spec else None
    return target.node.get(prop) if target else None


if __name__ == "__main__":
    raise SystemExit(main())
