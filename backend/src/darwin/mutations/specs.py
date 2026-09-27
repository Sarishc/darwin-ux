"""UI Spec versions: the idempotent Generation 0 import and baseline lookups.

    python -m darwin.mutations.specs import     (make ui-spec-import)
    python -m darwin.mutations.specs show       (make ui-spec-show — read-only summary)

Import reads exactly one allowlisted file, frontend/src/ui-spec/generation-0.json
(never arbitrary paths), checks its shape, hashes its canonical JSON and:
  - inserts it as the Generation 0 baseline if absent;
  - does nothing if the same content is already the baseline;
  - refuses (BaselineConflictError) if a different Generation 0 exists.
The committed file is only read.
"""

import argparse
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models import UISpecVersion
from darwin.hypotheses.evidence import GENERATION_ZERO_SPEC
from darwin.memory.corpus import REPO_ROOT, read_allowlisted

from .apply import MAX_SPEC_BYTES, canonical_json, content_hash
from .surface import iter_targets

BASELINE_SOURCE = f"repo:{GENERATION_ZERO_SPEC.source_key}"


class BaselineConflictError(RuntimeError):
    pass


class SpecShapeError(ValueError):
    pass


def check_shape(spec: object) -> dict[str, object]:
    """The minimum the backend relies on; the full check is the frontend's Zod schema."""
    if not isinstance(spec, dict) or spec.get("version") != 1:
        raise SpecShapeError("not a version 1 UI Spec")
    if not isinstance(spec.get("generation"), int):
        raise SpecShapeError("generation missing")
    page = spec.get("page")
    if not isinstance(page, dict) or not isinstance(page.get("sections"), list):
        raise SpecShapeError("page.sections missing")
    ids = [t.component_id for t in iter_targets(spec)]
    if len(ids) != len(set(ids)):
        raise SpecShapeError("duplicate ids")
    if len(canonical_json(spec).encode()) > MAX_SPEC_BYTES:
        raise SpecShapeError("spec too large")
    return spec


def load_generation_zero() -> dict[str, object]:
    return check_shape(json.loads(read_allowlisted(GENERATION_ZERO_SPEC, REPO_ROOT)))


@dataclass(frozen=True)
class ImportOutcome:
    spec_id: uuid.UUID
    status: Literal["created", "unchanged"]
    content_hash: str


def import_generation_zero(session: Session) -> ImportOutcome:
    spec = load_generation_zero()
    if spec["generation"] != 0:
        raise SpecShapeError("generation-0.json must declare generation 0")
    page_id = str(spec["page"]["id"])  # type: ignore[index]
    digest = content_hash(spec)
    existing = session.scalar(
        select(UISpecVersion).where(UISpecVersion.page_id == page_id, UISpecVersion.generation == 0)
    )
    if existing is not None:
        if existing.content_hash != digest:
            raise BaselineConflictError(
                "a different Generation 0 baseline is already stored; baselines are immutable"
            )
        return ImportOutcome(existing.id, "unchanged", digest)
    row = UISpecVersion(
        id=uuid.uuid4(),
        page_id=page_id,
        status="baseline",
        generation=0,
        candidate_for_generation=None,
        parent_id=None,
        schema_version=int(spec["version"]),  # type: ignore[call-overload]
        spec=spec,
        content_hash=digest,
        source=BASELINE_SOURCE,
    )
    session.add(row)
    session.commit()
    return ImportOutcome(row.id, "created", digest)


def current_baseline(session: Session, page_id: str) -> UISpecVersion | None:
    """The highest-generation baseline of a page: the only valid mutation source."""
    return session.scalar(
        select(UISpecVersion)
        .where(UISpecVersion.page_id == page_id, UISpecVersion.status == "baseline")
        .order_by(UISpecVersion.generation.desc())
        .limit(1)
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UI Spec versions (import / show).")
    parser.add_argument("command", choices=("import", "show"))
    args = parser.parse_args(argv)
    engine = create_db_engine(str(Settings().database_url))
    try:
        with sessionmaker(engine)() as session:
            if args.command == "import":
                try:
                    outcome = import_generation_zero(session)
                except (BaselineConflictError, SpecShapeError) as error:
                    print(f"Import refused: {error}")
                    return 1
                digest = outcome.content_hash[:16]
                print(f"generation 0  {outcome.status}  {outcome.spec_id}  sha256={digest}…")
                return 0
            rows = session.scalars(select(UISpecVersion).order_by(UISpecVersion.created_at)).all()
            for row in rows:
                label = (
                    f"generation {row.generation}"
                    if row.status == "baseline"
                    else f"candidate for {row.candidate_for_generation}"
                )
                print(
                    f"{row.id}  {row.page_id}  {row.status:<9} {label:<18} "
                    f"sha256={row.content_hash[:12]}  parent={row.parent_id}  source={row.source}"
                )
            if not rows:
                print("No UI Spec versions. Run `make ui-spec-import`.")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
