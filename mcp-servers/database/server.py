"""KHUNKHAAR DB Context Tool — MCP server.

Helps a QA agent understand the real data model and verify business state, read-only.

Run: ~/KHUNKHAAR-QA/.venv/bin/python ~/KHUNKHAAR-QA/mcp-servers/database/server.py

Credentials come from the environment variable named by the profile. This server never accepts a
password as a tool argument, never reads one from a project file, and never returns one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from core.database import adapters as ad  # noqa: E402
from core.database import masking  # noqa: E402
from core.database.sql_guard import add_limit, extract_tables, validate_read_only  # noqa: E402
from core.shared import evidence as ev  # noqa: E402
from core.shared.config import current  # noqa: E402
from core.shared.toollog import get_logger, logged  # noqa: E402

logger = get_logger("mcp_db", "mcp_db.log")

server = MCPServer(
    name="khunkhaar-database",
    version="0.1.0",
    instructions=(
        "Read-only database context for QA. Only SELECT/WITH/EXPLAIN statements are accepted; DDL "
        "and DML are rejected before execution and the connecting role is read-only as well. "
        "Results are row-capped, timeout-capped, and sensitive columns are masked. "
        "Specify profile only when the operator has configured one - never supply a password."
    ),
)


def _profile(profile: str | None) -> dict[str, Any]:
    return ad.profile_by_name(profile)


def _adapter(profile: str | None):
    return ad.get_adapter(_profile(profile))


def _error(kind: str, message: str) -> dict[str, Any]:
    logger.info("DB ERROR kind=%s msg=%s", kind, message[:300])
    return {"ok": False, "error_kind": kind, "error": message, "rows": [], "columns": []}


def _guard(profile: dict[str, Any], sql: str) -> tuple[bool, str, str]:
    cfg = current()
    verdict = validate_read_only(sql)
    if not verdict.allowed:
        return False, verdict.reason, verdict.kind
    if cfg.read_only_database and profile.get("read_only") is False:
        return False, "global database.enforce_read_only is on but this profile declares write intent", "policy"
    return True, "ok", verdict.kind


@server.tool(description="Return the configured database profiles and the active read-only policy. Shows profile names, kinds, hosts, ports and database names only - never credentials.")
def db_profiles() -> dict[str, Any]:
    cfg = current()
    out = []
    for p in cfg.get("database", "profiles", []) or []:
        if not isinstance(p, dict):
            continue
        out.append({
            "name": p.get("name"),
            "kind": p.get("kind", "postgresql"),
            "host": p.get("host"),
            "port": p.get("port"),
            "database": p.get("database"),
            "user": p.get("user"),
            "read_only": p.get("read_only", True),
            "enabled": p.get("enabled", True),
            "credential_source": p.get("password_env") or p.get("uri_env") or "none",
            "redacted_columns": p.get("redacted_columns") or [],
        })
    return {
        "ok": True,
        "default_profile": cfg.get("database", "default_profile"),
        "enforce_read_only": cfg.read_only_database,
        "statement_timeout_ms": cfg.get("database", "statement_timeout_ms"),
        "max_rows": cfg.get("database", "max_rows"),
        "profiles": out,
    }


@server.tool(description="Connect to a profile and report server version, current database, current role and whether the session is read-only. Use this as the DB health check.")
@logged(logger)
def db_ping(profile: str | None = None) -> dict[str, Any]:
    try:
        adapter = _adapter(profile)
        info = adapter.server_info()
        return {"ok": True, "profile": _profile(profile).get("name"), "server": info}
    except (ad.DriverUnavailable, ad.NotSupported, KeyError, PermissionError) as exc:
        return _error("unavailable", str(exc))
    except Exception as exc:
        return _error("connection_failed", f"{type(exc).__name__}: {str(exc)[:300]}")


@server.tool(description="List databases the configured account can see (PostgreSQL).")
@logged(logger)
def db_list_databases(profile: str | None = None) -> dict[str, Any]:
    try:
        return {"ok": True, "databases": _adapter(profile).list_databases()}
    except ad.NotSupported as exc:
        return _error("not_supported", str(exc))
    except (ad.DriverUnavailable, KeyError, PermissionError) as exc:
        return _error("unavailable", str(exc))
    except Exception as exc:
        return _error("query_failed", f"{type(exc).__name__}: {str(exc)[:300]}")


@server.tool(description="List non-system schemas in the connected database.")
@logged(logger)
def db_list_schemas(profile: str | None = None) -> dict[str, Any]:
    try:
        return {"ok": True, "schemas": _adapter(profile).list_schemas()}
    except (ad.NotSupported, ad.DriverUnavailable, KeyError, PermissionError) as exc:
        return _error("not_supported" if isinstance(exc, ad.NotSupported) else "unavailable", str(exc))
    except Exception as exc:
        return _error("query_failed", f"{type(exc).__name__}: {str(exc)[:300]}")


@server.tool(description="List tables, partitioned tables and materialized views with size and approximate row counts.")
@logged(logger)
def db_list_tables(schema: str = "public", profile: str | None = None, database: str | None = None) -> dict[str, Any]:
    try:
        adapter = _adapter(profile)
        if hasattr(adapter, "list_tables"):
            tables = adapter.list_tables(schema, database=database) if adapter.kind == "mongodb" else adapter.list_tables(schema)
        else:
            return _error("not_supported", "adapter has no table listing")
        return {"ok": True, "schema": schema, "count": len(tables), "tables": tables}
    except (ad.NotSupported, ad.DriverUnavailable, KeyError, PermissionError) as exc:
        return _error("unavailable", str(exc))
    except Exception as exc:
        return _error("query_failed", f"{type(exc).__name__}: {str(exc)[:300]}")


@server.tool(description="Full column list with types, nullability and defaults, plus primary key, foreign keys (both directions), constraints, indexes and a row estimate.")
@logged(logger)
def db_describe_table(table: str, schema: str = "public", profile: str | None = None, database: str | None = None) -> dict[str, Any]:
    try:
        adapter = _adapter(profile)
        if adapter.kind == "mongodb":
            return {"ok": True, **adapter.describe_table(table, database=database)}
        try:
            detail = adapter.describe_table(table, schema)
        except ValueError as exc:
            return _error("invalid_identifier", str(exc))
        p = _profile(profile)
        columns = detail.get("columns", [])
        for col in columns:
            col["masked_by_policy"] = masking.mask_value(col.get("column_name", ""), "<probe>", p) == "***REDACTED***"
        return {"ok": True, **detail}
    except (ad.NotSupported, ad.DriverUnavailable, KeyError, PermissionError) as exc:
        return _error("unavailable", str(exc))
    except Exception as exc:
        return _error("query_failed", f"{type(exc).__name__}: {str(exc)[:300]}")


@server.tool(description="Every foreign-key relationship in a schema - the fastest way to understand how business entities connect.")
@logged(logger)
def db_relationships(schema: str = "public", profile: str | None = None) -> dict[str, Any]:
    try:
        adapter = _adapter(profile)
        if not hasattr(adapter, "relationships"):
            return _error("not_supported", f"{adapter.kind} relationships introspection is not implemented")
        rels = adapter.relationships(schema)
        return {"ok": True, "schema": schema, "count": len(rels), "relationships": rels}
    except (ad.NotSupported, ad.DriverUnavailable, KeyError, PermissionError) as exc:
        return _error("unavailable", str(exc))
    except Exception as exc:
        return _error("query_failed", f"{type(exc).__name__}: {str(exc)[:300]}")


@server.tool(description="List views and their definitions.")
@logged(logger)
def db_list_views(schema: str = "public", profile: str | None = None) -> dict[str, Any]:
    try:
        adapter = _adapter(profile)
        if not hasattr(adapter, "list_views"):
            return _error("not_supported", "adapter has no view introspection")
        views = adapter.list_views(schema)
        return {"ok": True, "schema": schema, "count": len(views), "views": views}
    except (ad.NotSupported, ad.DriverUnavailable, KeyError, PermissionError) as exc:
        return _error("unavailable", str(exc))
    except Exception as exc:
        return _error("query_failed", f"{type(exc).__name__}: {str(exc)[:300]}")


@server.tool(description="Run a controlled read-only SELECT. Non-SELECT statements, stacked statements and data-modifying CTEs are rejected. A LIMIT is enforced and sensitive columns are masked.")
@logged(logger)
def db_query(sql: str, params: list[Any] | None = None, max_rows: int | None = None,
             profile: str | None = None, save_evidence: bool = True, run_id: str | None = None) -> dict[str, Any]:
    prof = _profile(profile)
    allowed, reason, kind = _guard(prof, sql)
    if not allowed:
        result = _error("read_only_violation", f"rejected by read-only guard ({kind}): {reason}")
        if save_evidence:
            folder = ev.evidence_root("database", run_id or ev.new_run_id("db"))
            result["evidence"] = ev.relative(ev.write_json(folder, "rejected-query.json", {
                "sql": sql[:4000], "reason": reason, "guard_kind": kind, "profile": prof.get("name"),
            }))
        return result

    cfg = current()
    limit = cfg.db_max_rows(max_rows)
    # Fetch one row beyond the cap so `truncated` can be reported honestly.
    bounded = add_limit(sql, limit + 1)
    tables = extract_tables(bounded)
    try:
        adapter = ad.get_adapter(prof)
        qr = adapter.run_select(bounded, tuple(params) if params else None, limit)
    except (ad.DriverUnavailable, KeyError, PermissionError) as exc:
        return _error("unavailable", str(exc))
    except ad.NotSupported as exc:
        return _error("not_supported", str(exc))
    except Exception as exc:
        message = str(exc)[:400]
        if "permission denied" in message.lower() or "read-only" in message.lower() or "sql_statement_not_known" in message.lower():
            return _error("database_rejected_write", f"the database itself refused this statement: {message}")
        return _error("query_failed", f"{type(exc).__name__}: {message}")

    rows, masked = masking.mask_rows(qr.rows, tables, prof)
    payload = {
        "ok": True,
        "columns": qr.columns,
        "rows": rows,
        "row_count": qr.row_count,
        "truncated": qr.truncated,
        "applied_limit": qr.applied_limit,
        "duration_ms": qr.duration_ms,
        "referenced_tables": tables,
        "masked_columns": masked,
        "read_only_enforced": True,
        "sql_executed": bounded,
    }
    if save_evidence:
        folder = ev.evidence_root("database", run_id or ev.new_run_id("db"))
        written = ev.write_json(folder, "query.json", {
            "sql": bounded, "params": params, "profile": prof.get("name"),
            "row_count": qr.row_count, "duration_ms": qr.duration_ms,
        })
        payload["evidence"] = ev.relative(written)
    return payload


@server.tool(description="Show the execution plan for a read-only SELECT. EXPLAIN ANALYZE is refused because it would execute the statement.")
@logged(logger)
def db_explain(sql: str, analyze: bool = False, profile: str | None = None) -> dict[str, Any]:
    if analyze:
        return _error("read_only_violation", "EXPLAIN ANALYZE executes the statement and is never permitted")
    prof = _profile(profile)
    allowed, reason, kind = _guard(prof, sql)
    if not allowed:
        return _error("read_only_violation", f"{reason} ({kind})")
    body = sql.strip().rstrip(";")
    if body.lower().startswith("explain"):
        body = body[len("explain"):].lstrip()
    try:
        return {"ok": True, **ad.get_adapter(prof).explain(body)}
    except ad.NotSupported as exc:
        return _error("not_supported", str(exc))
    except (ad.DriverUnavailable, KeyError, PermissionError) as exc:
        return _error("unavailable", str(exc))
    except Exception as exc:
        return _error("query_failed", f"{type(exc).__name__}: {str(exc)[:300]}")


@server.tool(description="Capture a compact, comparable snapshot of row counts and a content digest per table. Used to verify business state before and after a test run.")
@logged(logger)
def db_snapshot(tables: list[str], schema: str = "public", profile: str | None = None,
                snapshot_id: str | None = None) -> dict[str, Any]:
    prof = _profile(profile)
    adapter = None
    try:
        adapter = ad.get_adapter(prof)
    except (ad.DriverUnavailable, KeyError, PermissionError) as exc:
        return _error("unavailable", str(exc))

    entries = []
    for table in tables[:50]:
        try:
            qualified = f"{ad.mask_schema(schema)}.{ad.mask_ident(table)}"
        except ValueError as exc:
            return _error("invalid_identifier", str(exc))
        # qualified is produced by mask_schema/mask_ident, which validate then quote.
        sql = (
            "SELECT count(*) AS n, "
            "coalesce(md5(string_agg(t::text, ',' ORDER BY t::text)), '(empty)') AS digest "
            f"FROM {qualified} t"  # nosec B608
        )
        try:
            rows = adapter.fetch_rows(sql, max_rows=5)
        except Exception as exc:
            rows = [{"n": None, "digest": f"<error {type(exc).__name__}: {str(exc)[:120]}>"}]
        if rows:
            entries.append({"table": f"{schema}.{table}", "row_count": rows[0]["n"], "digest": rows[0]["digest"]})
    sid = snapshot_id or ev.new_run_id("snap")
    folder = ev.evidence_root("database", sid)
    written = ev.write_json(folder, "snapshot.json", {"snapshot_id": sid, "profile": prof.get("name"), "tables": entries})
    return {"ok": True, "snapshot_id": sid, "captured": len(entries), "tables": entries,
            "evidence": ev.relative(written)}


@server.tool(description="Compare two snapshots (or a snapshot and a fresh capture) and report which tables changed, with row-count deltas. This is how state-transition verification is done without writes.")
@logged(logger)
def db_diff(snapshot_id: str, tables: list[str], schema: str = "public", profile: str | None = None) -> dict[str, Any]:
    from core.shared import paths

    target = paths.evidence_dir("database", snapshot_id) / "snapshot.json"
    if not target.exists():
        return _error("not_found", f"snapshot '{snapshot_id}' not found under evidence/database/")
    import json

    before = {e["table"]: e for e in json.loads(target.read_text())["tables"]}
    after = db_snapshot(tables, schema=schema, profile=profile, snapshot_id=None)
    if not after.get("ok"):
        return after
    diffs = []
    for entry in after["tables"]:
        b = before.get(entry["table"])
        if b is None:
            diffs.append({"table": entry["table"], "change": "only_in_after"})
            continue
        changed = b.get("digest") != entry.get("digest")
        delta = None
        if isinstance(b.get("row_count"), int) and isinstance(entry.get("row_count"), int):
            delta = entry["row_count"] - b["row_count"]
        diffs.append({"table": entry["table"], "changed": changed, "row_count_delta": delta,
                      "before": b.get("row_count"), "after": entry.get("row_count")})
    return {"ok": True, "baseline_snapshot": snapshot_id, "compared": len(diffs),
            "changed_tables": [d["table"] for d in diffs if d.get("changed")], "diff": diffs}


@server.tool(description="Inspect currently open transactions and idle-in-transaction sessions, to understand what a test run left behind.")
@logged(logger)
def db_transactions(profile: str | None = None) -> dict[str, Any]:
    try:
        rows = _adapter(profile).fetch_rows(
            "SELECT pid, usename, datname, state, xact_start, now()-xact_start AS age, "
            "coalesce(left(query,120),'') AS last_query "
            "FROM pg_stat_activity WHERE xact_start IS NOT NULL ORDER BY xact_start", max_rows=50)
        return {"ok": True, "count": len(rows), "transactions": rows}
    except AttributeError:
        return _error("not_supported", "transaction inspection is PostgreSQL-only")
    except (ad.DriverUnavailable, KeyError, PermissionError) as exc:
        return _error("unavailable", str(exc))
    except Exception as exc:
        return _error("query_failed", f"{type(exc).__name__}: {str(exc)[:300]}")


if __name__ == "__main__":
    logger.info("starting khunkhaar-db MCP server over stdio")
    server.run(transport="stdio")
