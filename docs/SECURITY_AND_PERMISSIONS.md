# Security and permissions

Phase 1 is built around explicit gates. This page states what the agent can do, what it cannot,
and where each control lives.

## Permission matrix

| Action | Default | Control |
| --- | --- | --- |
| Launch OpenCode / MCP servers | allowed | OpenCode config |
| Read files inside configured roots | allowed | `filesystem.roots` |
| Read `.env`, `.pem`, `.key`, `.p12`, `.pfx`, `data/profiles/**` | **refused** | `mcp-servers/filesystem/server.py` |
| Write files inside roots | allowed | `filesystem.roots` |
| Delete files | only under `runtime/tmp` | `fs_delete` |
| Navigate a browser | allowlisted origins, http(s) only | `browser.allowed_origins` + scheme gate |
| HTTP GET/HEAD/OPTIONS | allowed | `api.allowed_hosts` |
| HTTP POST/PUT/PATCH | allowed | `api.allowed_hosts` |
| HTTP DELETE | **blocked** twice | `allow_destructive` **and** `security.destructive_approved_targets` |
| Any browser/API request to link-local or metadata IPs | **blocked** | `netguard` on every redirect hop |
| SQL `SELECT`/`WITH`/`EXPLAIN` | allowed, capped | `sql_guard` + read-only role + session flag |
| SQL DDL/DML/stacked statements | **blocked** at three layers | see DATABASE_TOOL_GUIDE |
| Read database passwords | never exposed | env-var reference only; `db_profiles` shows the variable name |
| Run project startup scripts | **never automatic** | discovery does not execute anything |
| Run scanners against remote targets | requires approval | `zap_baseline.sh` gate; nuclei is template-local |

## Secrets handling

- Never stored in SQLite, reports, evidence or model-visible output.
- Redaction is structural (by key name) and textual (known key formats and connection strings).
- Booleans/numbers survive redaction so "is a credential present" remains answerable.
- The database profile password is read from `KHUNKHAAR_TEST_PG_PASSWORD`; the value lives in a
  0600 file outside the config and is loaded by `khunkhaar` at launch.
- API auth profiles resolve from an env var or a 0600 file; the tool never accepts a password
  argument and never reads credentials from project files.

## Command execution

The platform starts only fixed argv lists, never a shell:
`opencode` (via `os.execv`), `git` (read-only discovery), `docker` (pull/inspect/run),
`k6`, `nuclei`, `semgrep`, `bandit`, `pip-audit`. Bandit reports 0 issues after review; the
few verified-safe patterns carry a scoped `# nosec` with a written justification.

## Approval boundaries observed during installation

- **sudo** — never used. The one missing WebKit library was vendored locally instead
  (`scripts/install_webkit_deps.sh`). An apt path exists behind `--system` if the operator
  prefers it.
- **Destructive HTTP** — no DELETE was sent anywhere; the test that "executes" DELETE runs only
  against the local mock after adding `127.0.0.1` to the approved list inside the test, and the
  config keeps the list empty.
- **Scanning** — all scans (ZAP, nuclei, k6, Schemathesis) targeted `127.0.0.1:8731` only.
- **Production** — nothing outside this machine was contacted except package registries during
  installation and rule download for semgrep.

## What the model cannot do through these tools

- Cannot choose a database password or connection string.
- Cannot pass SQL that writes.
- Cannot reach hosts outside the allowlists.
- Cannot read the credential store.
- Cannot delete evidence.
- Cannot bypass redaction: it runs inside the server, before the response is serialized.

## Audit trail

- Tool calls are logged under `logs/` via `core/shared/toollog.py`.
- Health checks are recorded in `data/metadata/platform.sqlite3` (`tool_health`).
- Every DB query, API request and a11y scan can persist evidence under `evidence/`.
