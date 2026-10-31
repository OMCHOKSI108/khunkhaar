"""Local mock HTTP API used to validate the API Call Tool.

Bound to 127.0.0.1 only. This exists so every Phase 1 acceptance check runs against a target we
own, per spec section 5 ("Do not use a real production API for installation tests").

Start:  .venv/bin/python tools/api/mock_server.py
"""

from __future__ import annotations

import asyncio
import os
import time

import uvicorn
from fastapi import FastAPI, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

HOST = os.environ.get("KHUNKHAAR_MOCK_HOST", "127.0.0.1")
PORT = int(os.environ.get("KHUNKHAAR_MOCK_PORT", "8731"))

app = FastAPI(
    title="KHUNKHAAR Mock API",
    version="1.0.0",
    description="Throwaway target for validating the KHUNKHAAR API Call Tool.",
)

_flaky_state = {"calls": 0}


async def _json_object(request: Request) -> dict:
    """Malformed or non-object bodies are a client error, not a 500."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body must be valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return body


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Small app-like page so browser checks exercise real navigation, interaction and a11y."""
    return """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>KHUNKHAAR Mock App</title>
<style>body{font-family:system-ui;margin:2rem;max-width:40rem}label{display:block;margin:.5rem 0}
button{margin-top:1rem;padding:.5rem 1rem}#result{margin-top:1rem;color:#0a6c2f}</style></head>
<body>
<h1>KHUNKHAAR Mock App</h1>
<main>
  <form id="login" aria-labelledby="loginHeading">
    <h2 id="loginHeading">Sign in</h2>
    <label for="email">Email</label>
    <input id="email" name="email" type="email" required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" required>
    <label for="role">Role</label>
    <select id="role" name="role"><option value="qa">QA</option><option value="admin">Admin</option></select>
    <button type="submit" id="submit">Sign in</button>
  </form>
  <p id="result" role="status" aria-live="polite"></p>
  <a href="/items" id="itemsLink">Browse items</a>
</main>
<script>
  document.getElementById('login').addEventListener('submit', e => {
    e.preventDefault();
    document.getElementById('result').textContent = 'signed in as ' + e.target.email.value;
    console.log('login submitted');
  });
  fetch('/health').then(r => r.json()).then(d => {
    const s = document.createElement('p'); s.id='health'; s.textContent='api '+d.status;
    document.querySelector('main').appendChild(s);
  });
</script>
</body></html>"""


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "khunkhaar-mock", "time": time.time()}


@app.get("/items")
def list_items(limit: int = Query(5, ge=1, le=100), q: str | None = None) -> dict:
    return {
        "count": limit,
        "query": q,
        "items": [{"id": i, "name": f"item-{i}", "price": round(i * 1.5, 2)} for i in range(1, limit + 1)],
    }


@app.post("/items", responses={400: {"description": "Body must be a JSON object"}})
async def create_item(request: Request) -> dict:
    body = await _json_object(request)
    return {"created": True, "echo": body, "id": 99}


@app.put("/items/{item_id}", responses={400: {"description": "Body must be a JSON object"}})
async def replace_item(item_id: int, request: Request) -> dict:
    return {"replaced": item_id, "echo": await _json_object(request)}


@app.patch("/items/{item_id}", responses={400: {"description": "Body must be a JSON object"}})
async def patch_item(item_id: int, request: Request) -> dict:
    return {"patched": item_id, "echo": await _json_object(request)}


@app.delete("/items/{item_id}")
def delete_item(item_id: int) -> dict:
    return {"deleted": item_id}


@app.post("/upload")
async def upload(file: UploadFile, note: str = Form("none")) -> dict:
    data = await file.read()
    return {"received_bytes": len(data), "filename": file.filename, "content_type": file.content_type,
            "note": note}


@app.post("/form")
async def form_endpoint(credential: str = Form(...), name: str = Form(...)) -> dict:
    # Deliberately echoes the field name only - the value must not survive redaction.
    return {"form_keys": ["credential", "name"], "credential_present": bool(credential)}


@app.get("/auth/whoami")
def whoami(authorization: str | None = Header(None), x_api_key: str | None = Header(None)) -> dict:
    return {
        "has_authorization": bool(authorization),
        "auth_scheme": (authorization or "").split(" ")[0] or None,
        "has_x_api_key": bool(x_api_key),
    }


@app.get("/secret-response")
def secret_response(response: Response) -> dict:
    response.headers["Set-Cookie"] = "session=supersecret-value; Path=/"
    response.headers["X-RateLimit-Remaining"] = "42"
    return {"api_token": "tok_live_abcdef123456", "user": {"email": "qa@example.com"}}


@app.get("/redirect-via")
def redirect_via(to: str = Query(...)) -> Response:
    return Response(status_code=302, headers={"location": to})


@app.get("/echo-headers")
async def echo_headers(request: Request) -> dict:
    return {"headers": dict(request.headers)}


@app.get("/bad-request")
def bad_request() -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": "invalid_payload", "detail": "field 'id' required"})


@app.get("/server-error")
def server_error() -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": "internal"})


@app.get("/flaky")
def flaky() -> JSONResponse:
    """Fails twice with 503 then succeeds - exercises bounded retries."""
    _flaky_state["calls"] += 1
    if _flaky_state["calls"] < 3:
        return JSONResponse(status_code=503, content={"attempt": _flaky_state["calls"]})
    n = _flaky_state["calls"]
    _flaky_state["calls"] = 0
    return JSONResponse(status_code=200, content={"recovered_after": n})


@app.get("/slow")
async def slow(delay: float = Query(2.0, ge=0, le=30)) -> dict:
    await asyncio.sleep(delay)
    return {"delayed": delay}


@app.get("/large")
def large(kb: int = Query(64, ge=1, le=8192)) -> PlainTextResponse:
    return PlainTextResponse("x" * (1024 * kb))


@app.post("/graphql")
async def graphql(request: Request) -> dict:
    payload = await request.json()
    query = str(payload.get("query", ""))
    if "fail" in query:
        return {"errors": [{"message": "Field 'fail' doesn't exist on type 'Query'"}]}
    if "users" in query:
        return {"data": {"users": [{"id": 1, "name": "ada"}, {"id": 2, "name": "grace"}]}}
    return {"data": {"echo": query, "variables": payload.get("variables")}}


@app.get("/no-schema")
def no_schema() -> PlainTextResponse:
    return PlainTextResponse("this is not json", media_type="text/plain")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
