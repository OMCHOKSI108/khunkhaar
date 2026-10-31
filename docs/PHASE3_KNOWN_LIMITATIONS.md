# Phase 3 known limitations

Every item below is a boundary of what was built or verified, not a promise of future work. Read it
before relying on an analysis, enabling an integration or exposing the dashboard.

Companion documents: [architecture](PHASE3_ARCHITECTURE.md),
[completion report](PHASE3_COMPLETION_REPORT.md), [test results](PHASE3_TEST_RESULTS.md),
[security review](PHASE3_SECURITY_REVIEW.md).

## Environment and platform

- Developed and verified on Linux with Python 3.12 and SQLite. Windows and macOS were not tested.
- The platform has no installer, service unit or package. It runs from this checkout with
  `.venv/bin/python`; an operator schedules monitoring with their own cron or systemd timer.
- The metadata store is SQLite in WAL mode with a single-writer model. Concurrent long-running
  commands against the same root are not coordinated beyond SQLite's own locking.
- Evidence, reports and the metadata database are protected by file permissions, not encryption at
  rest.

## Git intelligence

- Only the read-only commands in `git.read_only_commands` are available. There is no write path,
  by design, so no operation that would require one (for example applying a patch) exists.
- Base-branch detection tries `git.base_branch_candidates` in order and falls back to requiring
  `--base`; a repository with an unusual default branch needs the flag.
- Symbol attribution is AST-based for Python and heuristic for JavaScript/TypeScript
  (`git-hunk-header`) or absent (`none`). The heuristic labels itself; it is not a symbol-precise
  mapping.
- Analysis caps apply: `max_files_in_report`, `max_diff_bytes_per_file`,
  `max_symbol_attribution_files`, `max_files_per_commit`, `max_graph_nodes`, `max_graph_edges`,
  `max_impacted` and `max_depth`. Hitting a cap is reported as a floor, not silently expanded.
- Working-tree analysis reports staged, unstaged and untracked files by path; it does not read the
  content of untracked files for symbol attribution.
- Binary diffs are reported as binary; no textual symbol analysis is attempted.
- Submodule contents and Git LFS payloads are not analysed.
- A repository whose state changes during a read aborts the analysis with `StateChangedError`.
  Long builds or editors writing into the checkout can trigger this; rerun when the tree is quiet.

## Impact graph and risk

- The graph is built from imports, `require`/`import` statements, test layout conventions and
  framework markers. Dynamic imports, dependency injection, runtime routing tables and generated
  code are not resolved. Missing nodes are listed rather than guessed.
- TypeScript is parsed when tree-sitter is available (`impact.parse_js_ts: true`); other languages
  fall back to the import/require resolver with lower confidence.
- Impact traversal is depth- and count-bounded. `max_impacted` being reached means the list is a
  floor.
- The risk score is an approximate prioritisation signal with explicit factor contributions. It is
  **not** a probability of failure and must not be presented as one. An empty change set can still
  score above zero because the coverage-gap factor contributes.
- The graph is rebuilt on demand within the configured caps; there is no incremental index or
  background refresh.

## Merge-request review

- Local refs are the only verified path. The optional GitLab adapter was tested with fakes and a
  closed port; it has not been executed against a real GitLab instance in this environment.
- The adapter reads merge-request metadata and diff identities only. It does not read discussions,
  approvals, pipelines or CI logs, and it never writes.
- A GitLab outage degrades the review to the local comparison; the response is unchanged and the
  failure is recorded.
- GitHub pull requests are not supported; there is no GitHub API client.

## Regression generation and replay

- Only findings with status `confirmed` and a machine-checkable scenario are eligible
  (`regression.require_confirmed_finding: true`). A confirmed finding without a stored scenario is
  reported as ineligible, not converted into a guessed test.
- Generated tests are Python `pytest` modules. JavaScript and TypeScript test suites are not
  generated or executed.
- Validation is empirical: two normal runs must reproduce the defect and one inverted run must not.
  A test that does not meet that bar is marked unreliable and, by default, quarantined. Flaky
  environments can therefore produce `unreliable` rather than a false claim of reliability.
- Validation and replay use synthetic targets and a disposable SQLite database in this
  environment. The Postgres persistence path requires credentials that were not available, so it
  is implemented but unverified here.
- Generated tests run as subprocesses with the operator's environment and are not OS-sandboxed.
- The test module imports the application under test over HTTP; it does not import the repository's
  source into the KHUNKHAAR process.

## Planning and execution

- The planner selects from existing missions, impacted surfaces and validated regression tests. It
  does not invent new mission types or use an LLM.
- `--impact-only` can legitimately produce an empty plan. That is reported and saved; no run is
  fabricated.
- A plan's digest covers its meaning. Editing the stored document changes the digest and execution
  is refused until the plan is rebuilt and re-approved.
- Actions that require approval are listed in the plan. Execution without a recorded approval is
  refused unless `--approve` records it in the same invocation.
- Non-loopback targets require an unexpired `khunkhaar project authorize` grant for the exact
  target; adversarial mode additionally requires `--yes`.
- Budgets are hard stops: `observe` defaults to 0 steps / 0 requests / 0 browser sessions / 120 s;
  `controlled` to 80 / 200 / 4 / 600 s; `adversarial` to 160 / 400 / 8 / 900 s. Explicit
  `--max-*` flags replace a default for that run, including upward, because the operator owns the
  command line; they are not clamped to the mode default. Exhaustion is recorded as `cancelled` or
  `budget_exceeded`, not as success.
- Browser missions remain serial and are not part of the Phase 3 acceptance chain; that chain was
  deliberately narrowed to three API missions plus regression replay.
- Verification re-runs a reproduction outside the discovering agent. An unreachable target yields
  `environment_unavailable`, never `confirmed`.
- An interrupted run is recorded as `interrupted` with a partial summary. The process is not
  resumed; rerun the command.

## Monitoring and notifications

- There is no daemon and no installed scheduler. `monitor run-due` must be invoked by the
  operator's scheduler; `monitor doctor` prints the lines to add.
- `catch_up: false`: a window missed while the machine was off is recorded as `missed`. It is never
  reported as a successful check, and the machine being offline is never a defect.
- A clock jump can move windows; the schedule stores a timezone offset captured at creation time,
  not a timezone database.
- Checks are limited to `smoke`, `regression` and `invariants`, within `check_max_seconds` and
  `check_timeout_seconds`.
- Terminal notifications are always attempted unless disabled. Desktop notifications require
  `notify-send`; an absent binary records a warning. Email and webhook channels are **not
  implemented**: enabling one records `not_sent` rather than making an outbound request.
- No notification contains source code, request bodies, headers, credentials, target URLs or
  database values.

## Dashboard

- Read-only. There is no route to build a plan, approve one, start a run or open a browser; those
  remain CLI operations.
- Single shared token, no user accounts, no per-user audit trail beyond the request-independent
  pages.
- Loopback only by default. A non-loopback bind requires `--allow-non-loopback` and is not
  recommended; the session cookie is not `Secure` because the default transport is plain HTTP on
  loopback.
- Verified through the ASGI test client. It was not manually exercised in a desktop browser in this
  environment.
- Evidence text is truncated and redacted; images are served inline; other file types are offered
  as an octet-stream attachment. No file outside `evidence/` and `reports/` is reachable.
- The dashboard reflects the metadata store at request time; there is no live update or push.

## Retention and evidence

- `retention apply` deletes nothing without `--confirm`. Default windows are `0` (disabled), so an
  unconfigured installation never deletes evidence.
- Evidence attached to a finding with a protected status is protected regardless of age.
- The index is derived from disk and can be rebuilt; deleting files outside the retention policy
  invalidates `evidence_refs` until `retention index --rebuild` runs.
- There is no global size cap by default (`max_evidence_bytes: 0`).

## Scale

- Caps are tuned for service-sized repositories on a developer machine, not monorepos with hundreds
  of thousands of files. Raising them increases runtime and memory; the resource metrics in a run
  summary show what a run consumed.
- No distributed execution, remote workers or parallel repositories. One project is analysed at a
  time.

## External paths not verified

| Path | Status |
| --- | --- |
| Real GitLab merge-request metadata | Implemented, fake-tested, never executed against an instance |
| Desktop notification delivery | Fake-tested; not popped in this non-interactive environment |
| Email / webhook notifications | Not implemented; `not_sent` recorded |
| Non-loopback dashboard bind | Refusal and warning tested; not exposed |
| Postgres persistence checks | Implemented; credentials unavailable, so unverified |
| Real browser missions in the Phase 3 chain | Covered by Phase 1/2 browser tests and the toolchain doctor |
| Adversarial testing against a shared host | Refused without `--yes`; not executed |

## Deliberately out of scope

Writes to any GitLab or repository, CI configuration, hooks, webhooks, bots, automatic source
edits, public or multi-user dashboard exposure, GitHub API integration, JavaScript/TypeScript test
execution and LLM-driven planning all remain out of scope. See the
[completion report](PHASE3_COMPLETION_REPORT.md#4-deliberately-not-implemented).
