# Phase 3 completion report

Release: **0.3.0**. Scope: Modules A–K plus monitoring, notifications and the private dashboard,
delivered as workstreams P0–P8. This report separates what was executed and observed from what was
implemented but not exercised against a real external system, what is partial by design, and what
was deliberately not built.

## Four-way status

### 1. Implemented and verified in this environment

| Capability | Evidence |
| --- | --- |
| Schema migration v1 → v6, run/report/evidence indexes | `tests/phase3/test_migrations.py`, `test_evidence_retention.py`; 543-test suite |
| Project registration and profiles without `.env` values | Phase 1/2 tests, isolation audit script |
| Read-only git identity, status, range/commit/working-tree/branch analysis | `tests/phase3/test_git_runner.py`, `test_git_analysis.py`, `test_git_cli.py`; synthetic dirty/multi-commit acceptance |
| Rename/delete/staged/unstaged/untracked facts | `test_acceptance_negative.py::test_multi_commit_rename_delete_and_dirty_state_are_reported` |
| Python AST symbol attribution, heuristic labels for other languages | `test_git_symbols.py`; TypeScript acceptance fixture records `git-hunk-header`/`none` honestly |
| Dependency graph, impacted tests/workflows/entities, missing-node reporting | `test_impact_graph.py`, `test_impact_cli.py`; Python and TypeScript acceptance chains |
| Approximate attention risk with factor contributions and explicit non-probability wording | `test_impact_risk.py`; acceptance asserts contributions sum to score and wording |
| Local merge-request review | `test_mr_cli.py` |
| GitLab outage leaves the local review unchanged | `test_mr_cli.py::test_gitlab_unavailable_leaves_the_local_review_identical` |
| Regression selection/generation/validation/replay | `test_regression.py`, `test_regression_cli.py`; acceptance validates four tests as `trusted` with two normal runs and one inverted run each |
| Knowledge build/show/retrieve | `test_knowledge.py` |
| Plan build/show/approve/list, digest drift refusal, approval gate | `test_planning.py`, `test_planning_cli.py`; acceptance exercises the gate and the refusal |
| Execution within budget with independent verification | `test_acceptance.py` live chain: 4 planned / 3 executed / 1 skipped, 3 confirmed findings, all 2/2 reproductions |
| Interrupted runs recorded as `interrupted` with partial summary | `test_acceptance_negative.py` engine- and CLI-level tests |
| Unreachable target and missing database are infrastructure errors, never findings | `test_acceptance_negative.py::test_an_unreachable_target_and_missing_database_are_infrastructure_errors` |
| Empty change set produces an empty plan and no run | `test_acceptance_negative.py::test_a_repository_with_no_changes_yields_an_empty_plan_and_no_run` |
| Monitoring due/missed arithmetic, checks, alerts, offline classification | `tests/phase3/test_monitoring.py` |
| Retention index/plan/apply with protected statuses | `test_evidence_retention.py` |
| Read-only dashboard, token login, safe evidence serving | `tests/phase3/test_dashboard.py`; acceptance asserts text/plain evidence and 404 for paths outside safe roots |
| Repository isolation across a full run | Acceptance hashes index and working tree before/after; equality required |
| Lint and type-free static checks | `ruff check core dashboard mcp-servers tools scripts tests` passes |

### 2. Implemented but not verified against a real external system

| Capability | Why not verified | What was verified instead |
| --- | --- | --- |
| GitLab merge-request metadata | No GitLab credentials or instance were provided; using one requires explicit authorization | Request construction, ref handling, token redaction and the unreachable-host path are tested with fakes/closed ports |
| Desktop notifications | The validation environment is non-interactive; popping a real notification would be intrusive | Channel selection, availability detection, payload redaction and failure-as-warning behavior are tested |
| Email and webhook notifications | Not implemented in Phase 3; the channels are disabled by default and enabling one records `not_sent` instead of sending | Configuration gates and the recorded `not_sent` delivery are tested; nothing was sent |
| Non-loopback dashboard binding | Deliberately not exposed; token would be the only gate | Loopback ASGI tests plus the refusal/warning logic for non-loopback hosts |
| Real browser execution in the Phase 3 acceptance chain | The chain was narrowed to three API agents so it stays bounded and deterministic; browser smoke tests live in Phase 1/2 suites | Browser toolchain doctor, browser smoke tests and monitoring checks |
| Non-loopback or adversarial live targets | Requires an explicit authorization grant and an operator-controlled host | Preflight refusal paths and authorization-grant tests |
| Postgres persistence checks requiring a password | Credential not present in this environment | SQLite persistence path, DB reader safety guard and missing-database behavior |

### 3. Partial by design

- **TypeScript repositories are analysed, not executed.** The acceptance TypeScript fixture proves
  analysis, impact and risk, and asserts zero QA runs, plans, regression tests and findings.
- **Non-Python symbol attribution is a labelled heuristic** (`git-hunk-header`) or absent (`none`).
- **Risk is an approximate prioritisation score**, not a probability or a prediction.
- **The dashboard is read-only.** Approval and execution stay on the CLI.
- **Monitoring has no daemon.** `khunkhaar monitor doctor` prints scheduler lines; an operator cron
  or systemd timer invokes `khunkhaar monitor run-due`. The platform installs nothing.
- **Regression validation executes pytest subprocesses** against generated tests; only synthetic
  targets were used.
- **The dashboard was exercised through the ASGI test client**, not a manual desktop browser.

### 4. Deliberately not implemented

- Any write to a company GitLab or repository: comments, approvals, labels, reviewer changes,
  commits, pushes, merges or pipeline triggers.
- CI configuration, Git hooks, webhooks, bots or runner installation in any analysed repository.
- Automatic source edits or auto-fix commits.
- Public exposure, multi-user accounts or remote access for the dashboard.
- Email and webhook notification delivery (terminal and desktop only).
- GitHub pull-request API integration (local refs and optional read-only GitLab only).
- Executing JavaScript/TypeScript test suites.
- LLM-driven mission generation or verification.

## Deviations and honest notes

1. The P8 acceptance chain executes the planned regression test and three API missions, not every
   mission the planner can propose. The planner itself is exercised separately with the full set.
2. The Python acceptance fixture is served by importing its `app.py` in-process; bytecode writing is
   disabled so the checkout stays byte-identical. This is a test-harness deviation, not a product
   behavior.
3. The report's four-way split is intentional: a module listed as verified above has a test that
   ran in this environment, not merely a class that exists.
4. `bandit` reports no high-severity issues; two medium B608 findings are SQL identifier
   constructions over read-only connections (one over an internal table constant, one over table
   names read from the target database). They are documented in the security review rather than
   suppressed.

## Release decision

0.3.0 is ready for local, private use on repositories the operator explicitly registers. It is not
a hosted service, does not write to any repository, and makes no claim that an unexecuted path
works. See `PHASE3_KNOWN_LIMITATIONS.md` before enabling monitoring, GitLab metadata or non-loopback
dashboard access.
