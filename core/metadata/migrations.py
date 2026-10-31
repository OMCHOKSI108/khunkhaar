"""Versioned SQLite migrations.

Phase 1 shipped one idempotent script and a single `SCHEMA_VERSION` constant. Phase 3 adds enough
tables that an ordered, recorded migration path is required: `migrate()` applies every unapplied
version in its own transaction, and each version is recorded in `schema_version`, so a database
written by an older install upgrades in place and a partially applied upgrade can be diagnosed.

Column additions are guarded because SQLite has no `ADD COLUMN IF NOT EXISTS`.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Callable, NamedTuple

from core.shared import paths


class Migration(NamedTuple):
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _script(sql: str) -> Callable[[sqlite3.Connection], None]:
    def runner(conn: sqlite3.Connection) -> None:
        conn.executescript(sql)
    return runner


V1 = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS projects (
    project_id     TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    root_path      TEXT NOT NULL UNIQUE,
    profile_path   TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    languages      TEXT,
    frameworks     TEXT,
    project_type   TEXT
);

CREATE TABLE IF NOT EXISTS discovery_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL,
    profile_path   TEXT NOT NULL,
    summary        TEXT
);

CREATE TABLE IF NOT EXISTS tool_health (
    tool           TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    version        TEXT,
    detail         TEXT,
    checked_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_runs (
    run_id         TEXT PRIMARY KEY,
    project_id     TEXT REFERENCES projects(project_id) ON DELETE SET NULL,
    kind           TEXT,
    status         TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS report_refs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT,
    run_id         TEXT,
    path           TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_refs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT,
    run_id         TEXT,
    kind           TEXT,
    path           TEXT NOT NULL,
    bytes          INTEGER,
    created_at     TEXT NOT NULL
);
"""

def _v2(conn: sqlite3.Connection) -> None:
    """Phase 2 tables are defined once, in the findings store; the migration delegates to it so
    the two definitions can never drift apart."""
    from core.findings import store as findings_store
    findings_store.ensure(conn)
    conn.executescript("""
CREATE INDEX IF NOT EXISTS findings_project_status ON findings (project_id, status);
CREATE INDEX IF NOT EXISTS finding_events_finding ON finding_events (finding_id);
CREATE INDEX IF NOT EXISTS missions_project_kind ON missions (project_id, kind);
""")

V3 = """
CREATE TABLE IF NOT EXISTS git_analyses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT NOT NULL,
    run_id         TEXT,
    kind           TEXT NOT NULL,
    base_ref       TEXT,
    head_ref       TEXT,
    commits        INTEGER,
    files_changed  INTEGER,
    insertions     INTEGER,
    deletions       INTEGER,
    renames        INTEGER,
    removed        INTEGER,
    conflicted     INTEGER,
    worktree_clean INTEGER,
    index_untouched INTEGER,
    artifact       TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS impact_nodes (
    node_id        TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    type           TEXT NOT NULL,
    name           TEXT NOT NULL,
    path           TEXT,
    meta           TEXT,
    confidence     REAL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS impact_edges (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT NOT NULL,
    src            TEXT NOT NULL,
    dst            TEXT NOT NULL,
    relation       TEXT NOT NULL,
    confidence     REAL,
    evidence       TEXT,
    updated_at     TEXT NOT NULL,
    UNIQUE (project_id, src, dst, relation)
);

CREATE TABLE IF NOT EXISTS impact_reports (
    report_id      TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    analysis_id    INTEGER REFERENCES git_analyses(id) ON DELETE SET NULL,
    base_ref       TEXT,
    head_ref       TEXT,
    risk_score     REAL,
    risk_factors   TEXT,
    artifact       TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS regression_tests (
    test_id        TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    finding_id     TEXT NOT NULL,
    category       TEXT NOT NULL,
    method         TEXT NOT NULL,
    objective      TEXT NOT NULL,
    preconditions  TEXT,
    role           TEXT,
    steps          TEXT,
    expected       TEXT,
    actual         TEXT,
    dependencies   TEXT,
    reliability    TEXT NOT NULL DEFAULT 'generated',
    confidence     REAL,
    generated_path TEXT,
    validated_path TEXT,
    last_success_at TEXT,
    last_failure_at TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS regression_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id        TEXT NOT NULL REFERENCES regression_tests(test_id) ON DELETE CASCADE,
    project_id     TEXT NOT NULL,
    run_id         TEXT,
    base_ref       TEXT,
    head_ref       TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    result         TEXT NOT NULL,
    duration_ms    INTEGER,
    note           TEXT,
    evidence       TEXT
);

CREATE TABLE IF NOT EXISTS qa_plans (
    plan_id        TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    run_id         TEXT,
    mode           TEXT NOT NULL,
    digest         TEXT NOT NULL,
    status         TEXT NOT NULL,
    approved_at    TEXT,
    approved_by    TEXT,
    summary        TEXT,
    artifact       TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_actions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id        TEXT NOT NULL REFERENCES qa_plans(plan_id) ON DELETE CASCADE,
    project_id     TEXT NOT NULL,
    agent          TEXT NOT NULL,
    kind           TEXT NOT NULL,
    feature        TEXT,
    target         TEXT,
    rationale      TEXT,
    est_requests   INTEGER,
    est_steps      INTEGER,
    est_sessions   INTEGER,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    selected_tests TEXT
);

CREATE TABLE IF NOT EXISTS authorizations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT NOT NULL,
    target         TEXT NOT NULL,
    mode           TEXT NOT NULL,
    granted_at     TEXT NOT NULL,
    granted_by     TEXT NOT NULL,
    expires_at     TEXT,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    item_id        TEXT PRIMARY KEY,
    scope          TEXT NOT NULL CHECK (scope IN ('global', 'project')),
    project_id     TEXT,
    kind           TEXT NOT NULL,
    title          TEXT NOT NULL,
    body_path      TEXT,
    tags           TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    CHECK ((scope = 'project' AND project_id IS NOT NULL)
        OR (scope = 'global' AND project_id IS NULL))
);

CREATE TABLE IF NOT EXISTS monitoring_schedules (
    schedule_id    TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    name           TEXT NOT NULL,
    expression     TEXT NOT NULL,
    mode           TEXT NOT NULL,
    target         TEXT,
    checks         TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    paused_at      TEXT,
    last_due_at    TEXT,
    last_run_at    TEXT,
    next_due_at    TEXT,
    artifact       TEXT
);

CREATE TABLE IF NOT EXISTS monitoring_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id    TEXT NOT NULL REFERENCES monitoring_schedules(schedule_id) ON DELETE CASCADE,
    project_id     TEXT NOT NULL,
    run_id         TEXT,
    due_at         TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    note           TEXT,
    evidence       TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    severity       TEXT,
    finding_id     TEXT,
    message        TEXT NOT NULL,
    channel        TEXT NOT NULL,
    status         TEXT NOT NULL,
    detail         TEXT,
    created_at     TEXT NOT NULL,
    delivered_at   TEXT
);

CREATE INDEX IF NOT EXISTS git_analyses_project ON git_analyses (project_id, created_at);
CREATE INDEX IF NOT EXISTS impact_edges_project ON impact_edges (project_id, src, dst);
CREATE INDEX IF NOT EXISTS regression_runs_test ON regression_runs (test_id, started_at);
CREATE INDEX IF NOT EXISTS knowledge_scope ON knowledge_items (scope, project_id, kind);
CREATE INDEX IF NOT EXISTS monitoring_runs_schedule ON monitoring_runs (schedule_id, due_at);
CREATE INDEX IF NOT EXISTS alerts_project ON alerts (project_id, created_at);
"""


def _add_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # nosec B608
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")  # nosec B608


def _v4(conn: sqlite3.Connection) -> None:
    """Evidence gains the fields the dashboard and retention policy need."""
    _add_column(conn, "evidence_refs", "sha256", "TEXT")
    _add_column(conn, "evidence_refs", "retention_class", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS evidence_refs_class ON evidence_refs (retention_class)")


def _v5(conn: sqlite3.Connection) -> None:
    """Dependency graph nodes and edges keep their provenance.

    `explicit` means a parser saw the relationship in the source; `inferred` means a heuristic
    (a naming convention, a Phase 2 route scan) proposed it. Storing provenance next to the edge
    keeps a rebuilt graph honest without having to consult the JSON copy.
    """
    _add_column(conn, "impact_nodes", "origin", "TEXT NOT NULL DEFAULT 'explicit'")
    _add_column(conn, "impact_edges", "origin", "TEXT NOT NULL DEFAULT 'explicit'")
    conn.execute("CREATE INDEX IF NOT EXISTS impact_nodes_project ON impact_nodes (project_id, type)")


def _v6(conn: sqlite3.Connection) -> None:
    """Executions record their own resource peak; LLM usage is a column so 'none' is a fact.

    Peak RSS is sampled from the OS by the orchestrator. It is never estimated from a model or
    copied from a neighbouring run.
    """
    _add_column(conn, "qa_runs", "peak_rss_kb", "INTEGER")
    _add_column(conn, "qa_runs", "llm_usage", "TEXT")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "phase1-platform", _script(V1)),
    Migration(2, "phase2-intelligence", _v2),
    Migration(3, "phase3-git-impact-regression-monitoring", _script(V3)),
    Migration(4, "evidence-index-extension", _v4),
    Migration(5, "impact-graph-provenance", _v5),
    Migration(6, "execution-resource-metrics", _v6),
)

LATEST_VERSION = MIGRATIONS[-1].version


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["v"]) if row and row["v"] is not None else 0


def apply(conn: sqlite3.Connection, target: int | None = None) -> list[Migration]:
    """Apply unapplied migrations in version order.

    Every statement is `CREATE ... IF NOT EXISTS` or a guarded column add, so a migration that
    dies halfway (DDL commits implicitly) can simply be retried: the version row is only written
    once the whole statement set has run, and re-running converges. Nothing here rewrites data.

    The caller owns the commit - `upgrade()` commits, and an apply whose transaction is discarded
    leaves the DDL behind but no recorded version, which the next apply repairs.
    """
    wanted = target if target is not None else LATEST_VERSION
    done: list[Migration] = []
    version = current_version(conn)
    for migration in MIGRATIONS:
        if migration.version > wanted or migration.version <= version:
            continue
        migration.apply(conn)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (migration.version,))
        version = migration.version
        done.append(migration)
    return done


def pending(target: int | None = None) -> list[dict[str, object]]:
    wanted = target if target is not None else LATEST_VERSION
    with closing(_connect()) as conn:
        version = current_version(conn)
        return [{"version": m.version, "name": m.name} for m in MIGRATIONS
                if version < m.version <= wanted]


def status() -> dict[str, object]:
    with closing(_connect()) as conn:
        version = current_version(conn)
        try:
            applied = [row[0] for row in conn.execute(
                "SELECT DISTINCT version FROM schema_version ORDER BY version")]
        except sqlite3.OperationalError:  # a database that has never been initialised
            applied = []
    return {"applied": applied, "current": version, "latest": LATEST_VERSION,
            "pending": [m.version for m in MIGRATIONS if m.version > version]}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(paths.metadata_db(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def upgrade(*, dry_run: bool = False) -> dict[str, object]:
    paths.metadata_db().parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        before = current_version(conn)
        applied: list[str] = []
        if not dry_run:
            conn.execute("PRAGMA journal_mode=WAL")
            for migration in apply(conn):
                applied.append(f"{migration.version}:{migration.name}")
            conn.commit()
        else:
            applied = [f"{m.version}:{m.name}" for m in MIGRATIONS if m.version > before]
        return {"before": before, "after": current_version(conn), "applied": applied,
                "dry_run": dry_run}
    finally:
        conn.close()