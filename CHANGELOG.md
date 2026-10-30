# Changelog

All notable changes to KHUNKHAAR QA are documented here.
Versions follow the phase plan: Phase 1 = infrastructure foundation (0.1.x).

## [0.1.0] — 2026-10-30 — Phase 1 (foundation)

### Added
- Central directory architecture at `~/KHUNKHAAR-QA`.
- Environment audit (`reports/PHASE1_ENVIRONMENT_AUDIT.md`) covering OS, resources, runtimes,
  Docker, databases, browsers, QA tooling, ports and network reachability.
- Isolated, pinned Python virtualenv at `.venv` (required: the host is PEP 668 externally-managed).
- Browser, API, database, security, fuzzing, accessibility and performance tool trees.
- API Call Tool MCP server.
- DB Context Tool MCP server (PostgreSQL-first adapter architecture, read-only by default).
- Browser automation MCP server.
- Filesystem/evidence MCP server.
- `khunkhaar` global launcher with `--version`, `--doctor`, `--help`, `--project-info`.
- Non-destructive project discovery and per-project profiles.
- SQLite platform metadata store (no plaintext credentials).
- Infrastructure validation suite and Phase 1 completion report.

### Preserved (deliberately not changed)
- OpenCode CLI v2.2.0 and its existing `mcp.rea` configuration entry.
- Playwright Python 1.62.0 and the existing Chromium browser cache (no downgrade, no forced upgrade).
- PostgreSQL clusters 16/main (5432) and 17/dev (5440) — Phase 1 tests use a disposable Docker DB.
- The ~50 pre-existing tools under `~/.local` and the nvm global root.

### Deferred to Phase 2 / Phase 3
- Autonomous exploration, intelligent bug discovery, independent verification,
  automatic regression generation. `khunkhaar scan|explore|security|report` are stubs that
  refuse to run and say so.
