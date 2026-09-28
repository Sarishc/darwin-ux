"""Controlled experiments (Step 14): Generation 0 vs one sandbox-approved candidate.

Deterministic and auditable end to end:

  CandidateEvaluationRun (pass, re-checked) -> draft Experiment (human CLI)
  -> explicit human start (start gate) -> stable-hash assignment (serving)
  -> exposure telemetry (only after a successful render) -> outcome telemetry
  -> frequentist analysis -> immutable ExperimentAnalysis -> HUMAN REVIEW

Nothing here declares a winner, promotes a candidate, deploys, or replaces
Generation 0. No model (LLM, Jev, Muse) chooses traffic or interprets results.
"""
