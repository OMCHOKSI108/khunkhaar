"""Mints active session tokens for multi-role testing against a project's own user table.

Two sources for the schema + connection details, tried in order:

1. Per-project auto-discovery (core/server/store.web_project_auth, populated by
   engine_bridge.auto_configure_auth at recon time via the LLM harness) - secret VALUES are
   re-read live from that project's own .env (core/server/project_env.py) on every call, never
   persisted. This is what makes fully agent-driven multi-project testing possible without the
   operator hand-editing config/khunkhaar.yaml per project.
2. The manual/global auth.db_mint block in config/khunkhaar.yaml, for when auto-discovery is
   off or didn't find a usable schema - secrets come from KHUNKHAAR's own environment.

Either way the schema (table/column/role names) is config-driven, never hardcoded to one
project's layout.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

from core.database.adapters import mask_ident, profile_by_name
from core.server import project_env, store
from core.server.path_resolver import resolve_codebase_path
from core.shared.config import current

TOKEN_CACHE: dict[str, dict[str, str]] = {}


def _mint_jwt(user_id: Any, secret: str, claim_key: str) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload_data = {claim_key: user_id, "iat": int(time.time()), "exp": int(time.time()) + 86400 * 30}
    payload = base64.urlsafe_b64encode(json.dumps(payload_data, default=str).encode()).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def _connection_params_for_project(project_id: str | None, codebase_dir: str | None) -> dict[str, Any] | None:
    """Per-project schema (cached) + connection secrets (read live, never cached)."""
    mint_cfg = current().section("auth").get("db_mint") or {}
    if not project_id or not mint_cfg.get("auto_discover_from_project_env"):
        return None
    schema = store.get_project_auth_config(project_id)
    if not schema or not schema.get("roles"):
        return None
    root = resolve_codebase_path(codebase_dir) if codebase_dir else None
    if root is None:
        return None
    env = project_env.read_env_file(root)
    password_key = schema.get("password_env_key")
    jwt_key = schema.get("jwt_secret_env_key")
    password = env.get(password_key) if password_key else None
    jwt_secret = env.get(jwt_key) if jwt_key else None
    if not password or not jwt_secret:
        return None
    return {
        "schema": schema, "host": schema.get("host") or "127.0.0.1",
        "port": int(schema.get("port") or 5432), "database": schema.get("database"),
        "user": schema.get("user"), "password": password, "jwt_secret": jwt_secret,
        "cache_key": f"project:{project_id}",
    }


def _connection_params_global() -> dict[str, Any] | None:
    mint_cfg = current().section("auth").get("db_mint") or {}
    if not mint_cfg.get("enabled") or not (mint_cfg.get("roles") or []):
        return None
    try:
        profile = profile_by_name(mint_cfg.get("database_profile"))
    except Exception:
        return None
    password = os.environ.get(str(profile.get("password_env") or ""), "")
    jwt_secret = os.environ.get(str(mint_cfg.get("jwt_secret_env") or "JWT_SECRET"), "")
    if not password or not jwt_secret:
        return None
    return {
        "schema": mint_cfg, "host": profile.get("host", "127.0.0.1"),
        "port": int(profile.get("port", 5432)), "database": profile.get("database"),
        "user": profile.get("user"), "password": password, "jwt_secret": jwt_secret,
        "cache_key": f"global:{profile.get('name')}",
    }


def _mint_from_params(params: dict[str, Any]) -> dict[str, str]:
    schema = params["schema"]
    try:
        users_table = mask_ident(schema.get("users_table") or "users")
        id_column = mask_ident(schema.get("id_column") or "id")
        role_column = mask_ident(schema.get("role_column") or "role")
        active_column = schema.get("active_column")
        active_sql = f" AND {mask_ident(active_column)} = true" if active_column else ""
        claim_key = str(schema.get("claim_user_id") or "userId")
        session_table = schema.get("session_table")
        session_fk = mask_ident(schema.get("session_user_fk") or "user_id") if session_table else None
        session_token_col = mask_ident(schema.get("session_token_column") or "access_token") if session_table else None
        session_table_sql = mask_ident(session_table) if session_table else None
    except ValueError:
        return {}

    tokens: dict[str, str] = {}
    try:
        import psycopg2

        conn = psycopg2.connect(
            host=str(params["host"]), port=int(params["port"]), dbname=params.get("database"),
            user=params.get("user"), password=params["password"], connect_timeout=3,
        )
        cur = conn.cursor()
        for entry in schema.get("roles") or []:
            value, key = entry.get("value"), entry.get("key")
            if not value or not key:
                continue
            cur.execute(
                f"SELECT {id_column} FROM {users_table} WHERE {role_column} = %s{active_sql} LIMIT 1",
                (value,),
            )
            row = cur.fetchone()
            if not row:
                continue
            user_id = row[0]
            token = _mint_jwt(user_id, params["jwt_secret"], claim_key)
            if session_table_sql:
                cur.execute(
                    f"INSERT INTO {session_table_sql} (id, {session_fk}, {session_token_col}, "
                    f"created_at, updated_at) VALUES (gen_random_uuid(), %s, %s, NOW(), NOW())",
                    (user_id, token),
                )
            tokens[key] = token
            tokens[str(value).lower()] = token
        conn.commit()
        conn.close()
    except Exception:
        return {}
    return tokens


def get_role_tokens(codebase_dir: str | None = None, project_id: str | None = None) -> dict[str, str]:
    """Mint one session token per configured role. Tries per-project auto-discovery first,
    falls back to the manual auth.db_mint block in config/khunkhaar.yaml."""
    params = _connection_params_for_project(project_id, codebase_dir) or _connection_params_global()
    if params is None:
        return {}

    cache_key = params["cache_key"]
    if cache_key in TOKEN_CACHE:
        return TOKEN_CACHE[cache_key]

    tokens = _mint_from_params(params)
    TOKEN_CACHE[cache_key] = tokens
    return tokens
