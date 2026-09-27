"""Persisting the research run after every node: the run summary + one step row.

Called by the node wrapper in graph.py, one short transaction per node, so a
crash leaves an accurate "last completed step" behind. Only compact,
code-generated metadata is written (see darwin.db.models.research).
"""

import uuid
from typing import Any

from sqlalchemy import func

from darwin.db.models import Hypothesis, ResearchRun, ResearchStep
from darwin.hypotheses.service import SessionFactory

from .state import ResearchState

# research outcome -> hypothesis lifecycle
_HYPOTHESIS_STATUS = {"succeeded": "accepted", "rejected": "rejected"}


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def record_step(
    session_factory: SessionFactory,
    state: ResearchState,
    node: str,
    outcome: str,
    detail: dict[str, Any],
) -> None:
    with session_factory() as session:
        run = session.get(ResearchRun, uuid.UUID(state["research_run_id"]), with_for_update=True)
        if run is None:
            raise LookupError("research run disappeared")
        session.add(
            ResearchStep(
                run_id=run.id,
                sequence=state["steps"],
                node=node,
                outcome=outcome[:64],
                detail=detail,
            )
        )
        run.current_node = node
        run.steps = state["steps"]
        run.queries = list(state.get("queries", []))
        run.retrieval_attempts = state.get("retrieval_attempts", 0)
        run.llm_calls = state.get("llm_calls", 0)
        run.input_tokens = state.get("input_tokens", 0)
        run.output_tokens = state.get("output_tokens", 0)
        run.calls_without_usage = state.get("calls_without_usage", 0)
        run.hypothesis_run_id = _uuid(state.get("hypothesis_run_id"))
        run.hypothesis_id = _uuid(state.get("hypothesis_id"))
        run.critique = state.get("critique")
        run.review_reason = state.get("review_reason")
        run.updated_at = func.now()
        if node == "human_review":
            run.status = "waiting_for_human"
            run.stop_reason = state.get("review_reason") or "human_review"
        elif node == "finalize":
            status = state.get("final_status") or "failed"
            run.status = status
            run.stop_reason = state.get("stop_reason") or "internal_no_outcome"
            run.completed_at = func.now()
            if run.hypothesis_id is not None and status in _HYPOTHESIS_STATUS:
                hypothesis = session.get(Hypothesis, run.hypothesis_id, with_for_update=True)
                if hypothesis is not None:
                    hypothesis.status = _HYPOTHESIS_STATUS[status]
        session.commit()
