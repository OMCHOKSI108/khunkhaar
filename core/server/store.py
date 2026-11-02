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
            id TEXT,
            project_id TEXT,
            category TEXT,
            title TEXT,
            description TEXT,
            role TEXT,
            severity TEXT,
            selected INTEGER,
            status TEXT,
            comment TEXT,
            PRIMARY KEY (project_id, id)
        );
        CREATE TABLE IF NOT EXISTS web_checklist_specs (
            id TEXT,
            project_id TEXT,
            spec TEXT,
            PRIMARY KEY (project_id, id)
        );
        CREATE TABLE IF NOT EXISTS web_project_auth (
            project_id TEXT PRIMARY KEY,
            config TEXT
        );
        """)
        # Safe migration if table existed without description
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(web_projects)").fetchall()]
        if "description" not in cols:
            conn.execute("ALTER TABLE web_projects ADD COLUMN description TEXT")
        # Safe migration: `id` used to be the sole PRIMARY KEY, which collides once a second
        # project generates the same chk-0001.. ids. Rebuild onto a composite key if needed.
        _migrate_to_composite_key(conn, "web_checklists",
                                  "id, project_id, category, title, description, role, severity, selected, status, comment")
        _migrate_to_composite_key(conn, "web_checklist_specs", "id, project_id, spec")


def _migrate_to_composite_key(conn: sqlite3.Connection, table: str, columns: str) -> None:
    pk_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall() if r["pk"]]
    if pk_cols == ["project_id", "id"] or pk_cols == ["id", "project_id"]:
        return  # already migrated
    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
    # Recreate with a composite key, then copy every row across (no row needs dropping: the
    # old single-column PK only collided *across* projects, never within one).
    if table == "web_checklists":
        conn.execute("""CREATE TABLE web_checklists (
            id TEXT, project_id TEXT, category TEXT, title TEXT, description TEXT,
            role TEXT, severity TEXT, selected INTEGER, status TEXT, comment TEXT,
            PRIMARY KEY (project_id, id))""")
    else:
        conn.execute("""CREATE TABLE web_checklist_specs (
            id TEXT, project_id TEXT, spec TEXT, PRIMARY KEY (project_id, id))""")
    conn.execute(f"INSERT OR REPLACE INTO {table} ({columns}) SELECT {columns} FROM {table}_old")
    conn.execute(f"DROP TABLE {table}_old")


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
        conn.execute("DELETE FROM web_checklist_specs WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM web_project_auth WHERE project_id = ?", (project_id,))


from core.server import store_chat


def add_chat_message(msg: ChatMessage) -> None:
    store_chat.add_chat_message(_get_conn, msg)


def get_chat_messages(project_id: str) -> list[ChatMessage]:
    return store_chat.get_chat_messages(_get_conn, project_id)


def save_checklist(project_id: str, items: list[ChecklistItem]) -> None:
    store_chat.save_checklist(_get_conn, project_id, items)


def get_checklist(project_id: str) -> list[ChecklistItem]:
    return store_chat.get_checklist(_get_conn, project_id)


def save_checklist_specs(project_id: str, specs: dict[str, dict]) -> None:
    """Persist the structured test spec behind each checklist item (how to execute it)."""
    with _get_conn() as conn:
        conn.execute("DELETE FROM web_checklist_specs WHERE project_id = ?", (project_id,))
        conn.executemany(
            "INSERT INTO web_checklist_specs (id, project_id, spec) VALUES (?, ?, ?)",
            [(item_id, project_id, json.dumps(spec)) for item_id, spec in specs.items()],
        )


def get_checklist_spec(project_id: str, item_id: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT spec FROM web_checklist_specs WHERE project_id = ? AND id = ?",
            (project_id, item_id),
        ).fetchone()
        return json.loads(row["spec"]) if row else None


def save_project_auth_config(project_id: str, config: dict) -> None:
    """Per-project auth-mint schema (table/column/role names + which .env key names hold the
    secrets). Never the secret values themselves - those are re-read from the project's own
    .env at mint time, see core/server/project_env.py."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO web_project_auth (project_id, config) VALUES (?, ?)",
            (project_id, json.dumps(config)),
        )


def get_project_auth_config(project_id: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT config FROM web_project_auth WHERE project_id = ?", (project_id,)
        ).fetchone()
        return json.loads(row["config"]) if row else None



