"""Centralized event logger and streaming telemetry hub."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

GLOBAL_LOG_QUEUES: list[asyncio.Queue] = []
PROJECT_LOG_QUEUES: dict[str, list[asyncio.Queue]] = {}
LOG_HISTORY: list[dict[str, Any]] = []
MAX_HISTORY = 300


def record_log(project_id: str, message: str, level: str = "INFO") -> dict[str, Any]:
    """Record log event in history and dispatch to all active SSE subscribers."""
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3],
        "project_id": project_id,
        "level": level.upper(),
        "message": message,
    }
    LOG_HISTORY.append(entry)
    if len(LOG_HISTORY) > MAX_HISTORY:
        LOG_HISTORY.pop(0)

    # Dispatch to project-specific queues
    for q in PROJECT_LOG_QUEUES.get(project_id, []):
        q.put_nowait(entry)

    # Dispatch to global central queues
    for gq in GLOBAL_LOG_QUEUES:
        gq.put_nowait(entry)

    return entry


def get_recent_logs(limit: int = 100) -> list[dict[str, Any]]:
    """Retrieve the most recent log history."""
    return LOG_HISTORY[-limit:]


def register_project_listener(project_id: str, queue: asyncio.Queue) -> None:
    PROJECT_LOG_QUEUES.setdefault(project_id, []).append(queue)


def unregister_project_listener(project_id: str, queue: asyncio.Queue) -> None:
    if project_id in PROJECT_LOG_QUEUES and queue in PROJECT_LOG_QUEUES[project_id]:
        PROJECT_LOG_QUEUES[project_id].remove(queue)


def register_global_listener(queue: asyncio.Queue) -> None:
    GLOBAL_LOG_QUEUES.append(queue)


def unregister_global_listener(queue: asyncio.Queue) -> None:
    if queue in GLOBAL_LOG_QUEUES:
        GLOBAL_LOG_QUEUES.remove(queue)
