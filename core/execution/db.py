"""Read-only database access for verification.

Two backends: SQLite files that live inside the KHUNKHAAR tree (the synthetic application) and
the Phase 1 PostgreSQL adapter. Neither can write. A finding that claims "the row was persisted"
must be backed by a read here, not by an HTTP response alone.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from core.shared import paths


class DatabaseReadError(RuntimeError):
    pass


class SqliteReader:
    def __init__(self, path: str | Path):
        resolved = Path(path).expanduser().resolve()
        root = paths.kh_root().resolve()
        if root not in resolved.parents and resolved != root:
            raise DatabaseReadError(f"refusing to read a database outside the KHUNKHAAR tree: {resolved}")
        if not resolved.is_file():
            raise DatabaseReadError(f"database file not found: {resolved}")
        self.path = resolved

    def query(self, sql: str, params: tuple = (), limit: int = 200) -> list[dict[str, Any]]:
        if not sql.lstrip().lower().startswith(("select", "with", "pragma")):
            raise DatabaseReadError("only read-only statements are allowed")
        uri = f"file:{self.path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, params).fetchmany(limit)
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        rows = self.query(sql, params, limit=1)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def tables(self) -> list[str]:
        return [row["name"] for row in self.query(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


class PostgresReader:
    def __init__(self, profile_name: str | None = None):
        from core.database.adapters import get_adapter, profile_by_name
        self.adapter = get_adapter(profile_by_name(profile_name))

    def query(self, sql: str, params: tuple = (), limit: int = 200) -> list[dict[str, Any]]:
        return self.adapter.fetch_rows(sql, params, max_rows=limit)

    def scalar(self, sql: str, params: tuple = ()) -> Any:
        rows = self.query(sql, params, limit=1)
        return next(iter(rows[0].values())) if rows else None
