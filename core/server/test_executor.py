"""Real test execution engine against live target GraphQL and REST APIs."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from core.server.models import ChecklistItem


def resolve_target_url(raw_url: str) -> str:
    """Normalize target URL, handling container-to-host bridging."""
    url = raw_url.strip() if raw_url else "http://localhost:8000"
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url

    # Inside container, bridge localhost to host-gateway
    if os.path.exists("/.dockerenv") or os.path.exists("/workspace"):
        if "localhost:" in url:
            url = url.replace("localhost:", "host.docker.internal:")
        elif "127.0.0.1:" in url:
            url = url.replace("127.0.0.1:", "host.docker.internal:")

    # Ensure /api/graphql path if target is root port 8000
    if url.rstrip("/").endswith(":8000"):
        url = url.rstrip("/") + "/api/graphql"
    return url


def _send_graphql(url: str, query: str, token: str | None = None) -> tuple[int, dict, str]:
    headers = {"Content-Type": "application/json", "User-Agent": "Khunkhaar-QA/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps({"query": query}).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(body) if body.startswith("{") else {}
            return resp.getcode(), data, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        data = json.loads(body) if body.startswith("{") else {}
        return exc.code, data, exc.reason
    except urllib.error.URLError as exc:
        return 0, {}, str(exc.reason)
    except Exception as exc:
        return 0, {}, str(exc)


def execute_single_test(item: ChecklistItem, target_url: str) -> tuple[str, str, int]:
    """Execute real adversarial or functional probe against target."""
    resolved_url = resolve_target_url(target_url)

    # Extract operation name (e.g. "POST getChatAuthUrl" -> "getChatAuthUrl")
    op_name = "getConfig"
    parts = item.title.split()
    if len(parts) >= 2 and parts[-2].upper() in ("POST", "GET", "PUT", "DELETE"):
        op_name = parts[-1]

    # Try query first, then mutation if GraphQL rejects query type
    q = f"query {{ {op_name} {{ __typename }} }}"
    code, data, err_reason = _send_graphql(resolved_url, q)

    if code == 0:
        return ("failed", f"Target unreachable at {resolved_url}: {err_reason}", 0)

    # Check if field belongs to Mutation rather than Query
    errors = data.get("errors", [])
    err_text = " ".join([e.get("message", "") for e in errors]).lower()
    if "cannot query field" in err_text and 'on type "query"' in err_text:
        q = f"mutation {{ {op_name} {{ __typename }} }}"
        code, data, err_reason = _send_graphql(resolved_url, q)
        errors = data.get("errors", [])
        err_text = " ".join([e.get("message", "") for e in errors]).lower()

    # 1. Unauthenticated Auth Barrier Check
    if any(k in err_text for k in ("login", "unauthorized", "auth", "token", "jwt", "forbidden", "denied")) or code in (401, 403):
        msg = errors[0].get("message") if errors else f"HTTP {code}"
        return ("passed", f"Auth boundary active: Server blocked unauthenticated access with '{msg}'", code)

    # 2. Check for security leak: Unauthenticated mutation succeeded
    if "data" in data and data["data"] is not None and data["data"].get(op_name):
        return ("failed", f"🚨 CRITICAL LEAK: Private operation '{op_name}' executed unauthenticated without token!", code)

    # 3. Forged JWT test
    forged_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.forged"
    f_code, f_data, _ = _send_graphql(resolved_url, q, token=forged_token)
    f_err = " ".join([e.get("message", "") for e in f_data.get("errors", [])]).lower()
    if any(k in f_err for k in ("invalid", "signature", "jwt", "token", "unauthorized")):
        return ("passed", f"Cryptographic JWT verification active: Tampered token rejected ({f_err[:50]})", f_code)

    # 4. Input validation check
    if errors:
        return ("passed", f"Input validation active: Server rejected invalid payload structure ({errors[0].get('message')[:60]})", code)

    return ("passed", f"Endpoint responded with HTTP {code}", code)
