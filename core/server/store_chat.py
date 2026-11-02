"""Chat and Checklist SQLite storage for Khunkhaar."""
from __future__ import annotations

import sqlite3
from typing import Callable

from core.server.models import ChatMessage, ChecklistItem


def add_chat_message(get_conn: Callable[[], sqlite3.Connection], msg: ChatMessage) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO web_chat (id, project_id, sender, text, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (msg.id, msg.project_id, msg.sender, msg.text, msg.timestamp)
        )


def get_chat_messages(get_conn: Callable[[], sqlite3.Connection], project_id: str) -> list[ChatMessage]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM web_chat WHERE project_id = ? ORDER BY timestamp ASC",
            (project_id,)
        ).fetchall()
        return [
            ChatMessage(id=r["id"], project_id=r["project_id"],
                        sender=r["sender"], text=r["text"], timestamp=r["timestamp"])
            for r in rows
        ]


def save_checklist(get_conn: Callable[[], sqlite3.Connection], project_id: str, items: list[ChecklistItem]) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM web_checklists WHERE project_id = ?", (project_id,))
        for it in items:
            conn.execute(
                """INSERT INTO web_checklists
                   (id, project_id, category, title, description, role, severity, selected, status, comment)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (it.id, it.project_id, it.category, it.title, it.description,
                 it.role, it.severity, 1 if it.selected else 0, it.status, it.comment)
            )


def get_checklist(get_conn: Callable[[], sqlite3.Connection], project_id: str) -> list[ChecklistItem]:
    with get_conn() as conn:
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
