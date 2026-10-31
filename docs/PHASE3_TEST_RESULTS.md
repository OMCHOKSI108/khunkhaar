# Phase 3 test results

Recorded on 2026-10-31 in the installation at `~/KHUNKHAAR-QA`, Python 3.12 virtual environment.
Every number below is the output of a command that was executed, not an estimate.

## Test suite

```bash
.venv/bin/python -m pytest tests -q
```

```
543 passed in 89.69s
```

That total is 167 Phase 1/2 tests plus 376 Phase 3 tests. No test was skipped in this run (the
Playwright, mock-server and MCP stdio tests all ran). The Phase 3 suite alone:

```bash
.venv/bin/python -m pytest tests/phase3 -q
# 376 passed in 75.75s
```

### P8 acceptance suites

```bash
.venv/bin/python -m pytest tests/phase3/test_acceptance.py -q
# 2 passed in 19.62s

.venv/bin/python -m pytest tests/phase3/test_acceptance_negative.py -q
# 5 passed in 3.55s
```

`test_acceptance.py` runs the full Python chain: a real synthetic repository with a real git
history, the live application served on a loopback port, git analysis, impact, plan, approval,
execution, verification, regression generation and validation, reporting and the dashboard. Its
TypeScript counterpart proves analysis and impact while asserting that nothing was executed.

`test_acceptance_negative.py` covers the empty repository, multi-commit rename/delete plus dirty
worktree, engine interruption, CLI interruption (exit 130), and an unreachable target with a
missing database. The last four are asserted to produce infrastructure errors or an `interrupted`
run — never a confirmed defect. The complete assertions live in the test file; they include:

- the repository's index and every file's content and mtime are unchanged after a full run;
- every run artifact resolves under the KHUNKHAAR root;
- evidence outside `evidence/` and `reports/` is not served by the dashboard;
- a plan never starts a run, and an approval-gated plan refuses execution until approved.

## Lint

```bash
ruff check core dashboard mcp-servers tools scripts tests
# All checks passed!
```

The run above follows the 0.3.0 cleanup commit that removed pre-existing unused imports and dead
locals and fixed an undefined `Path` annotation in the browser session teardown.

## Bandit

```bash
.venv/bin/python -m bandit -q -r core dashboard mcp-servers tools -f txt
```

```
Total lines of code: 19818
Total issues (by severity):
    Low: 11
    Medium: 2
    High: 0
```

The two medium items are B608 (string-built SQL) in read-only contexts:

1. `core/agents/base.py` builds `SELECT count(*) FROM "<table>"` from a table name returned by the
   target database itself, over a read-only SQLite connection.
2. `core/metadata/store.py` builds a query over an internal table constant.

No SQL write path exists; the SQLite reader opens files with `mode=ro` and the database adapters
enforce read-only statements and a statement timeout. These are documented in
`PHASE3_SECURITY_REVIEW.md` rather than hidden with a blanket suppression. Bandit also reports
low-severity subprocess-related findings for git and discovery subprocess calls; those calls use
argument lists (never a shell), fixed binaries and validated refs.

## What was not exercised

- A real GitLab instance, email/webhook delivery, and a real desktop notification.
- Browser missions inside the Phase 3 acceptance chain (they are covered by Phase 1/2 browser
  smoke tests and by the deterministic monitor tests).
- Non-loopback targets and non-loopback dashboard binding.
- JavaScript/TypeScript test execution (analysis only, by design).

These are listed again, with operational consequences, in `PHASE3_KNOWN_LIMITATIONS.md`.
