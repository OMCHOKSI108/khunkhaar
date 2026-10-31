# Phase 2 known limitations

These limitations are observed properties of the Phase 2 engine as validated on 2026-10-30. They
are documented so that results are not over-trusted and so that future work has a concrete list.
Nothing here is a hidden defect: each item names the boundary that was actually hit during
validation.

## 1. Runtime probing requires a machine-readable contract

API, security and workflow probes consume the runtime OpenAPI document (`/openapi.json`) for
paths, methods, request schemas and response content types. A target without OpenAPI (or with a
hand-written contract that omits schemas) falls back to browser-only exploration and will produce
far fewer findings. The synthetic FastAPI app exposes 14 operations; that is why the API surface
could be exercised.

Discovery itself is static and regex-based. It recognises FastAPI, Flask, Django and Express
signals (`core/intelligence/mapper.py`), scanning a bounded set of source files. Other frameworks
or generated routes may be missed even when the runtime contract is complete.

## 2. Journey happy paths can fail on untyped schema fields

The synthetic `POST /api/events` operation declares a datetime field without a JSON type. The
payload builder therefore supplies a string, and Pydantic rejects it with `422`. The workflow
agent records this honestly (`happy path itself did not complete; mutations may be unreliable`)
and limits itself to other features. This is a schema-quality limitation, not an engine crash,
but it means journey coverage depends on the quality of the published contract.

## 3. Endpoint selection is heuristic

Probes pick a create/action operation by path keywords and action ranking (`submit`, `approve`,
`accept`, `complete`, `close`, `publish`). Features whose routes do not follow those conventions
may get no mission, or the wrong operation. The engine never guesses a destructive verb: DELETE
probes are not generated, and the API client gates destructive calls behind an explicit flag.

## 4. Database verification is SQLite-only

Persistence checks (`atomicity`, `concurrency`, row-count deltas) open the database with Python's
`sqlite3` module in read-only URI mode (`core/execution/db.py`). The Phase 1 PostgreSQL adapter is
not wired into Phase 2 probes. A PostgreSQL-backed target can still be tested at the API level,
but row-level assertions are unavailable.

## 5. Browser checks are narrow

- Responsive testing uses one emulated device (iPhone 13, 390px wide) in Chromium. Other
  breakpoints, orientations and engines are not exercised.
- The responsive probe visits at most six same-origin pages discovered from links on the landing
  page; pages not linked from there are not checked.
- The accessibility probe runs axe-core against the landing page only, and reports only
  `critical`/`serious` violations. Authenticated sub-pages need a dedicated scan.
- UI checks verify layout metrics and DOM/axe signals, not visual appearance, copy quality or
  interaction timing.

## 6. Browser budget is tight

Adversarial mode allows 8 browser sessions and the validated run used 7. Adding another browser
probe (for example a second viewport or a deeper accessibility pass) will hit the cap before the
run completes. Budgets are deliberate, but operators should expect to raise
`--max-browser-sessions` for targets with more UI missions.

## 7. Exploration is deterministic, not LLM-driven

The Observe → Think → Act → Verify → Learn loop is implemented with deterministic heuristics and
persisted history. No language model participates in Phase 2 decisions. This keeps runs
reproducible and cheap, but the engine will not invent novel multi-step business scenarios the
way an LLM-guided explorer might. The "Think" step ranks candidate actions; it does not reason
about domain semantics beyond the extracted rules.

## 8. Rule extraction depends on documentation

The invariant engine extracts rules from explicit docs (for example
`tests/fixtures/vulnapp/docs/BUSINESS_RULES.md`) and from inferred code patterns. Inferred rules
are weaker: they are labelled as inferred and should be reviewed before a finding is escalated.
A target with no documentation yields fewer invariants; the engine may then report generic
contract violations rather than business-logic defects.

## 9. Journey findings are grouped per feature

Accepted workflow mutations are stored as one finding per feature, with individual mutation names
in the `actual` field (for example `F-c4a7f5e9f2` for `requests`). This avoids flooding the store
with near-duplicates, but a dashboard cannot filter by mutation without parsing the finding body.

## 10. No retry policy beyond budgets and verifier attempts

Individual HTTP steps are executed once; a transient network failure fails the mission and is
reported as an infrastructure error. The independent verifier runs two attempts, which is enough
for the confirmed/likely decision table but is not a general retry/backoff framework. Slow
targets may need higher `--max-seconds` and per-request timeouts (default 10 s).

## 11. Concurrency testing is bounded and best-effort

The concurrency probe fires five parallel POSTs and detects duplicate rows or unexpected success
codes. It is a smoke-level race detector: it does not prove the absence of races, and on SQLite it
depends on the target's own locking behaviour. It also creates real rows on the target; the
synthetic database is disposable, but against a shared environment this is a side effect to plan
for.

## 12. Test credential handling

Role credentials come from a JSON file passed with `--users`; the Phase 2 fixture contains
deliberately fake test passwords in plaintext. The engine loads them into memory to log in and
persists only redacted request/response evidence. Operators must use disposable test accounts,
never personal or production credentials, and should keep such files out of version control for
real targets. No credential value is written to the metadata store, reports or logs.

## 13. Mode safety boundaries

`observe` performs read-only page inspection; `controlled` adds authenticated probes; `adversarial`
adds journey mutations and concurrency bursts. The engine refuses to start runtime commands without
`--target`, and adversarial mode requires `--yes`. There is no automatic detection that a target
is production: authorization is an operator assertion. The engine cannot make a destructive test
safe merely by being pointed at an arbitrary URL.

## 14. Regression intelligence replays, it does not bisect

`regression_intel` re-executes each stored finding's reproduction and records whether it still
violates. It does not identify the commit that introduced or fixed a defect, does not perform
bisection, and does not watch CI. Findings without a machine reproduction are marked
`needs_human` and cannot be replayed automatically.

## 15. Reporting and knowledge scope

- Reports are emitted as JSON and Markdown only; no PDF/HTML/issue-tracker export.
- Findings are deduplicated by fingerprint within a project. The same defect observed through two
  different probe families can still appear as two findings (for example the rule-level R3 finding
  and the grouped journey finding).
- Knowledge is scoped by `project_id` and the isolation suite passes, but the metadata store is a
  single SQLite file. Two parallel scans of the same project would contend on it; serialise runs.
- Phase 2 does not include scheduled scans, CI integration, or a long-running daemon.

## 16. Phase 1 boundary

Phase 1 tools (Playwright, MCP servers, k6, nuclei, ZAP, Schemathesis) remain available and their
suite passes, but Phase 2 only invokes Playwright, httpx and SQLite directly. Phase 1 scanners are
not orchestrated by the Phase 2 engine, so a Phase 2 run is not a substitute for the Phase 1 scan
matrix.