# PHASE 1 — ENVIRONMENT AUDIT

Generated: 2026-10-30
Host: Ubuntu 24.04.5 LTS (noble), kernel 7.0.0-42-generic, x86_64
User: omchoksi (member of `sudo` and `docker` groups)

No secrets, keys, tokens, or connection strings are recorded in this report.

---

## 1. Host resources

| Item | Value | Assessment |
|---|---|---|
| CPU | 12 cores | Healthy |
| RAM | 15 GiB total, **~1.3 GiB available** | **Constrained** — see §7 |
| Swap | 7.8 GiB, 5.8 GiB already used | Under pressure |
| Disk (`/home`) | 376G total, **200G free** (47% used), ZFS `rpool/USERDATA` | Healthy |

## 2. Language runtimes

| Dependency | Version | Location | Classification |
|---|---|---|---|
| Python 3 | 3.12.3 | `/usr/bin/python3` | Installed & healthy |
| pip | 24.0 system / 26.2.2 inside fresh venv | system + venv | Installed, **requires venv** (see below) |
| `venv` module | working | stdlib | Installed & healthy |
| pipx | 1.4.3 | | Installed & healthy |
| Node.js | v22.23.4 | `~/.nvm/versions/node/v22.23.4/bin/node` (primary) | Installed & healthy |
| Node.js (secondary) | system `/usr/bin/node` also present | | Config note — nvm entry precedes it on PATH |
| npm | 10.9.9 | prefix `~/.nvm/versions/node/v22.23.4` | Installed & healthy — **global installs need no sudo** |
| Go | 1.27.1 | `/usr/local/go/bin/go` | Installed (enables Go-based tools natively) |
| Java | OpenJDK 21.0.12.1 | | Installed & healthy (prerequisite for OWASP ZAP) |

**PEP 668:** `/usr/lib/python3.12/EXTERNALLY-MANAGED` exists. System-wide `pip install` is blocked.
Decision: all KHUNKHAAR Python tooling is installed into a **pinned, isolated virtualenv** at
`~/KHUNKHAAR-QA/.venv`, which was verified by an actual create + `pip download` round-trip.
Existing `pip --user` packages were left untouched.

## 3. Version control & agent CLI

| Dependency | Version | Location | Classification |
|---|---|---|---|
| Git | 2.56.0 | | Installed & healthy |
| **OpenCode CLI** | **v2.2.0** | `~/.opencode/bin/opencode` | Installed & healthy |
| OpenCode config | `~/.config/opencode/opencode.json` (0600) | | **Installed but requires configuration** — additive only |
| OpenCode service file | `~/.config/opencode/service.json` (contains a `password` key) | | **DO NOT READ OR COPY** — excluded from all KHUNKHAAR context |

Verified OpenCode 2.2.0 MCP schema from the live file (not assumed):

```json
{ "mcp": { "<name>": { "type": "local", "command": ["..."], "enabled": true, "environment": {} } } }
```

One pre-existing server (`rea`) is registered and must be preserved. An OpenCode process is
currently running (listening on `127.0.0.1:49374`), so the config file will be backed up before
any modification and edited additively.

## 4. Containers & databases

| Dependency | Version | Classification |
|---|---|---|
| Docker Engine | 29.9.2 (`/usr/local/bin/docker`) | Installed, healthy, **rootless for this user** — `docker info` succeeds without sudo (14 containers, 21 images) |
| Docker Compose | v5.1.4 | Installed & healthy |
| PostgreSQL server | cluster **16/main on 5432** (online), cluster **17/dev on 5440** (online) | Installed & healthy — **must not be disturbed** |
| `psql` client | 18.6 (PGDG) | Installed & healthy (newer than both servers; wire-compatible) |
| `mysql` client | not found | **Missing** — optional for Phase 1 (PostgreSQL has priority per spec §4.C) |
| `mongosh` | present (`/usr/bin/mongosh`) | Installed — but no MongoDB server listening |
| Python `psycopg2-binary` | 2.9.12 (pip --user) | Installed; will be superseded inside the venv |
| Python `asyncpg` | not installed | **Missing** |
| Python `pymongo` | not installed | **Missing** |
| Redis | listening on 6379 and 6380 | Installed (not required by Phase 1) |

Design decision: Phase 1 database validation will **not** create roles or databases inside the two
live clusters. A disposable PostgreSQL runs in Docker on a free port and is destroyed after the
test, eliminating any risk to existing data.

## 5. Browsers

| Dependency | Status | Classification |
|---|---|---|
| Google Chrome | `/usr/bin/google-chrome` | Installed & healthy |
| Firefox | `/usr/bin/firefox` (snap) + `/snap/bin/firefox` | Installed; snap emits a mount-namespace warning — irrelevant to Playwright's own build |
| WebKit | absent | **Missing** |
| Playwright **Python** | **1.62.0** (`~/.local/bin/playwright`) | Installed & healthy — **do not downgrade** |
| Playwright browsers cached | `chromium-1234`, `chromium_headless_shell-1234`, `ffmpeg-1011` (391 MB) | Chromium ready; **Firefox & WebKit not downloaded** |
| Playwright **Node** | not installed (no global npm copy) | **Missing** |
| Latest upstream Playwright | 1.64.1 (PyPI and npm agree) | **Installed but requires configuration** — pin the Node package to **1.62.0** to match the working Python install rather than upgrade the user's existing stack |

## 6. QA tooling inventory

| Tool | Status | Classification |
|---|---|---|
| Playwright (Python) | 1.62.0 | Already installed and healthy |
| Chromium | downloaded | Already installed and healthy |
| Firefox (Playwright build) | binary not downloaded | Missing → install |
| WebKit (Playwright build) | binary not downloaded | Missing → install; **system libraries may require sudo** |
| HTTPX | 0.28.1 (pip --user) | Already installed and healthy |
| Pydantic | 2.13.4 | Already installed and healthy |
| Requests | 57 user packages present; verify inside venv | Installed but requires configuration |
| Pytest | not in filtered scan | Missing → install |
| Schemathesis | not found | Missing → install |
| Hypothesis | not found | Missing → install |
| FastAPI (mock server for testing) | present in `~/.local/bin` | Already installed and healthy |
| Semgrep | not found | Missing → install via pipx |
| Bandit | not found | Missing → install |
| Dependency vuln scanner | none found | Missing → install (pip-audit) |
| OWASP ZAP | not found; Java 21 available | Missing → install via **Docker image**, on-demand only |
| Nuclei | not found; Go 1.27 available | Missing → install upstream binary to `~/.local/bin` |
| Bruno CLI / Newman | neither present | Missing → select after compatibility check |
| axe-core | not found | Missing → install as Node package |
| k6 | not found | Missing → install upstream binary v2.3.0 to `~/.local/bin` |

## 7. Ports

Observed in use (relevant subset): `53` (systemd-resolved), `80`, `631`, `4000` (litellm),
`5432`, `5433`, `5440`, `6379`, `6380`, `8085`, `16926`, `18789`, `4096`, `49374` (opencode),
plus ephemeral editor ports.

Verified free for KHUNKHAAR use: **8731, 8732, 5445, 55432**.
Allocated: `8731` mock API server, `5445` disposable PostgreSQL (Docker publish).

## 8. Network & installation reachability

| Endpoint | Result |
|---|---|
| `https://pypi.org/simple/` | HTTP 200 |
| `https://registry.npmjs.org/` | HTTP 200 |
| `https://github.com` | HTTP 200 |
| venv create + `pip download mcp==1.0.0` | succeeded (transitive wheels fetched) |
| npm registry version query | succeeded |
| k6 latest release | v2.3.0 |
| Nuclei latest release | v3.11.1 |

## 9. Pre-existing user footprint (preserve)

`~/.local` is **8.2 GB** and already contains a large agent/CLI toolset (`aider`, `claude`,
`codex-*`, `goose`, `cursor-agent`, `fastapi`, `httpx`, `playwright` and ~50 others).
`~/.npm` is 514 MB. Rule adopted: **never reinstall, upgrade, or remove anything under `~/.local`
or the nvm global root as a side effect of KHUNKHAAR setup.** KHUNKHAAR binaries land in
`~/KHUNKHAAR-QA/bin`, its Python in `~/KHUNKHAAR-QA/.venv`, and only `khunkhaar` itself is
symlinked onto PATH.

## 10. Risks and decisions carried into the install plan

1. **Memory pressure (~1.3 GiB free, swap largely consumed).** WebKit, ZAP (JVM), a Docker
   PostgreSQL and k6 are all memory-hungry. Mitigation: run heavy tools sequentially, keep ZAP as
   an on-demand container that is removed after use, and re-check headroom before each of those
   steps. If a step is OOM-killed it will be reported as a failure, not silently skipped.
2. **WebKit system libraries.** `playwright install-deps` requires sudo/apt. Policy: attempt a
   download + launch with no system changes first; if the launch fails, report the exact missing
   packages and stop for approval rather than running `apt` unasked.
3. **Playwright version skew risk.** Node Playwright will be pinned at 1.62.0 to share the
   existing browser cache; upgrading Python Playwright to 1.64.1 was rejected as an unnecessary
   change to a working install.
4. **OpenCode config safety.** Back up `~/.config/opencode/` before the additive edit; verify the
   `rea` server entry survives byte-for-byte afterwards.
5. **No credential ingestion.** `service.json` and any project `.env` values are excluded from
   reports, SQLite metadata, and AI context; only variable *names* and presence are recorded.
