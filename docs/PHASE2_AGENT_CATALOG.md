# Phase 2 — Agent Catalog

Eight specialist agents are registered in `core/agents/specialists.py`. All of them share one
execution contract (`core/agents/base.py`):

* **Inputs** — a `ProbeContext` (target, base URL, OpenAPI/static operations, user identities,
  read-only database reader, budget, structured log, evidence writer) and one `Mission`.
* **Outputs** — an `AgentOutcome` containing zero or more suspected issues (with steps, expected,
  actual, evidence and a machine-readable reproduction) plus notes and infrastructure errors.
* **Tools** — declared per agent below. Tools are capabilities granted by the orchestrator; an
  agent cannot add tools, raise a budget, change its mode or approve destructive actions.
* **Evidence** — every issue references evidence files written under
  `evidence/phase2/<run_id>/`; every probe records what it sent, what came back and any database
  delta. Findings never contain secrets (evidence/log writers redact).
* **Failure handling** — a probe that raises is recorded as an infrastructure error with a
  traceback; it never becomes a finding. Missions with errors are marked `degraded`; the run
  continues. Budget exhaustion stops the run and is reported as `budget_exceeded`.
* **Verification** — no agent can confirm its own finding. Confirmation is produced only by the
  independent verifier (section 9).

Limits below are the agent-level defaults in `AGENT_SPECS`; the run-level budgets in
`core/orchestrator/engine.py` and `core/runtime/budget.py` cap the whole session and always win.

---

## 1. User Simulator (`user_simulator`)

| Field | Value |
| --- | --- |
| Responsibility | Drive the real UI like a user: explore reachable screens, record states, actions and console/network errors. |
| Inputs | Base URL, authenticated cookie from a configured identity, mission `max_steps`. |
| Tools | browser, api |
| Limits | max_steps 40, max_browser_sessions 2 (run budget also applies) |
| Outputs | Exploration report: per-state URL, title, headings, actionable elements, console errors, failed requests; saved as `exploration.json`. |
| Stop conditions | 2 consecutive states without new URLs or actions (idle), step cap, time budget. |
| Failure handling | Browser launch errors surface as mission infrastructure errors; individual console errors are recorded, not treated as findings. |
| Evidence | Structured observation of every visited state and the final exploration summary. |

## 2. Workflow Breaker (`workflow_breaker`)

| Field | Value |
| --- | --- |
| Responsibility | Break documented workflows: replay happy paths and apply controlled mutations (skip prerequisites, repeat actions, stale ids, reorder, change actor). |
| Inputs | Contract operations, request schemas, user roles, mission invariant or feature. |
| Tools | api, browser, database |
| Limits | max_requests 80, max_journey_mutations 8 |
| Outputs | A finding per feature when one or more mutations are accepted, with the full executed step list and a journey reproduction (journey + mutation) the verifier can rebuild. Also runs the workflow-transition probe. |
| Failure handling | Features without a buildable create/action path are not planned as journeys; a happy path that fails to complete is noted and mutations are flagged as possibly unreliable. |
| Evidence | `journey-*.json`, `mutation-*.json`, `workflow-*.json`. |

## 3. Edge-Case Hunter (`edge_hunter`)

| Field | Value |
| --- | --- |
| Responsibility | Probe empty, missing, negative, extreme, unicode and time-boundary inputs, including UTC day-boundary behaviour. |
| Inputs | Contract operations and request schemas; invariant kind (`validation`, `timezone`, ...). |
| Tools | api |
| Limits | max_requests 40 |
| Outputs | Validation and timezone findings with request/response evidence. |
| Failure handling | Missing schema field or endpoint yields an infrastructure error, never a guess. |
| Evidence | `validation.json`, `timezone-boundary.json`. |

## 4. Security Hunter (`security_hunter`)

| Field | Value |
| --- | --- |
| Responsibility | Object-level authorization (IDOR/BOLA), cross-user reads/writes and unauthenticated access to private JSON endpoints. |
| Inputs | Two or more user identities, contract detail/action operations, JSON response types. |
| Tools | api, browser, database |
| Limits | max_requests 60 |
| Outputs | Confirmed-on-verification findings naming the exact cross-user request and response. |
| Failure handling | Fewer than two identities → infrastructure error. HTML pages are excluded from the auth-boundary check so public pages are not reported as leaks. |
| Evidence | `idor-*.json`, `auth-boundary.json`. |

## 5. UX Inspector (`ux_inspector`)

| Field | Value |
| --- | --- |
| Responsibility | Responsive overflow at 390px, axe-core accessibility scan and link/console health. |
| Inputs | Base URL, authenticated cookies, device emulation (`iphone13`). |
| Tools | browser, api |
| Limits | max_browser_sessions 3 |
| Outputs | Overflow findings naming offending elements; accessibility and broken-link findings when observed. |
| Failure handling | Browser/budget errors are infrastructure errors. Overflow is measured against `document.documentElement.clientWidth`, because `window.innerWidth` inflates under mobile meta tags with fixed-width content. |
| Evidence | `responsive.json` + screenshot, `accessibility.json`, link report. |

## 6. API Hunter (`api_hunter`)

| Field | Value |
| --- | --- |
| Responsibility | Exercise the API contract with invalid payloads, missing fields, type and boundary errors. |
| Inputs | Contract operation + request schema. |
| Tools | api |
| Limits | max_requests 50 |
| Outputs | Validation findings showing the invalid payload was accepted and persisted/readable. |
| Failure handling | No schema → infrastructure error. |
| Evidence | `validation.json`. |

## 7. Database / Concurrency Hunter (`db_hunter`)

| Field | Value |
| --- | --- |
| Responsibility | Verify persistence, atomicity and concurrency against the actual database (read-only reader). |
| Inputs | API operations, read-only SQLite reader, app map entities. |
| Tools | api, database |
| Limits | max_requests 60 |
| Outputs | Atomicity findings (rows added after a rejected operation) and concurrency findings (more than one row created by concurrent exclusive requests). |
| Failure handling | No database configured → `unverified` infrastructure error; the probe never writes to the database directly. Concurrency probes establish a clean precondition through the application's own API, bounded to 5 drain calls, so repeated runs stay reproducible. |
| Evidence | `atomicity-action.json`, `concurrency.json` with before/after table counts. |

## 8. Regression Intelligence (`regression_intel`)

| Field | Value |
| --- | --- |
| Responsibility | Re-run the recorded reproduction of previously confirmed findings to detect reintroduction, even if the documented rule has disappeared since. |
| Inputs | Confirmed/likely findings from the store (their reproduction payload). |
| Tools | api, browser, database |
| Limits | max_requests 40 per mission |
| Outputs | Updated findings (same fingerprint) when the defect still reproduces. Regression missions are kept separate from invariant missions; they are not deduplicated away. |
| Failure handling | Unavailable probe or unreproducible journey → infrastructure error. |
| Evidence | Same probe/journey evidence paths, plus the verification record. |

---

## 9. Independent Verifier (`core/verification/verifier.py`)

Not an agent and not a tool-holder. It is the control that makes confirmation meaningful:

* fresh `ApiClient` instances and fresh logins for every attempt (no shared cookies);
* rebuilds the recorded reproduction — either a `ProbeContext` + probe or a journey + named
  mutation;
* two attempts; the classification table is in
  [PHASE2_TESTING_METHODOLOGY.md](PHASE2_TESTING_METHODOLOGY.md);
* writes the status transition and reproduction ratio (`n/m`) to the store as an event
  attributed to `independent_verifier`.

## 10. Orchestrator (`core/orchestrator/engine.py`)

The orchestrator is the only component allowed to plan, charge budgets, grant tools, persist
findings and trigger verification. It:

* refuses unknown modes and unknown commands;
* refuses runtime commands without `--target`;
* keeps destructive HTTP methods behind the Phase 1 netguard double gate;
* stops planning when `Budget.can_continue()` is false;
* records every mission and every verification as structured log events.

## 11. Permission model

* Agents receive only the tools listed above; there is no dynamic permission API.
* Agents cannot write to the findings store; they return outcomes to the orchestrator.
* Agents cannot promote their own issues; only the verifier writes final statuses.
* Database access is the Phase 1 read-only reader; `DELETE`/`DROP`/writes are rejected by the SQL
  guard before execution.
* Budgets are consumed centrally; an agent cannot reset or extend them.