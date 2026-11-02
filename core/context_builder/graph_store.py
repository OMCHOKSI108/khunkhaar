"""Code Relationship Graph Store backed by SQLite."""
from __future__ import annotations

import sqlite3
from typing import Any

from core.context_builder.models import CodeRelationship, CodeSymbol
from core.shared import paths


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(paths.metadata_db(), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_graph_tables() -> None:
    with _get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS code_symbols (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, symbol_name TEXT NOT NULL,
            symbol_type TEXT NOT NULL, file_path TEXT NOT NULL, start_line INTEGER,
            end_line INTEGER, signature TEXT, docstring TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sym_proj_name ON code_symbols(project_id, symbol_name);

        CREATE TABLE IF NOT EXISTS code_relationships (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_symbol TEXT NOT NULL,
            target_symbol TEXT NOT NULL, relationship_type TEXT NOT NULL, file_path TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_rel_proj_src ON code_relationships(project_id, source_symbol);
        CREATE INDEX IF NOT EXISTS idx_rel_proj_tgt ON code_relationships(project_id, target_symbol);
        """)


def save_symbols(project_id: str, symbols: list[CodeSymbol]) -> None:
    init_graph_tables()
    with _get_conn() as conn:
        conn.execute("DELETE FROM code_symbols WHERE project_id = ?", (project_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO code_symbols (id, project_id, symbol_name, symbol_type, "
            "file_path, start_line, end_line, signature, docstring) VALUES (?,?,?,?,?,?,?,?,?)",
            [(s.id, s.project_id, s.symbol_name, s.symbol_type, s.file_path, s.start_line,
              s.end_line, s.signature, s.docstring) for s in symbols],
        )


def save_relationships(project_id: str, rels: list[CodeRelationship]) -> None:
    init_graph_tables()
    with _get_conn() as conn:
        conn.execute("DELETE FROM code_relationships WHERE project_id = ?", (project_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO code_relationships (id, project_id, source_symbol, "
            "target_symbol, relationship_type, file_path) VALUES (?,?,?,?,?,?)",
            [(r.id, r.project_id, r.source_symbol, r.target_symbol, r.relationship_type, r.file_path) for r in rels],
        )


def find_symbol(project_id: str, name: str) -> list[dict[str, Any]]:
    init_graph_tables()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM code_symbols WHERE project_id = ? AND symbol_name LIKE ? LIMIT 20",
            (project_id, f"%{name}%"),
        ).fetchall()
        return [dict(r) for r in rows]


def get_symbol_references(project_id: str, name: str) -> list[dict[str, Any]]:
    init_graph_tables()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM code_relationships WHERE project_id = ? AND "
            "(source_symbol LIKE ? OR target_symbol LIKE ?) LIMIT 50",
            (project_id, f"%{name}%", f"%{name}%"),
        ).fetchall()
        return [dict(r) for r in rows]


def trace_execution_flow(project_id: str, start_symbol: str, max_depth: int = 3) -> list[dict[str, Any]]:
    init_graph_tables()
    visited: set[str] = set()
    queue = [(start_symbol, 0)]
    flow: list[dict[str, Any]] = []

    with _get_conn() as conn:
        while queue:
            curr, depth = queue.pop(0)
            if curr in visited or depth >= max_depth:
                continue
            visited.add(curr)
            rows = conn.execute(
                "SELECT * FROM code_relationships WHERE project_id = ? AND source_symbol = ? LIMIT 10",
                (project_id, curr),
            ).fetchall()
            for r in rows:
                item = dict(r)
                item["depth"] = depth
                flow.append(item)
                queue.append((item["target_symbol"], depth + 1))
    return flow
