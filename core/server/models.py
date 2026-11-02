"""Data models for Khunkhaar Web Platform."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ProjectRecord:
    id: str
    name: str
    description: str = ""
    root_path: str = ""
    github_url: str = ""
    tags: list[str] = field(default_factory=list)
    target_urls: list[str] = field(default_factory=list)
    context_links: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)


@dataclass
class ChatMessage:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    project_id: str = ""
    sender: str = "user"  # "user" | "agent" | "system"
    text: str = ""
    timestamp: str = field(default_factory=_now)


@dataclass
class ChecklistItem:
    id: str
    project_id: str
    category: str  # code, end-user, cyber, rbac
    title: str
    description: str = ""
    role: str = "guest"
    severity: str = "medium"  # critical, high, medium, low
    selected: bool = True
    status: str = "pending"  # pending, running, passed, failed
    comment: str = ""
