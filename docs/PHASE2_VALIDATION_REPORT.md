# Phase 2 validation report

This report records what was actually installed, executed and observed for the KHUNKHAAR Phase 2
"Autonomous Adversarial QA Intelligence Engine". It contains no projected results: every number
below comes from a run artifact under `reports/` or `evidence/`, or from a command whose output is
quoted.

- Date: 2026-10-30
- Installation: `/home/omchoksi/KHUNKHAAR-QA`
- Launcher: `/home/omchoksi/.local/bin/khunkhaar` (version `0.2.0`; the validated run below was
  produced on `0.1.0`, before the Phase 2 version bump, with identical engine code)
- Final validated run: `phase2-20261030-144021`
- Final report: `reports/phase2/vulnapp-99adb8c2/reports/20261030-144100-qa-report.md`

## 1. Environment

| Item | Value |
| --- | --- |
| Python (venv) | 3.12.3 |
| Playwright browsers | Chromium 151.0.7922.34, Firefox 153.0, WebKit 26.5 |
| OpenCode | v2.2.0 |
| MCP servers | 4/4 connected (`khunkhaar-browser`, `khunkhaar-api`, `khunkhaar-database`, `khunkhaar-filesystem`) |
| Metadata store | SQLite schema 1, 3 projects, 13 runs |
| Synthetic target | FastAPI + SQLite at `http://127.0.0.1:8740` |
| Database file | `runtime/tmp/synthetic/vulnapp.db` |

`khunkhaar --doctor` exits `0` and prints `all required tools available` (23 checks). The only
non-`ok` row is `postgres-test-profile: needs-env`, which is expected in a shell where the Phase 1
test database password is not loaded; it is not a Phase 2 dependency.

## 2. Commands executed

Discovery only (no network):

```bash
khunkhaar scan --project-root tests/fixtures/vulnapp
```

Full end-to-end validation of the synthetic application:

```bash
khunkhaar scan \
  --project-root tests/fixtures/vulnapp \
  --target http://127.0.0.1:8740 \
  --db runtime/tmp/synthetic/vulnapp.db \
  --users tests/fixtures/vulnapp/qa-users.json \
  --mode adversarial --full --yes
```

Report regeneration from the findings store:

```bash
khunkhaar report --project-root tests/fixtures/vulnapp
```

Independent verification of stored findings:

```bash
khunkhaar verify \
  --project-root tests/fixtures/vulnapp \
  --target http://127.0.0.1:8740 \
  --db runtime/tmp/synthetic/vulnapp.db \
  --users tests/fixtures/vulnapp/qa-users.json \
  --mode controlled --yes
```

Phase 2 engine regression suite:

```bash
.venv/bin/python -m pytest tests/phase2 -q      # 33 passed
.venv/bin/python -m pytest tests --ignore=tests/phase2 -q  # 134 passed (Phase 1)
.venv/bin/python -m pytest tests -q             # 167 passed
```

OpenCode skill registration check (server API, no model call):

```bash
opencode serve --port <port>
curl -u opencode:<password> 'http://127.0.0.1:<port>/api/skill?location[directory]=/home/omchoksi/KHUNKHAAR-QA'
```

Observed: 269 registered skills, including `khunkhaar-scan`, `khunkhaar-verify` and
`khunkhaar-triage` from `.opencode/skills/<name>/SKILL.md`.

## 3. Discovery result

From `reports/phase2/vulnapp-99adb8c2/phase2-20261030-144021-run.json`:

| Metric | Value |
| --- | --- |
| Framework | FastAPI |
| Endpoints | 14 |
| Entities | 9 |
| Documented rules / invariants | 8 / 8 |
| Features | `clock`, `clock-page`, `events`, `login`, `reports`, `reports-page`, `requests`, `requests-page` |
| OpenAPI operations used at runtime | 14 |
| Roles discovered | `user`, `other`, `admin` |
| Database reachable | yes |

The engine received only `tests/fixtures/vulnapp/docs/BUSINESS_RULES.md` (R1–R8) and the running
application. The injected defect list was not passed to any agent or config file.

## 4. Final end-to-end run

| Metric | Value |
| --- | --- |
| Run id | `phase2-20261030-144021` |
| Command / mode | `all` / `adversarial` |
| Missions planned / executed / skipped / failed | 22 / 22 / 0 / 0 |
| Findings | 9 total, 9 `confirmed` |
| Verification | every finding reproduced 2/2 independent attempts |
| Steps | 29 / 160 |
| Requests | 180 / 400 |
| Browser sessions | 7 / 8 |
| Elapsed | 7.18 s / 900 s |
| Cancelled | no |

All eight specialist agents executed at least one mission:

| Agent | Missions | Issues |
| --- | --- | --- |
| `user_simulator` | 1 | 0 |
| `workflow_breaker` | 4 | 5 |
| `edge_hunter` | 1 | 1 |
| `security_hunter` | 2 | 1 |
| `ux_inspector` | 3 | 1 |
| `api_hunter` | 1 | 1 |
| `db_hunter` | 2 | 2 |
| `regression_intel` | 8 | 8 |

`user_simulator` is an exploration agent: it produced navigation history but no finding on this
target, which is a valid outcome. `regression_intel` replayed all eight known findings by id.

## 5. Injected defect coverage

The synthetic application contains eight deliberate defects. Each was discovered, reproduced by
the independent verifier, and persisted with evidence:

| # | Documented rule | Injected defect | Finding | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| 1 | R1 | Missing object-level authorization (IDOR) | `F-a7a00b40a7` | confirmed 2/2 | `evidence/phase2/phase2-20261030-144021/idor-105.json` |
| 2 | R2 | Duplicate submission creates a second record | `F-fc83e8e0a5` | confirmed 2/2 | `evidence/phase2/phase2-20261030-144021/duplicate-submission.json` |
| 3 | R3 | Approval allowed before submit | `F-dafce80e45` | confirmed 2/2 | `evidence/phase2/phase2-20261030-144021/workflow-103.json` |
| 4 | R4 | Missing amount/title validation | `F-ec1f702b22` | confirmed 2/2 | `evidence/phase2/phase2-20261030-144021/validation.json` |
| 5 | R5 | Check-then-insert race on clock-in | `F-857523eca4` | confirmed 2/2 | `evidence/phase2/phase2-20261030-144021/concurrency.json` |
| 6 | R6 | Local (+05:30) day boundary instead of UTC | `F-5260235d97` | confirmed 2/2 | `evidence/phase2/phase2-20261030-144021/timezone-boundary.json` |
| 7 | R7 | Audit row written before the 404 validation | `F-fc885e62fd` | confirmed 2/2 | `evidence/phase2/phase2-20261030-144021/atomicity-action.json` |
| 8 | R8 | Fixed 1200px table overflows a 390px viewport | `F-0ba40c1179` | confirmed 2/2 | `evidence/phase2/phase2-20261030-144021/responsive.json` |

A ninth finding, `F-c4a7f5e9f2`, records accepted workflow mutations on `requests`. It is the
journey-level grouping of the same state-machine defect (R3) and is deliberately kept separate
from the rule-level finding; the report lists both.

Raw issue evidence also includes `journey-J-*.json`, `mutation-J-*.json`, `auth-boundary.json`,
`accessibility.json` and `link-health.json`.

## 6. Verification and false-positive reduction

Verification re-executes the recorded reproduction with fresh clients. Discovery agents cannot
set a status themselves.

- The final run auto-verified all nine findings; each produced `violated: true` on two
  independent attempts, recorded under `evidence/phase2/phase2-20261030-144021/verification/`.
- An earlier over-broad `auth_boundary` finding (`F-637b251b8c`) claimed that unauthenticated HTML
  pages were accessible. The verifier rebuilt the probe after the JSON-only filter was added and
  returned `not_reproducible` 0/2
  (`logs/runs/phase2-20261030-141044.jsonl`, line 10). The finding was downgraded rather than
  reported.
- An early concurrency attempt returned `not_reproducible` because stale open shifts caused the
  burst to be rejected for the wrong reason
  (`logs/runs/phase2-20261030-141948.jsonl`, line 36). The probe was corrected to drain any open
  shift before the burst; the subsequent run confirmed the race 2/2.
- No finding in the final report is below `confirmed`, and no infrastructure error was converted
  into a bug claim.

## 7. Defects found in KHUNKHAAR during validation (fixed)

These were real engine defects exposed by executing the engine, not by inspection:

1. **Redaction corrupted contract data in memory.** `ApiClient._decode` redacted JSON before
   agents consumed it, turning `{"password": {"type": "string"}}` into a string and breaking
   login/schema parsing. Fixed by keeping response bodies raw in memory and redacting only at the
   persistence boundary.
2. **Auth-boundary false positive on HTML routes.** The probe treated any `200` from `/` and
   `/clock-page` as unauthenticated access. Fixed by recording response content types and probing
   JSON endpoints only.
3. **Standalone probes missing from the verifier registry.** `auth_boundary`, `accessibility` and
   `links` could be discovered but not re-verified (`no probe available for kind`). Added to
   `PROBE_BY_KIND`.
4. **Concurrency probe polluted by accumulated shifts.** Added a bounded drain via the matching
   clock-out operation before firing parallel requests.
5. **Journey missions proposed for features without a viable journey.** Scan failed on
   `clock`/`clock-page`. Fixed by filtering features with `build_journey(...) is not None`.
6. **Responsive check used an inflated viewport width.** Under 390px emulation with fixed content,
   `window.innerWidth`/`scrollWidth` reported 1216. Switched to `document.documentElement.clientWidth`
   (390) and the real 1200px offender was detected.
7. **Browser budget too small.** Three missions exhausted three sessions before responsive and
   regression replays ran. Raised `controlled` to 4 and `adversarial` to 8; the final run used 7/8.
8. **Regression notes said `regression replay of None`.** Findings expose `id`, not `finding_id`;
   the generator now reads `finding.get("id")`.
9. **Mission deduplication collision.** The invariant/regression key test checked a prefix that
   never matched, so regression missions were deduplicated against invariant probes. Keying was
   corrected and a regression test added.
10. **Stale summary counts.** The CLI printed `findings.by_status` from discovery instead of the
    post-verification store summary; it now prints `findings_stats.by_status`.

Each fix has at least one regression test in `tests/phase2/`.

## 8. Test results

| Suite | Result |
| --- | --- |
| Phase 1 infrastructure (`tests`, excluding `tests/phase2`) | 134 passed |
| Phase 2 engine (`tests/phase2`) | 33 passed |
| Combined | 167 passed in 19.57 s |

Phase 2 tests cover the findings model and event history, budget enforcement/cancellation, mission
generation/deduplication, journey construction and mutations, contract redaction, probe registry
completeness and the verifier decision table.

## 9. Phase 1 compatibility

- `khunkhaar --version` → `khunkhaar 0.2.0  (root: /home/omchoksi/KHUNKHAAR-QA)`
- `khunkhaar --doctor` → exit 0, `all required tools available`, MCP `4/4`.
- Phase 1 suite (134 tests) passes unchanged; MCP stdio boot tests and the mock server remain
  operational.
- No Phase 1 configuration was overwritten. The OpenCode config backup from Phase 1 remains at
  `~/.config/opencode/opencode.json.bak-20261030-121904`.

## 10. Definition-of-Done checklist

| Requirement | Status | Evidence |
| --- | --- | --- |
| Central orchestrator + 8 specialist agents | done | `core/orchestrator/engine.py`, `core/agents/specialists.py`; final run agent table |
| No mock agents | done | agents execute real HTTP/browser/DB probes |
| Application intelligence (routes, models, schema, APIs, auth, rules) | done | `intelligence/vulnapp-99adb8c2/app-map.json`, 8 rules with evidence |
| Observe → Think → Act → Verify → Learn with anti-loop | done | exploration state persisted per project; user simulator history in run notes |
| Journey mutation with budgets | done | 22 missions within 29 steps / 180 requests / 7 sessions |
| Business-invariant engine | done | 8 invariants extracted; `core/invariants/` |
| Independent verification with statuses and rates | done | 9/9 confirmed 2/2; false-positive downgrade recorded |
| Severity separate from confidence | done | report columns and schema |
| Strict false-positive reduction | done | two false positives downgraded, none in final report |
| CLI `scan/explore/security/api/workflows/verify/report` | done | `khunkhaar --help`; commands exercised |
| Two knowledge scopes, no cross-project leaks | done | project-scoped metadata and evidence; isolation suite passes |
| Three modes with authorization gate | done | `--yes` required; adversarial run recorded |
| Execution control (budgets, cancellation, logs, no self-permission changes) | done | `core/runtime/budget.py`; budget tests; JSONL logs |
| Synthetic app with 8 injected defects, not hardcoded | done | 8/8 found and confirmed; rules file is the only spec |
| 7 Phase 2 documents | done | this document plus architecture, catalog, methodology, security, schema, limitations |
| CHANGELOG updated | done | `CHANGELOG.md` Phase 2 section |
| Phase 1 still operational | done | 134 tests, `--doctor` exit 0 |
| OpenCode reusable skills | done | 3 skills under `.opencode/skills/`, listed by the OpenCode server API |

## 11. Evidence index

- Run summary: `reports/phase2/vulnapp-99adb8c2/phase2-20261030-144021-run.json`
- Markdown report: `reports/phase2/vulnapp-99adb8c2/reports/20261030-144100-qa-report.md`
- JSON report: `reports/phase2/vulnapp-99adb8c2/reports/20261030-144100-qa-report.json`
- Probe evidence: `evidence/phase2/phase2-20261030-144021/`
- Verification records: `evidence/phase2/phase2-20261030-144021/verification/`
- Structured log: `logs/runs/phase2-20261030-144021.jsonl`
- App map: `intelligence/vulnapp-99adb8c2/app-map.json`

## 12. How to reproduce

1. Start the synthetic application (disposable, loopback only):

   ```bash
   .venv/bin/python -m uvicorn tests.fixtures.vulnapp.app:app --host 127.0.0.1 --port 8740
   ```

2. Run the command in section 2. Use a fresh database file or reset
   `runtime/tmp/synthetic/vulnapp.db` to avoid accumulated shifts and requests.
3. Inspect the generated report and evidence paths. Do not run the adversarial mode against any
   target that is not disposable or explicitly authorized.