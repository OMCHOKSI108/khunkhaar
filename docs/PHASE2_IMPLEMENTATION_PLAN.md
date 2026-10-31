# Phase 2 — Implementation and Compatibility Plan

Status: approved for implementation on 2026-10-30. Phase 1 remains the baseline.
Scope: QA intelligence and autonomous discovery. No CI/CD, scheduler or dashboard (Phase 3).

## 1. What Phase 1 already provides (reused, not rebuilt)

| Capability | Reused for |
| --- | --- |
| `core/browser/session.py` (Playwright async, tracing, video, a11y) | user simulator, explorer, UX inspector |
| `core/shared/http_client.py` + netguard allowlists | API hunter, workflow breaker |
| `core/database/adapters.py` (read-only Postgres, masking) | database-aware verification |
| `core/project_discovery/detector.py` + profiles | project identity and scope |
| `core/metadata/store.py` (SQLite) | findings, missions, invariants, verification history |
| `core/shared/evidence.py`, `redaction.py` | evidence capture and secret safety |
| Four MCP servers + OpenCode config | interactive agent sessions |
| `khunkhaar` launcher and doctor | unchanged entry point |

No Phase 1 file is rewritten. The metadata schema is extended additively (v1 → v2).

## 2. New packages

```
core/runtime/        budget.py, jsonlog.py          execution limits, structured logs
core/intelligence/   mapper.py, rules.py             static application map + rule inventory
core/findings/       models.py, store.py             persistent findings + verification history
core/execution/      api.py, browser.py, db.py       bounded executors shared by agents
core/missions/       models.py, generator.py         mission planning from the app map
core/journeys/       models.py, mutation.py          journey model + controlled mutations
core/exploration/    engine.py                       Observe → Think → Act → Verify → Learn
core/agents/         base.py + 8 specialized agents  discovery responsibilities
core/verification/   verifier.py                     independent reproduction and classification
core/orchestrator/   engine.py                       modes, budgets, run lifecycle
core/reporting/      report.py                       markdown/JSON reports from findings
```

Each agent declares: responsibilities, inputs, outputs, allowed executors, limits, failure
handling and required evidence. Agents never mark their own finding confirmed.

## 3. Data model (SQLite schema v2, additive)

- `app_maps(project_id, generated_at, path, summary_json)`
- `invariants(id, project_id, rule_id, description, source, feature, roles, method, confidence,
  entities, created_at)` — explicit vs inferred separated by `source`/`confidence`.
- `missions(id, project_id, kind, title, target, spec_json, status, created_at)`
- `journeys(id, project_id, name, steps_json, created_at)`
- `findings(id, project_id, fingerprint UNIQUE, feature, category, title, description, severity,
  confidence, reproducibility, status, role, initial_state, steps_json, expected, actual,
  evidence_json, remediation, source_refs_json, created_at, updated_at)`
- `finding_events(id, finding_id, at, actor, action, note, evidence_json)` — full verification
  history; status changes are only allowed through this path.
- `exploration_states(id, run_id, project_id, state_hash, url, actions_json, created_at)`

Severity and confidence are separate columns. Dedupe is by `(project_id, fingerprint)` where the
fingerprint hashes category + feature + normalized title + expected/actual class.

## 4. Execution model

- Modes: `observe` (read-only analysis and safe GET exploration), `controlled` (authorized
  browser/API against a designated test target), `adversarial` (mutation, fuzzing, concurrency),
  each with explicit scope and budgets.
- Budget object enforces max steps, browser sessions, API requests, wall-clock seconds and
  concurrency; exceeding any limit raises and ends the run cleanly with a recorded status.
- Destructive HTTP methods keep the Phase 1 double gate (caller flag + approved host). Database
  access stays read-only; state is created through the application API, not direct writes.
- Non-loopback targets require the host in `security.authorized_targets` in both controlled and
  adversarial modes.

## 5. Discovery and verification flow

1. **Intelligence**: map routes, endpoints, models/tables, auth, specs and `RULE:`/`Business
   rule` lines from docs and comments. Rules are classified by keywords into generic invariant
   types (authorization, validation, workflow order, idempotency, timezone, partial write,
   responsiveness). Unsupported text stays `inferred`/unverified.
2. **Mission generation**: one mission per rule-probe pair and per discovered endpoint family.
3. **Discovery**: the owning agent runs its probe against the app and records a *suspected*
   finding (`status=unverified`) with evidence paths and reproduction steps.
4. **Independent verification**: a separate verifier creates fresh sessions/clients, re-runs the
   probe, compares expected vs actual and classifies: confirmed, likely, unverified,
   not_reproducible, false_positive, needs_human. Success rate over N attempts is recorded.
5. **Persistence/reporting**: findings and events in SQLite; reports are generated from storage,
   never from memory.

## 6. Synthetic application

`tests/fixtures/vulnapp/` is a disposable FastAPI + SQLite application with UI pages and JSON
endpoints. Its `docs/BUSINESS_RULES.md` states eight rules (approval order, validation, ownership,
duplicate submission, clock workflow, UTC boundaries, atomic failure, mobile layout). The agents
receive only the application + its documentation; they do not receive a defect list or fixed
endpoints. Tests assert that the engine discovers and verifies at least the eight violations, and
that a clean control endpoint produces no finding (false-positive check).

The app runs on `127.0.0.1:8740` with a per-run SQLite file under `runtime/tmp/synthetic/`, and
is registered as an allowed browser/API target in config. It is never started automatically for
real projects.

## 7. CLI

New subcommands: `scan`, `explore`, `security`, `api`, `workflows`, `verify`, `report`.
Common flags: `--target URL`, `--project-root PATH`, `--mode`, `--max-steps`,
`--max-requests`, `--timeout`, `--json`, `--yes` (authorization acknowledgement for
adversarial mode on loopback/shared test hosts). Each command exits non-zero on infrastructure
failure and prints what it actually executed. Phase 1 behaviour (`khunkhaar` with no
arguments, `--version`, `--doctor`, `--project-info`) is untouched.

## 8. Compatibility and rollback

- Schema migration is `CREATE TABLE IF NOT EXISTS` + `ALTER`-free; existing rows are untouched.
- New config keys are appended with defaults; missing keys behave as disabled.
- The OpenCode config is not modified in Phase 2.
- Rollback: delete the new tables and packages; Phase 1 continues to function.
- Validation: Phase 1 suite (134 tests) must still pass; new Phase 2 suite runs the synthetic
  app end to end and records evidence under `reports/phase2/`.

## 9. Honest limits (documented up front)

- The "Think" stage is a deterministic novelty/risk policy, optionally augmented by an OpenCode
  LLM session; it is not an unrestricted autonomous LLM loop.
- Probes cover the invariant categories above; unknown business rules surface only as
  `needs_human`.
- Concurrency probes are bounded and run against disposable targets only.
- Phase 3 concerns (scheduling, dashboards, CI/CD, long-term regression automation) are out of
  scope.
