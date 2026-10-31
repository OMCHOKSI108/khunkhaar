# Phase 2 — Findings Schema

Phase 2 stores everything in the existing platform SQLite database
(`data/metadata/platform.sqlite3`, mode 0600). The schema is additive: Phase 1 tables are
untouched, and `store.init()` migrates existing databases in place.

## 1. Tables

```sql
app_maps(project_id, generated_at, path, summary_json,
         PRIMARY KEY (project_id, generated_at))

invariants(id, project_id, rule_id, description, source, feature, roles, method,
           confidence, entities, created_at)

missions(id, project_id, kind, title, target, spec_json, status, created_at)

journeys(id, project_id, name, steps_json, created_at)

findings(id, project_id, fingerprint UNIQUE, feature, category, title, description,
         severity, confidence, reproducibility, status, role, initial_state,
         steps_json, expected, actual, evidence_json, remediation, source_refs,
         rule_ids, invariant_id, reproduction_json, created_at, updated_at)

finding_events(id AUTOINCREMENT, finding_id REFERENCES findings(id) ON DELETE CASCADE,
               at, actor, action, note, evidence_json)

exploration_states(id AUTOINCREMENT, run_id, project_id, state_hash, url,
                   actions_json, created_at)
```

All rows carry `project_id`. The store exposes only project-filtered reads; there is no
cross-project query.

## 2. Enumerations

**Categories** (`core/findings/models.py`): `functional`, `security`, `ux`, `accessibility`,
`api`, `database`, `performance`, `concurrency`, `business_logic`, `reliability`.

**Severities** (business impact): `critical`, `high`, `medium`, `low`, `informational`.

**Statuses**: `unverified`, `likely`, `confirmed`, `not_reproducible`, `false_positive`,
`needs_human`.

Only the independent verifier may move a finding out of `unverified`
(`VERIFIER_STATUSES = STATUSES - {"unverified"}`).

**Confidence by status**:

| Status | Confidence |
| --- | --- |
| `confirmed` | 0.95 |
| `likely` | 0.70 |
| `unverified` | 0.40 |
| `needs_human` | 0.30 |
| `not_reproducible` | 0.10 |
| `false_positive` | 0.05 |

Severity and confidence are deliberately independent: a high-impact issue that reproduced only
once is `high` severity with `likely` status and 0.70 confidence.

## 3. Identity and deduplication

`fingerprint = sha256(project_id | category | normalised(feature) | normalised(title) |
rule_id)[:24]` and `finding_id = "F-" + fingerprint[:10]`.

Normalisation lowercases and collapses non-alphanumerics, so a rediscovered defect with different
spacing/punctuation updates the existing row instead of creating a second one. Journey mutations
for the same feature share one title, so several accepted mutations update one workflow finding.

## 4. Reproduction

`reproduction_json` is the machine-readable recipe the verifier replays:

```json
{
  "type": "probe",
  "probe_kind": "authorization",
  "invariant": {"id": "INV-1", "kind": "authorization", "feature": "requests",
                "description": "A user may only read their own requests; ...",
                "source": "docs/BUSINESS_RULES.md:5", "rule_id": "1"}
}
```

or, for journey findings:

```json
{"type": "journey", "feature": "requests",
 "journey": {"steps": [...]}, "mutation": {"name": "skip to approve", "steps": [...]}}
```

`reproducibility` is the verifier's ratio, e.g. `"2/2"`, `"1/2"`, `"0/2"`.

## 5. Example finding (real, from the synthetic validation run)

```json
{
  "id": "F-a7a00b40a7",
  "project_id": "vulnapp-99adb8c2",
  "fingerprint": "a7a00b40a76472044cc88d91",
  "feature": "requests",
  "category": "security",
  "title": "A user may only read their own requests; another user's request must be refused.",
  "description": "Rule: ... Expected: a user other than the owner receives 403/404 ... Observed: HTTP 200 and the owner's data was returned.",
  "severity": "high",
  "confidence": 0.95,
  "reproducibility": "2/2",
  "status": "confirmed",
  "role": "user",
  "initial_state": "status=adversarial target=http://127.0.0.1:8740",
  "steps": [
    "user creates a resource via POST /api/requests",
    "resource id = 87",
    "other requests GET /api/requests/87 without owning the resource"
  ],
  "expected": "a user other than the owner receives 403/404 for another user's resource",
  "actual": "HTTP 200 and the owner's data was returned",
  "evidence": {"probe": "evidence/phase2/phase2-20261030-144021/idor-87.json"},
  "remediation": "Enforce object-level ownership on every read/write; return 404 for resources the caller does not own.",
  "source_refs": ["docs/BUSINESS_RULES.md:5"],
  "rule_ids": ["1"],
  "invariant_id": "INV-1"
}
```

## 6. Event history

Every discovery, rediscovery, status change and verification note is appended to
`finding_events`:

```
security_hunter        discovered          <title>
independent_verifier   status:confirmed    reproduced 2/2 independent attempts
security_hunter        rediscovered        <title>
regression_intel       rediscovered        Regression: <title>
```

`action` is `discovered`, `rediscovered`, `status:<status>`, or a free-form actor action. The
event row carries the evidence path, so the full verification history is auditable after the
run.

## 7. Invariants

Each invariant row records the rule text, its source `file:line`, the bound feature, the
verification method, a confidence, the involved entities, and `origin` (explicit statement vs
inferred). Invariants are regenerated from the repository on every run; a rule removed from the
documentation no longer produces an invariant, but previously confirmed findings remain and
continue to be exercised by regression missions.

## 8. Migration

`core/findings/store.py`:

1. ensures the Phase 1 metadata schema,
2. executes `CREATE TABLE IF NOT EXISTS` for the Phase 2 tables,
3. checks `PRAGMA table_info(findings)` and `ALTER TABLE ... ADD COLUMN` for `invariant_id` and
   `reproduction_json` on databases created by an earlier Phase 2 build,
4. sets the database file to mode 0600.

The migration is idempotent and safe to run on every command.