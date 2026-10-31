"""Schema migrations: ordering, idempotency, and the Phase 1/2 data that must survive."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from core.metadata import migrations, store
from core.shared import paths

PHASE3_TABLES = ("git_analyses", "impact_nodes", "impact_edges", "impact_reports",
                 "regression_tests", "regression_runs", "qa_plans", "plan_actions",
                 "authorizations", "knowledge_items", "monitoring_schedules",
                 "monitoring_runs", "alerts")


def test_upgrade_from_scratch_reaches_latest(kh_root):
    result = migrations.upgrade()
    assert result["before"] == 0
    assert result["after"] == migrations.LATEST_VERSION
    assert [int(part.split(":")[0]) for part in result["applied"]] == list(
        range(1, migrations.LATEST_VERSION + 1))
    assert result["dry_run"] is False


def test_upgrade_is_idempotent(kh_root):
    migrations.upgrade()
    second = migrations.upgrade()
    assert second["applied"] == []
    assert second["before"] == second["after"] == migrations.LATEST_VERSION


def test_dry_run_does_not_write(kh_root):
    result = migrations.upgrade(dry_run=True)
    assert result["before"] == 0 and result["after"] == 0
    assert len(result["applied"]) == migrations.LATEST_VERSION
    with closing(_raw_conn()) as conn:
        assert migrations.current_version(conn) == 0


def test_phase1_and_phase2_tables_survive(kh_root):
    store.init()
    store.upsert_project("alpha-00000000", "alpha", str(kh_root / "alpha"),
                         str(kh_root / "profile.json"), ["python"], ["fastapi"], "web-app")
    store.record_tool_health("git", "ok", "2.56.0", None)
    with store.connect() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "qa_runs", "evidence_refs", "findings", "missions"} <= tables
    for table in PHASE3_TABLES:
        assert table in tables
    assert store.get_project("alpha-00000000")["name"] == "alpha"


def test_evidence_index_columns_added(kh_root):
    store.init()
    with store.connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence_refs)")}
    assert {"sha256", "retention_class"} <= columns


def test_partial_upgrade_converges_on_retry(kh_root):
    """A version row is only written after its statement set ran, so a retry finishes the job."""
    conn = _raw_conn()
    try:
        migrations.apply(conn, target=1)
        conn.commit()
        # Simulate a crash after V3's DDL but before its version row was committed.
        conn.executescript(migrations.V3)
        conn.commit()
    finally:
        conn.close()

    conn = _raw_conn()
    try:
        applied = [m.version for m in migrations.apply(conn, target=migrations.LATEST_VERSION)]
        conn.commit()
    finally:
        conn.close()
    assert applied == list(range(2, migrations.LATEST_VERSION + 1))
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) n FROM sqlite_master WHERE name='git_analyses'"
                            ).fetchone()["n"] == 1
        assert [r[0] for r in conn.execute(
            "SELECT DISTINCT version FROM schema_version ORDER BY version")] == list(
            range(1, migrations.LATEST_VERSION + 1))


def test_uncommitted_apply_is_not_recorded(kh_root):
    """The caller owns the commit; DDL that commits implicitly cannot fake a recorded version."""
    conn = _raw_conn()
    try:
        migrations.apply(conn, target=1)
    finally:
        conn.close()
    conn = _raw_conn()
    try:
        assert migrations.current_version(conn) == 0
        assert [m.version for m in migrations.apply(conn, target=1)] == [1]
    finally:
        conn.close()


def _raw_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(paths.metadata_db(), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def test_knowledge_scope_is_enforced_by_the_schema(kh_root):
    """Project knowledge cannot be stored without a project, global knowledge cannot borrow one."""
    store.init()
    with store.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO knowledge_items (item_id, scope, project_id, kind, title, "
                     "created_at, updated_at) VALUES ('k1','project',NULL,'note','t','now','now')")
    with store.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO knowledge_items (item_id, scope, project_id, kind, title, "
                     "created_at, updated_at) VALUES ('k2','global','alpha-00000000','note','t',"
                     "'now','now')")


def test_status_reports_pending(kh_root):
    assert migrations.status()["pending"] == list(range(1, migrations.LATEST_VERSION + 1))
    migrations.upgrade()
    status = migrations.status()
    assert status["pending"] == [] and status["current"] == status["latest"]
