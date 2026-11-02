"""Harvests DB credentials, role users, and mints active JWT tokens for deep multi-role testing."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any
import psycopg2

from core.server.path_resolver import resolve_codebase_path

TOKEN_CACHE: dict[str, dict[str, str]] = {}


def _parse_env_file(env_path: Path) -> dict[str, str]:
    config = {}
    if not env_path.is_file():
        return config
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            config[k.strip()] = v.strip().strip('"').strip("'")
    return config


def _mint_jwt(user_id: str, secret: str) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload_data = {"userId": user_id, "iat": int(time.time()), "exp": int(time.time()) + 86400 * 30}
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def get_role_tokens(codebase_dir: str | None) -> dict[str, str]:
    """Inspect codebase .env and PostgreSQL DB to harvest or mint valid tokens for roles."""
    global TOKEN_CACHE
    resolved = resolve_codebase_path(codebase_dir)
    if not resolved:
        return {}

    cache_key = str(resolved)
    if cache_key in TOKEN_CACHE:
        return TOKEN_CACHE[cache_key]

    env_config = _parse_env_file(resolved / ".env")
    jwt_secret = env_config.get("JWT_SECRET")
    pg_host = env_config.get("POSTGRES_HOST", "127.0.0.1")
    pg_port = int(env_config.get("POSTGRES_PORT", "5432"))
    pg_db = env_config.get("POSTGRES_DATABASE", "lwhub_dev")
    pg_user = env_config.get("POSTGRES_USERNAME", "postgres")
    pg_pass = env_config.get("POSTGRES_PASSWORD", "postgres")

    if not jwt_secret:
        return {}

    tokens: dict[str, str] = {}
    try:
        conn = psycopg2.connect(host=pg_host, port=pg_port, dbname=pg_db, user=pg_user, password=pg_pass, connect_timeout=3)
        cur = conn.cursor()
        for role_name in ("SUPER_ADMIN", "ADMIN", "USER", "INTERN", "CONTRACTUAL"):
            cur.execute("SELECT id, email FROM users WHERE role = %s AND is_active = true LIMIT 1", (role_name,))
            row = cur.fetchone()
            if row:
                u_id, u_email = row[0], row[1]
                t = _mint_jwt(u_id, jwt_secret)
                cur.execute("INSERT INTO access_tokens (id, user_id, access_token, created_at, updated_at) VALUES (gen_random_uuid(), %s, %s, NOW(), NOW())", (u_id, t))
                key = "admin" if role_name in ("SUPER_ADMIN", "ADMIN") else ("intern" if role_name == "INTERN" else "employee")
                tokens[key] = t
                tokens[role_name.lower()] = t
        conn.commit()
        conn.close()
    except Exception:
        pass

    TOKEN_CACHE[cache_key] = tokens
    return tokens
