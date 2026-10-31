# CLI guide

`khunkhaar` is a single command with subcommands. Every Phase 3 command is read-only unless it is
`qa`, `regression validate`, `regression run` or `monitor run-due`, which send requests only to a
preflighted target. Nothing writes to an analysed repository.

This guide documents the commands that exist and were exercised in this environment. Phase 2
commands (`scan`, `explore`, `security`, `api`, `workflows`, `verify`, `report`) are documented in
the [Phase 2 architecture](PHASE2_ARCHITECTURE.md); the MCP servers and `opencode` launch are
covered in [OPENCODE_INTEGRATION.md](OPENCODE_INTEGRATION.md).

## Conventions

- Run `khunkhaar <command> --help` for the exact flag list.
- `--path` selects the repository (default: current directory). It must be a registered project
  for most Phase 3 commands; register once with `khunkhaar project register`.
- `--json` prints a stable, machine-readable payload on stdout. Human output is for reading.
- Exit codes: `0` success/no-op, `1` an executed check failed or a run failed, `2` refused,
  misconfigured or an unavailable environment (nothing was sent), `3` state changed mid-read, a
  regression validation environment was unavailable, or a monitoring window was missed, `130`
  interrupted by the operator.
- The platform never installs a cron entry, a service, a Git hook or anything into a repository.

## Setup

```bash
khunkhaar --version
khunkhaar --doctor                       # toolchain and service health
khunkhaar status                         # schema, projects, findings, disk
khunkhaar projects                       # registered projects and QA totals

khunkhaar project register --path ~/code/my-app
khunkhaar project show --path ~/code/my-app
khunkhaar config show                    # effective configuration, credential values never shown
khunkhaar config show --section planner
```

Testing permission is separate from registration. A non-loopback target needs an explicit,
expiring grant:

```bash
khunkhaar project authorize --project-id <id> --target https://qa.example.test \
  --mode controlled --days 7 --note "release 4.2 smoke"
```

## Change analysis

### git status / git analyze

```bash
khunkhaar git status  --path ~/code/my-app
khunkhaar git analyze --path ~/code/my-app                      # base...HEAD by default
khunkhaar git analyze --path ~/code/my-app --base origin/main --head feature/x
khunkhaar git analyze --path ~/code/my-app --commit HEAD
khunkhaar git analyze --path ~/code/my-app --range base..head
khunkhaar git analyze --path ~/code/my-app --working-tree       # staged + unstaged + untracked
khunkhaar git analyze --path ~/code/my-app --branch feature/x
khunkhaar git analyze --path ~/code/my-app --mr-source feature/x --mr-target main
```

Output includes identity, branch and base with the source of the base decision, ahead/behind,
staged/unstaged/untracked/conflicted counts, changed files with rename/delete/binary labels, and
`index_untouched`. `git status` and `git analyze` return `3` if the checkout changed during the
read (`StateChangedError`); rerun when the tree is quiet.

### impact

```bash
khunkhaar impact --path ~/code/my-app
khunkhaar impact --path ~/code/my-app --depth 3 --rebuild-graph
```

Reads the latest saved change set (run `git analyze` first), builds or reuses the dependency graph,
lists impacted tests, workflows and entities, names missing nodes honestly, and prints the risk
score with its factor contributions and the non-probability wording. The score is an
approximate attention signal; never report it as a probability.

### mr analyze

```bash
khunkhaar mr analyze --path ~/code/my-app --base origin/main --head feature/x
khunkhaar mr analyze --path ~/code/my-app --gitlab-mr 1234   # requires gitlab.enabled + allowlist
```

The local comparison is authoritative. GitLab metadata is optional, read-only and disabled by
default; an outage leaves the local review unchanged and is reported.

### knowledge

```bash
khunkhaar knowledge build    --path ~/code/my-app
khunkhaar knowledge show     --path ~/code/my-app --section confirmed_bugs
khunkhaar knowledge retrieve --path ~/code/my-app \
  --changed src/payments/service.py --changed tests/test_payments.py
```

`build` distils the project document and the global technique library. `retrieve` ranks this
project's history against changed paths; it is a ranking, not a prediction.

## Regression tests

Only independently confirmed findings with a machine-checkable scenario are eligible.

```bash
khunkhaar regression list     --path ~/code/my-app
khunkhaar regression generate --path ~/code/my-app
khunkhaar regression generate --path ~/code/my-app --finding <finding-id> --json

# Prove a generated test reproduces the defect before trusting it.
khunkhaar regression validate --path ~/code/my-app --target http://127.0.0.1:8740 \
  --users ~/qa/roles.json --db ~/KHUNKHAAR-QA/runtime/tmp/synthetic/app.db

# Replay selected tests within a budget.
khunkhaar regression run --path ~/code/my-app --target http://127.0.0.1:8740 \
  --selector impact --show-plan
khunkhaar regression run --path ~/code/my-app --target http://127.0.0.1:8740 \
  --users ~/qa/roles.json --max-seconds 300
```

Validation requires a violation in every normal run (default 2) and a passing inverted run. A
generated test that does not meet that bar is `unreliable`/`not_reproduced` and is not promoted.
`environment_unavailable` is an infrastructure result, never a defect. Validation exits `3` when
the environment was unavailable and `1` when a verdict failed.

## Plans and execution

A plan is a reviewable artifact with a digest. Execution reconstructs exactly the stored missions
and regression ids; a drifted digest is refused.

```bash
# 1. Build a plan for a change set.
khunkhaar plan --path ~/code/my-app --target http://127.0.0.1:8740 --mode controlled
khunkhaar plan --path ~/code/my-app --impact-only --target http://127.0.0.1:8740
khunkhaar plan --path ~/code/my-app --max-actions 20

# 2. Review it.
khunkhaar plan show --plan <plan-id>
khunkhaar plan list --path ~/code/my-app

# 3. Approve the exact digest.
khunkhaar plan approve --plan <plan-id> --by operator

# 4. Execute exactly that plan.
khunkhaar qa --plan <plan-id> --db ~/KHUNKHAAR-QA/runtime/tmp/synthetic/app.db \
  --users ~/qa/roles.json

# Or build and execute an impact-only plan in one step (still subject to the approval gate).
khunkhaar qa --impact-only --path ~/code/my-app --target http://127.0.0.1:8740
```

A stored plan owns its target; passing `--target` with `--plan` is refused. `--approve` records
approval of the exact digest in the same invocation. `--no-verify` skips independent verification
(new findings stay unverified). Budget flags (`--max-steps`, `--max-requests`, `--max-seconds`,
`--max-browser-sessions`) replace the mode default for that run; pass a lower value to tighten it.
Every run still stops at a finite budget. Adversarial mode against a non-loopback target requires
`--yes`.

An empty plan is a legitimate result: it is saved, reported, and no run is recorded. An interrupted
run exits `130` and is stored as `interrupted` with a partial summary. A budget stop is
`cancelled`/`budget_exceeded` and exits `1`.

## Monitoring

Monitoring is off until `monitoring.enabled: true`. Even then, nothing is installed.

```bash
khunkhaar monitor set --path ~/code/my-app --name nightly \
  --schedule "0 3 * * *" --target http://127.0.0.1:8740 \
  --checks smoke,regression --mode controlled

khunkhaar monitor doctor --schedule <schedule-id>    # prints the exact scheduler line
khunkhaar monitor list
khunkhaar monitor status
khunkhaar monitor run-due --schedule <schedule-id>   # invoke from the operator's scheduler
khunkhaar monitor history --limit 20
khunkhaar monitor pause  --schedule <schedule-id>
khunkhaar monitor resume --schedule <schedule-id>
```

A five-field cron expression or `@daily`/`@hourly`/`@weekly`/`@monthly`/`@yearly` aliases are
accepted. A window missed while the machine was off is stored as `missed` and exits `3`; it is
never reported as a successful check, and offline is not a defect. A pause creates no missed run.

## Dashboard

```bash
khunkhaar dashboard start                  # http://127.0.0.1:8899/
khunkhaar dashboard start --port 8899
```

The token is generated on first start at `data/profiles/dashboard/token` (0600) and is never
printed. See [DASHBOARD_GUIDE.md](DASHBOARD_GUIDE.md).

## Retention

```bash
khunkhaar retention index                 # index new artifacts from disk
khunkhaar retention index --rebuild
khunkhaar retention plan                  # dry run, default windows are disabled
khunkhaar retention plan --days 30 --class-days screenshot=14
khunkhaar retention apply                 # still a dry run
khunkhaar retention apply --confirm       # delete eligible evidence only
```

`apply` touches only `evidence/` and `runtime/tmp/` and never removes evidence attached to a
finding whose status is protected (`confirmed`, `likely`, `unverified`, `needs_human` or
`false_positive`). Windows of `0` disable eligibility.

## A complete local cycle

```bash
REPO=~/code/my-app
TARGET=http://127.0.0.1:8740

khunkhaar project register --path "$REPO"
khunkhaar git analyze --path "$REPO" --base origin/main
khunkhaar impact --path "$REPO"
khunkhaar knowledge build --path "$REPO"
khunkhaar plan --path "$REPO" --target "$TARGET" --mode controlled
khunkhaar plan show --plan "$PLAN_ID"
khunkhaar plan approve --plan "$PLAN_ID"
khunkhaar qa --plan "$PLAN_ID"
khunkhaar report --project-root "$REPO"
```

Every step either produces an artifact under the KHUNKHAAR root or refuses with a printed reason.
There is no silent fallback that turns a missing tool, database or environment into a finding.
