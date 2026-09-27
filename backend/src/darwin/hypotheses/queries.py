"""Signal -> retrieval query: deterministic, one mapping per supported signal type.

No LLM writes these queries (that is the Research agent's job, later). Each
query combines three kinds of words, chosen so the Step 8 hashing embeddings
can find the evidence a hypothesis needs:

1. the signal's own vocabulary ("rage click", "error burst"), which matches
   the detector definition that explains *why* the signal fired;
2. the affected component id when the signal names one — the Generation 0
   UI Spec chunks list component ids, so this finds the component's spec;
3. generic UX concepts a hypothesis about that signal is usually about
   (feedback/response for repeated clicks; validation/error display for
   repeated errors). These are domain knowledge, not tuned to one case.
"""

from dataclasses import dataclass

from darwin.db.models import BehaviorSignal
from darwin.signals import detectors

# 5: the Step 8 golden eval found 0.923 of expected sources in the top 5
# (standard chunking), and 5 excerpts of <= 2400 chars keep the evidence
# section around 12k characters — enough context, bounded cost.
DEFAULT_TOP_K = 5
MAX_TOP_K = 8


class UnsupportedSignalError(ValueError):
    """No query mapping exists for this signal type (a code change adds one)."""


@dataclass(frozen=True)
class RetrievalPlan:
    query: str
    top_k: int


def signal_component(signal: BehaviorSignal) -> str | None:
    """The signal's component if it is a safe identifier; free text is never used."""
    value = signal.evidence.get("component")
    if isinstance(value, str) and detectors.COMPONENT_PATTERN.fullmatch(value):
        return value
    return None


def build_retrieval_plan(signal: BehaviorSignal, top_k: int = DEFAULT_TOP_K) -> RetrievalPlan:
    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")
    if signal.signal_type == detectors.RAGE_CLICK:
        component = signal_component(signal) or "button"
        query = (
            f"rage click repeated clicks on {component} button: component feedback, "
            "delayed response and friction in the Generation 0 UI Spec"
        )
    elif signal.signal_type == detectors.ERROR_BURST:
        query = (
            "error burst repeated form errors: signup form validation, error display "
            "and error messages in the Generation 0 UI Spec"
        )
    else:
        raise UnsupportedSignalError(f"no retrieval mapping for signal type {signal.signal_type!r}")
    return RetrievalPlan(query=query, top_k=top_k)
