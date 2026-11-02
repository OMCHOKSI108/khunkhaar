"""SQLite Persistence and Audit Logging for Sandbox Executions."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from core.sandbox.models import SandboxJob, SandboxResult
from core.server.store import _get_conn


def init_sandbox_tables() -> None:
    with _get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sandbox_executions (
            id TEXT PRIMARY KEY, assessment_id TEXT, execution_profile TEXT NOT NULL,
            language TEXT NOT NULL, status TEXT NOT NULL, exit_code INTEGER,
            timeout_seconds INTEGER, execution_time_ms INTEGER, cpu_seconds REAL,
            peak_memory_mb REAL, network_policy TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sandbox_artifacts (
            id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, name TEXT NOT NULL,
            storage_path TEXT NOT NULL, file_size INTEGER, mime_type TEXT,
            checksum TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sandbox_audit_logs (
            id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, event_type TEXT NOT NULL,
            actor TEXT NOT NULL, policy_decision TEXT, metadata TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_sb_exec_id ON sandbox_executions(id);
        CREATE INDEX IF NOT EXISTS idx_sb_art_exec ON sandbox_artifacts(execution_id);
        """)


def record_execution_start(job: SandboxJob) -> None:
    init_sandbox_tables()
    with _get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO sandbox_executions
               (id, assessment_id, execution_profile, language, status, timeout_seconds, network_policy)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (job.id, job.target_id, job.execution_profile, job.language, job.status.value,
             job.timeout_seconds, job.network_policy.value),
        )
    record_audit_log(job.id, "JOB_STARTED", "agent", "ALLOWED", {"profile": job.execution_profile})


def record_execution_finish(result: SandboxResult) -> None:
    init_sandbox_tables()
    with _get_conn() as conn:
        conn.execute(
            """UPDATE sandbox_executions SET status = ?, exit_code = ?, execution_time_ms = ?,
               cpu_seconds = ?, peak_memory_mb = ?, completed_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (result.status.value, result.exit_code, result.execution_time_ms,
             result.resource_usage.cpu_seconds, result.resource_usage.peak_memory_mb, result.execution_id),
        )
        for art in result.artifacts:
            art_id = f"art-{uuid.uuid4().hex[:10]}"
            conn.execute(
                """INSERT INTO sandbox_artifacts (id, execution_id, name, storage_path, file_size, mime_type, checksum)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (art_id, result.execution_id, art["name"], art["storage_path"], art["file_size"],
                 art["mime_type"], art["checksum"]),
            )
    record_audit_log(result.execution_id, "JOB_FINISHED", "runner", result.status.value,
                     {"exit_code": result.exit_code, "artifacts_count": len(result.artifacts)})


def record_audit_log(execution_id: str, event_type: str, actor: str, policy_decision: str, metadata: dict) -> None:
    init_sandbox_tables()
    with _get_conn() as conn:
        log_id = f"aud-{uuid.uuid4().hex[:10]}"
        conn.execute(
            """INSERT INTO sandbox_audit_logs (id, execution_id, event_type, actor, policy_decision, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (log_id, execution_id, event_type, actor, policy_decision, json.dumps(metadata)),
        )


def get_execution_record(execution_id: str) -> dict[str, Any] | None:
    init_sandbox_tables()
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM sandbox_executions WHERE id = ?", (execution_id,)).fetchone()
        return dict(row) if row else None


def get_execution_artifacts(execution_id: str) -> list[dict[str, Any]]:
    init_sandbox_tables()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sandbox_artifacts WHERE execution_id = ? ORDER BY created_at ASC",
            (execution_id,),
        ).fetchall()
        return [dict(r) for r in rows]

