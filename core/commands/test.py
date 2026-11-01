"""Interactive and flag-driven QA runner using OpenCode LLM agents."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def _opencode_binary() -> str | None:
    return shutil.which("opencode")


def _prompt_user(prompt: str, default: str = "") -> str:
    try:
        if default:
            val = input(f"{prompt} [{default}]: ").strip()
            return val or default
        return input(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(130)


def _get_agent_prompt(agent_name: str) -> str:
    cfg_file = Path.home() / ".config" / "opencode" / "opencode.json"
    if cfg_file.is_file():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            agent_cfg = data.get("agent", {}).get(agent_name, {})
            if agent_cfg and agent_cfg.get("prompt"):
                return str(agent_cfg["prompt"])
        except Exception:
            pass
    return f"You are the {agent_name} QA agent. Use khunkhaar MCP servers to inspect and test the application."


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="khunkhaar test",
        description="Launch an OpenCode AI QA Agent (Tester-all, Tester-end-user, or Tester-swe)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", "-a", action="store_true", help="Full-Stack Tester: Codebase + Frontend + Backend + DB")
    group.add_argument("--end-user", "-e", action="store_true", help="End-User Tester: Live Product URL exploratory QA")
    group.add_argument("--swe", "-s", action="store_true", help="SWE & Backend Tester: Codebase + GraphQL/REST APIs + DB")

    parser.add_argument("--path", "-p", default="", help="Path to project codebase (default: current directory)")
    parser.add_argument("--target", "-t", default="", help="Target URL under test (e.g. http://localhost:8000)")
    parser.add_argument("--env-file", default="", help="Path to .env or credentials file (optional)")
    parser.add_argument("--message", "-m", default="", help="Custom initial test instruction")
    parser.add_argument("--run", "-r", action="store_true", help="Run non-interactively via opencode run")

    args = parser.parse_args(argv)

    binary = _opencode_binary()
    if not binary:
        print("khunkhaar test: 'opencode' binary not found on PATH. Please install OpenCode first.")
        return 2

    agent = ""
    mode_label = ""

    if args.all:
        agent = "khunkhaar-tester-all"
        mode_label = "Full-Stack Tester (Codebase + Frontend + Backend + DB)"
    elif args.end_user:
        agent = "khunkhaar-tester-end-user"
        mode_label = "End-User Tester (Live Product URL)"
    elif args.swe:
        agent = "khunkhaar-tester-swe"
        mode_label = "SWE & Backend Tester (Codebase + GraphQL/REST + DB)"
    else:
        # Interactive Mode
        print("\n" + "=" * 60)
        print("  KHUNKHAAR AI QA AGENT LAUNCHER")
        print("=" * 60)
        print("Select what you want to test:")
        print("  [1] Tester-all       — Full-Stack QA (Codebase + Frontend + Backend + DB)")
        print("  [2] Tester-end-user  — End-User QA (Live Product URL / Browser journeys)")
        print("  [3] Tester-swe       — Backend & SWE QA (Codebase + GraphQL/REST APIs + DB)")
        print("=" * 60)

        choice = _prompt_user("Choose mode (1-3)", default="1")
        if choice in ("1", "all", "tester-all"):
            agent = "khunkhaar-tester-all"
            mode_label = "Full-Stack Tester"
        elif choice in ("2", "end-user", "tester-end-user"):
            agent = "khunkhaar-tester-end-user"
            mode_label = "End-User Tester"
        elif choice in ("3", "swe", "tester-swe"):
            agent = "khunkhaar-tester-swe"
            mode_label = "Backend & SWE Tester"
        else:
            print("Invalid choice. Exiting.")
            return 2

    # Collect project details
    codebase_path = args.path
    target_url = args.target
    env_file = args.env_file

    if not codebase_path and agent in ("khunkhaar-tester-all", "khunkhaar-tester-swe"):
        cwd = str(Path.cwd().resolve())
        codebase_path = _prompt_user("Enter Codebase path", default=cwd)

    if not target_url and agent in ("khunkhaar-tester-all", "khunkhaar-tester-end-user"):
        target_url = _prompt_user("Enter Target Server/Product URL", default="http://localhost:8000")

    if not env_file and agent in ("khunkhaar-tester-all", "khunkhaar-tester-swe"):
        default_env = ""
        if codebase_path and (Path(codebase_path) / ".env").is_file():
            default_env = str(Path(codebase_path) / ".env")
        env_file = _prompt_user("Enter .env or credentials file path (optional, press Enter to skip)", default=default_env)

    # Build initial agent instruction
    agent_instructions = _get_agent_prompt(agent)
    parts = [
        agent_instructions,
        "",
        f"=== ACTIVE QA MISSION: {mode_label.upper()} ===",
    ]
    if codebase_path:
        parts.append(f"Codebase Path: {Path(codebase_path).resolve()}")
    if target_url:
        parts.append(f"Target URL: {target_url}")
    if env_file and Path(env_file).is_file():
        parts.append(f"Credentials / .env File: {Path(env_file).resolve()}")
    if args.message:
        parts.append(f"Instruction: {args.message}")
    else:
        parts.append(
            "Instructions: Inspect the codebase schemas/routes, test the live API/GraphQL endpoints, "
            "verify error handling and edge cases, check database state if applicable, and report all discovered bugs with clear reproduction evidence."
        )

    initial_prompt = "\n".join(parts)
    workdir = str(Path(codebase_path).resolve()) if codebase_path and Path(codebase_path).is_dir() else str(Path.cwd())

    print("\n" + "-" * 60)
    print(f"Launching OpenCode with Persona: {agent}")
    print(f"Target URL: {target_url or 'N/A'}")
    print(f"Working Directory: {workdir}")
    print("-" * 60 + "\n")

    if args.run:
        cmd = [binary, "run", "--agent", agent, initial_prompt]
    else:
        # Interactive TUI mode (top-level opencode takes --prompt and directory)
        cmd = [binary, "--standalone", "--prompt", initial_prompt, workdir]

    try:
        os.chdir(workdir)
        os.execvp(binary, cmd)
    except Exception as exc:
        print(f"khunkhaar test: failed to launch opencode: {exc}")
        return 1
