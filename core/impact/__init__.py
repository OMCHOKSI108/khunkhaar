"""Module C - dependency graph and change impact analysis.

The graph is built from the files a registered project contains, linked against the Phase 2
application map and journey store, and stored per project. Nothing here writes to an inspected
repository: extraction only reads tracked text files, and every persisted artifact lives under
KHUNKHAAR's own root.

An edge always points from the dependent to the dependency (`controller imports service`), so a
change is propagated against the edge direction: the impacted set is the reverse-reachable set of
whatever changed.
"""

from __future__ import annotations

__all__ = ["model", "extract", "resolve", "traverse", "risk", "graph"]
