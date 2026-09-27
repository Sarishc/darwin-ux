"""Bounded research workflow (Step 10): a LangGraph graph over existing services.

    signal -> deterministic query -> retrieval -> sufficiency heuristic
      -> [at most one deterministic refinement + second retrieval]
      -> Step 9 grounded hypothesis -> one critique call
      -> accept | human review (resume from CLI) | reject | stop

LangGraph only orchestrates. Retrieval (Step 8), the EvidenceBundle, the
hypothesis request, strict schema and grounding checks (Step 9) are called,
never reimplemented. There is no tool registry, no Jev, no Muse, no mutation.
"""
