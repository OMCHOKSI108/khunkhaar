"""Optional, read-only bridges to external systems.

Nothing in this package may change an external system. The GitLab adapter is the first member:
it can issue GET requests only, to a host the operator has allowlisted twice, and every failure
becomes a structured `unavailable` result that local analysis can ignore.
"""
