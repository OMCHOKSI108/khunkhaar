# KHUNKHAAR QA — Phase 3 implementation plan

Companion to `PHASE3_AUDIT.md`. This is the plan to approve before code changes. It is written to
reuse the Phase 1/2 architecture rather than replace it, and every workstream ends with executed
tests, not files existing.

Version target: `0.3.0`. Phase 1 (134 tests) and Phase 2 (33 tests) must keep passing at every
step; a re-run of the `vulnapp` scenario is the Phase 2 behavioural control.

## 0. Ground rules for every workstream

- Never write inside a company repository. Output goes only under `~/KHUNKHAAR-QA/`.
- Git access is read-only and enforced in one module: a fixed allowlist of subcommands
  (`rev-parse`, `log`, `diff`, `status --porcelain=v2`, `ls-files`, `ls-tree`, `merge-base`,
  `show`, `show-ref`, `for-each-ref`, `cat-file`, `count-objects`, `config --get`). Anything that
  touches the index, worktree, refs or remotes is refused by code, not by convention.
- No CI, no `.gitlab-ci.yml`, no GitHub Actions, no runners, no webhooks, no git hooks, no bots,
  no auto-commit/push/merge, no source-code modification.
- Every persisted artifact passes through `core/shared/redaction.py` and `evidence.write_json`
  (mode 0600). Agents see raw values in memory only.
- No agent-generated shell command is executed. Executors are typed: git, HTTP, browser, SQLite.
- Each workstream commits to a checklist: new tests, `pytest tests` full green, `--doctor` exit 0,
  audit of what changed in `CHANGELOG.md`.

## 1. Workstream P0 — foundations (no behaviour change)

Goal: make room for Phase 3 without disturbing Phase 1/2.

Changes:

- `core/metadata/store.py` → split schema into `core/metadata/schema/v1.sql, v2.sql, ...` plus a
  `migrate()` that applies unapplied versions in order inside a transaction and records them in
  `schema_version`. Add `khunkhaar --schema-status` output in `--doctor`.
- New `core/metadata/migrations.py` with up-only migrations and a `--dry-run` report.
- New directory tree (created by `core/shared/paths.py` helpers, not by hand):

  ```text
  git-intelligence/{repository-index,branch-analysis,commit-analysis,diff-analysis,impact-reports}
  regression/{generated,validated,history,metadata}
  monitoring/{schedules,executions,alerts}
  intelligence/{global-knowledge,project-knowledge,dependency-graphs,regression-intelligence}
  data/{metadata,project-state,execution-history}
  dashboard/
  ```

- `core/shared/paths.py`: add path functions for every new root, keeping the "no absolute home
  paths in config" rule.
- `config/khunkhaar.yaml`: add `git`, `impact`, `regression`, `monitoring`, `dashboard`,
  `notifications`, `retention` sections; fix stale `project.phase`.
- `core/cli.py`: introduce a router that supports two-token commands
  (`git status`, `git analyze`, `regression list`, `regression run`, `monitor status`,
  `project register`) while keeping every existing Phase 1/2 invocation byte-compatible. Add
  `commands/` package with one module per command group; `main()` delegates.
- Evidence index table `evidence_items` (id, project_id, run_id, kind, path, bytes, sha256,
  created_at) populated by `evidence.write_*`; enables dashboard and retention.
- Retention: `khunkhaar retention plan` (dry run, reports bytes per class) and
  `khunkhaar retention apply --confirm` (deletes only paths under `evidence/`/`runtime/tmp`
  older than configured days and never rows referenced by an open finding).

Tests: migration up/upgrade-from-v1/idempotency, CLI compatibility table (every Phase 1/2 command
still parses), path helpers refuse escapes outside the root, evidence index bookkeeping, retention
dry-run never deletes.

## 2. Workstream P1 — Module A: advanced Git intelligence

New module `core/git/`:

- `repo.py` — identity, current branch, HEAD sha, base/target/source branch resolution, upstream,
  detached-HEAD and merge/rebase/cherry-pick conflict state (read from `.git` metadata files only),
  remote list, worktree root validation against the registered project path.
- `state.py` — porcelain-v2 parse: staged, unstaged, untracked, renamed (`R` with score), deleted,
  conflicted (`U`), ignored summary.
- `history.py` — commit listing, ranges (`base..head`, `base...head`), merge commits, per-commit
  changed files, authors, signed-off/Co-authored metadata, commit message features.
- `diffing.py` — unified diff capture, hunk extraction, and symbol attribution. Python symbols via
  stdlib `ast`; JS/TS via the approved parser (see dependency decision) with a labelled regex
  fallback; added/removed/modified/renamed classification per symbol.
- `model.py` — serialisable `GitSnapshot`, `ChangeSet`, `FileChange`, `SymbolChange`.
- `guard.py` — the read-only subcommand allowlist plus assertions run before/after each analysis:
  `git status --porcelain=v2` and `git rev-parse HEAD` must be identical. If they differ, the run
  is aborted and recorded as an infrastructure error, never as a defect.

Commands:

```bash
khunkhaar git status
khunkhaar git analyze
khunkhaar git analyze --base main
khunkhaar git analyze --base origin/main --head HEAD
khunkhaar git analyze --commit <sha>
khunkhaar git analyze --range <base>..<head>
khunkhaar git analyze --working-tree
khunkhaar git analyze --branch <name>
```

Output: JSON under `git-intelligence/branch-analysis|commit-analysis|diff-analysis/` plus a
terminal summary; every analysis records a `git_analyses` row (project, base, head, files changed,
insertions, deletions, risk later filled in).

Tests (against a synthetic repo built by fixtures, git 2.56.0 available): clean repo, single commit
ahead, multi-commit range, merge commit, staged vs unstaged, untracked, renamed with content
change, deleted file, conflict state, detached HEAD, non-repo directory, repository outside the
registered root (refused), and a test that asserts the working tree and index are untouched.

## 3. Workstream P2 — Module C: change impact analysis

New module `core/impact/`:

- `graph.py` — persistent directed graph, nodes typed `file | symbol | class | service | api |
  entity | workflow | journey | test | config`, edges typed `imports | calls | implements |
  exposes | reads | writes | triggers | tests`. Stored as `impact_nodes` / `impact_edges` tables
  plus a JSON graph under `intelligence/dependency-graphs/<project>.json` for inspection.
- `extract.py` — per-language extraction: Python (stdlib `ast`, plus route decorators recognised by
  the existing Phase 2 mapper), JS/TS (approved parser; recognises NestJS/Express decorators,
  imports, class methods, exported functions), SQL/Prisma/Sequelize/Alembic schema files for
  entities, GraphQL SDL/operations for resolvers.
- `resolve.py` — links symbols to API operations from the Phase 2 app map and OpenAPI contract, and
  entities to database tables from the Phase 2 schema map; unresolved links are recorded as
  `inferred` with confidence.
- `traverse.py` — impact set for a changeset: n-hop neighbourhood, plus reverse reachability to
  journeys and tests.
- `risk.py` — transparent additive score:

  | Factor | Signal |
  | --- | --- |
  | change complexity | hunks, symbols touched, cyclomatic delta, files deleted/renamed |
  | dependency impact | in-degree of impacted nodes, breadth of downstream set |
  | business criticality | journey/workflow reachability, documented-rule linkage (Phase 2 rules) |
  | security sensitivity | auth surface, permission checks, tokens, payment/PII identifiers, writes |
  | historical bug density | confirmed findings per file/symbol from Phase 2 store |
  | test coverage relevance | tests linked to impacted nodes, last-green age |

  Report the score **and** the per-factor contribution, with the mandatory wording "approximate
  prioritisation signal, not a proven probability".

Commands: `khunkhaar impact` (from the last analysis), `khunkhaar impact --rebuild-graph`,
`khunkhaar impact --json`.

Tests: graph build on the Python synthetic fixture resolves file→symbol→api→entity→workflow;
TS fixture graph resolves import/call edges; a change to a service file surfaces its controller,
entity, workflow and linked tests; score explanation sums to the score; unknown language falls back
and is labelled inferred; project isolation (graph of project A never appears in project B query).

## 4. Workstream P3 — Module B: merge-request / pull-request analysis

- `khunkhaar git analyze --base origin/main --head HEAD` produces the effective MR diff using local
  refs only. Also `--mr-source`/`--mr-target` aliases for branch names, and `--merge-base` three-dot
  semantics as default for MR-style comparisons.
- `khunkhaar mr analyze --base <ref> --head <ref>` prints the §4 output: change summary, modified
  modules, affected services/API endpoints/GraphQL operations/database models/business workflows,
  security implications, potential regressions, risk score, recommended QA plan. It is composed from
  Modules A + C plus the Phase 2 findings store — no new network dependency.
- Optional GitLab read-only adapter `core/integrations/gitlab.py`: enabled only when
  `gitlab.enabled: true` and `token_env` names an existing variable; host must be explicitly added
  to `api.allowed_hosts`; only `GET` requests to merge-request metadata/diff endpoints are
  permitted by code. If the API is unavailable (offline, no token, 401), the tool records
  `gitlab: unavailable` and continues with local refs. There is no POST/PUT/PATCH path in the
  module at all.

Tests: MR-style analysis on a synthetic repo with a feature branch; missing remote-tracking ref
handled as infrastructure error (not a defect); GitLab disabled-by-default unit test asserting no
HTTP call is attempted; token read by env-var name only and never persisted; offline run produces
identical local-ref results.

## 5. Workstream P4 — Modules D + E: regression intelligence and historical knowledge

Regression pipeline `core/regression/`:

1. `select.py` — eligible findings: status `confirmed` (2/2) only; `likely`, `unverified` and
   `needs_human` are excluded from generation and listed as excluded with the reason.
2. `derive.py` — convert the reproduction (probe record or journey mutation) into a test intent:
   category, objective, preconditions, required role, steps, expected result, dependencies.
3. `generate.py` — emit real, runnable artifacts into `regression/generated/<project>/<test-id>/`:
   pytest modules for API/DB-invariant/workflow/security checks (reusing `core/execution` clients),
   Playwright specs for browser journeys, and GraphQL operation tests when a schema is mapped.
4. `validate.py` — run the generated test against the target: it must fail while the defect is
   present and pass when the reproduction's expectation is inverted (or after the app is fixed, when
   available). Only validated tests move to `regression/validated/` and gain `reliability: trusted`.
5. `history.py` — every execution appends `regression_runs` (test id, project, commit/base/head,
   environment, duration, result, evidence path) and updates
   `last_success` / `last_failure` / `confidence`.
6. `runner.py` — `khunkhaar regression list|run|run --selector impact --show-plan` executes the
   selection inside its own budget class; failure of infrastructure is recorded as
   `environment_unavailable`, never as an application defect.

Historical knowledge `core/knowledge/`:

- `project.py` — writes per-project knowledge to `intelligence/project-knowledge/<project_id>/`:
  confirmed/resolved bugs, recurring patterns, invariants, workflows, API behaviours, permission
  boundaries, DB relations, risky modules, edge cases, false-positive patterns (with the reason the
  claim was rejected).
- `global.py` — writes only technique-level, non-project knowledge to
  `intelligence/global-knowledge/` (probe patterns, testing strategies). Enforced by a test that
  greps global artifacts for project ids, file paths and rule text.
- `retrieve.py` — given a changeset, returns relevant history (e.g. "this workflow previously failed
  on transaction integrity") and biases mission/test selection. Selection weights are recorded in
  the plan artifact so the reason is auditable.

Tests: confirmed finding → generated pytest module → validated → trusted; `likely` finding is
excluded; execution history survives reruns; selection by impact returns the linked test;
transaction-integrity example from §7 actually changes ordering; global knowledge contains no
project identifiers; regeneration is deterministic for the same finding.

## 6. Workstream P5 — Module F: intelligent QA execution planner

- `core/planning/planner.py` — merges three inputs: impact result, historical knowledge, eligible
  regression tests, and the Phase 2 mission generator. Produces a `QaPlan` artifact
  (`data/project-state/<project>/plan-<id>.json`) listing each proposed action with agent, target,
  mode, expected cost (requests/steps/sessions), risk rationale and whether human approval is
  required.
- `khunkhaar plan` prints and stores the plan. `khunkhaar qa --plan <id>` executes exactly that
  plan; `khunkhaar qa --impact-only` restricts to impact-derived missions.
- Approval: destructive or external actions require `--approve` recorded in the plan with a
  timestamp; a plan whose digest changed is refused (`plan drifted`), which prevents reviewing one
  plan and executing another.
- Parallelism: read-only API probes may run in a bounded thread pool (existing `parallel` client);
  browser missions and journey mutations run serially. Concurrency is a budget dimension, not a
  config free-for-all.
- Resource monitoring: sample RSS and elapsed time per agent, record peak in `qa_runs`; token and
  model usage recorded only when an LLM is used — Phase 2/3 run no LLM in the execution loop, and
  the plan states `llm_usage: none` explicitly instead of inventing numbers.
- Cancellation: existing `Budget.cancel()` extended to git/impact/regression/monitor phases.

Tests: plan-then-execute equivalence (executed mission set == approved plan), drift detection,
`--impact-only` selection, approval requirement for mutating/adversarial actions, cancellation
mid-run leaves a complete `cancelled` record, budget exhaustion produces partial evidence and never
a defect claim.

## 7. Workstream P6 — Modules G + I: monitoring and notifications

- Command name is `monitor` (spec §13), config section stays `monitoring`.
- `khunkhaar monitor set --project <id> --schedule "<cron>" --mode controlled --target <url>
  --checks smoke,regression,invariants` writes a schedule JSON under `monitoring/schedules/`.
  Nothing is installed on the system; `khunkhaar monitor doctor` prints the exact cron or
  systemd-user line the user can add themselves.
- `khunkhaar monitor run-due` executes due schedules, records executions with
  `started_at`/`finished_at`/result/evidence, and marks any past-due window `missed` — the system
  never claims a check ran while the machine was off.
- `pause` / `resume` / `list` / `status` / `history`; `status` answers honestly whether the next run
  is in the future, due or missed.
- Notifications `core/notify/`: terminal (always, structured) and desktop via `notify-send` when
  `notifications.desktop.enabled: true`. Alert classes per §11. Payload built from a redaction
  whitelist (finding id, severity, feature, one-line title, relative evidence path) — no code,
  no secrets, no request bodies. Email/messaging adapters ship disabled and unconfigured; enabling
  one requires explicit user configuration and is out of scope until requested.

Tests: due-window math, missed-run honesty after simulated downtime, pause/resume, notification
payload never contains a secret-looking value (reuse the Hypothesis property suite pattern),
`notify-send` absent → warning recorded, no crash.

## 8. Workstream P7 — Module H: private dashboard

- FastAPI + Jinja (FastAPI and uvicorn already installed) in `dashboard/`, served by
  `khunkhaar dashboard start --port <port>`; bind is hard-coded to `127.0.0.1` and starting with any
  other interface requires an explicit override flag plus a printed warning.
- Auth: token stored in `data/profiles/dashboard/` (mode 0600), generated on first start, required
  by every route except the login page; no cookies without `SameSite=Strict`; no external CDN,
  fonts or scripts — CSS/JS vendored locally.
- Screens: overall (projects, executions, confirmed/open/resolved defects, regression pass/fail,
  security findings, risk trend, agent performance, false-positive rate), project (repository info,
  current branch, last analyzed commit, latest execution, impact summary, findings, regression
  history, critical workflows, risky modules, trends), execution detail (agents, tools, browser/API/
  DB evidence sanitized, logs, screenshots, traces, findings + verification), diff analysis (base,
  head, changed files, impacted dependencies, risk, selected regression tests, plan, outcome).
- Read-only over the SQLite store and evidence index; the dashboard never triggers execution.

Tests: bind is loopback, unauthenticated request is rejected, token comparison is constant-time,
every page renders from real store rows for the `vulnapp` run, evidence links resolve to existing
files, no template emits raw request bodies or secrets, responsive check at 390 px reusing the
Phase 2 responsive probe, and a test that the dashboard cannot start a scan.

## 9. Workstream P8 — acceptance and documentation

Synthetic git repository fixtures under `tests/fixtures/` (created by fixture code with a temp
`git init`, never touching real projects):

- `ts-project` (TypeScript/NestJS-style): known prior defect, changed API validation rule, broken
  permission check, changed DB relationship, existing regression scenario, modified workflow.
- `py-project` (FastAPI-style): same six conditions, reusing the Phase 2 `vulnapp` app as the
  running target so Phase 2 probes apply unchanged.

End-to-end acceptance (the 14 steps of §16), executed and captured:
discover → git diff → changed files → impact map → affected workflows → regression selection →
execute → discover defect → independent verification → generate regression test → rerun it →
evidence-based report → visible in dashboard → project isolation preserved.

Negative/edge scenarios (the second §16 list): no changes, multi-commit branch, staged and
unstaged, deleted and renamed files, GitLab unavailable, fully offline, no QA history, interrupted
execution (SIGINT mid-plan), browser infrastructure failure, missing DB credentials — each must
produce an infrastructure record, not a confirmed defect.

Documentation deliverables:

1. `PHASE3_AUDIT.md` (written) and `PHASE3_IMPLEMENTATION_PLAN.md` (this file)
2. `docs/PHASE3_ARCHITECTURE.md`
3. `docs/PHASE3_COMPLETION_REPORT.md` — with the four-way status split: fully implemented and
   tested / implemented but partially tested / incomplete / not implemented, plus known limitations
4. `docs/PHASE3_TEST_RESULTS.md` — commands, outputs, evidence paths
5. `docs/PHASE3_SECURITY_REVIEW.md` — privacy rules enforced, where in code, and the test that
   proves each
6. Updated CLI documentation (`docs/CLI_GUIDE.md` replacing the Phase 2 command section in README)
7. Updated dashboard documentation (`docs/DASHBOARD_GUIDE.md`)
8. Updated configuration documentation (`docs/CONFIGURATION.md`)
9. `CHANGELOG.md` 0.3.0 entry
10. `docs/PHASE3_KNOWN_LIMITATIONS.md`

## 10. Estimated sequencing

| Order | Workstream | Depends on | Phase 1/2 revalidation |
| --- | --- | --- | --- |
| 1 | P0 foundations | — | full suite + `--doctor` |
| 2 | P1 git engine | P0 | full suite |
| 3 | P2 impact graph | P1 | full suite |
| 4 | P3 MR analysis | P1, P2 | full suite |
| 5 | P4 regression + knowledge | P2, P3 | full suite + `vulnapp` re-run |
| 6 | P5 planner | P4 | full suite + `vulnapp` re-run |
| 7 | P6 monitoring + notifications | P5 | full suite |
| 8 | P7 dashboard | P0–P5 | full suite |
| 9 | P8 acceptance + docs | all | 14-step E2E + negatives + full suite |

Rough effort, dominated by P2, P4 and P7. No workstream is declared complete by file existence;
each is complete only when its tests have been executed and its evidence paths recorded.

## 11. Decisions requested from you before P0 starts

1. **Parser dependency**: approve installing `tree-sitter` + `tree-sitter-python`,
   `tree-sitter-javascript`, `tree-sitter-typescript` into the pinned venv (recorded in
   `requirements.lock.txt`), or start with stdlib `ast` + labelled regex heuristics for JS/TS.
   Recommendation: approve — Module C's value on your TypeScript projects depends on real parsing.
2. **Approval gate strictness**: require `khunkhaar plan --approve` before every `khunkhaar qa`
   run (recommended for company-adjacent repositories), or keep the current `--yes` flag and gate
   only impact-driven/adversarial runs.
3. **Dashboard**: enable by default on a fixed loopback port with token auth, or keep it fully
   opt-in (`khunkhaar dashboard start`) — recommendation: opt-in.
4. **Monitoring installation**: engine-only (`run-due` for the user's own cron/systemd timer,
   nothing installed automatically — recommended), versus me writing but not installing a systemd
   user unit for you to review.
5. **Target repositories for validation**: which local checkouts may be analysed read-only during
   development (for example `~/projects/lw-hub`), and confirmation that analysis output stays in
   `~/KHUNKHAAR-QA` only.
6. **Version**: ship Phase 3 as `0.3.0` with Phase 1/2 behaviour frozen.