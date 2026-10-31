#!/usr/bin/env python3
"""Prove that registered project profiles never carry .env values across projects.

Run: .venv/bin/python scripts/validate_project_isolation.py [--json]

A secret value found in a profile is only reported as a leak when no non-.env project
file contains the same string, i.e. the profile could not have picked it up from shared
infrastructure configuration (a docker image named "postgres", for example).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.shared import paths  # noqa: E402

SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "venv", "dist", "build",
             "__pycache__", ".next", "target", "coverage", "runtime"}
ENV_GLOB = ".env*"
PLACEHOLDER = re.compile(
    r"(?i)^(null|none|true|false|changeme|change_me|example|sample|your[_-]|xxx+|<.*>|\.\.\.|\$\{.*\})$"
)
MAX_FILE_BYTES = 512 * 1024


def env_pairs(root: Path) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for path in sorted(root.glob(ENV_GLOB)):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and value:
                pairs.setdefault(key, value)
    return pairs


def secret_values(root: Path) -> dict[str, str]:
    out = {}
    for key, value in env_pairs(root).items():
        if PLACEHOLDER.match(value) or len(value) < 6:
            continue
        out[key] = value
    return out


def non_env_corpus(root: Path) -> str:
    """Text of project files that are legitimate discovery input, never .env files."""
    chunks: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.startswith(".env"):
                continue
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                    continue
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
        if len(chunks) > 4000:
            break
    return "\n".join(chunks)


def profile_value_keys(obj, path="") -> list[str]:
    """Any dict entry whose key suggests a raw value rather than a name/presence flag."""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            lowered = str(key).lower()
            if lowered in {"value", "values", "secret", "secrets", "password", "token", "credentials"}:
                if not isinstance(value, (bool, list, dict)) or value:
                    found.append(child)
            found.extend(profile_value_keys(value, child))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            found.extend(profile_value_keys(item, f"{path}[{index}]"))
    return found


def audit(profiles: dict[str, dict]) -> list[dict]:
    envs = {pid: secret_values(Path(p["root"])) for pid, p in profiles.items()}
    corpora = {pid: non_env_corpus(Path(p["root"])) for pid, p in profiles.items()}
    results: list[dict] = []

    for pid, profile in profiles.items():
        profile_path = Path(profile["_path"])
        text = profile_path.read_text(encoding="utf-8")
        data = json.loads(text)
        record = {
            "project": pid,
            "profile_mode": oct(profile_path.stat().st_mode & 0o777),
            "raw_value_keys": profile_value_keys(data),
            "own_env_values_in_profile": [],
            "cross_project_values_in_profile": [],
            "leaks": [],
        }

        for key, value in envs[pid].items():
            if value in text:
                explained = value in corpora[pid]
                record["own_env_values_in_profile"].append(
                    {"variable": key, "explained_by_non_env_file": explained}
                )
                if not explained:
                    record["leaks"].append({"variable": key, "kind": "own_value_unexplained"})

        for other, other_envs in envs.items():
            if other == pid:
                continue
            for key, value in other_envs.items():
                if value in text and value not in corpora[pid]:
                    record["cross_project_values_in_profile"].append({"from_project": other, "variable": key})
                    record["leaks"].append({"variable": key, "kind": f"cross_project:{other}"})

        if record["raw_value_keys"]:
            record["leaks"].append({"variable": None, "kind": f"raw_value_keys:{record['raw_value_keys']}"})
        record["ok"] = not record["leaks"]
        results.append(record)
    return results


def main() -> int:
    registry = paths.registry_dir()
    profiles: dict[str, dict] = {}
    for path in sorted(registry.glob("*/project-profile.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_path"] = str(path)
        profiles[path.parent.name] = data

    if not profiles:
        print("no registered project profiles - run khunkhaar inside a project first")
        return 1

    results = audit(profiles)
    if "--json" in sys.argv:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            print(f"project: {result['project']}  mode={result['profile_mode']}  ok={result['ok']}")
            if result["own_env_values_in_profile"]:
                for item in result["own_env_values_in_profile"]:
                    tag = "explained by a non-.env file" if item["explained_by_non_env_file"] else "LEAK"
                    print(f"  own env value of {item['variable']}: {tag}")
            if result["cross_project_values_in_profile"]:
                for item in result["cross_project_values_in_profile"]:
                    print(f"  CROSS-PROJECT value from {item['from_project']} ({item['variable']})")
            if result["raw_value_keys"]:
                print(f"  raw value-bearing keys: {result['raw_value_keys']}")
            if result["ok"]:
                print("  no secret values from .env files are stored in this profile")
        leaked = [r["project"] for r in results if not r["ok"]]
        print(f"\n{len(results)} profile(s) checked; {'LEAKS: ' + ', '.join(leaked) if leaked else 'no leaks'}")

    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
