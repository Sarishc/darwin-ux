"""Human approval, generation promotion and rollback (Step 15).

    candidate      != generation   (a candidate is data under evaluation)
    analysis       != approval     (evidence, never authority)
    approval       != promotion    (a human decision at time A)
    promotion      =  atomic active-pointer move + immutable audit (time B, re-validated)
    rollback       =  pointer reversal to an earlier generation, never deletion

Only explicit human CLI commands approve, promote or roll back. No model,
decider, evaluator or analysis can.
"""
