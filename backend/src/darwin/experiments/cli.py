"""Experiment CLI — the ONLY way to create, start, pause, stop, complete or analyze.

    python -m darwin.experiments.cli create --key <key> --allocation-bp 1000 \\
        --primary rage_click_session_rate --guardrails form_error_session_rate,... \\
        [--evaluation-run-id <uuid>] [--min-sample 100] [--traffic-source simulated]
    python -m darwin.experiments.cli start    --experiment-id <uuid> --confirm <key>
    python -m darwin.experiments.cli pause    --experiment-id <uuid>
    python -m darwin.experiments.cli stop     --experiment-id <uuid> --reason human_decision
    python -m darwin.experiments.cli complete --experiment-id <uuid>
    python -m darwin.experiments.cli analyze  --experiment-id <uuid>
    python -m darwin.experiments.cli show     --experiment-id <uuid>
    python -m darwin.experiments.cli assign   --experiment-id <uuid> --session-id <uuid> ...

The human boundary: `start` requires the operator to retype the experiment key
(--confirm). There is no HTTP route and no model-callable path for any of this.
Output: ids, hashes, allocation, metric names, counts, rates, intervals —
never spec content, events, session lists or Product Memory.
"""

import argparse
import json
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import CandidateEvaluationRun, Experiment, ExperimentAnalysis
from darwin.logging_config import configure_logging

from .assignment import assign
from .service import (
    ExperimentNotFoundError,
    SessionFactory,
    analyze_experiment,
    complete_experiment,
    create_experiment,
    pause_experiment,
    start_experiment,
    stop_experiment,
)
from .vocabulary import METRIC_NAMES, STOP_REASONS, TRAFFIC_SOURCES


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DarwinUX experiments (human commands only).")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a DRAFT experiment (never starts it)")
    create.add_argument("--key", required=True)
    create.add_argument("--evaluation-run-id", type=uuid.UUID)
    create.add_argument("--allocation-bp", type=int, required=True)
    create.add_argument("--primary", required=True, choices=METRIC_NAMES)
    create.add_argument("--guardrails", required=True, help="comma-separated metric names")
    create.add_argument("--min-sample", type=int, default=100)
    create.add_argument("--traffic-source", default="simulated", choices=TRAFFIC_SOURCES)
    for name in ("start", "pause", "stop", "complete", "analyze", "show", "assign"):
        command = sub.add_parser(name)
        command.add_argument("--experiment-id", type=uuid.UUID, required=True)
        if name == "start":
            command.add_argument("--confirm", required=True, help="retype the experiment key")
        if name == "stop":
            command.add_argument("--reason", required=True, choices=STOP_REASONS)
        if name == "assign":
            command.add_argument("--session-id", type=uuid.UUID, nargs="+", required=True)
    return parser


def _latest_pass(factory: SessionFactory) -> uuid.UUID | None:
    with factory() as session:
        return session.scalar(
            select(CandidateEvaluationRun.id)
            .where(CandidateEvaluationRun.recommendation == "pass")
            .order_by(CandidateEvaluationRun.created_at.desc(), CandidateEvaluationRun.id)
            .limit(1)
        )


def _print_experiment(experiment: Experiment) -> None:
    print(f"experiment      {experiment.id}  ({experiment.experiment_key})")
    print(f"status          {experiment.status}   traffic_source={experiment.traffic_source}")
    print(f"evaluation      {experiment.candidate_evaluation_run_id}  (Step 13 pass)")
    print(f"control         {experiment.control_spec_hash}  (Generation 0)")
    print(f"candidate       {experiment.candidate_spec_hash}")
    print(
        f"allocation      control {experiment.control_allocation_bp} bp / "
        f"candidate {experiment.candidate_allocation_bp} bp (of 10000)"
    )
    print(f"primary         {experiment.primary_metric}")
    print(f"guardrails      {', '.join(experiment.guardrail_metrics)}")
    print(f"min sample      {experiment.minimum_sample_per_variant} exposed sessions per variant")


def _print_report(report: dict[str, object]) -> None:
    print(json.dumps(report, indent=2, sort_keys=True))


def run(argv: Sequence[str] | None, factory: SessionFactory) -> int:
    args = _parser().parse_args(argv)
    if args.command == "create":
        evaluation_id = args.evaluation_run_id or _latest_pass(factory)
        if evaluation_id is None:
            print("No passing candidate evaluation. Run `make candidate-eval` first.")
            return 1
        outcome = create_experiment(
            factory,
            experiment_key=args.key,
            candidate_evaluation_run_id=evaluation_id,
            candidate_allocation_bp=args.allocation_bp,
            primary_metric=args.primary,
            guardrail_metrics=[g.strip() for g in args.guardrails.split(",") if g.strip()],
            minimum_sample_per_variant=args.min_sample,
            traffic_source=args.traffic_source,
        )
        if not outcome.created:
            detail = f" ({outcome.detail})" if outcome.detail else ""
            print(f"REFUSED: {', '.join(outcome.reasons)}{detail}. Nothing was created.")
            return 1
        with factory() as session:
            experiment = session.get(Experiment, outcome.experiment_id)
            assert experiment is not None
            _print_experiment(experiment)
        print("note            draft only; start it with `make experiment-start` (human command)")
        return 0

    try:
        if args.command == "start":
            with factory() as session:
                experiment = session.get(Experiment, args.experiment_id)
                if experiment is None:
                    raise ExperimentNotFoundError(str(args.experiment_id))
                if args.confirm != experiment.experiment_key:
                    print("REFUSED: --confirm must be the experiment key. Nothing was started.")
                    return 1
            result = start_experiment(factory, args.experiment_id)
        elif args.command == "pause":
            result = pause_experiment(factory, args.experiment_id)
        elif args.command == "stop":
            result = stop_experiment(factory, args.experiment_id, args.reason)
        elif args.command == "complete":
            result = complete_experiment(factory, args.experiment_id)
        elif args.command == "analyze":
            analysis = analyze_experiment(factory, args.experiment_id)
            print(f"analysis        {analysis.analysis_id}  ({analysis.status})")
            print(
                f"assessment      {analysis.assessment}  reasons={','.join(analysis.reason_codes)}"
            )
            print(f"report_hash     {analysis.report_hash}")
            _print_report(analysis.report)
            print("note            evidence for HUMAN review; nothing was promoted or changed")
            return 0
        elif args.command == "show":
            with factory() as session:
                experiment = session.get(Experiment, args.experiment_id)
                if experiment is None:
                    raise ExperimentNotFoundError(str(args.experiment_id))
                _print_experiment(experiment)
                latest = session.scalar(
                    select(ExperimentAnalysis)
                    .where(ExperimentAnalysis.experiment_id == experiment.id)
                    .order_by(ExperimentAnalysis.created_at.desc(), ExperimentAnalysis.id)
                    .limit(1)
                )
                if latest is not None:
                    print(f"latest analysis {latest.id}  {latest.assessment}")
            return 0
        else:  # assign: read-only preview of the deterministic assignment
            with factory() as session:
                experiment = session.get(Experiment, args.experiment_id)
                if experiment is None:
                    raise ExperimentNotFoundError(str(args.experiment_id))
                for sid in args.session_id:
                    variant = assign(
                        experiment.experiment_key, sid, experiment.candidate_allocation_bp
                    )
                    print(f"{sid}  {variant}")
            return 0
    except ExperimentNotFoundError as error:
        print(f"No such experiment: {error}")
        return 1
    if not result.changed:
        print(f"REFUSED: {', '.join(result.reasons)}. Status stays {result.status}.")
        return 1
    print(f"experiment      {result.experiment_id}  -> {result.status}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(str(settings.database_url))
    try:
        return run(argv, sessionmaker(engine))
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
