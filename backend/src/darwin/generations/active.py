"""Which UI Spec version is the page's current generation?

    pointer present  -> the spec the active_generation pointer names (Generation 0,
                        a promoted generation, or one rolled back to)
    no pointer       -> the page's highest-generation BASELINE (pre-Step-15 behaviour:
                        Generation 0 imported from the repository)

Every consumer that needs "the current generation" — the mutation source, experiment
eligibility and control, variant serving, the /demo endpoint — uses this one function.
A candidate is never returned.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from darwin.db.models import ActiveGeneration, UISpecVersion


def active_pointer(session: Session, page_id: str) -> ActiveGeneration | None:
    return session.get(ActiveGeneration, page_id)


def active_spec(session: Session, page_id: str) -> UISpecVersion | None:
    pointer = active_pointer(session, page_id)
    if pointer is not None:
        return session.get(UISpecVersion, pointer.ui_spec_version_id)
    return session.scalar(
        select(UISpecVersion)
        .where(UISpecVersion.page_id == page_id, UISpecVersion.status == "baseline")
        .order_by(UISpecVersion.generation.desc())
        .limit(1)
    )
