# Phase 2 — Architecture

KHUNKHAAR QA Phase 2 adds an autonomous, adversarial QA engine on top of the Phase 1
infrastructure. It maps an application, derives executable rules from the application's own
artifacts, plans missions, executes them against a real target, and requires a second,
independent execution before any defect is called confirmed.

Phase 1 is reused as-is: Playwright browser sessions, the bounded HTTP client and netguard, the
read-only database readers, the MCP servers, the project registry, the evidence store and the
`khunkhaar` launcher. Nothing in Phase 1 was rebuilt. The additive database migration is
described in [PHASE2_IMPLEMENTATION_PLAN.md](PHASE2_IMPLEMENTATION_PLAN.md).

## 1. Pipeline

```
repository ──▶ detector ──▶ mapper ──▶ rules classifier ──▶ invariants
                                        │
target URL ──▶ OpenAPI/static contract ─┴──▶ mission generator ──▶ orchestrator
                                                                        │
                        ┌───────────────────────────────────────────────┤
                        ▼                                               ▼
             specialist agents (8)                             regression replays
             probes + exploration + journeys
                        │
                        ▼
          suspected findings (status = unverified)
                        │
                        ▼
          independent verifier (fresh clients, 2 attempts)
                        │
        confirmed / likely / not_reproducible / needs_human / unverified
                        │
                        ▼
        findings store (SQLite) ──▶ report (JSON + Markdown)
```

Each stage is a Python module with a narrow contract:

| Stage | Module | Responsibility |
| --- | --- | --- |
| Project detection | `core/project_discovery/detector.py` | identify framework, stack, languages |
| Application mapping | `core/intelligence/mapper.py` | routes, controllers, models, schema, APIs, docs; explicit vs inferred |
| Rule classification | `core/intelligence/rules.py` | classify documented statements into invariant kinds with confidence |
| Invariant generation | `core/intelligence/rules.py` | attach feature, verification method, source line, entities |
| Contract reduction | `core/missions/spec.py` | reduce OpenAPI (or static map) to operations, payload schemas, response types |
| Mission planning | `core/missions/generator.py` | invariant → mission, journeys for feasible features, regression replays |
| Orchestration | `core/orchestrator/engine.py` | budgets, modes, execution, recording, verification, reporting |
| Specialist execution | `core/agents/specialists.py` | eight agents with declared tools and limits |
| Executable checks | `core/agents/probes.py` | probes that derive endpoints and payloads at runtime |
| Exploration | `core/exploration/engine.py` | Observe → Think → Act → Verify → Learn crawl with anti-loop state |
| Journeys | `core/journeys/models.py`, `core/journeys/runner.py` | happy path + controlled mutations |
| Verification | `core/verification/verifier.py` | independent re-execution and classification |
| Persistence | `core/findings/models.py`, `core/findings/store.py` | findings, events, invariants, missions, journeys, app maps |
| Reporting | `core/reporting/report.py` | JSON and Markdown reports from the store |

## 2. Application intelligence

`mapper.build()` walks the repository and produces an `ApplicationMap`:

* **endpoints** — framework-aware extraction (FastAPI/Flask/Express/Django/Next, plus OpenAPI
  documents checked into the repository), each with method, path, source file and line.
* **entities** — `CREATE TABLE` statements, ORM models and migrations.
* **docs** — markdown/requirements/rules documents. Document statements are passed through the
  rules classifier so a rule written in prose becomes an invariant, not just an attachment.
* **rules** — each rule records `rule_id`, text, source `file:line`, kind and confidence.
  Explicit rules (numbered/imperative statements) carry higher confidence than hedged ones.
* **features** — derived from paths and entities; used to bind invariants to surfaces.

No defect is encoded here. A rule states what must hold; execution decides whether it holds.

## 3. Target contract

At run time the orchestrator fetches `/openapi.json` (then `/api/openapi.json`, `/swagger.json`,
`/v3/api-docs`). The contract is reduced to operations with:

* method, path, summary, operation id and parameters,
* resolved JSON request schema (used to build valid and invalid payloads),
* response content types (used to distinguish JSON API endpoints from public HTML pages).

If no contract exists, missions that require one report an infrastructure limitation instead of
guessing silently.

## 4. Missions and modes

A mission is the unit of work: which agent, which probe kind or journey, which feature, which
severity, which mode is required. Modes gate destructive/aggressive checks:

| Mode | Steps | Requests | Browser sessions | Seconds | Mission floor |
| --- | --- | --- | --- | --- | --- |
| `observe` | 0 | 0 | 0 | 120 | read-only planning/exploration |
| `controlled` | 80 | 200 | 4 | 600 | documented-rule probes, standard checks |
| `adversarial` | 160 | 400 | 8 | 900 | concurrency, idempotency, journey mutation |

Budgets are hard ceilings in `core/runtime/budget.py`; every HTTP request, browser session and
agent step is charged before it happens. `Budget.cancel()` stops the run and
`Budget.can_continue()` lets the orchestrator stop planning before a ceiling is hit.

Command → mission filter (`core/orchestrator/engine.py`):

| Command | Missions |
| --- | --- |
| `scan` | static discovery only (no target) |
| `scan --full` | all missions (`all`) |
| `security` | security hunter + unauthenticated boundary |
| `api` | API hunter + validation edge cases |
| `workflows` | workflow breaker + database hunter (defaults to adversarial) |
| `explore` | browser exploration only |
| `verify` | re-run every `unverified` finding |
| `report` | render from the findings store |

## 5. Specialist execution model

Every agent receives a `ProbeContext` (target, base URL, contract, operations, user identities,
database reader, budget, structured log, evidence writer). Probes:

1. select endpoints from the contract by scored keyword matching — never from hardcoded routes;
2. build payloads from the JSON schema (valid or deliberately invalid);
3. execute real requests through the bounded client or the browser;
4. record steps, statuses, database deltas and evidence files;
5. return a `ProbeResult` with `violated`, `expected`, `actual`, `steps`, `evidence` and an
   optional `infrastructure_error`.

An infrastructure error is never a finding. It is logged with a traceback and reduces the
mission to `degraded`.

Detailed agent contracts are in [PHASE2_AGENT_CATALOG.md](PHASE2_AGENT_CATALOG.md).

## 6. Independent verification

The discovering agent can only propose. `Verifier` builds a fresh `ProbeContext` with new
logins/no shared cookies, rebuilds the recorded reproduction (probe or journey mutation) and
executes it twice. Classification:

* 2/2 reproductions → `confirmed`
* 1/2 → `likely`
* 0/2 → `not_reproducible`
* any infrastructure failure and no usable attempt → `unverified`
* no machine-readable reproduction recorded → `needs_human`

Only the verifier writes statuses other than `unverified`; `core/findings/models.py` encodes
this separation. Each transition is appended to `finding_events` with actor, note and evidence
path, so the history is auditable and a regression replay can update the same fingerprint.

## 7. Persistence

The platform SQLite database (`data/metadata/platform.sqlite3`, mode 0600) is extended with
additive tables: `app_maps`, `invariants`, `missions`, `journeys`, `findings`,
`finding_events`, `exploration_states`. Initialization is idempotent; missing columns on an
existing Phase 1 database are added with `ALTER TABLE` after a `PRAGMA table_info` check.

Every read is filtered by `project_id`; the store exposes no cross-project query. Findings are
identified by a fingerprint of project, category, feature, normalised title and rule id, so a
rediscovered defect updates one row and appends a `rediscovered` event.

## 8. Evidence and logs

* Evidence: `evidence/phase2/<run_id>/` (probes, journeys, verification) and
  `evidence/browser/<run>/` (screenshots). Written by `core/shared/evidence.py`, which redacts
  by key/value pattern and sets mode 0600.
* Run logs: `logs/runs/<run_id>.jsonl` — one JSON object per event (discovery, target, mission,
  api call, probe error, verification, budget). Redacted, 0600.
* Reports: `reports/phase2/<project_id>/<run_id>-run.json` and
  `reports/phase2/<project_id>/reports/<stamp>-qa-report.{json,md}`.

## 9. Compatibility with Phase 1

* No Phase 1 entry point changed; `khunkhaar` without a subcommand still discovers and launches
  OpenCode exactly as before.
* The four MCP servers, browser session, HTTP client, netguard, database readers, registry and
  evidence module are reused, not forked.
* The new `core/execution/api.py` is Phase 2-only. It keeps response bodies raw in memory
  because agents need real tokens and ids; every persisted artifact is redacted by the shared
  evidence/log writers. This is deliberate: redacting at the internal boundary corrupted the
  OpenAPI document and login tokens during early validation.