"""Execution planning: review first, execute second.

A plan is a stored, digest-protected document. It names every action, its expected cost and
whether the operator must approve it before it may run. `khunkhaar qa` executes the actions of a
stored plan and nothing else; an edited plan is refused as drifted rather than executed.
"""
