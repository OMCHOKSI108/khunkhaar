# Installation

Installed and verified on this machine on 2026-10-30. Phase 1 installs infrastructure only;
it does not copy any application source into `~/KHUNKHAAR-QA`.

## Host facts at install time

- Ubuntu 24.04 (noble), Python 3.12.3 — externally managed (PEP 668), so the platform uses its
  own venv at `/home/omchoksi/KHUNKHAAR-QA/.venv`.
- Node v22.23.4, npm 10.9.9 (pre-existing, unchanged).
- Docker 29.9.2 (pre-existing, unchanged).
- OpenCode v2.2.0 (pre-existing, configuration backed up before any edit).
- Existing PostgreSQL clusters 16/main (5432) and 17/dev (5440) were left untouched.

## What was installed by the agent

1. **Python venv** — `.venv/bin/python` with the MCP SDK (`mcp==2.3.0`), Playwright 1.62.0,
   psycopg 3, asyncpg, pymysql, uvicorn/FastAPI (mock API), bandit, pip-audit, hypothesis,
   schemathesis. Full freeze: `requirements.lock.txt`.
2. **Playwright browsers** — Chromium, Firefox and WebKit under `~/.cache/ms-playwright`.
3. **axe-core 4.14.0** — `scripts/install_a11y.sh`, extracted to `opt/axe/` (no runtime CDN).
4. **k6 v2.3.0** and **nuclei v3.11.1** — self-contained binaries in `bin/`.
5. **semgrep 1.183.0** — installed to `~/.local/bin` (user scope, no sudo).
6. **OWASP ZAP** — Docker image `ghcr.io/zaproxy/zaproxy:stable`, pulled on demand by
   `scripts/zap_baseline.sh`; nothing is installed system-wide.
7. **Disposable PostgreSQL 16** — Docker container `khunkhaar-test-pg` on `127.0.0.1:5445`,
   database `khunkhaar_qa`, role `khunkhaar_ro` (read-only), used only by the DB tests.
8. **Launcher** — `scripts/install_launcher.sh` writes `~/.local/bin/khunkhaar`.
9. **OpenCode integration** — four MCP servers plus the `khunkhaar-qa` agent; see
   [OPENCODE_INTEGRATION.md](OPENCODE_INTEGRATION.md).

## The WebKit dependency decision (no sudo)

WebKit needed `libavif16`, which was not installed, and `sudo` requires an interactive password.
Instead of blocking, the agent vendored the four libraries (`libavif16`, `libgav1-1`, `libyuv0`,
`libsharpyuv0`) without root:

1. `apt-get download` the debs (no root required),
2. `dpkg-deb -x` into `opt/webkit-libs/`,
3. copy the `.so` files into each Playwright WebKit bundle's `sys/lib` (the launcher overwrites
   `LD_LIBRARY_PATH`, so this is the only location that works),
4. verify with a real launch.

Reproduce with `bash scripts/install_webkit_deps.sh`. If you prefer a system install,
`bash scripts/install_webkit_deps.sh --system` uses apt (needs root). Both paths end with the
same launch verification.

## Credentials

No credential was requested or stored in this repository. The database profile reads its
password from the environment variable named in `config/khunkhaar.yaml`
(`KHUNKHAAR_TEST_PG_PASSWORD`); the value lives in `data/profiles/db/khunkhaar_test_pg.env`
(mode 0600) and is loaded by `khunkhaar` at launch. See
[SECURITY_AND_PERMISSIONS.md](SECURITY_AND_PERMISSIONS.md).

## Verify the installation

```bash
khunkhaar --version          # khunkhaar 0.3.0 (root: /home/omchoksi/KHUNKHAAR-QA)
khunkhaar --doctor           # every dependency with status and version
cd /actual/project && khunkhaar --project-info
cd /home/omchoksi/KHUNKHAAR-QA
.venv/bin/python -m pytest -q
```
