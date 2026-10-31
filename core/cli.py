#!/usr/bin/env python3
"""KHUNKHAAR QA command line.

    khunkhaar                discover the current project and launch the QA OpenCode environment
    khunkhaar --version      print the platform version
    khunkhaar --doctor       check every dependency the platform depends on
    khunkhaar --project-info print what discovery sees for the current directory
    khunkhaar --help

Future phases add `scan`, `explore`, `security` and `report`; they are declared here so the
interface is stable, but they refuse to run until the behaviour exists.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
# Only fixed argv lists, never a shell.
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import commands as command_registry  # noqa: E402
from core.metadata import store  # noqa: E402
from core.project_discovery import detector  # noqa: E402
from core.shared import paths  # noqa: E402

MCP_SERVERS = ("khunkhaar-browser", "khunkhaar-api", "khunkhaar-database", "khunkhaar-filesystem")
PHASE2_COMMANDS = ("scan", "explore", "security", "api", "workflows", "verify", "report")
FUTURE_COMMANDS: dict[str, str] = {}

_PHASE3_HELP = "\n".join(command_registry.help_lines())

HELP = f"""KHUNKHAAR QA {paths.version()} — centralized QA infrastructure

USAGE
  khunkhaar                     discover the current project and launch the QA environment
  khunkhaar --doctor            verify every tool this platform depends on
  khunkhaar --project-info      show what discovery sees (no launch)
  khunkhaar --version           print the version
  khunkhaar --help              this text

PHASE 2 COMMANDS
  khunkhaar scan                map the application and extract documented invariants
  khunkhaar explore             bounded browser exploration (Observe → Think → Act → Verify)
  khunkhaar security            authorization, IDOR and unauthenticated-access probes
  khunkhaar api                 API contract and input-validation probes
  khunkhaar workflows           journey mutation + idempotency + concurrency probes
  khunkhaar verify              independently re-run unverified findings
  khunkhaar report              write JSON + Markdown reports from the findings store

PHASE 3 COMMANDS
{_PHASE3_HELP}

COMMON FLAGS
  --target URL                  application under test (required by every command except scan/report)
  --project-root PATH           repository to analyse (default: current directory)
  --db PATH                     SQLite database for persistence verification (read-only)
  --users PATH                  JSON file mapping role -> username/password (test credentials only)
  --mode observe|controlled|adversarial
  --max-steps N --max-requests N --max-seconds N --max-browser-sessions N
  --max-missions N --no-verify --json --yes

OPTIONS
  --json                        machine-readable output for --doctor / --project-info
  --no-discovery                launch without re-scanning the project
  --no-write                    discover but do not update the project registry
  --print-launch                print the exact opencode command instead of running it
  --root PATH                   override the KHUNKHAAR root (default: this installation)

ENVIRONMENT
  KHUNKHAAR_ROOT                platform root
  KHUNKHAAR_OPENCODE_ARGS       extra arguments passed to opencode at launch
"""


def _say(message: str = "") -> None:
    print(message)


# ---------------------------------------------------------------- launch


def _load_credentials() -> list[str]:
    """Load operator-owned profile env files so DB MCP tools can authenticate.

    Values stay in this process environment and are inherited by the MCP servers OpenCode
    spawns; they are never written to configuration, logs or reports.
    """
    loaded = []
    base = paths.profiles_dir() / "db"
    if not base.is_dir():
        return loaded
    for env_file in sorted(base.glob("*.env")):
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip().strip('"').strip("'")
            loaded.append(env_file.name)
        except OSError:
            continue
    return loaded


def _context_prompt(profile: dict) -> str:
    lines = [
        f"KHUNKHAAR QA session for project '{profile['name']}' ({profile['project_id']}).",
        f"Project root: {profile['root']}",
        f"Type: {profile['project_type']}; languages: "
        f"{', '.join(l['name'] for l in profile['languages'][:6]) or 'unknown'}; "
        f"frameworks: {', '.join(profile['frameworks'][:8]) or 'none detected'}.",
        f"Full profile: {paths.project_dir(profile['project_id']) / 'project-profile.json'}",
        "Use the khunkhaar-* MCP tools for browser, API, database and evidence work. Browser "
        "navigation is limited to browser.allowed_origins; API calls are limited to "
        "api.allowed_hosts; database access is read-only and row-capped. Store screenshots, "
        "traces and reports under the KHUNKHAAR evidence/reports tree, not in the project.",
    ]
    return "\n".join(lines)


def _opencode_binary() -> str | None:
    return shutil.which("opencode")


def cmd_launch(args: list[str]) -> int:
    discovery = "--no-discovery" not in args
    print_launch = "--print-launch" in args

    store.init()
    profile = detector.detect()
    if not profile.get("valid_project"):
        _say(f"khunkhaar: {profile.get('reason')}")
        _say(f"khunkhaar: run this inside a project directory, or use --project-info to inspect {Path.cwd()}")
        return 2

    if discovery:
        if "--no-write" not in args:
            target = detector.write_profile(profile)
            _say(f"project: {profile['project_id']}  ({profile['project_type']})")
            _say(f"profile: {target}")
        else:
            _say(f"project: {profile['project_id']}")
    else:
        stored = store.get_project(detector.project_id_for(Path(profile["root"])))
        if stored and stored.get("profile_path"):
            profile = json.loads(Path(stored["profile_path"]).read_text(encoding="utf-8"))

    loaded = _load_credentials()
    if loaded:
        _say(f"credentials: {len(loaded)} profile file(s) loaded into the session environment")

    binary = _opencode_binary()
    if binary is None:
        _say("khunkhaar: opencode CLI not found on PATH - install it before launching the QA interface")
        return 2

    extra = os.environ.get("KHUNKHAAR_OPENCODE_ARGS", "").split()
    command = [binary]
    if "--standalone" not in extra:
        command.append("--standalone")
    command += [*extra, "--prompt", _context_prompt(profile), profile["root"]]

    if print_launch:
        _say(json.dumps({"command": command, "cwd": profile["root"],
                         "env_loaded": loaded, "project_id": profile["project_id"]}, indent=2))
        return 0

    os.chdir(profile["root"])
    os.environ["KHUNKHAAR_ROOT"] = str(paths.kh_root())
    os.environ["KHUNKHAAR_PROJECT_ID"] = profile["project_id"]
    try:
        # Resolved absolute binary with an argv list.
        os.execv(binary, command)  # nosec B606
    except OSError as exc:
        _say(f"khunkhaar: could not start opencode: {exc}")
        return 2


# ---------------------------------------------------------------- project info


def cmd_project_info(json_output: bool) -> int:
    profile = detector.detect()
    if json_output:
        print(json.dumps(profile, indent=2, sort_keys=True))
        return 0 if profile.get("valid_project") else 1
    if not profile.get("valid_project"):
        _say(f"no project detected: {profile.get('reason')}")
        return 1
    _say(f"project id   {profile['project_id']}")
    _say(f"name         {profile['name']}")
    _say(f"root         {profile['root']}")
    _say(f"type         {profile['project_type']}")
    language_list = ", ".join(f"{entry['name']} ({entry['files']})" for entry in profile["languages"][:6])
    _say(f"languages    {language_list or '-'}")
    _say(f"frameworks   {', '.join(profile['frameworks']) or '-'}")
    infra = profile["infrastructure"]
    _say(f"docker       {'yes' if infra['docker'] else 'no'} "
         f"({len(infra['compose_services'])} compose service(s))")
    _say(f"databases    {', '.join(infra['databases']) or '-'}")
    _say(f"env vars     {len(infra['env_variable_names'])} name(s) recorded, values never read")
    tests = profile["existing_tests"]
    _say(f"tests        {', '.join(tests['frameworks']) or 'no framework found'}; "
         f"{tests['test_file_count']} test file(s)")
    api = profile["api_definitions"]
    specs = len(api["openapi"]) + len(api["graphql_schemas"]) + len(api["postman_collections"])
    _say(f"api specs    {specs} definition file(s)")
    _say(f"scanned      {profile['scan']['files_scanned']} files"
         f"{' (truncated)' if profile['scan']['truncated'] else ''}")
    return 0


# ---------------------------------------------------------------- doctor


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        # cmd is always a fixed argv list built by the doctor checks.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # nosec B603
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)[:200]
    output = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode, output.splitlines()[0][:200] if output else ""


def _check_browser() -> list[dict]:
    checks = []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - import failure means the venv is broken
        return [{"tool": "playwright", "status": "missing", "version": None, "detail": str(exc)}]
    with sync_playwright() as pw:
        for engine in ("chromium", "firefox", "webkit"):
            try:
                browser = getattr(pw, engine).launch(headless=True)
                version = browser.version
                browser.close()
                checks.append({"tool": f"playwright-{engine}", "status": "ok", "version": version, "detail": None})
            except Exception as exc:
                hint = "run scripts/install_webkit_deps.sh" if engine == "webkit" else None
                checks.append({"tool": f"playwright-{engine}", "status": "unavailable",
                               "version": None, "detail": hint or str(exc).splitlines()[0][:160]})
    return checks


def _check_opencode_mcp() -> dict:
    config = Path.home() / ".config" / "opencode" / "opencode.json"
    if not config.is_file():
        return {"tool": "opencode-mcp", "status": "missing", "version": None,
                "detail": f"{config} not found"}
    try:
        document = json.loads(config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"tool": "opencode-mcp", "status": "broken", "version": None, "detail": str(exc)}
    servers = document.get("mcp") or {}
    if isinstance(servers, dict) and isinstance(servers.get("servers"), dict):
        servers = servers["servers"]
    present = [name for name in MCP_SERVERS if isinstance(servers, dict) and name in servers]
    missing = [name for name in MCP_SERVERS if name not in present]
    status = "ok" if not missing else "partial"
    detail = ", ".join(present) or "no KHUNKHAAR servers registered"
    if missing:
        detail += f" | missing: {', '.join(missing)}"
    return {"tool": "opencode-mcp", "status": status, "version": f"{len(present)}/{len(MCP_SERVERS)}",
            "detail": detail}


def _check_db() -> dict:
    from core.database import adapters as ad

    profile = ad.profile_by_name(None)
    env_name = str(profile.get("password_env") or "")
    if env_name and not os.environ.get(env_name):
        return {"tool": "postgres-test-profile", "status": "needs-env", "version": None,
                "detail": f"{env_name} is not set in this shell (khunkhaar loads it at launch)"}
    try:
        info = ad.get_adapter(profile).server_info()
        return {"tool": "postgres-test-profile", "status": "ok", "version": info.get("version"),
                "detail": f"{info.get('database')} as {info.get('current_user')} read_only={info.get('read_only')}"}
    except Exception as exc:
        return {"tool": "postgres-test-profile", "status": "failed", "version": None,
                "detail": str(exc)[:200]}


def _check_mock_api() -> dict:
    import httpx

    from core.shared.config import current

    port = 8731
    for entry in current().get("api", "allowed_hosts", []) or []:
        text = str(entry)
        if ":" in text:
            port = int(text.rsplit(":", 1)[1])
            break
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
        return {"tool": "mock-api", "status": "ok" if response.status_code == 200 else "degraded",
                "version": response.json().get("service"), "detail": f"127.0.0.1:{port}"}
    except Exception:
        return {"tool": "mock-api", "status": "not-running", "version": None,
                "detail": "start: .venv/bin/python tools/api/mock_server.py (QA fixtures only)"}


def cmd_doctor(json_output: bool) -> int:
    store.init()
    checks: list[dict] = []

    checks.append({"tool": "python", "status": "ok", "version": sys.version.split()[0],
                   "detail": sys.executable})
    for tool, args in (("node", ["node", "--version"]), ("npm", ["npm", "--version"]),
                       ("docker", ["docker", "--version"]), ("git", ["git", "--version"])):
        code, line = _run(args)
        checks.append({"tool": tool, "status": "ok" if code == 0 else "missing",
                       "version": line if code == 0 else None, "detail": None if code == 0 else line})
    code, line = _run(["opencode", "--version"], timeout=15)
    checks.append({"tool": "opencode", "status": "ok" if code == 0 else "missing",
                   "version": line if code == 0 else None, "detail": None})
    checks.extend(_check_browser())
    for tool, args in (("k6", [str(paths.kh_root() / "bin" / "k6"), "version"]),
                       ("nuclei", [str(paths.kh_root() / "bin" / "nuclei"), "-version"]),
                       ("semgrep", ["semgrep", "--version"]),
                       ("bandit", [str(paths.venv_bin() / "bandit"), "--version"]),
                       ("pip-audit", [str(paths.venv_bin() / "pip-audit"), "--version"])):
        if not Path(args[0]).exists() and "/" in args[0]:
            checks.append({"tool": tool, "status": "missing", "version": None, "detail": args[0]})
            continue
        code, line = _run(args, timeout=20)
        checks.append({"tool": tool, "status": "ok" if code == 0 else "broken",
                       "version": line if code == 0 else None, "detail": None if code == 0 else line})
    axe = paths.kh_root() / "opt" / "axe" / "axe.min.js"
    axe_pkg = paths.kh_root() / "opt" / "axe" / "package.json"
    axe_version = None
    if axe_pkg.is_file():
        try:
            axe_version = json.loads(axe_pkg.read_text(encoding="utf-8")).get("version")
        except json.JSONDecodeError:
            axe_version = None
    checks.append({"tool": "axe-core", "status": "ok" if axe.is_file() else "missing",
                   "version": axe_version,
                   "detail": None if axe.is_file() else "run scripts/install_a11y.sh"})
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as dist_version

    for dist in ("hypothesis", "schemathesis", "tree-sitter", "tree-sitter-python",
                 "tree-sitter-javascript", "tree-sitter-typescript"):
        try:
            checks.append({"tool": dist, "status": "ok", "version": dist_version(dist), "detail": None})
        except PackageNotFoundError:
            checks.append({"tool": dist, "status": "missing", "version": None,
                           "detail": f"{paths.venv_bin() / 'pip'} install {dist}"})
    code, line = _run(["docker", "image", "inspect", "ghcr.io/zaproxy/zaproxy:stable", "--format",
                       "{{.Id}}"], timeout=15)
    checks.append({"tool": "zap-image", "status": "ok" if code == 0 else "missing",
                   "version": line[:12] if code == 0 else None,
                   "detail": None if code == 0 else "docker pull ghcr.io/zaproxy/zaproxy:stable"})
    checks.append(_check_opencode_mcp())
    checks.append(_check_db())
    checks.append(_check_mock_api())

    try:
        from core.metadata import migrations

        stats = store.stats()
        schema = migrations.status()
        behind = bool(schema["pending"])
        checks.append({"tool": "metadata-store", "status": "needs-migration" if behind else "ok",
                       "version": f"schema {stats['schema_version']}",
                       "detail": (f"{stats['projects']} project(s), {stats['qa_runs']} run(s)"
                                  + (f" | pending migrations {schema['pending']}" if behind else ""))})
    except Exception as exc:
        checks.append({"tool": "metadata-store", "status": "failed", "version": None, "detail": str(exc)})

    free = shutil.disk_usage(paths.kh_root()).free
    checks.append({"tool": "disk", "status": "ok" if free > 5 * 1024**3 else "low",
                   "version": f"{free / 1024**3:.1f} GiB free", "detail": None})

    for entry in checks:
        store.record_tool_health(entry["tool"], entry["status"], entry.get("version"), entry.get("detail"))

    if json_output:
        print(json.dumps({"ok": all(c["status"] in ("ok", "needs-env", "not-running") for c in checks),
                          "checks": checks}, indent=2, sort_keys=True))
    else:
        _say(f"KHUNKHAAR QA {paths.version()} — doctor")
        _say(f"{'TOOL':<26}{'STATUS':<14}{'VERSION':<24}DETAIL")
        for entry in checks:
            detail = (entry.get("detail") or "")[:70]
            _say(f"{entry['tool']:<26}{entry['status']:<14}{str(entry.get('version') or '-'):<24}{detail}")
        _say()
        blocked = [c for c in checks if c["status"] not in ("ok", "needs-env", "not-running")]
        _say("all required tools available" if not blocked else
             f"{len(blocked)} item(s) need attention: {', '.join(c['tool'] for c in blocked)}")
    return 0 if all(c["status"] in ("ok", "needs-env", "not-running") for c in checks) else 1


# ---------------------------------------------------------------- entry point


# ---------------------------------------------------------------- phase 2


def _phase2_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"khunkhaar {command}")
    parser.add_argument("--target", help="application base URL (http/https)")
    parser.add_argument("--project-root", default=".", help="repository to analyse")
    parser.add_argument("--db", dest="db_path", help="SQLite database used for persistence checks")
    parser.add_argument("--users", dest="users_file", help="JSON role -> credentials file")
    parser.add_argument("--mode", choices=("observe", "controlled", "adversarial"))
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--max-browser-sessions", type=int)
    parser.add_argument("--max-missions", type=int, default=60)
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--full", action="store_true",
                        help="scan: also run every mission allowed by the mode")
    parser.add_argument("--yes", action="store_true",
                        help="acknowledge adversarial testing against an authorized target")
    parser.add_argument("--json", action="store_true")
    return parser


def cmd_phase2(command: str, argv: list[str]) -> int:
    from core.metadata import store as meta_store
    from core.orchestrator.engine import Options, Orchestrator, OrchestratorError
    from core.reporting.report import write_report
    from core.runtime.budget import BudgetExceeded

    args = _phase2_parser(command).parse_args(argv)
    root = Path(args.project_root).expanduser().resolve()
    meta_store.init()

    if command == "report":
        profile = detector.detect(root)
        if not profile.get("valid_project"):
            _say(f"khunkhaar report: {profile.get('reason')}")
            return 2
        written = write_report(profile["project_id"])
        if args.json:
            print(json.dumps(written, indent=2))
        else:
            _say(f"report: {written['markdown']}")
            _say(f"json:   {written['json']}")
        return 0

    mode = args.mode or ("adversarial" if command == "workflows" else "controlled")
    effective = command
    if command == "scan" and args.full:
        effective = "all"
    if effective in ("all", "explore", "security", "api", "workflows") and not args.target:
        _say(f"khunkhaar {command}: --target is required for runtime testing")
        return 2

    options = Options(
        root=root, target=args.target, db_path=args.db_path, users_file=args.users_file,
        mode=mode, verify=not args.no_verify, max_steps=args.max_steps,
        max_requests=args.max_requests, max_seconds=args.max_seconds,
        max_browser_sessions=args.max_browser_sessions, max_missions=args.max_missions,
        command=effective, authorized=args.yes,
    )
    orchestrator = Orchestrator(options)
    try:
        summary = asyncio.run(orchestrator.run())
    except OrchestratorError as exc:
        _say(f"khunkhaar {command}: {exc}")
        return 2
    except BudgetExceeded as exc:
        _say(f"khunkhaar {command}: stopped by budget — {exc}")
        _say(f"partial evidence: {json.dumps(orchestrator.summary.get('evidence', {}))}")
        return 1
    except Exception as exc:  # a failed run must be visible, never a silent success
        _say(f"khunkhaar {command}: run failed — {type(exc).__name__}: {exc}")
        return 1
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print_summary(command, summary)
    return 0


def _print_summary(command: str, summary: dict) -> None:
    discovery = summary.get("discovery") or {}
    _say(f"khunkhaar {command}: project {discovery.get('project_id')} "
         f"({discovery.get('frameworks') or ['unknown']})")
    _say(f"  endpoints {discovery.get('endpoints')}  rules {discovery.get('rules')}  "
         f"invariants {discovery.get('invariants')}  files {discovery.get('files_scanned')}")
    target = summary.get("target") or {}
    if target.get("connected"):
        _say(f"  target {target.get('target')}  contract {target.get('contract')}  "
             f"operations {target.get('operations')}  database {target.get('database')}")
    missions = summary.get("missions") or {}
    if missions:
        _say(f"  missions planned {missions.get('planned')}  executed {missions.get('executed')}  "
             f"skipped {missions.get('skipped')}  failed {missions.get('failed')}")
        for note in (missions.get("skipped_detail") or [])[:5]:
            _say(f"    skip: {note}")
    findings = summary.get("findings") or {}
    if findings:
        _say(f"  findings created {findings.get('created')}  "
             f"updated {findings.get('updated')}")
        stats = summary.get("findings_stats") or {}
        if stats.get("by_status"):
            _say(f"  by status: {stats.get('by_status')}")
    for verification in (summary.get("verification") or [])[:20]:
        _say(f"    {verification['finding_id']}: {verification['status']} "
             f"({verification['reproductions']}/{verification['attempts']})")
    evidence = summary.get("evidence") or {}
    if evidence:
        _say(f"  evidence: {evidence.get('run')}")
        _say(f"  log:      {evidence.get('log')}")
    report = summary.get("report")
    if report:
        _say(f"  report: {report.get('markdown')}")


def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args:
        return cmd_launch([])
    head = args[0]
    if head in ("-h", "--help", "help"):
        print(HELP)
        return 0
    if head in ("-v", "--version"):
        print(f"khunkhaar {paths.version()}  (root: {paths.kh_root()})")
        return 0
    if head == "--doctor":
        return cmd_doctor("--json" in args)
    if head == "--project-info":
        return cmd_project_info("--json" in args)
    if head in PHASE2_COMMANDS:
        return cmd_phase2(head, args[1:])

    resolved = command_registry.resolve(args)
    if resolved is not None:
        spec, rest = resolved
        try:
            handler = command_registry.handler(spec)
        except (ImportError, AttributeError, RuntimeError) as exc:
            _say(f"khunkhaar {spec.name}: unavailable - {type(exc).__name__}: {exc}")
            return 3
        return handler.call(rest)

    if head in FUTURE_COMMANDS:
        _say(f"khunkhaar {head}: not implemented. {FUTURE_COMMANDS[head]}")
        return 3
    if head.startswith("-"):
        return cmd_launch(args)
    suggestions = command_registry.known_prefix(head)
    _say(f"khunkhaar: unknown command '{head}'. Try khunkhaar --help")
    if suggestions:
        _say(f"khunkhaar: under '{head}': {', '.join(suggestions)}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
