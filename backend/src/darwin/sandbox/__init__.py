"""Candidate sandbox evaluation (Step 13): is a SAFE candidate actually BETTER?

    CandidateUISpec -> provenance re-check -> frontend harness (real Zod schema,
    real registry, real SpecPage in jsdom; telemetry captured, never sent)
      -> schema | render | functional | accessibility | regression | ux_intent | performance
      -> deterministic policy (candidate_eval.v1) -> pass | human_review | reject
      -> immutable CandidateEvaluationRun

Categories are reported separately and never blended into a score. `pass`
means only: eligible for FUTURE human approval / experiment setup. Nothing
here deploys, promotes, allocates traffic or writes repository files.
"""
