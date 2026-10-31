# Synthetic application — documented invariants

This file is the only specification the QA engine receives. The application below is disposable
and intentionally contains violations of these rules. The defect list is not given to the agents.

RULE R1: A user may only read their own requests; another user's request must be refused.
RULE R2: Repeating an identical request submission must not create duplicate records.
RULE R3: A request may only be approved after it is submitted; approving a draft must fail.
RULE R4: A request amount must be positive and its title non-empty.
RULE R5: Only one open clock-in per user; clock-out requires an open clock-in.
RULE R6: Daily reports use the UTC day boundary.
RULE R7: A failed approval must not leave an audit record behind.
RULE R8: The dashboard must not require horizontal scrolling at 390px.
