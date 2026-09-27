"""Grounded hypothesis generation (Step 9): one bounded, validated LLM call.

    BehaviorSignal -> deterministic retrieval query -> Product Memory retrieval
      -> EvidenceBundle -> hypothesis.v1 request -> LLMProvider (one call)
      -> strict schema validation -> grounding checks -> HypothesisRun (+ Hypothesis)

No agent, no loop, no tools, no mutation. A Hypothesis describes a possible
problem; it never proposes code, UI Specs or experiments.
"""
