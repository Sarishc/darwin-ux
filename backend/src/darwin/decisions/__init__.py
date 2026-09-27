"""The decision layer (Step 11): one bounded gate after a finished research run.

    ResearchRun + Hypothesis + Critique -> DecisionRequest (decision_request.v1)
      -> Decider (rules | fake | llm | jev), called once
      -> strict validation -> fail-closed policy -> DecisionRun

`proceed` means only: the research artifact is eligible to enter a FUTURE
mutation-generation stage. Nothing here creates, approves or deploys a
mutation or starts an experiment, and no decider can add routes, raise
budgets, call tools or bypass a human gate: a decider chooses one of three
words and DarwinUX decides what that word is allowed to mean.
"""
