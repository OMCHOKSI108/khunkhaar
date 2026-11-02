"""Persistent SQLite storage for telemetry and test execution logs."""
from __future__ import annotations

import sqlite3
from typing import Any
from core.server import store

LOG_DB_INIT = False


def _init_log_table() -> None:
    global LOG_DB_INIT
    if not LOG_DB_INIT:
        with store._get_conn() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS web_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT,
                level TEXT,
                message TEXT,
                timestamp TEXT
            )
            """)
        LOG_DB_INIT = True


def save_log(project_id: str, level: str, message: str, timestamp: str) -> None:
    """Save log entry to SQLite."""
    _init_log_table()
    with store._get_conn() as conn:
        conn.execute(
            "INSERT INTO web_logs (project_id, level, message, timestamp) VALUES (?, ?, ?, ?)",
            (project_id, level, message, timestamp)
        )


def get_project_logs(project_id: str, limit: int = 150) -> list[dict[str, Any]]:
    """Retrieve historical logs for a specific project."""
    _init_log_table()
    with store._get_conn() as conn:
        rows = conn.execute(
            "SELECT project_id, level, message, timestamp FROM web_logs WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def get_all_logs(limit: int = 200) -> list[dict[str, Any]]:
    """Retrieve global logs for central /logs page."""
    _init_log_table()
    with store._get_conn() as conn:
        rows = conn.execute(
            "SELECT project_id, level, message, timestamp FROM web_logs ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
