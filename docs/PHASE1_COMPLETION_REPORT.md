# Phase 1 — Completion Report

Date: 2026-10-30
Version: 0.1.0
Root: `/home/omchoksi/KHUNKHAAR-QA`
Host: Ubuntu 24.04.5 LTS, kernel 7.0.0-42-generic, x86_64

This report maps each Phase 1 requirement to the evidence that was actually produced on this
machine. Every file below exists in the repository; paths are relative to the KHUNKHAAR root.
Claims without a matching artifact are listed under [Deviations](#deviations-and-limitations)
rather than reported as done.

---

## 1. Environment inspection and installation

| Requirement | Result | Evidence |
| --- | --- | --- |
| Inspect the machine before installing anything | Full audit of OS, CPU/RAM/swap, disk, runtimes, Docker, DB clusters, browsers, QA tools, ports, network | `reports/PHASE1_ENVIRONMENT_AUDIT.md` |
| Do not modify existing working installs | OpenCode config backed up; Playwright Python 1.62.0, both PostgreSQL clusters, `~/.local` tools and the nvm root untouched | audit §3–§4; `~/.config/opencode/opencode.json.bak-20261030-121904` (sha256 `fbd84bd7…f177456`) |
| Isolated Python environment (PEP 668 host) | `.venv` on Python 3.12.3; `requirements.lock.txt` pins the full tree (86 entries) | `.venv/`, `requirements.lock.txt` |
| No application source copied into the central directory | `core/`, `mcp-servers/`, `tests/` are infrastructure code only; project data stays in each project; profiles store metadata and env-var *names* | `docs/PROJECT_DISCOVERY.md`, `scripts/validate_project_isolation.py` |
| No fake agent implementations | `agents/` and `skills/` are intentionally empty; nothing pretends to exist | directory listing |

## 2. Browser toolchain

Installed: Playwright 1.62.0 with Chromium 151.0.7922.34, Firefox 153.0 and WebKit 26.5.

| Requirement | Result | Evidence |
| --- | --- | --- |
| Real page load | Chromium, Firefox and WebKit each load the mock page and read the DOM | `reports/validation/browser_tests.txt` (19 passed) |
| Screenshot | PNG written under `evidence/browser/<run>/` | `test_screenshot_and_trace_are_written_to_evidence` |
| Trace | Playwright trace zip written and re-openable | same test |
| Video | Recording produced when enabled | `test_video_recording_produces_a_file` |
| Console + network inspection | Buffered entries returned through the tool surface | `test_console_and_network_are_captured` |
| Form interaction | Fill, submit, read result | `test_form_flow_fills_submits_and_reads_the_result` |
| Accessibility tree | Landmarks/labels exposed | `test_accessibility_tree_exposes_landmarks_and_labels` |
| Auth storage state | Save and reuse a storage-state file | `test_auth_state_can_be_saved_and_reused` |
| Mobile emulation | iPhone 13 descriptor and custom viewport | `test_mobile_viewport_emulation`, `test_custom_viewport_overrides_desktop_preset` |
| Navigation guard | Non-http(s) schemes and non-allowlisted origins refused | `test_non_http_schemes_are_refused`, `test_origins_outside_the_allowlist_are_refused` |
| axe-core accessibility scan | axe-core 4.14.0 injected, violations persisted | `test_a11y_scan_runs_axe_and_writes_evidence`; `khunkhaar --doctor` reports axe-core 4.14.0 |

WebKit note: the distro-packaged Playwright needs shared libraries that were not present and
would have required `sudo apt install`. They were vendored without root instead — see
`docs/INSTALLATION.md` and `docs/TROUBLESHOOTING.md`.

## 3. API Call Tool

| Requirement | Result | Evidence |
| --- | --- | --- |
| Safe HTTP testing against a local mock | Mock service at `127.0.0.1:8731`; GET/HEAD/OPTIONS/POST/PUT/PATCH/DELETE, upload and form endpoints | `tools/api/mock_server.py`; `reports/validation/api_tests.txt` (42 passed) |
| Domain/target allowlist | Requests refused unless the host is in `api.allowed_hosts`; metadata IPs denied unconditionally | `tests/api/test_api_tool.py` |
| Destructive methods restricted | `DELETE` requires caller `allow_destructive` **and** a host listed in `security.destructive_approved_targets` | same suite |
| Credential redaction | Headers, query parameters, bodies and response bodies redacted structurally and textually | same suite |
| No real production API used for validation | Only the local mock and localhost targets were contacted | config, test fixtures |
| Never auto-use credentials from unrelated files | Tool accepts explicit per-request auth; nothing is discovered from `.env` | `docs/API_TOOL_GUIDE.md` |

## 4. Database Context Tool

| Requirement | Result | Evidence |
| --- | --- | --- |
| Disposable local database | PostgreSQL 16 container on `127.0.0.1:5445`, `khunkhaar_qa` | `docs/DATABASE_TOOL_GUIDE.md`; `reports/validation/doctor.txt` |
| Read-only by default | Dedicated `khunkhaar_ro` role, `default_transaction_read_only`, `SET LOCAL transaction_read_only=on`, `statement_timeout` | `core/database/`, `reports/validation/database_tests.txt` (38 passed) |
| Block DDL/DML in read-only mode | SQL guard rejects forbidden statements, comment-smuggled keywords and stacked statements | `tests/security/test_properties.py` (Hypothesis) |
| No arbitrary SQL from agent input | Only catalog/snapshot helpers and validated `SELECT` with a mandatory row cap | `mcp-servers/database/server.py` |
| No password exposure | Credentials referenced by env-var name (`KHUNKHAAR_TEST_PG_PASSWORD`); never stored in config, SQLite or reports | `docs/SECURITY_AND_PERMISSIONS.md` |
| Sensitive column masking | Configured columns masked on output | `khunkhaar_test_pg` profile in `config/khunkhaar.yaml` |

## 5. MCP servers and OpenCode integration

Four stdio servers, each with a real JSON-RPC boot test:

| Server | Tools | Evidence |
| --- | --- | --- |
| `khunkhaar-browser` | open/close, click, fill, read, screenshot, video, trace, network, console, downloads, storage state, mobile viewport, a11y scan | `reports/validation/mcp_stdio_tests.txt` (4 passed) |
| `khunkhaar-api` | request, batch, sequence, upload, extract, OpenAPI import | same |
| `khunkhaar-database` | profiles, schemas, tables, describe, query, snapshot, explain | same |
| `khunkhaar-filesystem` | evidence/report read-write, listing, cleanup | same |

| Requirement | Result | Evidence |
| --- | --- | --- |
| Do not invent config properties | Schema verified against the installed OpenCode file/binary before writing anything | `reports/PHASE1_ENVIRONMENT_AUDIT.md` §3 |
| Additive, reversible configuration | Original file preserved byte-for-byte as `opencode.json.bak-20261030-121904`; the pre-existing `rea` entry is kept | sha256 `fbd84bd7…f177456` |
| Servers actually connect | `opencode mcp list` from a project shows all four `connected` | `reports/validation/opencode_mcp_list.txt` |
| Agent can really call a tool through OpenCode | End-to-end run of `khunkhaar-qa` against the local mock returned `STATUS=200 SERVICE=khunkhaar-mock` | `reports/validation/opencode_e2e.txt` |
| No secrets logged | Config contains paths only; no tokens or connection strings | `docs/OPENCODE_INTEGRATION.md` |

## 6. Project discovery, metadata and isolation

| Requirement | Result | Evidence |
| --- | --- | --- |
| Recognise an arbitrary project | Detects root, type, languages, frameworks, Docker/compose, databases, test frameworks, API specs | `reports/validation/cli_checks.txt` (reference web-app: PHP/Laravel, 651 files) |
| Store only variable names, never values | 53 env-var names recorded for the reference project, values never read | same file; `docs/PROJECT_DISCOVERY.md` |
| Metadata store without plaintext credentials | SQLite schema v1; 2 projects, 0 runs; no secret material | `khunkhaar --doctor` output |
| Per-project isolation | Profiles are mode `0600`; audit finds no `.env` value stored in either profile and no cross-project bleed | `reports/validation/project_isolation.txt` ("2 profile(s) checked; no leaks") |
| Stable project identity | Slug + first 8 hex of SHA-256 of the absolute root | `core/project_discovery/detector.py` |

## 7. CLI surface

| Command | Behaviour | Evidence |
| --- | --- | --- |
| `khunkhaar` | discovers the current project and launches OpenCode with the KHUNKHAAR prompt from the project root | `reports/validation/launch_checks.txt` |
| `khunkhaar --version` | `khunkhaar 0.1.0` | `reports/validation/cli_checks.txt` |
| `khunkhaar --doctor` | 23 checks; exits 0 with "all required tools available" | `reports/validation/doctor.txt` |
| `khunkhaar --project-info` | human and `--json` output | `reports/validation/cli_checks.txt` |
| `khunkhaar scan/explore/security/report` | refuse with exit 3 and an explicit "not implemented in Phase 1" message | `reports/validation/launch_checks.txt` |
| Outside a project | refuses with a clear message and exit 2 | `reports/validation/launch_checks.txt` |
| No hardcoded home directory | Root resolved from `$KHUNKHAAR_ROOT` or the install location | `core/shared/paths.py` |

## 8. Security, fuzzing, performance and DAST

All runs targeted the local mock only.

| Tool | Result | Evidence |
| --- | --- | --- |
| bandit 1.9.4 | 0 issues (3416 lines; 11 reviewed `# nosec` suppressions) | `reports/security/bandit.txt` |
| semgrep 1.183.0 (`p/security-audit`) | 0 findings | `reports/security/semgrep.txt` |
| pip-audit 2.10.1 | no known vulnerabilities in the venv | `reports/security/pip-audit.txt` |
| Hypothesis 6.170.13 | 7 properties (forbidden statements, smuggling, stacking, LIMIT handling, redaction) | `reports/security/hypothesis.txt` |
| Schemathesis 4.32.7 | 142 generated cases, 142 passed / 11 operations | `reports/security/schemathesis.txt` |
| nuclei 3.11.1 | local templates only (`-duc -ni`), health check matched | `reports/security/nuclei.txt` |
| k6 2.3.0 | 490 requests, 0 failures, p95 2.72 ms against thresholds | `reports/performance/k6-smoke.txt` |
| OWASP ZAP baseline (Docker) | 0 failures; 2 Medium, 5 Low, 4 Informational alerts (missing security headers on the mock) | `reports/security/zap-baseline.txt`, `reports/security/zap/20261030-071956/` |

## 9. Automated tests and evidence retention

`pytest` suite: **134 passed** (`reports/validation/full_suite.txt`).

| Suite | Tests | Evidence |
| --- | --- | --- |
| Browser | 19 | `reports/validation/browser_tests.txt` |
| API tool | 42 | `reports/validation/api_tests.txt` |
| Database tool | 38 | `reports/validation/database_tests.txt` |
| Filesystem/evidence | 24 | `reports/validation/filesystem_tests.txt` |
| MCP stdio boot | 4 | `reports/validation/mcp_stdio_tests.txt` |
| Security properties | 7 | `reports/security/hypothesis.txt` |

Run archives live under `evidence/` with a cleanup policy for `runtime/tmp`.

## 10. Definition of Done

> `cd <any project>; khunkhaar` recognises the project and launches OpenCode with browser, API
> and database infrastructure installed, tested and available.

Verified with a real web-app project:

1. `khunkhaar` detects `webapp-92cf5dbe` and prints the resolved launch command
   (`reports/validation/launch_checks.txt`).
2. OpenCode loads all four KHUNKHAAR MCP servers and reports them `connected`
   (`reports/validation/opencode_mcp_list.txt`).
3. A real agent session called `khunkhaar-api.api_request` and returned
   `STATUS=200 SERVICE=khunkhaar-mock` (`reports/validation/opencode_e2e.txt`).

## Deviations and limitations

Recorded so nothing is overstated.

1. **WebKit libraries are vendored, not installed system-wide.** The distro Playwright WebKit
   build needs `libavif`/`libwoff` and friends. Installing them needed `sudo`, so the packages
   were extracted with `apt-get download` + `dpkg-deb -x` into
   `opt/` and copied into each WebKit bundle's `sys/lib`. WebKit now launches (26.5). A future
   `sudo apt install` would replace this cleanly.
2. **ZAP is not pre-installed.** The Docker image is pulled on first use (currently
   `ghcr.io/zaproxy/zaproxy:stable`, digest `sha256:030d3df5…384238`). Non-loopback targets
   additionally require `--allow-remote` and an approved host.
3. **The OpenCode TUI has no `--agent` flag.** `khunkhaar` launches the TUI with the project
   context prompt and all MCP servers available; `khunkhaar-qa` is a primary agent and can be
   selected in the session or invoked with `opencode run --agent khunkhaar-qa`. The global
   default agent was deliberately **not** changed, because that would alter the user's
   behaviour in unrelated OpenCode sessions.
4. **`opencode mcp list --standalone` can print "No MCP servers configured" on a cold start**
   even though the private server does spawn the servers (visible in its own log). Querying the
   warmed background service reports the real state; the end-to-end run is the authoritative
   check. Documented in `docs/TROUBLESHOOTING.md`.
5. **Postman/newman and Bruno were not installed.** Phase 1 API coverage uses the API Call Tool,
   Schemathesis and k6; newman would duplicate the HTTP layer without adding Phase 1 value.
6. **`postgres-test-profile` shows `needs-env` in non-interactive shells.** The doctor cannot
   see `KHUNKHAAR_TEST_PG_PASSWORD` unless `khunkhaar` loads the profile; this is expected and
   does not fail the doctor.
7. **ZAP alerts are real but out of scope for Phase 1.** The missing headers belong to the mock
   service, which exists to exercise the scanners; they are recorded, not suppressed.
8. **The pre-existing `rea` MCP server intermittently logs a transport error** from its own npm
   dependency (`prebuild-install` deprecation). It is unrelated to KHUNKHAAR and was left as is.
9. **Project registry currently holds two projects** (a web-app and a code-audit project). Other
   projects are discovered on first launch; nothing is pre-registered.

## Reproducing the evidence

```bash
cd ~/KHUNKHAAR-QA
.venv/bin/pytest                                    # 134 tests
khunkhaar --doctor                                  # tool matrix
.venv/bin/python scripts/validate_project_isolation.py
```

The mock API (`tools/api/mock_server.py`, port 8731) and the disposable PostgreSQL container
(port 5445) must be running for the browser/API/database suites; both are local-only and were
started manually for the recorded runs.
