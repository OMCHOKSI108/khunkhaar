# Senior QA Architecture & Quality Audit

## 1. Audit of Existing Khunkhar Implementation

### Architecture Overview
Khunkhar currently consists of three historical phases:
1. **Phase 1 (Infrastructure & Toolchain):** 4 MCP stdio servers (`khunkhaar-api`, `khunkhaar-browser`, `khunkhaar-database`, `khunkhaar-filesystem`), Playwright integration with vendored WebKit libraries, basic CLI wrapper, and static security tools.
2. **Phase 2 (Autonomous Adversarial QA Engine):** 8 specialist agents (`user_simulator`, `workflow_breaker`, `edge_hunter`, `security_hunter`, etc.), static regex route mapper, OpenAPI contract reducer, journey runner, and independent verifier.
3. **Phase 3 (Git-Aware Intelligence):** Read-only git analyzer, AST-based symbol attribution, impact dependency graph, attention risk scoring, and a read-only loopback dashboard.

---

### Root Causes of Shallow, Generic, and Brittle Testing

1. **Rigid, Narrow Route Discovery:**
   `core/intelligence/mapper.py` relies on 5 simple regular expressions tuned almost exclusively to standard REST routes (`app.get()`, `@app.route()`). It completely missed GraphQL (`type Query`, `type Mutation`), Next.js App Router (`app/api/.../route.ts`), Streamlit, NestJS controllers, Django REST Framework viewsets, and Laravel controllers. On modern multi-server applications (such as `lw-hub-main-server`), it discovered only 2 endpoints out of 284 operations and 0 invariants, causing test runs to exit in seconds.

2. **Absence of Dynamic Server & Port Discovery:**
   The engine assumed a `--target` URL would always be passed manually. It has no mechanism to inspect running local ports (`ss`, `lsof`, `netstat`), detect Docker compose services, or health-check whether an application is on port 3000, 8000, 8501, or 5000.

3. **OpenCode Integration Gap:**
   While MCP servers were exposed, OpenCode was launched either with a generic context prompt or required manual multi-step interactions. There was no autonomous multi-phase pipeline (Reconnaissance -> Strategy -> Execution -> Verification -> Reporting) orchestrated with persistent progress.

4. **Lack of Side-Effect & Notification Assertions:**
   Tests traditionally checked only the HTTP status code (200 OK). Side effects such as notifications, audit logs, and database row transitions were not automatically validated against documented requirements.

5. **No Persistent Testing Workspace:**
   Test progress and evidence were scattered across `reports/` and `evidence/`, rather than maintaining a trackable, resumable project workspace in `.khunkhar/` containing architecture, test cases, and execution logs.

---

## 2. Redesign Plan: The Autonomous Senior QA Engine

To transform Khunkhar into a true Senior QA Architect, we will implement:

1. **Universal Project Reconnaissance Engine (`core/recon/`):**
   - Universal framework detection (FastAPI, Flask, Django, Streamlit, Express, NestJS, Next.js, GraphQL, Spring, Laravel, etc.).
   - Dynamic port & service discovery (auto-detects live frontend, backend, GraphQL, and database ports).
   - Document & business rule ingestion from project `docs/`, `README`, `.env.example`, and source code.

2. **Adaptive Test Strategy & Test Case Generator (`core/strategy/`):**
   - Generates structured, prioritized test cases covering the 18 mandatory QA categories.
   - Tailors testing strategy to project type (e.g. Streamlit UI testing vs GraphQL backend RBAC testing).

3. **Persistent Testing Workspace (`.khunkhar/`):**
   - Automatically maintains `project-context.md`, `architecture.md`, `test-strategy.md`, `test-cases.json`, `execution-log.json`, `bugs/`, and `final-report.md`.
   - Supports resumable execution.

4. **One-Command Autonomous Execution (`khunkhar`):**
   - Running `khunkhar` with no flags automatically executes the full reconnaissance, environment discovery, test generation, deep execution, and final reporting.
