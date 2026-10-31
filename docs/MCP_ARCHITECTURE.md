# MCP architecture

Four stdio MCP servers, one shared core, one evidence tree. Each server is a plain Python
process started by OpenCode with `.venv/bin/python`; nothing listens on a network port.

```
OpenCode ──stdio──> mcp-servers/{api,browser,database,filesystem}/server.py
                        │
                        └── core/
                            ├── browser/session.py      persistent Playwright session
                            ├── database/adapters.py    pg / mysql / mongo adapters
                            ├── database/sql_guard.py   read-only statement validator
                            ├── shared/config.py        config/khunkhaar.yaml
                            ├── shared/credentials.py   env-var or 0600 file profiles
                            ├── shared/evidence.py      evidence/<area>/<run-id>/
                            ├── shared/netguard.py      SSRF / allowlist matching
                            ├── shared/redaction.py     structural + textual redaction
                            └── metadata/store.py       SQLite platform state
```

## Servers and tools

### `khunkhaar-browser`
`browser_open`, `browser_navigate`, `browser_read`, `browser_a11y_tree`,
`browser_a11y_scan` (axe-core), `browser_screenshot`, `browser_act`, `browser_evaluate`,
`browser_console`, `browser_network`, `browser_downloads`, `browser_save_auth_state`,
`browser_close`, `browser_status`.

One session at a time; the session survives across tool calls. An origin gate runs before any
launch: non-http(s) schemes are always refused, and `browser.allowed_origins` applies when set.

### `khunkhaar-api`
`api_request`, `api_request_many`, `api_graphql`, `api_discover_openapi`, `api_policy`,
`api_check_target`.
Methods are split into safe / mutating / destructive. Destructive methods need both
`allow_destructive=true` **and** a host in `security.destructive_approved_targets`.
Responses are size-capped, retried on configured statuses, and redacted before reaching the model.

### `khunkhaar-database`
`db_profiles`, `db_ping`, `db_list_databases`, `db_list_schemas`, `db_list_tables`,
`db_describe_table`, `db_relationships`, `db_list_views`, `db_query`, `db_explain`,
`db_snapshot`, `db_diff`, `db_transactions`.

Read-only at three independent layers: client SQL validator, read-only database role, and
`SET LOCAL transaction_read_only = on` plus `statement_timeout`. Rows are capped and sensitive
columns are masked by policy.

### `khunkhaar-filesystem`
`fs_resolve`, `fs_read`, `fs_write_text`, `fs_list`, `fs_mkdir`, `fs_delete`,
`evidence_save`, `evidence_list`, `fs_tmp_cleanup`, `fs_storage_report`.

Paths are confined to the configured roots. Credential-shaped files are refused even inside a
root. Delete is limited to `runtime/tmp`; evidence is never deletable through this tool.

## Protocol-level verification

`tests/mcp/test_stdio_boot.py` boots each server as a subprocess, performs the MCP handshake,
checks the advertised name and required tools, verifies every tool has a description and an
object input schema, calls a tool with missing required arguments (must return `isError`), and
then calls a zero-argument tool successfully. Evidence:
`reports/validation/mcp_stdio_tests.txt`.

## Evidence layout

```
evidence/
  browser/<run-id>/trace.zip, video/, screenshots/, a11y/axe.json
  api/<run-id>/request.json, response.json
  database/<run-id>/query.json, snapshot.json, rejected-query.json
  filesystem/<run-id>/<artifact>
```

`evidence.*` writes are mode 0600. Paths returned to the model are relative to the KHUNKHAAR
root, never absolute host paths.
