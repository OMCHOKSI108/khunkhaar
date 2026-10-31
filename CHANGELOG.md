# Changelog

All notable changes to KHUNKHAAR QA are documented here.
Versions follow the phase plan: Phase 1 = infrastructure foundation (0.1.x), Phase 2 = autonomous
adversarial QA engine (0.2.x), Phase 3 = private, git-aware QA intelligence (0.3.x).

## [0.3.0] — 2026-10-31 — Phase 3 (private, git-aware QA intelligence)

### Added — P0 (platform groundwork)
- Schema 4 migration with `git_analyses`, `evidence_refs`, `authorizations` and retention classes;
  additive only, Phase 1/2 tables untouched.
- Isolation-aware `paths` module (`KHUNKHAAR_ROOT` overrides), evidence index with sha256 records,
  retention plan command and the Phase 3 CLI router.

### Added — P1 (Module A: read-only git intelligence)
- `core/git/runner.py`: every git call goes through a read-only allowlist intersected with a hard
  floor, validated refs/pathspecs, `--no-optional-locks --no-pager`, and a before/after
  `(porcelain v2 status, HEAD)` equality guard that aborts with `StateChangedError` if a read
  changed anything. Credential-bearing remotes are redacted; credential env vars are stripped.
- `core/git/analysis.py`: identity (branch, detached state, paused operation, upstream, ahead/
  behind, commit count), worktree state (staged/unstaged/untracked/conflicted/renames), branch,
  range (`..` and `...` labelled as different claims), commit and working-tree change sets, hunk
  parsing, per-file language, binary detection and pagination.
- Python symbol attribution via stdlib `ast` (`core/git/symbols.py`): added/modified/removed
  definitions with qualified names, decorators counted as part of the definition, innermost
  nesting wins. Files that do not parse are reported as *unknown, not absent*; git's hunk-header
  label is preserved and labelled as a heuristic when parsing is disabled or over budget.
- Commit history detail: author email, trailers (`Signed-off-by`, `Co-authored-by`, generic
  `Token: value` last paragraph), per-commit changed-file lists from one `git log --name-status -M`
  read, per-commit caps, and an explicit note that a merge commit has no file list of its own.
- Commands `khunkhaar git status` and `khunkhaar git analyze
  [--base|--commit|--range|--working-tree|--branch] [--head] [--json] [--save]`, with exit codes
  2 (unregistered/usage), 1 (refused/malformed ref) and 3 (repository changed mid-read).
- Each analysis writes a 0600 JSON artifact under `evidence/<project>/git-analysis/` indexed in
  `evidence_refs` with its sha256, while the `git_analyses` row stores counts only — no repository
  file list or content is copied into the metadata database.

### Validation — P0 + P1 (recorded evidence)
- `pytest tests`: 368 passed (167 Phase 1/2 + 201 Phase 3). `ruff` clean on the new modules;
  `bandit` reports no new findings (three pre-existing Phase 2 items remain, tracked for P8).
  `khunkhaar --doctor`: exit 0, MCP 4/4.
- Repository-untouched guarantee proved through the shipped CLI: sha256 over the index, every
  file's content and mtime, and the reflog is identical before and after `git status` plus four
  analyze modes on a deliberately dirty repository.

### Added — P2 (Module C: dependency graph and change impact)
- Schema 5 migration: `origin` on `impact_nodes`/`impact_edges` plus the project index. Rows are
  stored with a `project_id|node_id` key, so two projects with identical relative paths never
  share a graph row; loading restores local ids.
- `core/impact/extract.py` builds a typed graph from a checkout: Python `ast`, tree-sitter for
  JavaScript/TypeScript/TSX, and schema/config readers (SQL, Prisma, GraphQL, YAML keys). Edges
  carry relation, confidence and origin (`explicit` vs `inferred`); unknown languages are
  reported as unparsed with a reason instead of producing invented edges.
- `core/impact/resolve.py` links the Phase 2 app map: routes→handlers, handlers→ORM entities
  (direct and one call hop), workflows/journeys→triggering APIs, tests→code by naming convention.
- `core/impact/traverse.py` walks the graph in the reverse direction (dependent → dependency) from
  changed files, matched symbols and containing classes; reports API/entity/workflow/journey
  surfaces, reachable tests, missing paths, unparsed seeds and truncation.
- `core/impact/risk.py` explains an approximate 0–100 attention score with six weighted factors
  whose rounded contributions sum exactly to the score. The report says in plain words that this
  is a prioritisation signal, not a proven failure probability.
- `khunkhaar impact [--path] [--depth N] [--limit N] [--rebuild-graph] [--save|--no-save]
  [--json]` reads the latest saved `git analyze` artifact, builds or reuses the project graph,
  and writes a 0600 JSON artifact plus an `impact_reports` row (factors only). `--no-save`
  writes neither the graph nor the report. The command never writes to the repository.
- New `impact` config section: AST languages, source-size and symbol caps, graph node/edge caps,
  default depth, maximum impacted nodes, and the estimate-label switch.

### Validation — P2 (recorded evidence)
- `pytest tests`: 393 passed. `ruff` clean on the new modules and tests; `bandit` reports no
  findings in `core/impact` or `core/commands/impact.py`.
- End-to-end fixture proves file → symbol → API → entity → workflow → test traversal in both
  Python and TypeScript (including a NestJS controller), score contributions summing to the
  score, unparsed-language honesty, project isolation, 0600 artifacts, and a repository state
  digest identical before and after `khunkhaar impact`.

### Added — P3 (Module B: merge-request analysis and optional read-only GitLab)
- `git analyze --mr-source/--mr-target` compares the merge base of two local refs (three-dot) and
  never fetches; a target that only exists as a remote-tracking ref is refused with an
  infrastructure error, not guessed. MR mode cannot be combined with commit/range/worktree modes.
- `core/mr/compose.py` turns the change set, reverse walk, risk factors, Phase 2 findings and
  optional GitLab header into a review: modules, services, API/GraphQL endpoints, models,
  workflows, journeys, security implications, candidate regressions and a recommended QA plan.
  Every recommendation carries `status: recommended - not executed`; the plan is not a result.
- `khunkhaar mr analyze [--path] [--mr-target/--base] [--mr-source/--head] [--gitlab-mr IID]
  [--gitlab-project] [--depth N] [--limit N] [--rebuild-graph] [--save|--no-save] [--json]`
  writes a 0600 review artifact, a `git_analyses` row and an `impact_reports` row. The artifact
  keeps a top-level `changeset`, so `khunkhaar impact` can chain onto the same review.
- `core/integrations/gitlab.py`: GET-only metadata adapter, disabled by default and refused
  unless the host/port is in `api.allowed_hosts`; the token is read from the named environment
  variable only, never persisted, and diff bodies are discarded. A disabled, unauthorised,
  unreachable or 401 GitLab leaves the local review byte-identical and reports `unavailable`.
- New `gitlab` config section and documented `gitlab:` block in `config/khunkhaar.yaml`.

### Validation — P3 (recorded evidence)
- `pytest tests`: 408 passed (393 before P3 + 15 Module B tests). `ruff` clean on the new modules
  and tests; `bandit` reports no findings in `core/mr`, `core/integrations` or
  `core/commands/mr.py`.
- Adapter tests run against a loopback fake GitLab: the token appears only in the request header,
  never in the result or artifact; a 401 becomes `unavailable`; the module contains no write HTTP
  verb. CLI tests prove merge-base isolation, mixed-mode refusal, a missing `origin/main` exiting 1
  with zero rows/findings, an outage leaving the local review identical, and finding → security →
  plan reachability.
- Shipped-CLI smoke under an isolated `KHUNKHAAR_ROOT` on a disposable synthetic repository: the
  review listed only the feature commit, `khunkhaar impact` chained onto the saved artifact, the
  artifact was 0600, a fake `.env` secret was absent, and the index sha256 plus worktree status
  were unchanged after both commands.

### Added — P4 (Modules D + E: regression intelligence and historical knowledge)
- `core/regression/select.py` selects only independently confirmed findings with a machine
  scenario; `likely`, `unverified` and `needs_human` findings are excluded with the reason
  recorded, never silently dropped.
- `core/regression/derive.py` turns a confirmed finding into test intent: a captured journey
  transcript is preferred when the finding links one (steps, actors and expectations only,
  payloads dropped), otherwise the probe kind and invariant are used. `test_id_for` derives a
  deterministic `RT-` id from project, finding and intent.
- `core/regression/generate.py` writes `test_<id>.py` plus a redacted `intent.json` under
  `regression/generated/<project>/<test_id>/`; regeneration is byte-deterministic and preserves
  execution-learned reliability/confidence instead of resetting them.
- `core/regression/runtime.py`, `process.py`, `validate.py`: replay execution, a structured pytest
  subprocess, and validation requiring every normal run to observe the violation plus one inverted
  run that passes. Only then is the test copied to `regression/validated/` with
  `reliability: trusted`. `environment_unavailable` is never a promotion and never a defect;
  `not_reproduced` is never trusted.
- `core/regression/history.py` and `runner.py`: history rows update confidence additively
  (pass +0.03, fail −0.10, infra unchanged) and a budgeted runner classifies pass / fail /
  environment_unavailable / not_run. Exhausted budget is `not_run`, never a failure; quarantined
  tests are not executed. Evidence writes stay 0600 and redacted.
- Commands `khunkhaar regression list|generate|validate|run`: `run --selector impact` reads the
  latest saved git analysis for changed paths and builds an auditable plan; `--show-plan` is a
  true dry run that executes nothing. Every network action preflights `netguard` and requires
  `--target`; a non-loopback target additionally needs an explicit unexpired authorization row
  for that exact target. Exit codes: 1 fail, 2 environment unavailable, 3 not run.
- `core/knowledge/project.py` distils per-project knowledge from the store only — confirmed and
  resolved bugs, recurring patterns, invariants, workflows (payload-free), permission boundaries,
  DB relations, risky modules, edge cases, false-positive patterns and regression coverage.
  Written as 0600 `knowledge.json`/`knowledge.md` and indexed as a project knowledge item.
- `core/knowledge/global.py`: a curated ten-entry technique library with no project identifier,
  repository path, rule text or finding title; stored under `intelligence/global-knowledge/`.
- `core/knowledge/retrieve.py` ranks history against a changeset with additive, auditable weights
  (changed path, same feature, status, severity, transaction integrity, trusted test, rejected
  claim), returns per-item contributions matching the item weight, and orders trusted tests first.
- Commands `khunkhaar knowledge build|show|retrieve`; store helpers `upsert_knowledge_item`,
  `get_knowledge_item`, `list_knowledge_items`.

### Validation — P4 (recorded evidence)
- `pytest tests`: 429 passed (408 before P4 + 21 Module D/E tests). `ruff` clean on the new modules
  and tests; `bandit` reports no findings in `core/regression`, `core/knowledge` or their command
  modules. One earlier full run had a single transient Playwright screenshot failure that passed
  standalone and in the final full run; it is an environment flake, not a regression.
- Module D fixture proves: confirmed-only generation, exclusion reasons for unconfirmed findings,
  journey transcript replay, deterministic regeneration preserving history, promotion only after
  all violating runs plus an inverted pass, `environment_unavailable` neither promoting nor
  lowering confidence, `not_reproduced` never trusted, quarantined tests skipped, and impact
  selection returning the linked test while explaining the others.
- Module E fixture proves: documents hold no payloads, rejected claims keep the recorded reason,
  the global library contains no project id, root, finding title, rule text or repo paths,
  retrieval weights sum to each item weight, transaction integrity changes the ordering, and the
  CLI builds, shows and retrieves.

### Added — P5 (Module F: execution planner with an approval gate)
- Schema 6 migration adds `qa_runs.peak_rss_kb` and `qa_runs.llm_usage`; execution metrics are
  recorded as facts, and `llm_usage` is stored as `none` rather than omitted.
- `core/planning/planner.py` turns the latest saved git analysis, the built impact report, the
  project's ranked knowledge and the confirmed-only regression selection into one reviewable plan.
  Every action carries its agent, target, required mode, cost estimate, risk rationale (impact
  reasons, knowledge weight, selected tests) and approval flag. A mission whose feature the change
  set never reaches is excluded with the reason instead of being scheduled anyway.
- The plan is written as a redacted 0600 artifact and a `qa_plans` row. Its digest is sha256 over
  canonical JSON excluding only volatile keys (`plan_id`, `digest`, `created_at`, `status`,
  `approval`, `artifact`, `saved`, `stored_rows`); loading recomputes and compares it, so an edited
  artifact, forged digest or mismatched row is refused as `plan drifted` rather than executed.
- Approval is explicit and recorded: adversarial actions and non-loopback targets require it, and
  a plan records `approved_at`/`approved_by` in both the document and the database. Lifecycle
  transitions (`draft` → `approved` → `executing` → `completed`/`failed`/`cancelled`/
  `budget_exceeded`) are one-way; a plan that already ran is never executed again.
- `khunkhaar plan`, `plan show`, `plan approve` and `plan list`: build from `--path`/`--target`/
  `--mode`/`--impact-only`, inspect with rationale, approve an exact digest, and list stored plans.
  `khunkhaar qa --plan <id>` executes exactly the stored missions and regression test ids;
  `qa --impact-only` builds and executes an impact-trimmed plan; an empty impact-only plan is saved
  for review and executes nothing. `qa` refuses `--target` alongside a stored plan, and refuses a
  plan whose repository no longer maps to the registered project.
- Shared `core/commands/targets.py` preflight (scheme, allowlist, address class and, for
  non-loopback hosts, an unexpired authorization grant) is now used by `qa` and regression
  execution alike. Adversarial plans against a shared external host additionally require `--yes`.
- Bounded parallelism only for read-only API probes: the planner marks just
  `standalone:auth_boundary` missions `parallel_safe`, and the orchestrator runs consecutive safe
  missions in a pool of `planner.api_workers` (never more than the budget's `max_concurrency`).
  Browser, journey and state-changing missions stay serial, and plan order is preserved.
- Executions record per-agent elapsed time and peak RSS (sampled from the OS, not estimated),
  expose the peak on `qa_runs`, and print `llm_usage: none`. Cancellation and budget exhaustion
  leave a complete `cancelled`/`budget_exceeded` record with the partial evidence already written;
  budget exhaustion never claims a defect.

### Validation — P5 (recorded evidence)
- `pytest tests`: 452 passed (429 before P5 + 23 planner and CLI tests). `ruff` clean on the new
  planner, command and test modules. `bandit` on the P5 surface reports one low-severity B105
  false positive in the pre-existing username normalisation (`"password": ""`), not a credential.
- Planner tests prove: impact/knowledge/regression inputs are merged without executing anything;
  a confirmed finding linked to a changed path yields an impact-derived regression mission while
  unreached baseline missions are excluded with reasons; impact-only without a change set is
  honestly empty; adversarial and external plans require approval with a timestamp; digest
  bookkeeping rejects an edited target and a forged digest; a plan without a target is not
  executable; execution reconstructs exactly the stored mission ids; an empty planned set is not
  replaced by generated missions; cancellation and budget exhaustion record the right status with
  partial evidence and zero confirmed findings; read-only missions use the bounded pool and keep
  order, while a state-changing mission closes a batch.
- CLI tests prove: build/show/approve/list round-trips through the artifact and the database,
  unregistered projects are refused, `qa` refuses a drifted plan with zero runs, a stored plan
  executes once and refuses a second run, an external target needs both an approval grant and
  `--approve`, impact-only plans execute only impact-derived missions, an empty plan executes
  nothing, and cancellation/budget map to plan statuses. Migration tests now derive their expected
  versions from `migrations.LATEST_VERSION`, so schema 6 is covered rather than hardcoded as 5.

### Added — P6 (Modules G + I: local monitoring and notifications)
- `core/monitoring/cron.py`: a dependency-free five-field cron parser with aliases, names,
  lists, ranges, steps, Sunday 0/7 equivalence and the standard "either DOM or DOW" rule. An
  expression with no occurrence within six years is refused at creation instead of never running.
- `core/monitoring/schedules.py`: a schedule is a 0600 JSON artifact plus a queryable SQLite row.
  Reading refuses an artifact whose schedule id, project, expression, mode, target or check list
  disagrees with the store, so hand-editing a definition cannot change what a cron invocation
  executes. Re-setting a schedule preserves stored `users_file`/`db_path` unless replaced
  explicitly. Pause records `paused_at` and never creates a missed run; resume recalculates the
  next window from the current time.
- `core/monitoring/runner.py`: `run-due` compares stored windows with the real clock. A window
  that passed while KHUNKHAAR was not running is written as a `missed` row with no start/finish
  time and never as a successful check; the next window is advanced before any check starts, so a
  crash cannot silently replay it. `catch_up: false` is the default; when enabled, only the latest
  window executes and only up to `max_overdue_before_missed`. A recorded execution for the same
  window is never run twice. Every execution writes `started_at`, `finished_at`, status, note and
  a 0600 evidence artifact under `evidence/<project>/monitoring/`.
- `core/monitoring/checks.py`: `smoke` (GET `/`), `regression` (every runnable generated test) and
  `invariants` (tests linked to documented invariants). Statuses are `passed`, `failed`, `error`,
  `environment_unavailable` or `skipped`; an unavailable target, budget exhaustion or a missing
  test environment is never a defect. A check that fails emits an alert but monitoring never
  creates, updates or confirms a finding. No regression or invariant test means `skipped` with
  "nothing was verified", never `passed`.
- `core/notify/notifier.py`: terminal alerts are structured JSON on stderr (stdout stays clean for
  `--json` command output); desktop delivery uses a fixed `notify-send` argv when enabled, and a
  missing binary is recorded as `unavailable` without crashing the run. Email and webhook cannot
  send: enabling either records `not_sent` instead of making an outbound request. Payloads are
  built from a named-field allowlist (finding id, severity, feature, one-line title, relative
  evidence path, counts) and every string is passed through value redaction.
- `khunkhaar monitor set|doctor|run-due|pause|resume|list|status|history`. `set` refuses while
  `monitoring.enabled` is false and applies the shared outbound preflight before storing a target;
  `doctor` only prints exact cron/systemd-user lines and says KHUNKHAAR installed nothing;
  `status` reports `scheduled`/`due`/`missed` from the real clock; `run-due` exits 1 on a failed
  check, 2 on an error or unavailable environment, 3 when only missed windows were recorded.
- The shared outbound preflight moved from `core/commands/targets.py` to
  `core/shared/target_policy.py`, so monitoring enforces the same scheme, allowlist, address-class
  and authorization-grant rules without importing the command layer.
- Fixed a redaction defect found by the new notification property test: well-known token formats
  (GitHub, Slack, AWS, JWT) were replaced with a URL-shaped suffix that still contained the token
  text. Token patterns are now replaced wholesale; database URLs keep only their scheme.

### Validation — P6 (recorded evidence)
- `pytest tests`: 503 passed (452 before P6 + 51 monitoring/notification tests); `pytest
  tests/phase3 tests/security`: 343 passed. `ruff` clean on the new monitoring, notifier, command,
  redaction and test modules.
- Tests prove: cron aliases/names/ranges/steps, DOM-or-DOW, Sunday 0/7, and malformed or impossible
  expressions refused; a two-hour simulated downtime records four `missed` rows with null
  start/finish and no execution, while a window inside the grace period executes once and a second
  `run-due` does not replay it; a crash after recording yields `idle`, not a false `missed`; pause
  creates no rows and resume recalculates; catch-up executes only the latest window within its
  limit; a non-loopback target without an authorization grant is `environment_unavailable` before
  any check runs, and with a grant it runs; a drifted artifact is refused as `misconfigured`.
  Checks are exercised against the local mock application (200 pass, transport error and 5xx
  classified separately), regression execution is replaced at the subprocess boundary, and no
  check path creates or edits a finding. Notification tests cover terminal structure, absent
  `notify-send`, fixed desktop argv, unsent external channels, unknown kinds/severities, and a
  Hypothesis property that five secret-looking formats never appear in terminal/desktop payloads,
  returned alerts or the alerts table.

### Added — P7 (Module H: private local dashboard)
- New `dashboard/` package (FastAPI + Jinja2, docs/OpenAPI/redoc disabled) that is strictly
  read-only: it imports the metadata store, the findings store and the evidence/report index, and
  has no orchestrator, executor, git or network client import anywhere in its tree.
- `dashboard/auth.py`: a 32-byte token generated with `secrets.token_urlsafe` into
  `data/profiles/dashboard/token` (parent 0700, file 0600, created with `O_EXCL`, symlinks
  refused, shorter existing tokens rejected). Session cookies are `issued_at.HMAC-SHA256(token,
  context:issued_at)`, validated with `hmac.compare_digest`, bounded to 8 hours, and set
  `HttpOnly` + `SameSite=Strict` + `Path=/`. The token value is never placed in a cookie, a page
  or command output.
- `dashboard/data.py` assembles every screen from stored rows only: overview totals (projects,
  executions, confirmed/open/not-reproduced/security findings, regression pass/fail/unavailable,
  false-positive rate), per-project cards with risk trend, agent performance from the latest run
  report, recent executions; project detail (repo/branch/commit from the discovery profile, latest
  change set, impact factors, documented rules, most-depended-on modules, findings, regression
  history, runs, impact trend); execution detail (agents, missions, budget, verification with
  evidence links, findings, screenshots, evidence table, tool health, logs, traces, reports); and
  diff detail (analysis flags, changed files, impacted nodes, selected tests, plan actions,
  outcome). Free text is value-redacted and length-capped; error message text and agent notes are
  counted, never rendered.
- Evidence serving is restricted to indexed rows whose paths resolve inside the platform's own
  `evidence/` or `reports/` trees. Raster images are the only inline type; every other file is
  streamed as an attachment, and text/JSON is read from a byte-capped window and served as
  redacted `text/plain`. HTML is never served as markup.
- Strict response hardening on every response: CSP `default-src 'none'` (inline styles and
  self/data images only), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: no-store`. Templates are vendored; there is no CDN, no JavaScript and no static
  route. Unauthenticated requests (except `/login`) receive a 401 page; logout is POST-only.
- `khunkhaar dashboard start [--port N] [--allow-non-loopback]`: binds `127.0.0.1` by default,
  refuses non-loopback hosts unless the explicit override flag is passed (with a stderr warning),
  validates the port, prints the URL and the token *path* (never the value), and states that the
  dashboard cannot start a scan. Uvicorn runs with warnings only and access logging off.
- Read helpers on the metadata store: `list_qa_runs`, `list_report_refs`, `list_evidence_refs`,
  `get_evidence_ref`, `list_impact_reports` and `regression_stats`, all query-only and bounded.
- `jinja2>=3.1` added to `config/requirements.txt` and pinned `jinja2==3.1.6` in
  `requirements.lock.txt` (MarkupSafe was already pinned).

### Validation — P7 (recorded evidence)
- `pytest tests`: 536 passed (503 before P7 + 33 dashboard tests); `pytest tests/phase3
  tests/security`: 376 passed. `ruff` clean on `dashboard/`, `core/commands/dashboard.py`,
  `core/metadata/store.py` and `tests/phase3/test_dashboard.py`.
- Dashboard tests run against a seeded real store (registered project, runs, findings of every
  status, evidence artifacts, report JSON, git analysis, impact report, approved plan, regression
  test/run) and assert real row values on all four pages. Security coverage: token file mode
  0600/parent 0700 and stable across calls, symlink refusal, wrong-token 401, cookie flags and
  Max-Age, forged/expired session rejection, a spy proving `hmac.compare_digest` is used, 401 on
  every page without a session, logout requiring a session, and no raw token in any response.
- Isolation coverage: evidence paths outside `evidence/`/`reports/` (absolute, traversal) are
  404; image/text/HTML evidence handling is asserted; project A's pages never show project B's
  findings; the route table has no scan/execute endpoint; an AST scan proves no execution import;
  page loads leave findings, evidence files and the repository tree byte-identical. A real
  Playwright run at 390×664 reuses the Phase 2 overflow probe and finds no horizontal overflow
  offender on every page.

### Added — P8 (acceptance, isolation proof and release documentation)
- End-to-end acceptance suite (`tests/phase3/test_acceptance.py`) on disposable Python and
  TypeScript repositories: registration, read-only git analysis, impact and risk, knowledge,
  regression generation and validation, plan build with the approval gate, exact-plan execution,
  independent verification, report generation, dashboard evidence serving and monitoring. The
  Python chain runs 4 planned / 3 executed / 1 skipped missions and confirms 3 findings with 2/2
  reproductions; the TypeScript chain proves analysis without execution (zero runs, plans, tests
  or findings).
- Negative and edge acceptance suite (`tests/phase3/test_acceptance_negative.py`, 5 tests): an
  empty change set produces an empty plan and records no run; a multi-commit rename/delete/binary
  history plus dirty state is reported with `index_untouched: true`; an engine interruption is
  stored as `interrupted` with a partial summary and evidence; the CLI interruption path exits 130
  and closes both the run and plan rows; an unreachable target combined with a missing database
  records infrastructure errors and zero findings.
- Honest interruption semantics: `KeyboardInterrupt`/`asyncio.CancelledError` finish the run row
  as `interrupted`, persist the partial summary and re-raise; the `qa` command records the plan
  status and a defensive run closure if the engine did not.
- Fixture isolation fix: the acceptance app is imported with bytecode writing disabled, so the
  repository under analysis stays byte-identical across a full run (the suite hashes the index,
  every file's content and mtime before and after).
- Release documentation set: architecture, completion report, test results, security review,
  known limitations, CLI guide, dashboard guide and configuration reference, plus README Phase 3
  links.
- `bandit` residuals reviewed and documented rather than suppressed: two medium B608 findings are
  read-only SQL identifier construction (`core/agents/base.py:161`,
  `core/metadata/store.py:435`); no high-severity findings.
- Lint cleanup across 15 files found by the first full `ruff` run, including a real undefined name
  (`Path` in `core/browser/session.py` teardown annotation), unused imports, dead locals, ambiguous
  loop variable names and placeholder-free f-strings. `ruff check core dashboard mcp-servers tools
  scripts tests` is clean.
- Version `0.3.0` in `VERSION`; the CLI prints it via `paths.version()`.

### Validation — P8 (recorded evidence)
- `pytest tests`: 543 passed in 89.69s. `pytest tests/phase3`: 376 passed in 75.75s.
  `tests/phase3/test_acceptance.py`: 2 passed in 19.62s.
  `tests/phase3/test_acceptance_negative.py`: 5 passed in 3.55s.
- `ruff check core dashboard mcp-servers tools scripts tests`: all checks passed.
- `bandit` over 19,818 LOC: 0 high, 2 medium (documented above), 11 low.
- Repository isolation proved by digest equality in the acceptance suite; no company repository
  was used as a fixture.
- Full details: `docs/PHASE3_TEST_RESULTS.md`, `docs/PHASE3_SECURITY_REVIEW.md`,
  `docs/PHASE3_KNOWN_LIMITATIONS.md`.

## [0.2.0] — 2026-10-30 — Phase 2 (autonomous adversarial QA engine)

### Added
- Central orchestrator with eight specialist agents: user simulator, workflow breaker,
  edge-case hunter, security hunter, UX inspector, API hunter, database/concurrency hunter and
  regression intelligence. Agents execute real probes; none are mocked.
- Application intelligence: repo map, routes, controllers, models, DB schema, API operations,
  auth surfaces, jobs, config and tests; explicit and inferred rules with evidence.
- Observe → Think → Act → Verify → Learn exploration with persisted history and anti-loop rules.
- Journey construction and controlled mutations (skip, repeat, reorder, change actor, stale id,
  concurrency) with per-mission budgets and defensible expectations.
- Business-invariant engine with eight rule kinds (authorization, idempotency, workflow,
  validation, concurrency, timezone, atomicity, responsive).
- Independent verifier: fresh clients, two attempts, statuses `confirmed`, `likely`,
  `not_reproducible`, `unverified`, `needs_human`, plus reproduction rates. The discovering agent
  cannot change a finding's status.
- Persistent findings model with severity separate from confidence, append-only event history,
  fingerprint deduplication, and an additive SQLite schema scoped by `project_id`.
- Execution runtime: step/request/browser-session/seconds budgets, cancellation, structured JSONL
  logs, redaction at the persistence boundary, and agents that cannot alter their own permissions.
- CLI commands: `khunkhaar scan|explore|security|api|workflows|verify|report` with mode selection
  (`observe`, `controlled`, `adversarial`), authorization acknowledgement and budget flags.
- Disposable synthetic FastAPI+SQLite application with eight deliberately injected defects;
  the defect list is not provided to the engine, only the documented rules R1–R8.
- Three OpenCode skills under `.opencode/skills/`: `khunkhaar-scan`, `khunkhaar-verify`,
  `khunkhaar-triage`.
- Phase 2 documentation: architecture, agent catalog, testing methodology, security model,
  findings schema, validation report and known limitations.
- 33 Phase 2 tests covering findings, budgets, missions, journeys, redaction, probe registry and
  verifier decisions.

### Changed
- Platform version is now `0.2.0` (`VERSION`, `package.json`).
- API responses stay raw in memory; redaction is applied only when writing evidence, reports and
  logs, so agents can consume tokens, ids and schemas without corruption.
- Operation records carry response content types; auth-boundary probing is limited to JSON
  endpoints, eliminating false positives on HTML pages.
- Concurrency probe drains any open shift before firing parallel requests.
- Responsive measurement uses `documentElement.clientWidth` instead of `innerWidth`, which
  mobile emulation inflates to the fixed content width.
- Mission deduplication keying corrected; regression replays no longer collide with invariant
  probes and use the finding's `id`.
- Browser budgets raised to 4 (controlled) and 8 (adversarial); the validated adversarial run
  used 7.
- CLI summary prints post-verification status counts.

### Validation (recorded evidence)
- Final adversarial run `phase2-20261030-144021`: 22/22 missions executed, 0 skipped, 0 failed;
  9 findings all confirmed 2/2; budget 29/160 steps, 180/400 requests, 7/8 browser sessions,
  7.18 s/900 s. All eight injected defects were found and independently reproduced.
- False-positive reduction demonstrated: an over-broad auth-boundary finding was downgraded to
  `not_reproducible` 0/2 after the probe was tightened; no unverified finding entered the report.
- `pytest tests`: 167 passed (134 Phase 1 + 33 Phase 2). `khunkhaar --doctor`: exit 0, MCP 4/4.
- OpenCode server API lists the three Phase 2 skills for the project directory.

### Known limitations
See `docs/PHASE2_KNOWN_LIMITATIONS.md` (OpenAPI dependency, SQLite-only persistence checks,
single responsive breakpoint, bounded deterministic exploration, grouped journey findings, and
others).

### Deferred to Phase 3
- CI/scheduled execution, PostgreSQL persistence probes, visual regression, multi-viewport browser
  matrices, LLM-guided exploration and issue-tracker export.

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
- axe-core 4.14.0 plus the `browser_a11y_scan` tool (`core/browser/session.py`,
  `mcp-servers/browser/server.py`); violations persisted under `evidence/browser/<run>/a11y/`.
- Hypothesis property suite (7 properties) for the SQL guard, LIMIT handling and redaction.
- MCP stdio boot tests: all four servers started over real JSON-RPC, tool schemas checked.
- k6 smoke script, local nuclei template, Schemathesis run, ZAP baseline wrapper
  (`scripts/zap_baseline.sh`, loopback allowed; remote needs `--allow-remote` and an approved
  host), and `requirements.lock.txt`.
- No-root WebKit dependency vendoring (`scripts/install_webkit_deps.sh`) and axe installer
  (`scripts/install_a11y.sh`).
- Project isolation audit (`scripts/validate_project_isolation.py`).
- Documentation set under `docs/`, including the Phase 1 completion report.

### Changed
- Project IDs now use the first 8 hex of SHA-256 of the absolute root (was SHA-1); registry
  re-run, IDs are stable hashes of each project root.
- MCP server internal names aligned with the OpenCode config (`khunkhaar-database`,
  `khunkhaar-filesystem`), fixing `--doctor` reporting `2/4`.
- `--doctor` now checks axe-core, Hypothesis and Schemathesis; MCP detail lists missing names.
- `tools/api/mock_server.py` returns a documented 400 for malformed/non-object JSON instead of
  500 (Schemathesis surfaced this).
- WebKit system libraries live in each bundle's `sys/lib` because the wrapper overwrites
  `LD_LIBRARY_PATH`.

### Validation (recorded evidence)
- 134 pytest tests pass; `--doctor` exits 0 ("all required tools available", 23 checks).
- bandit 0, semgrep 0, pip-audit clean; Schemathesis 142/142; Hypothesis 7/7;
  k6 490 requests / 0 failures / p95 2.72 ms; nuclei template matched; ZAP 0 failures,
  2 Medium + 5 Low + 4 Informational (missing headers on the mock).
- Isolation audit: 2 profiles, no leaks.
- End-to-end: `opencode run --agent khunkhaar-qa` called `khunkhaar-api.api_request` and got
  `STATUS=200` from the local mock; all four MCP servers report `connected`.

### Preserved (deliberately not changed)
- OpenCode CLI v2.2.0 and its existing `mcp.rea` configuration entry.
- Playwright Python 1.62.0 and the existing Chromium browser cache (no downgrade, no forced upgrade).
- PostgreSQL clusters 16/main (5432) and 17/dev (5440) — Phase 1 tests use a disposable Docker DB.
- The ~50 pre-existing tools under `~/.local` and the nvm global root.

### Deferred to Phase 2 / Phase 3
- Autonomous exploration, intelligent bug discovery, independent verification,
  automatic regression generation. `khunkhaar scan|explore|security|report` are stubs that
  refuse to run and say so.
