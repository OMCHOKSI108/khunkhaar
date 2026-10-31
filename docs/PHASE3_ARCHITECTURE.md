# Phase 3 architecture — private, git-aware QA intelligence

Phase 3 turns the Phase 2 adversarial QA engine into a change-aware local system: it reads git,
maps impact, generates and validates regression tests, proposes an approval-gated plan, executes
that plan within a budget, monitors scheduled checks and serves a read-only dashboard. Nothing in
this phase writes to a repository, a merge request, a pipeline or a shared service.

## Invariants

1. **Read-only git.** Every git invocation goes through `core/git/runner.py`, which intersects the
   configured allowlist with a hard floor of read subcommands, validates refs and pathspecs, adds
   `--no-optional-locks --no-pager`, strips credential-related environment variables and compares
   `(porcelain v2 status, HEAD)` before and after each call. A difference raises
   `StateChangedError` and aborts the analysis.
2. **One project at a time.** Every artifact, finding, plan and run is keyed by `project_id`, the
   slug plus the first eight hex characters of the SHA-256 of the absolute repository root.
3. **No invented evidence.** A failed tool, transport error, missing database, absent browser or
   unresolvable contract is recorded as infrastructure error or `environment_unavailable`. It can
   never become a confirmed finding.
4. **Independent verification.** Only `core/verification/verifier.py`, re-running the reproduction
   outside the discovering agent, moves a finding to `confirmed`. The acceptance chain requires
   two attempts and two reproductions.
5. **Approval before execution.** A plan is an artifact with a digest over its meaning. Executing
   it requires a matching approval when any action needs one; a drifted plan is refused.
6. **Bounded execution.** `controlled` mode defaults to 80 steps, 200 requests, 4 browser sessions
   and 600 seconds; explicit `--max-*` flags replace a default for that run. Budget exhaustion is a
   recorded outcome, not a failure to hide.
7. **No LLM at run time.** Missions, probes and verification are deterministic code. `llm_usage`
   is `none` in every run summary.
8. **Private by default.** The dashboard binds loopback, requires a token, is read-only, and
   serves only files under the evidence and reports roots.

## Component map

| Area | Code | Responsibility |
| --- | --- | --- |
| Platform | `core/shared/paths.py`, `core/shared/config.py`, `core/metadata/` | Root resolution, configuration gates, schema migrations (current version 6), run/report/evidence indexes |
| Project discovery | `core/project_discovery/` | Detect root, languages, frameworks, test tools, API specs, Docker/compose; store a mode-0600 profile; never read `.env` values |
| Git intelligence (A) | `core/git/` | Identity, branch, range/commit/working-tree change sets, renames/deletes, hunk parsing, Python AST symbol attribution, honest heuristic labels |
| Impact graph (C) | `core/impact/` | Build a cross-language dependency graph, resolve changed paths to nodes, traverse impacted tests/workflows/entities, score attention risk |
| MR analysis (B) | `core/mr/`, `core/integrations/gitlab.py` | Local merge-base review by default; optional read-only GitLab metadata (MR title, target, diff stats) when enabled |
| Regression (D) | `core/regression/` | Select confirmed findings, derive intents, generate pytest modules, validate them by normal + inverted runs, replay selected tests |
| History (E) | `core/knowledge/` | Project knowledge document, change retrieval and a global technique library |
| Planner (F) | `core/planning/planner.py` | Build reviewable plans from impact, knowledge, findings and regression; digest; approval gate; lifecycle |
| Orchestrator | `core/orchestrator/engine.py` | Run lifecycle, budgets, mission execution, issue recording, independent verification, partial-summary persistence, interruption handling |
| Monitoring (G) | `core/monitoring/` | Stored cron schedules, honest due/missed arithmetic, smoke/regression checks, alerts |
| Notifications (I) | `core/notify/notifier.py` | Terminal, desktop and optional email/webhook channels with counts and local paths only |
| Dashboard (H) | `dashboard/` | Read-only overview, project, execution and diff pages; token login; safe evidence serving |
| Retention | `core/retention.py` | Index/plan/apply cleanup with protected finding statuses |
| Legacy engine substrate | `core/agents/`, `core/missions/`, `core/execution/`, `core/journeys/` | Deterministic probes, mission generation, API/browser/database executors, journey runner, verifier |

## A Phase 3 cycle

```
git analyze ──> changeset artifact + git_analyses row
      │
      ├──> impact ──> graph artifact + impact report + risk factors
      │
      ├──> knowledge ──> project/global knowledge documents
      │
      └──> regression generate/validate ──> generated pytest + reliability
                     │
plan ────────────────┘
  │   actions: regression replay + impact-derived missions, each with reasons and estimated cost
  │   digest: sha256 over the plan's meaning; approval recorded against that digest
  ▼
qa --plan ──> Orchestrator
  │   budget ──> missions ──> probes ──> issues recorded as unverified
  │                              └──> verifier re-runs eligible issues ──> confirmed/rejected
  │   regression replay ──> pass/fail/environment_unavailable
  ▼
run summary + evidence index + report refs
  │
  ├──> report (JSON + Markdown)
  ├──> dashboard (read-only)
  └──> monitor run-due (checks, alerts, notification)
```

## Storage layout

All paths are resolved from `$KHUNKHAAR_ROOT` (default: installation root). They are mode 0600
where they can contain project facts.

| Path | Contents |
| --- | --- |
| `data/metadata/platform.sqlite3` | Projects, runs, report/evidence indexes, findings, analyses, impact reports, regression tests/runs, plans/actions, authorizations, knowledge, schedules/runs, alerts |
| `projects/registry/<project-id>/project-profile.json` | Discovered profile; variable names only, never values |
| `evidence/<project-id>/` | Git analyses, impact graphs, screenshots, traces, logs, regression artifacts |
| `reports/phase2/<project-id>/` | Run summaries and generated JSON/Markdown reports |
| `intelligence/` | Application maps and rule extracts |
| `runtime/tmp/` | Disposable work areas, TTL-managed |
| `logs/` | Per-run JSONL logs; credential-redacted |
| `data/profiles/dashboard/token` | Dashboard token, generated on first start, mode 0600 |

The repository under analysis is never a storage location. The isolation check in the acceptance
suite hashes the index and every file's content and mtime before and after a full run and requires
equality.

## Execution and verification

`Orchestrator.run()` starts a `qa_runs` row, discovers the project, connects to the target, plans
or reconstructs missions, executes them under a `Budget`, records issues, verifies new findings,
persists a summary and finishes the row. Exceptions and interruptions persist a partial summary:

- `BudgetExceeded` → `cancelled` or `budget_exceeded`
- `KeyboardInterrupt` / `asyncio.CancelledError` → `interrupted`
- any other exception → `failed`

Mission exceptions become infrastructure errors, never findings. A target that cannot be reached
still produces a completed run in which every affected agent reports infrastructure errors and no
finding is confirmed; the negative acceptance suite asserts exactly that.

## Trust boundaries

- **Repository → KHUNKHAAR:** read-only. No checkout, commit, stash, reset or index refresh.
- **KHUNKHAAR → target:** only after the host passes the allowlist and SSRF guards; mutating and
  destructive methods are limited by policy and approval.
- **KHUNKHAAR → database:** read-only connections, statement timeouts, row caps, identifier
  validation and column redaction; a SQLite file outside the KHUNKHAAR tree is refused.
- **KHUNKHAAR → GitLab:** disabled by default. When enabled it performs GET requests against the
  configured host only and never posts, approves, labels or triggers anything.
- **KHUNKHAAR → operator:** CLI output, reports and dashboard. Tokens and `.env` values never
  appear; evidence is served as redacted text or as an image, never as executable markup.
- **Dashboard → platform:** read-only. There is no route that starts a scan, test or browser.
