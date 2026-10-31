# Phase 2 — Testing Methodology

Phase 2 does not generate test ideas and stop there. Every invariant ends in an executable
check, and every claimed defect is re-executed by a separate verifier before it leaves the
`unverified` state.

## 1. From documentation to execution

1. **Map** the repository: routes, controllers, models, schema, docs, tests, auth and jobs.
2. **Classify** each documented statement into one invariant kind (authorization, idempotency,
   workflow, validation, concurrency, timezone, atomicity, responsive, accessibility, other)
   with a confidence and a source `file:line`.
3. **Bind** the invariant to a feature and a verification method.
4. **Plan** a mission: the agent and probe kind that can falsify the invariant, plus the API
   contract needed to do it.
5. **Execute** the probe/journey against the real target.
6. **Propose** a finding with status `unverified`.
7. **Verify** independently: two fresh attempts. Only then is a status final.
8. **Learn** — the event history, exploration states and regression replays feed the next run.

## 2. Probe catalogue

| Probe kind | What it does | Violation signal | Evidence |
| --- | --- | --- | --- |
| `authorization` | user A creates a resource; user B requests A's detail endpoint | 2xx and A's data returned | `idor-<id>.json` |
| `auth_boundary` | unauthenticated GETs to private **JSON** operations | 2xx without credentials | `auth-boundary.json` |
| `validation` | POST invalid payload (empty required text, non-positive number) built from the schema | 2xx and the record is readable | `validation.json` |
| `workflow` | create a resource, then call a transition from the wrong state | transition returns 2xx | `workflow-<id>.json` |
| `idempotency` | submit the identical payload twice | two records created for the same logical submission | `duplicate-submission.json` |
| `concurrency` | drain open state (bounded), then 5 parallel exclusive requests | more than one row created | `concurrency.json` |
| `timezone` | create a 23:30 UTC event 30 days back; query both UTC days | event appears on the wrong day | `timezone-boundary.json` |
| `atomicity` | perform an operation that must fail; compare all table counts | rows added after a non-2xx response | `atomicity-action.json` |
| `responsive` | open pages at 390px; measure `documentElement.clientWidth` vs `scrollWidth` | overflow and named offenders | `responsive.json` + screenshot |
| `accessibility` | axe-core scan of the landing page | axe violations | `accessibility.json` |
| `links` | crawl same-origin links; collect console/network failures | broken links / console errors | exploration/link evidence |

Endpoints and payloads are derived at run time from the OpenAPI document (or the static map when
no document exists). No probe contains a route, a table name or a defect id from any specific
application.

## 3. Journey mutation engine

A journey is built for features that have a create endpoint and at least one transition
endpoint. Steps are ordered by action rank (submit → approve → accept → complete → close →
publish) and path parameters are normalised to `{id}`.

Generated mutations (bounded to 8 per journey):

| Mutation | Expectation |
| --- | --- |
| skip to `<action>` | the action is rejected until prerequisites complete |
| repeat `<action>` | the second call is refused or has no additional effect |
| stale id before `<action>` | a non-existent resource is rejected with 404/400 |
| reorder actions | out-of-order transitions are rejected |
| change actor to `<role>` | another user cannot continue someone else's resource |

A mutation is a violation only when it is meaningful (e.g. repeating an approve) and the final
response is 2xx. Each accepted mutation is recorded, and accepted mutations for the same feature
update one finding rather than flooding the store.

## 4. Exploration loop

`core/exploration/engine.py` implements Observe → Think → Act → Verify → Learn:

* **Observe** — structured page snapshot (headings, forms, actionable elements, console errors,
  failed requests).
* **Think** — pick an unvisited same-origin action, avoiding URLs and state signatures already
  seen.
* **Act** — click/follow through the Phase 1 browser session.
* **Verify** — record the resulting URL, state signature and any new errors.
* **Learn** — persist state hashes in `exploration_states`; stop after 2 idle states, the step
  cap or the budget.

The loop is deterministic and bounded; it is not an unbounded LLM loop.

## 5. Independent verification and classification

The verifier runs with fresh clients and no shared cookies, twice:

| Reproductions | Status | Confidence |
| --- | --- | --- |
| 2/2 | `confirmed` | 0.95 |
| 1/2 | `likely` | 0.70 |
| 0/2 | `not_reproducible` | 0.10 |
| no usable attempt (infrastructure) | `unverified` | 0.40 |
| no recorded reproduction | `needs_human` | 0.30 |

The reproduction ratio is stored on the finding (`"2/2"`), and every transition is written to
`finding_events`. A finding that a discovering agent claimed but the verifier cannot reproduce is
downgraded — this happened during validation (see
[PHASE2_VALIDATION_REPORT.md](PHASE2_VALIDATION_REPORT.md)).

## 6. False-positive controls

* An infrastructure error can never become a finding.
* A probe must observe the violation itself; "the rule exists" is not evidence.
* The missing-validation probe requires both an accepted invalid request **and** readability of
  the created record.
* The atomicity probe requires a non-2xx response **and** a positive row delta in the same run.
* The auth-boundary probe excludes HTML pages; public pages returning 200 are not leaks.
* The responsive probe must name offending elements, not judge visual taste.
* The workflow mutation engine only counts meaningful mutations that end in 2xx.
* Rediscovery updates one fingerprint instead of creating duplicates.
* Destructive HTTP methods are refused unless explicitly allow-listed and approved.

## 7. Reproducibility

Every finding records a machine-readable `reproduction` object (`probe` + invariant, or
`journey` + named mutation) and human steps. Probes establish their own preconditions through
the application API (e.g. draining open shifts) so repeated runs and verifier attempts start
from a comparable state. Database checks compare before/after counts instead of absolute values,
so accumulated data from earlier runs does not change the verdict.

## 8. Isolation

Findings, invariants, missions, journeys and app maps are scoped by `project_id`; the store has
no unfiltered read. Two projects scanned in the same session cannot see each other's data. Global
reusable assets (probes, classifiers, agent code) contain no project facts; project-specific
knowledge lives only in the project-scoped rows and `intelligence/<project_id>/app-map.json`.

## 9. What is automated vs manual

Automated (executed in validation): discovery, mission planning, all eight probe families,
browser exploration, journey mutation, verification, persistence, reporting, budget enforcement,
regression replay.

Manual/operator responsibilities: providing authorized targets and **test** credentials,
approving destructive operations, reviewing `needs_human` findings, and deciding whether a
confirmed business-logic change is acceptable.

## 10. Test suites

* Phase 1 infrastructure suite: 134 tests (API, browser, database, MCP, security properties,
  load).
* Phase 2 suite (`tests/phase2/`, 33 tests): finding identity/enumerations, budget ceilings and
  cancellation, mission mapping/mode gating/dedupe, journey construction and mutations, contract
  reduction (including the redaction regression), probe registry completeness, and the verifier
  decision table.
* Synthetic end-to-end validation: `tests/fixtures/vulnapp` — a disposable FastAPI + SQLite app
  with eight deliberately injected defects. The engine reproduces all eight without any defect
  being encoded in the agents.