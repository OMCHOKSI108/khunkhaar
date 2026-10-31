"""Module H - the private, read-only local dashboard.

Nothing in this package starts a QA execution, imports the orchestrator or writes to a project.
It reads the metadata store and indexed evidence, renders Jinja templates with local assets only,
and binds to loopback unless the operator explicitly overrides that on the command line.
"""

__all__ = ["app", "auth", "data"]
