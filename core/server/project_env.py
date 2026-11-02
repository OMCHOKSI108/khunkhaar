"""Reads secret VALUES directly out of a registered project's own .env file.

Every other part of KHUNKHAAR treats a project's .env as off-limits (core/project_discovery
records variable NAMES only, filesystem.deny_names blocks reading .env through the MCP tools).
This module is the deliberate, narrow exception: the operator explicitly opted into fully
agent-driven multi-role testing (auth.db_mint.auto_discover_from_project_env in
config/khunkhaar.yaml), which only has teeth if KHUNKHAAR can mint real sessions without the
operator hand-copying each project's DB password and JWT secret into KHUNKHAAR's own .env.

Nothing read here is ever written to KHUNKHAAR's SQLite store, config file, logs or evidence -
auth_harvester.py re-reads it fresh on every token mint.
"""
from __future__ import annotations

from pathlib import Path

ENV_FILES = (".env", ".env.local")

DB_HOST_KEYS = ("POSTGRES_HOST", "DATABASE_HOST", "DB_HOST", "PGHOST")
DB_PORT_KEYS = ("POSTGRES_PORT", "DATABASE_PORT", "DB_PORT", "PGPORT")
DB_NAME_KEYS = ("POSTGRES_DATABASE", "POSTGRES_DB", "DATABASE_NAME", "DB_NAME", "PGDATABASE")
DB_USER_KEYS = ("POSTGRES_USERNAME", "POSTGRES_USER", "DATABASE_USER", "DB_USER", "PGUSER")
DB_PASSWORD_KEYS = ("POSTGRES_PASSWORD", "DATABASE_PASSWORD", "DB_PASSWORD", "PGPASSWORD")
JWT_SECRET_KEYS = ("JWT_SECRET", "JWT_SECRET_KEY", "JWT_SIGNING_KEY", "ACCESS_TOKEN_SECRET", "AUTH_SECRET")


def read_env_file(root: Path) -> dict[str, str]:
    """Parse the first .env/.env.local found at the project root. Values stay in memory only."""
    for name in ENV_FILES:
        path = root / name
        if not path.is_file():
            continue
        out: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        return out
    return {}


def _first(env: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if env.get(key):
            return env[key]
    return None


def _first_key(env: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if env.get(key):
            return key
    return None


def discover_db_and_jwt(root: Path) -> dict[str, str | None]:
    """Best-effort connection params + secret key names (not persisted - caller uses values
    immediately or discards them)."""
    env = read_env_file(root)
    return {
        "host": _first(env, DB_HOST_KEYS) or "127.0.0.1",
        "port": _first(env, DB_PORT_KEYS) or "5432",
        "database": _first(env, DB_NAME_KEYS),
        "user": _first(env, DB_USER_KEYS),
        "password": _first(env, DB_PASSWORD_KEYS),
        "jwt_secret": _first(env, JWT_SECRET_KEYS),
        "password_key": _first_key(env, DB_PASSWORD_KEYS),
        "jwt_secret_key": _first_key(env, JWT_SECRET_KEYS),
    }
