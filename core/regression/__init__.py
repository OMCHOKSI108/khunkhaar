"""Module D: regression tests generated from independently confirmed findings.

A finding reaches this package only after the Phase 2 verifier reproduced it twice. The generation
step turns its recorded reproduction into a runnable pytest module; validation proves the module
fails while the defect is present and passes when the same observation is inverted; execution
history then records every later run against the project's own commits. A test that cannot be
validated is never reported as trusted, and an unavailable target is recorded as
`environment_unavailable`, never as an application defect.
"""
