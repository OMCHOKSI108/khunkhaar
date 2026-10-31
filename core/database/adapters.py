"""Database adapters for the DB Context Tool.

PostgreSQL is the reference implementation and is fully exercised against a disposable server.
MySQL and Mongo follow the same interface; each raises a precise, actionable error when its
driver or server is absent rather than returning fabricated metadata (spec section 6: do not
sacrifice PostgreSQL reliability to ship every adapter at once).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from core.shared.config import current


class DriverUnavailable(RuntimeError):
    pass


class NotSupported(RuntimeError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    duration_ms: float
    applied_limit: int


def mask_ident(name: str) -> str:
    """Validate then quote an identifier. Anything unusual is refused instead of interpolated."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,127}", name or ""):
        raise ValueError(f"refusing unexpected identifier: {name!r}")
    return '"' + name + '"'


def mask_schema(name: str | None, default: str = "public") -> str:
    return mask_ident(name or default)


class PostgresAdapter:
    kind = "postgresql"

    def __init__(self, profile: dict[str, Any]):
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:
            raise DriverUnavailable("psycopg 3 is not installed in the KHUNKHAAR venv") from exc
        self.profile = profile
        self._psycopg = __import__("psycopg")
        self._conn = None

    # ---- connection ---------------------------------------------------------
    def connect(self):
        if self._conn is not None:
            return self._conn
        import os

        p = self.profile
        password = os.environ.get(str(p.get("password_env") or ""), "") or None
        if not password:
            raise DriverUnavailable(
                f"no credential available for profile '{p.get('name')}' - "
                f"export {p.get('password_env')} first (passwords are never read from project files)"
            )
        conn = self._psycopg.connect(
            host=str(p.get("host", "127.0.0.1")),
            port=int(p.get("port", 5432)),
            dbname=p.get("database") or None,
            user=p.get("user") or None,
            password=password,
            connect_timeout=float(p.get("connect_timeout", 8)),
            application_name="khunkhaar-qa",
        )
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _timeout_ms(self) -> int:
        cfg = current()
        return int(self.profile.get("statement_timeout_ms") or cfg.get("database", "statement_timeout_ms", 5000))

    def fetch_rows(self, sql: str, params: tuple = (), max_rows: int | None = None) -> list[dict[str, Any]]:
        conn = self.connect()
        limit = max_rows or current().db_max_rows(None)
        cur = conn.cursor()
        try:
            cur.execute(f"SET LOCAL statement_timeout = {int(self._timeout_ms())}")
            cur.execute("SET LOCAL transaction_read_only = on")
            cur.execute(sql, params or None)
            if cur.description is None:
                return []
            cols = [d.name for d in cur.description]
            fetched = cur.fetchmany(limit + 1)
            rows = [dict(zip(cols, list(r))) for r in fetched[:limit]]
            return rows
        finally:
            cur.close()
            conn.rollback()  # never commit through this tool

    # ---- schema intelligence ------------------------------------------------
    def server_info(self) -> dict[str, Any]:
        rows = self.fetch_rows(
            "SELECT current_setting('server_version') AS version, "
            "current_database() AS database, current_user AS current_user, "
            "pg_is_in_recovery() AS in_recovery, "
            "current_setting('transaction_read_only') AS read_only"
        )
        return rows[0] if rows else {}

    def list_databases(self) -> list[dict[str, Any]]:
        return self.fetch_rows(
            "SELECT datname AS name, pg_encoding_to_char(encoding) AS encoding, "
            "datcollate AS collation, datallowconn AS allow_connect "
            "FROM pg_database WHERE NOT datistemplate ORDER BY datname"
        )

    def list_schemas(self) -> list[dict[str, Any]]:
        return self.fetch_rows(
            "SELECT nspname AS name, pg_get_userbyid(nspowner) AS owner, "
            "nspacl IS NOT NULL AS has_acl FROM pg_namespace "
            "WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' ORDER BY nspname"
        )

    def list_tables(self, schema: str = "public") -> list[dict[str, Any]]:
        return self.fetch_rows(
            "SELECT c.relname AS table_name, CASE c.relkind WHEN 'r' THEN 'table' "
            "WHEN 'p' THEN 'partitioned_table' WHEN 'm' THEN 'materialized_view' ELSE c.relkind::text END AS type, "
            "pg_class_oid.reltuples::bigint AS approx_rows, "
            "pg_total_relation_size(c.oid) AS total_bytes, obj_description(c.oid) AS comment "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN LATERAL (SELECT oid, reltuples FROM pg_class WHERE oid=c.oid) pg_class_oid ON true "
            "WHERE n.nspname = %s AND c.relkind IN ('r','p','m') ORDER BY 1",
            (schema,), max_rows=2000,
        )

    def list_views(self, schema: str = "public") -> list[dict[str, Any]]:
        return self.fetch_rows(
            "SELECT table_name AS view_name, view_definition FROM information_schema.views "
            "WHERE table_schema = %s ORDER BY 1", (schema,),
        )

    def describe_table(self, table: str, schema: str = "public") -> dict[str, Any]:
        t = mask_ident(table)
        s = mask_schema(schema)
        quoted = f"{s}.{t}"
        columns = self.fetch_rows(
            "SELECT ordinal_position AS position, column_name, data_type, "
            "coalesce(domain_name, udt_name) AS internal_type, character_maximum_length AS max_len, "
            "numeric_precision AS num_precision, numeric_scale AS num_scale, is_nullable, column_default, "
            "generation_expression IS NOT NULL AS generated, col_description("
            "(SELECT oid FROM pg_class WHERE oid = (table_schema||'.'||table_name)::regclass), "
            "ordinal_position::int) AS comment "
            "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
            "ORDER BY ordinal_position", (schema, table), max_rows=1000,
        )
        pk = self.fetch_rows(
            "SELECT a.attname AS column_name, i.indisprimary AS is_primary "
            "FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum = ANY(i.indkey) "
            "WHERE n.nspname=%s AND c.relname=%s AND i.indisprimary ORDER BY array_position(i.indkey, a.attnum)",
            (schema, table),
        )
        unique = self.fetch_rows(
            "SELECT conname AS constraint_name, pg_get_constraintdef(c.oid) AS definition "
            "FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace JOIN pg_class t ON t.oid=c.conrelid "
            "WHERE n.nspname=%s AND t.relname=%s AND c.contype IN ('u','c','f') ORDER BY c.contype, conname",
            (schema, table), max_rows=200,
        )
        indexes = self.fetch_rows(
            "SELECT indexname AS index_name, indexdef AS definition FROM pg_indexes "
            "WHERE schemaname=%s AND tablename=%s ORDER BY 1", (schema, table), max_rows=200,
        )
        # information_schema.constraint_column_usage is owner-only, so a read-only role sees
        # nothing there. pg_catalog is used instead, which works for any role that can see the table.
        fk_select = (
            "SELECT con.conname AS constraint_name, "
            "a.attname AS column_name, n2.nspname AS foreign_schema, c2.relname AS foreign_table, "
            "a2.attname AS foreign_column, "
            "CASE con.confupdtype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' "
            "WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_update, "
            "CASE con.confdeltype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' "
            "WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_delete "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_class c2 ON c2.oid=con.confrelid JOIN pg_namespace n2 ON n2.oid=c2.relnamespace "
            "JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(local_attnum, ref_attnum, ord) ON true "
            "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.local_attnum "
            "JOIN pg_attribute a2 ON a2.attrelid=c2.oid AND a2.attnum=k.ref_attnum "
            "WHERE con.contype='f' AND "
        )
        fk_out = self.fetch_rows(fk_select + "n.nspname=%s AND c.relname=%s ORDER BY con.conname, k.ord",
                                 (schema, table))
        fk_in = self.fetch_rows(
            "SELECT n.nspname AS referencing_schema, c.relname AS referencing_table, "
            "a.attname AS referencing_column, con.conname AS constraint_name, "
            "CASE con.confupdtype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' "
            "WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_update, "
            "CASE con.confdeltype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' "
            "WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_delete "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_class c2 ON c2.oid=con.confrelid JOIN pg_namespace n2 ON n2.oid=c2.relnamespace "
            "JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(local_attnum, ref_attnum, ord) ON true "
            "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.local_attnum "
            "WHERE con.contype='f' AND n2.nspname=%s AND c2.relname=%s ORDER BY con.conname, k.ord",
            (schema, table),
        )
        # quoted is produced by mask_ident(), which validates then double-quotes.
        row_estimate = self.fetch_rows(f"SELECT count(*) AS n FROM {quoted}", max_rows=1)  # nosec B608
        return {
            "schema": schema,
            "table": table,
            "columns": columns,
            "primary_key": [r["column_name"] for r in pk],
            "constraints": unique,
            "indexes": indexes,
            "foreign_keys_outgoing": fk_out,
            "foreign_keys_incoming": fk_in,
            "approx_rows": row_estimate[0]["n"] if row_estimate else None,
        }

    def relationships(self, schema: str = "public") -> list[dict[str, Any]]:
        return self.fetch_rows(
            "SELECT n.nspname AS schema, c.relname AS table_name, a.attname AS column_name, "
            "confconf.nspname AS foreign_schema, confc.relname AS foreign_table, "
            "confda.attname AS foreign_column, "
            "CASE con.confdeltype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' "
            "WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_delete "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_class confc ON confc.oid=con.confrelid JOIN pg_namespace confconf ON confconf.oid=confc.relnamespace "
            # unnest pair-wise so composite foreign keys report every column, not just the first
            "JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(attnum, fattnum, ord) ON true "
            "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum "
            "JOIN pg_attribute confda ON confda.attrelid=confc.oid AND confda.attnum=k.fattnum "
            "WHERE con.contype='f' AND n.nspname=%s ORDER BY 1, 2, k.ord",
            (schema,), max_rows=1000,
        )

    def run_select(self, sql: str, params: tuple, max_rows: int) -> QueryResult:
        import time

        conn = self.connect()
        cur = conn.cursor()
        started = time.monotonic()
        try:
            cur.execute(f"SET LOCAL statement_timeout = {int(self._timeout_ms())}")
            cur.execute("SET LOCAL transaction_read_only = on")
            bounded = sql if re.search(r"\blimit\b", sql, re.I) else f"{sql} LIMIT {int(max_rows) + 1}"
            cur.execute(bounded, params or None)
            cols = [d.name for d in cur.description] if cur.description else []
            fetched = cur.fetchmany(max_rows + 1)
            rows = [dict(zip(cols, [_jsonable(v) for v in r])) for r in fetched[:max_rows]]
            return QueryResult(cols, rows, len(rows), len(fetched) > max_rows,
                               round((time.monotonic() - started) * 1000, 2), max_rows)
        finally:
            cur.close()
            conn.rollback()

    def explain(self, sql: str, analyze: bool = False) -> dict[str, Any]:
        if analyze:
            raise NotSupported("EXPLAIN ANALYZE executes the statement; only plain EXPLAIN is permitted")
        rows = self.fetch_rows("EXPLAIN " + sql, max_rows=200)
        return {"plan": [r.get("QUERY PLAN") for r in rows]}


def _jsonable(value: Any) -> Any:
    import datetime as dt
    import decimal

    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return f"<binary {len(value)} bytes>"
    if isinstance(value, (dict, list)):
        return value
    return value


class MySQLAdapter:
    """Same interface as PostgresAdapter, gated on a driver that is not installed here."""

    kind = "mysql"

    def __init__(self, profile: dict[str, Any]):
        try:
            import pymysql  # noqa: F401
        except ImportError as exc:
            raise DriverUnavailable(
                "MySQL adapter needs a driver: .venv/bin/pip install PyMySQL "
                "(no mysql client is present on this host either)"
            ) from exc
        raise NotSupported("MySQL adapter is scaffolded but unverified in Phase 1; PostgreSQL is the supported path")


class MongoAdapter:
    kind = "mongodb"

    def __init__(self, profile: dict[str, Any]):
        try:
            import pymongo  # noqa: F401
        except ImportError as exc:
            raise DriverUnavailable("pymongo is not installed in the KHUNKHAAR venv") from exc
        self.profile = profile
        self._client = None

    def _db_client(self):
        import os

        if self._client is None:
            from pymongo import MongoClient

            uri = self.profile.get("uri") or os.environ.get(str(self.profile.get("uri_env") or ""), "")
            if not uri:
                raise DriverUnavailable(
                    f"mongo profile '{self.profile.get('name')}' needs a connection string via "
                    f"{self.profile.get('uri_env') or 'uri'}"
                )
            self._client = MongoClient(uri, serverSelectionTimeoutMS=5000, read_preference=None)
        return self._client

    def server_info(self) -> dict[str, Any]:
        return self._db_client().server_info()

    def list_databases(self) -> list[dict[str, Any]]:
        return [{"name": d["name"], "size_on_disk": d["sizeOnDisk"]} for d in self._db_client().list_databases()]

    def list_tables(self, database: str | None = None, schema: str | None = None) -> list[dict[str, Any]]:
        db = self._db_client()[database or self.profile.get("database")]
        return [{"table_name": c, "type": "collection"} for c in db.list_collection_names()]

    def describe_table(self, table: str, database: str | None = None, schema: str | None = None) -> dict[str, Any]:
        db = self._db_client()[database or self.profile.get("database")]
        coll = db[table]
        return {
            "collection": table,
            "indexes": [{k: v for k, v in ix.items() if k in ("name", "key", "unique")} for ix in coll.list_indexes()],
            "approx_rows": coll.estimated_document_count(),
            "sample_keys": sorted({k for doc in coll.find({}, limit=20) for k in doc.keys()}),
        }

    def run_select(self, *args: Any, **kwargs: Any) -> QueryResult:
        raise NotSupported("MongoDB has no SQL; use the structured find() path instead")


ADAPTERS = {
    "postgresql": PostgresAdapter,
    "postgres": PostgresAdapter,
    "mysql": MySQLAdapter,
    "mongodb": MongoAdapter,
    "mongo": MongoAdapter,
}


def get_adapter(profile: dict[str, Any]):
    kind = str(profile.get("kind", "postgresql")).lower()
    adapter_cls = ADAPTERS.get(kind)
    if adapter_cls is None:
        raise NotSupported(f"no adapter for kind '{kind}' (available: {', '.join(sorted(ADAPTERS))})")
    return adapter_cls(profile)


def profile_by_name(name: str | None) -> dict[str, Any]:
    cfg = current()
    profiles = cfg.get("database", "profiles", []) or []
    target = name or cfg.get("database", "default_profile")
    if not target:
        raise KeyError("no default database profile is configured")
    for p in profiles:
        if isinstance(p, dict) and p.get("name") == target:
            if p.get("enabled") is False:
                raise PermissionError(
                    f"profile '{target}' is disabled in config/khunkhaar.yaml; enable it deliberately"
                )
            return p
    available = ", ".join(str(p.get("name")) for p in profiles if isinstance(p, dict))
    raise KeyError(f"unknown database profile '{target}'. configured profiles: {available or '(none)'}")
