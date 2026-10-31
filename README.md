# KHUNKHAAR QA

Centralized, project-agnostic QA infrastructure. Installed at `~/KHUNKHAAR-QA`
and exposed as the `khunkhaar` command from any directory.

```
cd /any/project
khunkhaar
```

Discovery runs first, then OpenCode starts from the project root with the KHUNKHAAR MCP
servers and a project-context prompt. The `khunkhaar-qa` agent is defined as a primary agent:
pick it in the session, or use it non-interactively with
`opencode run --agent khunkhaar-qa` (the global default agent is left untouched).

## What is actually installed (verified)

| Area | Component | Status |
| --- | --- | --- |
| Runtime | Python 3.12.3 in `.venv` (PEP 668 isolated) | ok |
| MCP | Python MCP SDK 2.3.0, 4 stdio servers | ok |
| Interface | OpenCode v2.2.0, agent `khunkhaar-qa` | ok |
| Browser | Playwright 1.62.0: Chromium 151, Firefox 153, WebKit 26.5 | ok |
| Accessibility | axe-core 4.14.0 (`browser_a11y_scan`) | ok |
| API | local mock at `127.0.0.1:8731`, allowlist + destructive gate | ok |
| Database | disposable PostgreSQL 16 on port 5445, read-only role | ok |
| Security | semgrep 1.183.0, bandit 1.9.4, pip-audit 2.10.1, nuclei 3.11.1 | ok |
| Fuzzing | Hypothesis 6.170.13, Schemathesis 4.32.7 | ok |
| Performance | k6 2.3.0 (local smoke, bounded by config) | ok |
| DAST | OWASP ZAP Docker image, on-demand wrapper | image pulled on first use |

`khunkhaar --doctor` is the authority. Last recorded output:
`reports/validation/doctor.txt`.

## Phase 2 — autonomous adversarial QA

Phase 2 adds a real execution engine on top of the Phase 1 toolchain: eight specialist agents,
bounded exploration, journey mutations, invariant probes, an independent verifier and persistent
findings. It is driven by the CLI, not by prompts:

```bash
khunkhaar scan --project-root <repo>                       # discovery, no network
khunkhaar scan --project-root <repo> --target <url> \
  --db <sqlite> --users <roles.json> --mode controlled --yes
khunkhaar verify --project-root <repo> --target <url> --users <roles.json> --yes
khunkhaar report --project-root <repo>
```

Validated end to end against the disposable synthetic app (`tests/fixtures/vulnapp`, loopback
port 8740): 22/22 missions executed, 9 findings all confirmed 2/2, all eight injected defects
found. Reports land under `reports/phase2/<project-id>/`, evidence under
`evidence/phase2/<run-id>/`. OpenCode skills `khunkhaar-scan`, `khunkhaar-verify` and
`khunkhaar-triage` live in `.opencode/skills/`.

Phase 2 documents: [architecture](docs/PHASE2_ARCHITECTURE.md),
[agent catalog](docs/PHASE2_AGENT_CATALOG.md),
[testing methodology](docs/PHASE2_TESTING_METHODOLOGY.md),
[security model](docs/PHASE2_SECURITY_MODEL.md),
[findings schema](docs/PHASE2_FINDINGS_SCHEMA.md),
[validation report](docs/PHASE2_VALIDATION_REPORT.md),
[known limitations](docs/PHASE2_KNOWN_LIMITATIONS.md).

## Test evidence

134 tests pass (`reports/validation/full_suite.txt`):

| Suite | Tests | Evidence |
| --- | --- | --- |
| Browser | 19 | `reports/validation/browser_tests.txt` |
| API tool | 42 | `reports/validation/api_tests.txt` |
| Database tool | 38 | `reports/validation/database_tests.txt` |
| Filesystem/evidence | 24 | `reports/validation/filesystem_tests.txt` |
| MCP stdio boot | 4 | `reports/validation/mcp_stdio_tests.txt` |
| Hypothesis properties | 7 | `reports/security/hypothesis.txt` |
| Phase 2 engine | 33 | `tests/phase2/` (full run: 167 tests pass) |

OpenCode integration evidence: `reports/validation/opencode_mcp_list.txt` (4 servers connected)
and `reports/validation/opencode_e2e.txt` (agent → `khunkhaar-api` → HTTP 200).

Scanner runs: `reports/security/{bandit,semgrep,pip-audit,nuclei,schemathesis}.txt`,
`reports/performance/k6-smoke.txt`.

## Documentation

- [docs/INSTALLATION.md](docs/INSTALLATION.md) — what was installed and how, including the no-root WebKit fix
- [docs/TOOLCHAIN.md](docs/TOOLCHAIN.md) — every tool, version and where it lives
- [docs/OPENCODE_INTEGRATION.md](docs/OPENCODE_INTEGRATION.md) — config, backup, agent selection
- [docs/MCP_ARCHITECTURE.md](docs/MCP_ARCHITECTURE.md) — servers, tools, evidence layout
- [docs/API_TOOL_GUIDE.md](docs/API_TOOL_GUIDE.md) — safe HTTP testing
- [docs/DATABASE_TOOL_GUIDE.md](docs/DATABASE_TOOL_GUIDE.md) — read-only DB context
- [docs/PROJECT_DISCOVERY.md](docs/PROJECT_DISCOVERY.md) — what is scanned and what is never stored
- [docs/SECURITY_AND_PERMISSIONS.md](docs/SECURITY_AND_PERMISSIONS.md) — the permission model
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — real problems hit during installation
- [docs/PHASE1_COMPLETION_REPORT.md](docs/PHASE1_COMPLETION_REPORT.md) — acceptance criteria vs evidence

## Layout

```
bin/            k6, nuclei
config/         khunkhaar.yaml (policy, profiles, limits)
core/           shared code: browser session, DB adapters, discovery, CLI, metadata store
mcp-servers/    api, browser, database, filesystem stdio servers
opt/            vendored runtime assets (axe-core, WebKit libraries)
projects/       registry/<project-id>/project-profile.json (mode 0600)
data/           metadata sqlite, credential profiles (0600), state
evidence/       screenshots, traces, DB snapshots, a11y reports
reports/        validation, security, performance evidence
scripts/        installers and validation helpers
tests/          pytest suites
tools/          local mock API
```

`runtime/tmp` is disposable: `fs_tmp_cleanup` removes files older than the configured TTL.
