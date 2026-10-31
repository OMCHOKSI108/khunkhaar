# Configuration

Configuration is a single YAML file, `config/khunkhaar.yaml`, under the KHUNKHAAR root. Defaults
are merged in by `core/shared/config.py`; a missing key falls back to the default in that file.
Safety-critical values are clamped in code, not trusted from YAML.

```bash
khunkhaar config show
khunkhaar config show --section git
khunkhaar config show --section planner --json
```

Credential **values** never appear in configuration, `config show`, SQLite, logs or reports. Only
environment variable names are stored.

## Root resolution

| Method | Effect |
| --- | --- |
| `KHUNKHAAR_ROOT=/path` | Use that existing directory as the root; an invalid path is an error |
| Default | The directory containing this checkout (`~/KHUNKHAAR-QA`) |

Everything else — `data/`, `evidence/`, `reports/`, `logs/`, `intelligence/`, `monitoring/`,
`regression/` — is derived from the root. A subprocess can use a different root only by setting
`KHUNKHAAR_ROOT` before it starts.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `KHUNKHAAR_ROOT` | Platform root override |
| `KHUNKHAAR_OPENCODE_ARGS` | Extra arguments appended to the `opencode` launch in Phase 1/2 |
| `KHUNKHAAR_GITLAB_TOKEN` | Read-only GitLab token; the variable name is set by `gitlab.token_env` |
| `KHUNKHAAR_TEST_PG_PASSWORD` | Password for the disposable Postgres profile; referenced by `database.profiles[].password_env` |
| `KHUNKHAAR_PROJECT_ID` | Set by the launcher for MCP servers in a session; not operator configuration |

Regression subprocesses additionally receive `KHUNKHAAR_TARGET`, `KHUNKHAAR_USERS`,
`KHUNKHAAR_DB`, `KHUNKHAAR_REGRESSION_RESULT` and `KHUNKHAAR_REGRESSION_INVERTED`. They are
internal to one test process.

## Safety floors and clamping

Some values cannot be widened by editing YAML:

- `git.read_only_commands` is intersected with a built-in read-only floor. A write-capable
  subcommand added to the file is ignored.
- `dashboard.host` is clamped to `127.0.0.1` for wildcard values when reported by `config show`.
  The `dashboard start` command independently defaults its bind to `127.0.0.1` and refuses any
  other host without `--allow-non-loopback`.
- API timeouts are clamped to `api.max_timeout_seconds`; database rows to `database.max_rows`;
  dashboard ports to 1024–65535.
- `api.allowed_hosts` starts empty in the code defaults (refuse everything). The shipped file adds
  four loopback entries for the mock and synthetic apps; a host not listed is refused.
- Network guards (`netguard`) always refuse link-local addresses and the cloud metadata address,
  regardless of `allow_private_ranges`.

Unknown YAML keys are kept and shown by `config show` but have no effect. A YAML file that is not
a mapping is refused at load. There is no JSON-schema validation.

## Sections

### `api`

| Key | Default (shipped) | Meaning |
| --- | --- | --- |
| `allowed_hosts` | four loopback entries | Host:port allowlist; requests to anything else are refused |
| `denied_hosts` | metadata IP and hostname | Refused even if allowlisted |
| `allow_private_ranges` | `true` | Local QA targets; link-local stays blocked |
| `default_timeout_seconds` / `max_timeout_seconds` | 10 / 120 | Per-request timeout and clamp |
| `safe_methods` / `mutating_methods` / `destructive_methods` | GET,HEAD,OPTIONS / POST,PUT,PATCH / DELETE | Method classes |
| `require_approval_for_mutating` / `..._destructive` | false / true | Approval gates |
| `max_retries` | 3 | Cap; a caller cannot ask for more |
| `max_request_bytes` / `max_response_bytes` / `max_body_chars_in_output` | 2 MB / 5 MB / 20k | Size caps |
| `evidence_enabled` | true | Persist request/response evidence |

### `database`

`enforce_read_only: true` blocks DDL/DML at three layers. `statement_timeout_ms`, `max_rows` and
`max_identifier_len` bound every query. Each profile stores `host`, `port`, `database`, `user`,
`password_env`, `read_only` and `redacted_columns`; a password value is never stored. A SQLite file
used for persistence verification must live inside the KHUNKHAAR tree.

### `auth`

Named profiles for test credentials: `{name, env_var, header, scheme}`. The value is resolved from
the environment at request time and never printed. `profiles: []` is the default.

### `browser`

Headless Chromium by default, tracing on, video off, 30 s navigation timeout, 200 kB output cap.
`allowed_origins` is an origin allowlist; non-http(s) schemes are always refused. An empty list
means any http(s) origin, which is why the shipped file lists only loopback origins.

### `filesystem`

`roots` bound every read and write; `deny_names` refuses credential files; `max_read_bytes` and
`tmp_ttl_hours` bound size and lifetime. The MCP servers enforce the same values.

### `security`

`scans_require_authorization: true` and an empty `authorized_targets` mean active scanning is
impossible until a target is listed. `forbid_production_targets` and `production_markers` gate
names that look production. `destructive_approved_targets` is required before a DELETE.
`zap.allow_scan: false` keeps the container scanner off.

### `fuzzing`

`max_cases_per_run: 200`, `max_runtime_seconds: 120`, and an allowlist inherited from
`api.allowed_hosts`.

### `project_discovery`

`max_files_scanned: 40000`, `skip_dirs`, and `secret_value_ingest: false` (names and presence only,
never values).

### `performance`

k6 binary path, `max_vus: 20`, `max_duration_seconds: 120`, and
`forbid_remote_targets_without_approval: true`.

### `metadata_store`

`engine: sqlite`, `path: data/metadata/platform.sqlite3`. The schema is migrated forward on
startup (current version 6).

### `git`

| Key | Default | Meaning |
| --- | --- | --- |
| `read_only_commands` | 15 read subcommands | Intersected with the hard floor |
| `require_registered_project` | true | Refuse an unregistered checkout |
| `verify_index_untouched` | true | Before/after `(porcelain v2, HEAD)` comparison |
| `max_files_in_report` / `max_diff_bytes_per_file` | 2000 / 200 kB | Report caps |
| `ast_symbol_attribution` / `max_symbol_attribution_files` | true / 60 | Python AST attribution |
| `commit_file_lists` / `max_files_per_commit` | true / 40 | Per-commit file lists |
| `base_branch_candidates` | origin/main, main, … | First local candidate wins; else `--base` |

### `impact`

`ast_languages`, `parse_js_ts`, `max_source_bytes`, `max_files`, `max_symbols_per_file`,
`max_graph_nodes`, `max_graph_edges`, `max_depth`, `max_impacted`. `label_as_estimate: true` keeps
the non-probability wording on every risk score.

### `regression`

`require_confirmed_finding: true`, `validation_runs: 2` (clamped to 1–3 by the CLI),
`auto_execute: false`, `quarantine_unreliable: true`.

### `gitlab`

`enabled: false`; when enabled, `host` must also be added to `api.allowed_hosts`. `project`,
`token_env`, `token_header`, `timeout_seconds` and `max_diff_files` configure the read-only
metadata request. No write endpoint exists.

### `planner`

| Key | Default | Meaning |
| --- | --- | --- |
| `require_approval` | true | Actions that need approval are listed in the plan |
| `always_approve_modes` | `[adversarial]` | Modes that always need approval |
| `default_mode` | `controlled` | Used when a command omits `--mode` |
| `respect_target_allowlist` | true | No action against a non-allowlisted target |
| `max_actions` | 80 | Plan cap |
| `api_workers` | 2 | Read-only API mission pool; browser/journey stay serial |
| `require_approval_for_adversarial` / `..._external_target` | true / true | Additional gates |

### `monitoring`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | false | `monitor set` refuses until this is true |
| `install_system_scheduler` | false | Policy flag; no code path installs a scheduler |
| `max_overdue_before_missed` | 2 | Overdue windows before a run is classified missed |
| `schedule_grace_seconds` | 900 | Late-window grace |
| `catch_up` | false | Offline windows stay missed, never silently executed |
| `check_max_seconds` / `check_timeout_seconds` | 600 / 120 | Run and per-check bounds |

### `dashboard`

`host: 127.0.0.1`, `port: 8899`, `require_token: true`,
`bind_non_loopback_requires_flag: true`. `dashboard start` uses the port as its default, binds
loopback unless `--host` and `--allow-non-loopback` are both given, and starts nothing by itself;
`enabled` is a policy marker.

### `notifications`

`terminal: true`; `desktop.enabled: true` with `notify-send` if installed; `email` and `webhook`
are disabled and **not implemented** — enabling one records a `not_sent` delivery instead of
sending. Payloads are restricted to ids, severity, a one-line title, a relative evidence path and
counts.

### `retention`

`evidence_days: 0` and `report_days: 0` disable automatic eligibility. `protect_statuses` keeps
evidence for findings with any protected status. `max_evidence_bytes: 0` means no global cap.
`tmp_ttl_hours: 24` governs disposable work areas. `apply` still requires `--confirm`.

### `telemetry`

`log_tool_calls: true` writes structured JSONL tool-call logs under `logs/`, redacted. There is no
external telemetry.

## Precedence

1. Command-line flags override the corresponding configuration value for that invocation (for
   example `--target`, `--mode`, `--max-actions`, `--port`, `--max-steps`).
2. `config/khunkhaar.yaml` overrides code defaults for keys it defines.
3. Code defaults and safety clamps apply last.

Configuration is read once per process. A running dashboard or MCP server does not notice an edit
until it is restarted; a CLI invocation always starts fresh.

## Changing configuration safely

1. Edit the YAML, keeping credentials as environment-variable names.
2. Run `khunkhaar config show --section <section>` and confirm the effective values.
3. For a new network target, add the host to `api.allowed_hosts` and, if it is not loopback,
   create an expiring grant with `khunkhaar project authorize`.
4. For monitoring or GitLab metadata, enable the section explicitly; both are off by default.
5. Back up the file before a broad change; there is no automatic config rollback.
