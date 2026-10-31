# Phase 2 — Security Model

Phase 2 executes real requests and real browser sessions against a target. Its security model
is therefore about **authorization, blast radius and data handling**, not only about the
vulnerabilities it looks for.

## 1. Authorization before any run

* Runtime commands (`explore`, `security`, `api`, `workflows`, `scan --full`, `verify`) require
  an explicit `--target URL`; `scan` and `report` are offline.
* The target must be present in the Phase 1 netguard allowlist (`config/khunkhaar.yaml`:
  `api.allowed_hosts` and `browser.allowed_origins`). Unknown hosts raise
  `PermissionError: target refused by allowlist` before any request is sent.
* Destructive HTTP methods listed in `api.destructive_methods` are refused unless
  `allow_destructive` is on **and** the host is listed in
  `security.destructive_approved_targets`. Agents never enable this themselves. `--yes`
  acknowledges an adversarial run; it does not bypass the destructive-host gate.
* Only applications the operator is authorized to test may be targeted. The Phase 2 validation
  target is a loopback synthetic application (`127.0.0.1:8740`) built for this purpose.
* Production targets must never receive destructive tests. Use `observe`/`controlled` modes and
  read-only database access; `adversarial` mode is for disposable or explicitly authorized
  environments.

## 2. Modes and blast radius

| Mode | Network | Browser | Writes | Intended use |
| --- | --- | --- | --- | --- |
| `observe` | plan/read only | allowed where needed | no probe writes | mapping and exploration on sensitive systems |
| `controlled` | documented-rule probes | allowed | only idempotent-safe API calls declared by probes | normal QA |
| `adversarial` | + idempotency/concurrency/journeys | allowed | repeated submissions, concurrent requests, journey mutations | disposable/authorized test environments |

Mode gating is enforced during mission planning; a mission that requires `adversarial` is
skipped (and reported as skipped) in `controlled` mode.

## 3. Budgets, cancellation and failure containment

`core/runtime/budget.py` enforces hard ceilings for agent steps, HTTP requests, browser sessions,
wall-clock seconds and concurrency. Every request/session/step is charged *before* execution.
`Budget.cancel()` stops the run; `Budget.can_continue()` prevents planning more work once a
ceiling is reached. A run that exceeds a budget exits with code 1 and a `budget_exceeded` status;
it does not continue in the background. There are no unbounded loops: exploration stops after 2
idle states or the step cap, journey mutations are capped, and mission counts are capped.

## 4. Agent permissions

* Tools are declared per agent and fixed at construction (`AGENT_SPECS`).
* Agents cannot change budgets, modes, allowlists or permissions at runtime; no such API exists.
* Agents cannot write findings or statuses; they return outcomes to the orchestrator.
* Only the independent verifier writes a final status, and it uses fresh clients.
* Database access is the Phase 1 read-only reader; the SQL guard rejects writes and DDL.
* Evidence is written through one shared writer, never opened for arbitrary paths by a probe.

## 5. Credential handling

* Phase 2 identities are supplied by the operator with `--users <file>`: a JSON object mapping a
  role name to `{username, password}` (or a username string). Use **test accounts only**.
* The file is read into memory; credentials are never written to the metadata store, reports,
  evidence or logs. Login responses (tokens) stay in the HTTP client's memory for the run.
* Store the users file with mode 0600 and keep it outside version control. The validation fixture
  (`tests/fixtures/vulnapp/qa-users.json`) contains disposable test credentials for the synthetic
  app only.
* Credentials found in unrelated project files are never auto-used; the loader only reads the
  file the operator names explicitly.
* Phase 1's server-side auth profiles (env var or `data/profiles/auth/*.json`) remain the
  mechanism for the MCP API tool; Phase 2 does not change them.

## 6. Redaction and data at rest

* Response bodies are kept **raw in memory** because agents need real ids and session tokens to
  execute and verify. They are never logged: the API client logs method, URL, status and duration
  only.
* Every persisted artifact goes through `core/shared/evidence.py` / `core/runtime/jsonlog.py`,
  which redact by secret-key pattern (`authorization`, `cookie`, `password`, `token`, `secret`,
  `session`, ...), by known credential formats, and by nested key traversal.
* Evidence and log files are written with mode 0600. The metadata database is 0600.
* Reports contain steps, statuses and evidence paths; they do not contain tokens or passwords.

## 7. Project isolation

All Phase 2 rows (`findings`, `finding_events`, `invariants`, `missions`, `journeys`,
`app_maps`, `exploration_states`) carry `project_id`. Reads are always filtered by it; there is
no cross-project query in the store. App maps are written under
`intelligence/<project_id>/`. Global assets (probes, classifiers, agents) contain no
project-specific facts, so knowledge cannot leak between projects.

## 8. Time-of-check / time-of-use and verifier independence

The discovering agent and the verifier use separate `ProbeContext` instances and fresh logins.
The verifier rebuilds the reproduction from the stored payload rather than trusting the
discovering agent's claimed result. A finding whose reproduction fails is downgraded, not
preserved. This is the main control against self-confirming agents.

## 9. Residual risks and mitigations

| Risk | Mitigation |
| --- | --- |
| A probe mutates state on a shared test environment | bounded calls; no destructive methods; drain preconditions use the app's own API; use disposable environments for adversarial mode |
| A test credential file leaks | 0600 requirement, test-only accounts, never persisted in artifacts |
| OpenAPI document is untrusted input | only reduced to method/path/schema; never executed; payloads built from data |
| Browser exploration follows external links | same-origin filter and `browser.allowed_origins` allowlist |
| Host header / SSRF style redirects | `follow_redirects=False` in the HTTP client; netguard validates every URL |
| Secret in a non-standard field name | value-pattern redaction (connection strings, cloud keys, JWTs) plus explicit "no secrets in evidence" policy |
| Budget bypass via parallel workers | `parallel()` charges all requests up front and caps workers |