# Toolchain

Every version below was read from the installed binary, not from a package manifest.
`khunkhaar --doctor` re-checks all of them and records the result in the metadata store.

## Runtime and interface

| Tool | Version | Path | Verified by |
| --- | --- | --- | --- |
| Python (venv) | 3.12.3 | `.venv/bin/python` | `python --version` |
| MCP SDK | 2.3.0 | venv | server handshake over stdio |
| Node | v22.23.4 | pre-existing | `node --version` |
| OpenCode | v2.2.0 | `~/.opencode/bin/opencode` | `opencode --version`, `opencode mcp list` |

## Browser stack

| Tool | Version | Notes |
| --- | --- | --- |
| Playwright (Python) | 1.62.0 | async API, persistent session |
| Chromium | 151.0.7922.34 | headless and headed |
| Firefox | 153.0 | headless and headed |
| WebKit | 26.5 | needs the vendored libs (see INSTALLATION) |
| axe-core | 4.14.0 | `opt/axe/axe.min.js`, vendored |

## API, database and evidence

| Tool | Purpose |
| --- | --- |
| FastAPI/uvicorn mock | disposable target at `127.0.0.1:8731` (`tools/api/mock_server.py`) |
| psycopg 3 / asyncpg | PostgreSQL drivers |
| pymysql | MySQL driver (adapter present; no MySQL instance is installed) |
| SQLite (stdlib) | metadata store `data/metadata/platform.sqlite3` (WAL, mode 0600) |

## Security, fuzzing, performance

| Tool | Version | Path | Last run |
| --- | --- | --- | --- |
| bandit | 1.9.4 | `.venv/bin/bandit` | 0 issues, `reports/security/bandit.txt` |
| semgrep | 1.183.0 | `~/.local/bin/semgrep` | 0 findings, `reports/security/semgrep.txt` |
| pip-audit | 2.10.1 | `.venv/bin/pip-audit` | no known vulns, `reports/security/pip-audit.txt` |
| nuclei | v3.11.1 | `bin/nuclei` | local template matched, `reports/security/nuclei.txt` |
| OWASP ZAP | image `ghcr.io/zaproxy/zaproxy:stable` | Docker, on demand | `scripts/zap_baseline.sh` |
| Hypothesis | 6.170.13 | venv | 7 properties passed, `reports/security/hypothesis.txt` |
| Schemathesis | 4.32.7 | venv | 142/142 cases passed, `reports/security/schemathesis.txt` |
| k6 | v2.3.0 | `bin/k6` | 490 requests, 0 failed, `reports/performance/k6-smoke.txt` |

## Test-only components and their cleanup

- **Mock API** — loopback only, in-memory state, no persistence. Stop with `pkill -f mock_server.py`.
- **PostgreSQL container** — disposable; `docker rm -f khunkhaar-test-pg` when no longer needed
  (tests start with a fresh container fixture when it is absent).
- **`runtime/tmp`** — scratch space; `fs_tmp_cleanup` enforces the TTL.

## Deliberate non-installs

- **newman / Bruno CLI** — the API tool already imports OpenAPI and executes requests itself;
  adding a duplicated runner would create a second policy path. Export remains possible via
  `api_discover_openapi` output. Decision recorded here rather than installing an unused tool.
- **Global pip packages** — the host is PEP 668 managed; everything lives in `.venv`.
- **System WebKit libraries** — vendored instead (no sudo); see INSTALLATION.
