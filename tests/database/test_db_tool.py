"""DB Context Tool acceptance tests (Phase 1 sections 6 and 12, "Database").

Runs against the disposable Docker PostgreSQL (127.0.0.1:5445) created for Phase 1 - never
against a real cluster. Proves schema intelligence, controlled reads, masking, evidence, and
that writes are refused at both the SQL guard and the database role.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.database import adapters as ad
from core.database.sql_guard import validate_read_only
from core.shared import paths

pytestmark = pytest.mark.database


@pytest.fixture(scope="module")
def db_mcp():
    from tests.conftest import load_server_module

    return load_server_module("database")


# ---------------------------------------------------------------- profile & health


def test_profiles_show_names_but_never_credentials(db_mcp):
    listing = db_mcp.db_profiles()
    assert listing["ok"] is True
    names = {p["name"] for p in listing["profiles"]}
    assert {"khunkhaar_test_pg", "local_pg_16"} <= names
    import os

    for profile in listing["profiles"]:
        assert not {"password", "passwd", "dsn", "uri", "connection_string"} & set(profile), profile
    secret = os.environ.get("KHUNKHAAR_TEST_PG_PASSWORD", "")
    assert secret and secret not in json.dumps(listing), "the password value leaked into the profile listing"
    assert listing["enforce_read_only"] is True
    disabled = next(p for p in listing["profiles"] if p["name"] == "local_pg_16")
    assert disabled["enabled"] is False


def test_ping_reports_a_read_only_session(db_mcp):
    ping = db_mcp.db_ping()
    assert ping["ok"], ping
    server = ping["server"]
    assert server["read_only"] == "on"
    assert server["current_user"] == "khunkhaar_ro"
    assert server["version"].startswith("16.")


# ---------------------------------------------------------------- schema intelligence


def test_schema_and_table_discovery(db_mcp):
    schemas = db_mcp.db_list_schemas()
    assert schemas["ok"] and "public" in [s["name"] for s in schemas["schemas"]]

    tables = db_mcp.db_list_tables()
    assert tables["ok"] is True
    found = {t["table_name"] for t in tables["tables"]}
    assert {"users", "orders", "products", "order_items"} <= found
    assert all("approx_rows" in t for t in tables["tables"])


def test_views_are_listed(db_mcp):
    views = db_mcp.db_list_views()
    assert views["ok"] and "active_orders" in [v["view_name"] for v in views["views"]]


def test_describe_table_returns_keys_constraints_and_indexes(db_mcp):
    detail = db_mcp.db_describe_table(table="orders")
    assert detail["ok"], detail
    columns = {c["column_name"]: c for c in detail["columns"]}
    assert "id" in columns and "user_id" in columns
    assert detail["primary_key"] == ["id"]
    assert any(k["column_name"] == "user_id" for k in detail["foreign_keys_outgoing"]), detail
    assert detail["indexes"], detail
    assert detail["approx_rows"] >= 0


def test_relationships_connect_the_business_entities(db_mcp):
    rels = db_mcp.db_relationships()
    assert rels["ok"], rels
    pairs = {(r["table_name"], r["foreign_table"]) for r in rels["relationships"]}
    assert ("orders", "users") in pairs
    assert len(pairs) >= 3


def test_invalid_identifier_is_rejected(db_mcp):
    result = db_mcp.db_describe_table(table='users"; DROP TABLE users; --')
    assert result["ok"] is False and result["error_kind"] == "invalid_identifier"


# ---------------------------------------------------------------- controlled reads


def test_select_works_and_is_bounded(db_mcp):
    result = db_mcp.db_query(sql="SELECT id, status FROM orders ORDER BY id", max_rows=2)
    assert result["ok"], result
    assert result["row_count"] == 2
    assert result["truncated"] is True, "two rows fit the cap; the 3rd proves truncation detection"
    assert "LIMIT" in result["sql_executed"].upper()
    assert result["read_only_enforced"] is True


def test_sensitive_columns_are_masked(db_mcp):
    result = db_mcp.db_query(sql="SELECT id, email, password_hash FROM users ORDER BY id")
    assert result["ok"], result
    emails = {row["email"] for row in result["rows"]}
    assert emails == {"***REDACTED***"}, emails
    assert {row["password_hash"] for row in result["rows"]} == {"***REDACTED***"}
    assert "email" in result["masked_columns"]


def test_parameters_are_bound_not_interpolated(db_mcp):
    result = db_mcp.db_query(sql="SELECT id FROM users WHERE email = %s", params=["ada@example.com"])
    assert result["ok"], result
    assert result["row_count"] == 1


def test_query_evidence_contains_no_credentials(db_mcp):
    result = db_mcp.db_query(sql="SELECT id FROM users", run_id="db-evidence-check")
    evidence = Path(paths.kh_root() / result["evidence"]).read_text()
    assert "KHUNKHAAR_TEST_PG_PASSWORD" not in evidence
    import os

    secret = os.environ.get("KHUNKHAAR_TEST_PG_PASSWORD", "")
    if secret:
        assert secret not in evidence, "the database password reached an evidence file"


# ---------------------------------------------------------------- write refusal, layer 1


@pytest.mark.parametrize("sql", [
    "INSERT INTO users (email) VALUES ('x@example.com')",
    "UPDATE users SET email = 'x'",
    "DELETE FROM users",
    "DROP TABLE users",
    "TRUNCATE users",
    "ALTER TABLE users ADD COLUMN evil int",
    "CREATE TABLE evil (id int)",
    "GRANT ALL ON users TO public",
    "COPY users TO '/tmp/users.csv'",
    "SELECT 1; DROP TABLE users",
    "WITH gone AS (DELETE FROM users RETURNING *) SELECT * FROM gone",
    "SELECT * INTO OUTFILE '/tmp/x' FROM users",
    "SELECT pg_sleep(10)",
    "SELECT 1 -- ; DELETE FROM users\n; DELETE FROM users",
])
def test_sql_guard_rejects_every_write_form(db_mcp, sql):
    result = db_mcp.db_query(sql=sql, save_evidence=False)
    assert result["ok"] is False
    assert result["error_kind"] == "read_only_violation", (sql, result)
    assert "rejected by read-only guard" in result["error"]


def test_comment_smuggled_single_line_write_runs_only_the_select(db_mcp):
    """The write is commented out for Postgres too - what executes must be the SELECT alone."""
    result = db_mcp.db_query(sql="SELECT 1 -- ; DELETE FROM users", save_evidence=False)
    assert result["ok"] is True
    assert result["rows"] == [{"?column?": 1}]
    assert db_mcp.db_query(sql="SELECT count(*) AS n FROM users")["rows"][0]["n"] == 3


def test_guard_allows_readonly_shapes():
    for sql in ("SELECT 1", "select id from users where id = 1", "WITH x AS (SELECT 1) SELECT * FROM x",
                "EXPLAIN SELECT 1", "SHOW search_path", "SELECT * FROM users -- trailing comment",
                "  select\n id\nfrom users\n"):
        verdict = validate_read_only(sql)
        assert verdict.allowed, (sql, verdict.reason)


def test_rejected_query_is_recorded_as_evidence(db_mcp):
    result = db_mcp.db_query(sql="DELETE FROM users", save_evidence=True)
    assert result["ok"] is False
    assert Path(paths.kh_root() / result["evidence"]).is_file()


# ---------------------------------------------------------------- write refusal, layer 2


def test_database_role_itself_refuses_writes():
    """Bypass the SQL guard entirely and talk to Postgres with the same read-only role."""
    profile = ad.profile_by_name(None)
    adapter = ad.get_adapter(profile)
    with pytest.raises(Exception) as caught:
        adapter.fetch_rows("INSERT INTO users (email) VALUES ('bypass@example.com') RETURNING id")
    message = str(caught.value).lower()
    assert "read-only" in message or "permission denied" in message, message
    adapter.close()


def test_statement_timeout_is_enforced_at_the_connection_layer():
    profile = ad.profile_by_name(None)
    adapter = ad.get_adapter(profile)
    with pytest.raises(Exception) as caught:
        adapter.fetch_rows("SELECT pg_sleep(5)")
    assert "timeout" in str(caught.value).lower() or "canceling" in str(caught.value).lower()


# ---------------------------------------------------------------- explain, snapshot, diff


def test_explain_is_allowed_but_analyze_is_refused(db_mcp):
    plan = db_mcp.db_explain(sql="SELECT * FROM users")
    assert plan["ok"], plan
    refused = db_mcp.db_explain(sql="SELECT * FROM users", analyze=True)
    assert refused["ok"] is False and refused["error_kind"] == "read_only_violation"


def test_snapshot_and_diff_detect_state_changes(db_mcp):
    snap = db_mcp.db_snapshot(tables=["users", "orders"], snapshot_id="db-test-baseline")
    assert snap["ok"] and snap["captured"] == 2, snap
    diff = db_mcp.db_diff(snapshot_id="db-test-baseline", tables=["users", "orders"])
    assert diff["ok"] and diff["changed_tables"] == [], diff
    missing = db_mcp.db_diff(snapshot_id="does-not-exist", tables=["users"])
    assert missing["ok"] is False and missing["error_kind"] == "not_found"


def test_transactions_are_inspectable(db_mcp):
    result = db_mcp.db_transactions()
    assert result["ok"], result
    assert isinstance(result["transactions"], list)


# ---------------------------------------------------------------- safety of the surface


def test_disabled_profile_cannot_be_used(db_mcp):
    result = db_mcp.db_ping(profile="local_pg_16")
    assert result["ok"] is False and result["error_kind"] == "unavailable"
    assert "disabled" in result["error"]


def test_unknown_profile_is_a_structured_error(db_mcp):
    result = db_mcp.db_ping(profile="nope")
    assert result["ok"] is False and result["error_kind"] == "unavailable"


def test_mysql_adapter_reports_the_missing_driver_clearly(db_mcp):
    cfg = __import__("core.shared.config", fromlist=["current"]).current()
    profiles = cfg.get("database", "profiles")
    profiles.append({"name": "tmp-mysql", "kind": "mysql", "host": "127.0.0.1", "port": 3306,
                     "database": "x", "user": "x", "read_only": True})
    try:
        result = db_mcp.db_ping(profile="tmp-mysql")
        assert result["ok"] is False and result["error_kind"] == "unavailable"
        assert "PyMySQL" in result["error"] or "not installed" in result["error"]
    finally:
        profiles.remove(profiles[-1])


def test_mongo_adapter_refuses_sql():
    from core.database.adapters import MongoAdapter, NotSupported

    adapter = MongoAdapter({"name": "tmp-mongo", "kind": "mongodb", "uri_env": "NOPE_UNSET"})
    with pytest.raises(NotSupported):
        adapter.run_select("SELECT 1", None, 10)


def test_tool_output_never_contains_the_password(db_mcp):
    import os

    secret = os.environ.get("KHUNKHAAR_TEST_PG_PASSWORD", "")
    assert secret, "test environment is missing the DB password"
    for payload in (db_mcp.db_profiles(), db_mcp.db_ping(), db_mcp.db_query(sql="SELECT 1")):
        assert secret not in json.dumps(payload)
