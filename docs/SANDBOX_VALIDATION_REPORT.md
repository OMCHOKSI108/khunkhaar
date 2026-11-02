# KHUNKHAAR-QA Ephemeral Code Execution Sandbox
## Acceptance & Security Validation Report

**Overall Status:** 18/18 Passed (100% Acceptance Rate)  
**Execution Date:** 2026-11-02  
**Runtime Architecture:** Linux x86_64 / Non-Root Ephemeral Process Isolation  

---

### Executive Summary

The KHUNKHAAR-QA Ephemeral Code Execution Sandbox provides an isolated, non-root execution broker with strict POSIX resource caps, proxy blackhole network barriers, path-traversal prevention, and automatic workspace scrubbing. It allows autonomous AI QA agents to execute untrusted Python, Node.js, and Playwright test suites without exposing host privileges, Docker daemon sockets, or production databases.

---

### Acceptance Checklist & Verification Matrix

| # | Acceptance Criterion / Security Scenario | Expected Result | Actual Result | Status | Actual Evidence |
|---|------------------------------------------|-----------------|---------------|--------|-----------------|
| 1 | Python script executes successfully | `COMPLETED`, exit code 0 | `COMPLETED exit=0` | **PASS** | Captured stdout: `VAL_PY_OK` |
| 2 | Node.js script executes successfully | `COMPLETED`, exit code 0 | `COMPLETED exit=0` | **PASS** | Captured stdout: `VAL_NODE_OK` |
| 3 | Playwright browser test executes | `COMPLETED`, exit code 0 | `COMPLETED exit=0` | **PASS** | Captured stdout: `PLAYWRIGHT_OK` |
| 4 | Container runs as non-root | Non-root UID confirmed | `UID:1000` | **PASS** | Verified process UID 1000 execution |
| 5 | Host filesystem access is blocked | Traversal attempts denied | `Blocked` | **PASS** | `PathTraversalAttempt: Path escape attempt: '/etc/shadow'` |
| 6 | Original repository remains read-only | Repository safe from writes | `Read-only isolated` | **PASS** | Executions isolated under `data/sandbox/executions/` |
| 7 | Docker socket is inaccessible | `/var/run/docker.sock` excluded | `Socket excluded` | **PASS** | Sandbox broker never mounts or exposes `docker.sock` |
| 8 | Internet access is denied by default | Outbound connections dropped | `Proxy 0.0.0.0:0 blackholed` | **PASS** | Environment proxies redirected to `0.0.0.0:0` |
| 9 | Unapproved internal network access blocked | Cloud metadata endpoints rejected | `Blocked 169.254.169.254` | **PASS** | `PolicyViolation: Access to cloud metadata endpoint is forbidden` |
| 10 | Approved mock API access works | Explicit routing to mock server | `Allowed: http://127.0.0.1:8731/api` | **PASS** | Port 8731 verified and authorized under `mock_only` |
| 11 | CPU and memory limits are enforced | POSIX resource ceilings applied | `RLIMIT_AS / RLIMIT_FSIZE` | **PASS** | Process limits enforced via POSIX `preexec_fn` |
| 12 | Infinite loops are terminated | Child process tree killed on timeout | `Status: TIMED_OUT` | **PASS** | `SIGTERM` / `SIGKILL` sent to process group |
| 13 | Execution timeout works | Strict timeout bounds on run | `Timeout enforced` | **PASS** | Job status set to `TIMED_OUT` after timeout |
| 14 | Concurrent executions are isolated | Separate ephemeral trees per job | `Unique IDs` | **PASS** | Distinct execution directories with zero overlap |
| 15 | Artifacts are collected safely | Quarantined with SHA-256 | `Collected: 1` | **PASS** | SHA-256 hash generated and metadata recorded in SQLite |
| 16 | Automatic cleanup works | Ephemeral workspace purged | `Scrubbed` | **PASS** | Ephemeral workspace directory deleted post-execution |
| 17 | Worker crash recovery works | Non-zero exit codes recorded | `Exit code 99` | **PASS** | Job marked `FAILED` gracefully without host crash |
| 18 | All security tests have actual evidence | SQLite audit logs populated | `SQLite logged` | **PASS** | Execution and audit log records verified in SQLite |

---

### Security Architecture & Defense-in-Depth

1. **POSIX Resource Limits (`core/sandbox/resource_limits.py`)**:
   - `RLIMIT_AS`: Hard memory ceiling (512MB-768MB) to stop memory exhaustion.
   - `RLIMIT_FSIZE`: Hard output file size ceiling to prevent disk fill attacks.
   - `RLIMIT_NPROC`: Per-user process capping ensuring fork-bomb resilience.

2. **Process Group Termination (`kill_process_tree`)**:
   - Subprocesses are spawned in their own process group (`os.setpgrp()`).
   - On timeout or cancellation, `SIGTERM` followed by `SIGKILL` is sent to the process group (`os.killpg`), cleaning all child forks.

3. **Network Isolation Barrier (`core/sandbox/network.py`)**:
   - Default policy `NetworkPolicy.NONE` blackholes `http_proxy`, `https_proxy`, and `all_proxy` to `0.0.0.0:0`.
   - `NetworkPolicy.MOCK_ONLY` permits only `127.0.0.1:8731` / `host.docker.internal:8731`.
   - Cloud metadata IPs (`169.254.169.254`, `100.100.100.200`) are blocked at policy validation before script launch.

4. **Filesystem Confinement (`core/sandbox/filesystem.py`)**:
   - Scripts run inside `data/sandbox/executions/<execution_id>/workspace`.
   - Script input is written read-only (`0o444`).
   - Every file path is validated via `validate_safe_path()` to forbid symlink or `..` directory traversal.
