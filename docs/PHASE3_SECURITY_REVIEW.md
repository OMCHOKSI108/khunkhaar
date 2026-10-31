# Phase 3 security review

This review covers the Phase 3 additions: git intelligence, impact analysis, merge-request review,
regression generation, planning, execution, monitoring, notifications and the dashboard. Phase 1
and Phase 2 controls remain in force and are summarized in
[SECURITY_AND_PERMISSIONS.md](SECURITY_AND_PERMISSIONS.md) and
[PHASE2_SECURITY_MODEL.md](PHASE2_SECURITY_MODEL.md).

KHUNKHAAR is a private, locally operated system. It is not exposed to a network by default, has no
multi-user accounts and sends no telemetry.

## Assets and adversaries

| Asset | Why it matters |
| --- | --- |
| Checked-out company repositories | Source code, git history, branch names, uncommitted work |
| Credentials | Git hosting tokens, database passwords, application test accounts |
| Evidence and reports | Screenshots, request/response excerpts, database rows, paths |
| Metadata store | Project profiles, findings, plans, schedules, authorizations |
| Operator machine | Shell environment, local files, running services |

Threats considered:

1. **Malicious repository content** — a crafted file name, diff, YAML, dependency manifest or test
   that tries to steer the tool into executing something or leaking data.
2. **Malicious target responses** — prompt-injection text, huge bodies, redirects to metadata
   endpoints, header or JSON values that look like credentials.
3. **Local unprivileged process** — another account or process on the same machine reading the
   dashboard, the token file, the SQLite store or evidence.
4. **Operator error** — enabling a remote channel, binding a public interface, authorizing the
   wrong host, approving a plan that was not read.
5. **Supply chain** — a compromised dependency or tool binary.

An LLM is not part of the Phase 3 execution path. Mission generation, probes, verification,
regression generation and planning are deterministic code; every run records `llm_usage: none`.

## Permission matrix (Phase 3 behavior)

| Action | Default | Control |
| --- | --- | --- |
| Read git metadata and file content of a registered repository | allowed | Read-only command allowlist ∩ hard floor; ref and pathspec validation |
| Write to a repository (index, worktree, refs, remotes, hooks) | **no code path exists** | No write subcommand in the allowlist; before/after state comparison |
| Analyse an unregistered repository | **refused** | `git.require_registered_project` |
| Send HTTP requests to a target | allowlisted host only | `api.allowed_hosts` + `netguard` |
| Send requests to a non-loopback target | **refused without a grant** | `khunkhaar project authorize`, unexpired, exact target |
| Adversarial mode against a shared host | **refused without `--yes`** | Planner/QA mode gate |
| Read a SQLite database for persistence verification | allowed only inside the KHUNKHAAR tree | `open_db` path guard; read-only connection |
| Send GitLab requests | **disabled** | `gitlab.enabled: false`; host must also be allowlisted |
| Write to GitLab (comment, approve, label, pipeline) | **no adapter exists** | Only GET metadata/diff endpoints are implemented |
| Start a scan, test or browser from the dashboard | **no route exists** | Dashboard is read-only |
| Bind the dashboard beyond loopback | **refused without flag** | `--allow-non-loopback`; warning printed |
| Install a cron entry or service | **never** | `install_system_scheduler: false`; `doctor` prints lines only |
| Send email or webhook notifications | **nothing is sent** | Channel not implemented; alert records `not_sent` |

## Read-only git

`core/git/runner.py` is the only path to `git`. It:

- intersects the configured command list with a hard floor of read subcommands; a configuration
  edit cannot make `commit`, `checkout`, `reset`, `stash` or `remote` executable;
- validates every ref and pathspec before use, so a ref cannot smuggle an option;
- runs `--no-optional-locks --no-pager`, sets `GIT_TERMINAL_PROMPT=0`,
  `GIT_CONFIG_NOSYSTEM=1`, `GIT_ATTR_NOSYSTEM=1`, and removes `GIT_SSH`, `GIT_SSH_COMMAND`,
  `GIT_PROXY_COMMAND` and `GIT_ASKPASS` from the child environment;
- takes a `(porcelain v2 status, HEAD)` snapshot before and after each call. A difference raises
  `StateChangedError` and aborts the analysis instead of continuing on a moving checkout.

The acceptance suite independently hashes the index and every file (content and mtime) before and
after a full analysis-and-execution run and requires equality. `test_git_runner.py` asserts that a
tool invocation cannot alter the index, and the dirty-state acceptance test asserts
`index_untouched: true` while staged, unstaged and untracked files are reported.

The one place a git-adjacent process runs a repository-supplied artifact is regression replay: a
generated pytest module imports the application under test over HTTP. It does not import the
repository's code into the KHUNKHAAR process.

## Network egress

- Every outbound capability calls `preflight_target` before the first request.
- `netguard` resolves the host, refuses non-http(s) schemes, link-local addresses, the cloud
  metadata address and the configured denied hosts. The checks apply to every redirect hop, not
  only the first request.
- Private ranges are allowed in the shipped configuration because this machine's QA targets are
  local; link-local and metadata addresses remain blocked regardless.
- Non-loopback targets additionally require an unexpired `project authorize` grant for the exact
  target. Registration is never treated as permission to test.
- Adversarial mode against a non-loopback host requires `--yes`.
- GitLab access is disabled and, when enabled, is bounded to GET requests against one configured
  origin. The token is read from `KHUNKHAAR_GITLAB_TOKEN` at request time; only the variable name
  is configuration.

## Credentials and secrets

- No password or token value is written to `config/khunkhaar.yaml`, SQLite, evidence, reports or
  logs. Configuration references environment variable names.
- Project discovery records `.env` variable **names and presence**, never values
  (`secret_value_ingest: false`).
- Redaction is structural (by key name) and textual (known token formats, connection strings).
  Booleans and numbers survive so "is a credential present" stays answerable.
- The database MCP tool cannot accept a password argument and never reads credentials from project
  files. The GitLab adapter redacts its token from errors and never stores diff text.
- Notification payloads are built from an allowlist of fields: finding id, severity, feature, a
  one-line title, a relative evidence path and counts. Source, headers, bodies, credentials,
  target URLs and database values are not eligible.
- Regression subprocesses receive only `KHUNKHAAR_TARGET`, optional credential-file and database
  paths, and a results path; they inherit the operator's environment, which is documented as a
  residual risk below.

## Dashboard

- Binds `127.0.0.1:8899` by default. A non-loopback bind is refused unless
  `--allow-non-loopback` is passed, and then a warning is printed.
- The access token lives in `data/profiles/dashboard/token`, mode 0600; the directory is tightened
  to 0700. A symlink or a token shorter than 32 characters is refused rather than regenerated.
  The token is never printed.
- Login compares with `hmac.compare_digest`. The cookie is an HMAC of the issue time keyed by the
  token, `HttpOnly`, `SameSite=Strict`, 8-hour TTL, and is not the token itself.
- `secure=False` is deliberate: the dashboard is plain HTTP on loopback. Do not expose it over a
  network without a TLS-terminating proxy.
- Every response carries `Cache-Control: no-store`, `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: no-referrer` and a `default-src 'none'` CSP.
- The evidence route resolves only inside `evidence/` or `reports/`, refuses absolute paths and
  traversal, serves text as redacted `text/plain`, images inline, and any other type as an
  octet-stream attachment. There is no route that executes an action.
- No OpenAPI schema, docs UI or health endpoint is exposed.

## Evidence and reports

- Evidence artifacts are written under `evidence/<project-id>/`, reports under
  `reports/phase2/<project-id>/`, logs under `logs/`, all inside the KHUNKHAAR root. The analysed
  repository is never a storage location.
- Artifacts that can contain project facts are written mode 0600 and redacted on write.
- SQLite connections enable foreign keys and use a schema version checked at startup; migrations
  run forward only.
- Retention never deletes evidence attached to a finding with status `confirmed`, `likely`,
  `unverified`, `needs_human` or `false_positive`, and `apply` deletes nothing without `--confirm`.
  It touches only `evidence/` and `runtime/tmp/`.

## Process execution

Only fixed argument vectors are used, never a shell:

- `git` via the read-only runner;
- `notify-send` for desktop notifications, when installed;
- `pytest` for generated regression modules, with `no:cacheprovider` and a timeout;
- the Phase 1/2 scanners and `opencode` launcher, unchanged from earlier phases.

Generated regression modules are produced by template code from a confirmed finding and a stored
scenario. They are not OS-sandboxed: they inherit the operator's environment and can make network
requests to the target. Treat a generated test like any other code before running it.

## Isolation from company infrastructure

- KHUNKHAAR installs nothing into an analysed repository: no CI file, hook, webhook, bot, runner or
  configuration.
- It never posts a comment, approves a merge request, changes labels or reviewers, commits, pushes,
  merges or triggers a pipeline.
- It reads local refs and (optionally) read-only GitLab metadata; it does not clone, fetch or push.
- The acceptance suite runs against synthetic repositories created inside the test's temporary
  root; no company checkout is used as a fixture.

## Static analysis

The full tree (`core`, `dashboard`, `mcp-servers`, `tools`, `scripts`, `tests`) passes
`ruff check`. `bandit` reports, over 19,818 lines:

- **0 high** severity issues;
- **2 medium** — B608 "possible SQL injection" in `core/agents/base.py:161` and
  `core/metadata/store.py:435`. Both construct SQL identifiers from fixed internal table constants
  or from table names read from the target database after allowlist validation, over read-only
  connections. They are documented rather than suppressed with `# nosec`;
- **11 low** severity issues, reviewed and accepted (subprocess usage with fixed argv, bind
  defaults, asserted/validated input).

These are residual risks, not proofs of exploitability.

## Residual risks

1. **Operator authority.** Approval, authorization and `--allow-non-loopback` are operator
   decisions. A careless approval executes exactly what was approved.
2. **No OS sandbox.** Regression tests run as the operator with the operator's environment.
3. **Target allowlist scope.** `api.allowed_hosts` is manually maintained; adding a production host
   would let read probes reach it. `netguard` blocks metadata and link-local addresses, not a
   misconfigured allowlist entry.
4. **Local process boundary.** The dashboard token and SQLite store are protected by file modes,
   not encryption at rest. Another process running as the same user can read them.
5. **Plain HTTP on loopback.** The session cookie is not `Secure`; local malware or a hostile
   browser extension could read it.
6. **GitLab token lifetime.** A token placed in the environment remains valid until revoked; use a
   read-only, project-scoped token.
7. **Heuristic analysis.** Impact edges, symbol attribution and risk are approximations. They order
   attention; they are not evidence of a defect.
8. **Dependency trust.** The virtual environment and tool binaries are trusted inputs; integrity is
   not verified at run time.

## Reporting a problem

This is a private personal system with no public deployment. Security issues are recorded in the
operator's local notes and, where they affect behavior, in `CHANGELOG.md` with a fix commit. There
is no bug bounty and no external telemetry to disable.
