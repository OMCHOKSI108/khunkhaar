"""SQLite storage for Khunkhaar Web Server projects and interactions."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from core.server.models import ChecklistItem, ChatMessage, ProjectRecord
from core.shared import paths

DB_PATH = paths.metadata_db()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS web_projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            root_path TEXT,
            github_url TEXT,
            tags TEXT,
            target_urls TEXT,
            context_links TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS web_chat (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            sender TEXT,
            text TEXT,
            timestamp TEXT
        );
        CREATE TABLE IF NOT EXISTS web_checklists (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            category TEXT,
            title TEXT,
            description TEXT,
            role TEXT,
            severity TEXT,
            selected INTEGER,
            status TEXT,
            comment TEXT
        );
        """)


def save_project(p: ProjectRecord) -> None:
    with _get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO web_projects
               (id, name, root_path, github_url, tags, target_urls, context_links, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (p.id, p.name, p.root_path, p.github_url,
             json.dumps(p.tags), json.dumps(p.target_urls),
             json.dumps(p.context_links), p.created_at)
        )


def get_projects() -> list[ProjectRecord]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM web_projects ORDER BY created_at DESC").fetchall()
        return [
            ProjectRecord(
                id=r["id"], name=r["name"], root_path=r["root_path"] or "",
                github_url=r["github_url"] or "", tags=json.loads(r["tags"] or "[]"),
                target_urls=json.loads(r["target_urls"] or "[]"),
                context_links=json.loads(r["context_links"] or "[]"),
                created_at=r["created_at"]
            )
            for r in rows
        ]


def get_project(project_id: str) -> ProjectRecord | None:
    with _get_conn() as conn:
        r = conn.execute("SELECT * FROM web_projects WHERE id = ?", (project_id,)).fetchone()
        if not r:
            return None
        return ProjectRecord(
            id=r["id"], name=r["name"], root_path=r["root_path"] or "",
            github_url=r["github_url"] or "", tags=json.loads(r["tags"] or "[]"),
            target_urls=json.loads(r["target_urls"] or "[]"),
            context_links=json.loads(r["context_links"] or "[]"),
            created_at=r["created_at"]
        )

