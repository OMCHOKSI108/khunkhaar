# Database Context Tool guide

`khunkhaar-database` answers "what is the real data model, and what does the data say?" without
ever writing.

## Three independent read-only layers

1. **Client guard** — `core/database/sql_guard.py` accepts only `SELECT`, `WITH … SELECT`,
   `EXPLAIN`, `SHOW`, `VALUES`. It rejects stacked statements, data-modifying CTEs, comments
   used to smuggle keywords, and side-channel functions (`pg_sleep`, `lo_import`, `dblink`,
   `pg_read_file`, …).
2. **Database role** — the test profile connects as `khunkhaar_ro`, a role without write
   privileges. Tests prove the server itself refuses an `INSERT` even when the client guard is
   bypassed.
3. **Session** — connections set `SET LOCAL transaction_read_only = on` and
   `statement_timeout` from config (5000 ms).

A write needs all three to fail simultaneously.

## Profiles

`config/khunkhaar.yaml`:

```yaml
database:
  default_profile: khunkhaar_test_pg
  enforce_read_only: true
  statement_timeout_ms: 5000
  max_rows: 500
  profiles:
    - name: khunkhaar_test_pg
      kind: postgresql
      host: 127.0.0.1
      port: 5445
      database: khunkhaar_qa
      user: khunkhaar_ro
      password_env: KHUNKHAAR_TEST_PG_PASSWORD
      redacted_columns: [users.email, users.password_hash, orders.card_last4]
    - name: local_pg_16
      enabled: false
```

- Passwords are **only** read from the environment variable named by the profile. The file
  `data/profiles/db/khunkhaar_test_pg.env` is mode 0600 and is loaded by `khunkhaar` at launch;
  `--doctor` reports `needs-env` when inspecting a bare shell, which is expected.
- `db_profiles` shows names, hosts, ports and credential *source names* — never values.
- The disposable server is container `khunkhaar-test-pg` (`postgres:16-alpine`, `127.0.0.1:5445`).

## Row caps, truncation and masking

- Every query gets a hard `LIMIT` (`max_rows + 1`) so `truncated` can be reported honestly.
- `redacted_columns` are replaced with `***REDACTED***` before the result leaves the server.
- `masked_columns` in the response lists what was masked; `referenced_tables` lists what was
  touched.

## State verification without writes

- `db_snapshot(tables=[...])` stores `count(*)` plus an `md5(string_agg(...))` digest per table
  under `evidence/database/<snapshot-id>/snapshot.json`.
- `db_diff(snapshot_id, tables=[...])` captures a fresh snapshot and reports per-table change
  and row-count delta.
- `db_transactions` lists open/idle-in-transaction sessions (PostgreSQL).

## Introspection tools

`db_list_schemas`, `db_list_tables`, `db_describe_table` (columns, PK, FKs both directions,
constraints, indexes, approximate rows), `db_relationships` (composite FKs reported per column
via `unnest … WITH ORDINALITY`), `db_list_views`, `db_explain` (never `ANALYZE`).

## Other engines

`core/database/adapters.py` also contains MySQL (pymysql) and MongoDB adapters. No MySQL or
MongoDB instance is installed, so only PostgreSQL is exercised by the suite; the adapters fail
with `driver_unavailable` / `not_supported` instead of pretending.

## Validation evidence

- 38 DB tests: `reports/validation/database_tests.txt` (includes role-layer write refusal,
  statement timeout, masking, snapshot/diff, and rejected-query evidence).
