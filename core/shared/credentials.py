"""Server-side credential resolution for the API tool.

The model must never have to handle a raw secret, so credentials are stored outside the
metadata store and referenced by an opaque profile name. Resolution order:

  1. an environment variable named by the profile (`env_var`)
  2. a 0600 file at data/profiles/auth/<profile>.json

Plaintext secrets are never written to SQLite, reports, or evidence. Reading a file the
project happens to contain is deliberately not supported (spec 5: "Never automatically use
credentials found in unrelated files").
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import paths


@dataclass
class ResolvedAuth:
    profile: str
    headers: dict[str, str]
    source: str


def auth_store_dir():
    d = paths.profiles_dir() / "auth"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_file_profile(name: str) -> dict | None:
    safe = os.path.basename(name)
    target = auth_store_dir() / f"{safe}.json"
    if not target.exists():
        return None
    data = json.loads(target.read_text(encoding="utf-8") or "{}")
    return data if isinstance(data, dict) else None


def list_profiles() -> list[str]:
    from_config = [
        p.get("name")
        for p in _configured_profiles()
        if isinstance(p, dict) and p.get("name")
    ]
    from_disk = sorted(p.stem for p in auth_store_dir().glob("*.json"))
    return sorted(set(from_config) | set(from_disk))


def _configured_profiles() -> list[dict]:
    from .config import current

    return current().get("auth", "profiles", []) or []


def resolve(profile: str) -> ResolvedAuth:
    """Return headers to attach for a named auth profile. Never returns the value to output."""
    if not profile:
        return ResolvedAuth(profile="", headers={}, source="none")

    cfg_entry = next(
        (p for p in _configured_profiles() if isinstance(p, dict) and p.get("name") == profile),
        None,
    )

    env_var = (cfg_entry or {}).get("env_var")
    if env_var:
        value = os.environ.get(str(env_var))
        if value:
            return ResolvedAuth(
                profile=profile,
                headers={str((cfg_entry or {}).get("header", "Authorization")): _auth_value_format(cfg_entry, value)},
                source=f"env:{env_var}",
            )

    file_profile = _read_file_profile(profile)
    if file_profile:
        headers = file_profile.get("headers") or {}
        return ResolvedAuth(profile=profile, headers={str(k): str(v) for k, v in headers.items()},
                            source=f"file:{profile}")

    raise KeyError(
        f"auth profile '{profile}' is not configured (no env var set and no profile file present)"
    )


def _auth_value_format(cfg_entry: dict | None, value: str) -> str:
    scheme = str((cfg_entry or {}).get("scheme", "")).strip()
    if scheme.lower() in ("bearer", "basic"):
        return f"{scheme.capitalize()} {value}"
    return value


def save_profile(name: str, headers: dict[str, str]) -> str:
    """Operator-facing helper used by the CLI, not exposed as an MCP tool.

    Writing credentials must be a deliberate local action; an agent cannot call this.
    """
    safe = os.path.basename(name)
    target = auth_store_dir() / f"{safe}.json"
    target.write_text(json.dumps({"headers": headers}, indent=2), encoding="utf-8")
    target.chmod(0o600)
    return str(target)
