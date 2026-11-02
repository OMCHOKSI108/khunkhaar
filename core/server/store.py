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
            description TEXT,
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
        # Safe migration if table existed without description
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(web_projects)").fetchall()]
        if "description" not in cols:
            conn.execute("ALTER TABLE web_projects ADD COLUMN description TEXT")


def save_project(p: ProjectRecord) -> None:
    with _get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO web_projects
               (id, name, description, root_path, github_url, tags, target_urls, context_links, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p.id, p.name, p.description, p.root_path, p.github_url,
             json.dumps(p.tags), json.dumps(p.target_urls),
             json.dumps(p.context_links), p.created_at)
        )


def update_project_context(project_id: str, description: str, target_urls: list[str], context_links: list[str]) -> None:
    with _get_conn() as conn:
        conn.execute(
            """UPDATE web_projects
               SET description = ?, target_urls = ?, context_links = ?
               WHERE id = ?""",
            (description, json.dumps(target_urls), json.dumps(context_links), project_id)
        )


def get_projects() -> list[ProjectRecord]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM web_projects ORDER BY created_at DESC").fetchall()
        return [
            ProjectRecord(
                id=r["id"], name=r["name"], description=r["description"] or "",
                root_path=r["root_path"] or "", github_url=r["github_url"] or "",
                tags=json.loads(r["tags"] or "[]"), target_urls=json.loads(r["target_urls"] or "[]"),
                context_links=json.loads(r["context_links"] or "[]"), created_at=r["created_at"]
            )
            for r in rows
        ]


def get_project(project_id: str) -> ProjectRecord | None:
    with _get_conn() as conn:
        r = conn.execute("SELECT * FROM web_projects WHERE id = ?", (project_id,)).fetchone()
        if not r:
            return None
        return ProjectRecord(
            id=r["id"], name=r["name"], description=r["description"] or "",
            root_path=r["root_path"] or "", github_url=r["github_url"] or "",
            tags=json.loads(r["tags"] or "[]"), target_urls=json.loads(r["target_urls"] or "[]"),
            context_links=json.loads(r["context_links"] or "[]"), created_at=r["created_at"]
        )


def delete_project(project_id: str) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM web_projects WHERE id = ?", (project_id,))
        conn.execute("DELETE FROM web_chat WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM web_checklists WHERE project_id = ?", (project_id,))


def add_chat_message(msg: ChatMessage) -> None:
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO web_chat (id, project_id, sender, text, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (msg.id, msg.project_id, msg.sender, msg.text, msg.timestamp)
        )


def get_chat_messages(project_id: str) -> list[ChatMessage]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM web_chat WHERE project_id = ? ORDER BY timestamp ASC",
            (project_id,)
        ).fetchall()
        return [
            ChatMessage(id=r["id"], project_id=r["project_id"],
                        sender=r["sender"], text=r["text"], timestamp=r["timestamp"])
            for r in rows
        ]


def save_checklist(project_id: str, items: list[ChecklistItem]) -> None:
    with _get_conn() as conn:
        conn.execute("DELETE FROM web_checklists WHERE project_id = ?", (project_id,))
        for it in items:
            conn.execute(
                """INSERT INTO web_checklists
                   (id, project_id, category, title, description, role, severity, selected, status, comment)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (it.id, it.project_id, it.category, it.title, it.description,
                 it.role, it.severity, 1 if it.selected else 0, it.status, it.comment)
            )


def get_checklist(project_id: str) -> list[ChecklistItem]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM web_checklists WHERE project_id = ?", (project_id,)
        ).fetchall()
        return [
            ChecklistItem(
                id=r["id"], project_id=r["project_id"], category=r["category"],
                title=r["title"], description=r["description"] or "",
                role=r["role"], severity=r["severity"],
                selected=bool(r["selected"]), status=r["status"], comment=r["comment"] or ""
            )
            for r in rows
        ]


