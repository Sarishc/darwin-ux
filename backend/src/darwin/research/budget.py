"""Hard limits for one research run. Enforced in code, mirrored by database CHECKs.

A run may be given a *tighter* budget (tests, evaluation) but never a looser
one: ResearchBudget rejects any value above the hard cap.

Why these numbers:
- 2 retrieval attempts: the deterministic first query, plus one targeted
  refinement. A third query from the same heuristics would only repeat itself;
  open-ended query reformulation is future Research-agent work.
- 1 hypothesis generation: retrying generation on the same evidence is a
  provider-retry question, not research, and would hide extra model calls.
- 1 critique call: the critique reviews; it never loops with the generator.
- 2 LLM calls in total: exactly generation + critique.
- 12 graph steps: the longest legal path is 11 (refinement + human review +
  resume); one step of headroom. LangGraph's recursion limit is only a backstop.
"""

from dataclasses import dataclass

HARD_MAX_RETRIEVAL_ATTEMPTS = 2
HARD_MAX_REFINEMENTS = 1
HARD_MAX_HYPOTHESIS_GENERATIONS = 1
HARD_MAX_CRITIQUE_CALLS = 1
HARD_MAX_LLM_CALLS = 2
HARD_MAX_GRAPH_STEPS = 12


class BudgetError(ValueError):
    """A budget above a hard cap (or below 1) was requested."""


@dataclass(frozen=True)
class ResearchBudget:
    max_retrieval_attempts: int = HARD_MAX_RETRIEVAL_ATTEMPTS
    max_llm_calls: int = HARD_MAX_LLM_CALLS
    max_graph_steps: int = HARD_MAX_GRAPH_STEPS
    max_refinements: int = HARD_MAX_REFINEMENTS
    max_hypothesis_generations: int = HARD_MAX_HYPOTHESIS_GENERATIONS
    max_critique_calls: int = HARD_MAX_CRITIQUE_CALLS

    def __post_init__(self) -> None:
        caps = {
            "max_retrieval_attempts": HARD_MAX_RETRIEVAL_ATTEMPTS,
            "max_llm_calls": HARD_MAX_LLM_CALLS,
            "max_graph_steps": HARD_MAX_GRAPH_STEPS,
            "max_refinements": HARD_MAX_REFINEMENTS,
            "max_hypothesis_generations": HARD_MAX_HYPOTHESIS_GENERATIONS,
            "max_critique_calls": HARD_MAX_CRITIQUE_CALLS,
        }
        for name, cap in caps.items():
            value = getattr(self, name)
            lowest = 0 if name == "max_refinements" else 1
            if not lowest <= value <= cap:
                raise BudgetError(f"{name} must be between {lowest} and {cap}, got {value}")

    def as_dict(self) -> dict[str, int]:
        return {
            "max_retrieval_attempts": self.max_retrieval_attempts,
            "max_llm_calls": self.max_llm_calls,
            "max_graph_steps": self.max_graph_steps,
            "max_refinements": self.max_refinements,
            "max_hypothesis_generations": self.max_hypothesis_generations,
            "max_critique_calls": self.max_critique_calls,
        }
