"""Mission model: the unit of work handed from the orchestrator to an agent."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

MODES = {"observe": 0, "controlled": 1, "adversarial": 2}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Mission:
    agent: str
    title: str
    kind: str
    project_id: str
    description: str = ""
    category: str = "functional"
    severity: str = "medium"
    invariant_id: str | None = None
    priority: int = 3
    mode_required: str = "controlled"
    # Only a planner sets this, and only for a GET-only probe kind; the generator never does.
    parallel_safe: bool = False
    payload: dict[str, Any] = field(default_factory=dict)
    mission_id: str = field(default_factory=lambda: f"M-{uuid.uuid4().hex[:10]}")
    status: str = "pending"
    created_at: str = field(default_factory=_now)

    def allowed_in(self, mode: str) -> bool:
        return MODES.get(mode, 9) >= MODES.get(self.mode_required, 9)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id, "project_id": self.project_id, "agent": self.agent,
            "title": self.title, "kind": self.kind, "description": self.description,
            "category": self.category, "severity": self.severity,
            "invariant_id": self.invariant_id, "priority": self.priority,
            "mode_required": self.mode_required, "parallel_safe": self.parallel_safe,
            "payload": self.payload, "status": self.status, "created_at": self.created_at,
        }
