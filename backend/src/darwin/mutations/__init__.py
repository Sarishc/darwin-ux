"""Candidate mutation generation (Step 12): AI proposes DATA, never code.

    DecisionRun(proceed) -> provenance re-check -> MutationRequest (mutation_request.v1)
      -> MutationGenerator (fixture | llm | muse), called once
      -> strict MutationSpec -> surface validation -> pure in-memory apply
      -> protected-field diff -> CandidateUISpec (immutable, content-addressed) + MutationRun

The mutation surface (surface.py) is the safety boundary: a generator can only
name an existing component, one allowlisted property of that component's
type, and a value from that property's closed enum or bounded plain text.
Nothing here writes files, renders, deploys, runs experiments or promotes a
candidate; candidates live in the database only.
"""
