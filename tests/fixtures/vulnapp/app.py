"""Synthetic vulnerable application (disposable).

FastAPI + SQLite, running on 127.0.0.1:8740. It intentionally violates the eight rules in
docs/BUSINESS_RULES.md. Nothing in this file is used by the QA engine to locate defects: the
engine receives only the app, its documentation and an OpenAPI document.

Start it with:
    .venv/bin/python -m uvicorn app:app --app-dir tests/fixtures/vulnapp --port 8740
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("VULN_DB", HERE.parents[2] / "runtime" / "tmp" / "synthetic"
                              / "vulnapp.db"))
LOCAL_OFFSET = timedelta(hours=5, minutes=30)

USERS = (
    ("alice", "employee", "alice-pass"),
    ("bob", "employee", "bob-pass"),
    ("admin", "manager", "admin-pass"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(password: str) -> str:
    return hashlib.sha256(f"vulnapp::{password}".encode()).hexdigest()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    for username, role, password in USERS:
        conn.execute(
            "INSERT INTO users (username, role, password_hash) VALUES (?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET role=excluded.role, password_hash=excluded.password_hash",
            (username, role, _hash(password)),
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _conn()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    _seed(conn)
    conn.commit()
    conn.close()
    yield


app = FastAPI(title="KHUNKHAAR synthetic application", version="1.0.0", lifespan=lifespan)


# ------------------------------------------------------------------ auth

class LoginIn(BaseModel):
    username: str = ""
    password: str = ""


def current_user(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = request.cookies.get("session")
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(401, "authentication required")
    conn = _conn()
    row = conn.execute(
        "SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ?", (token,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(401, "session is not valid")
    return dict(row)


@app.post("/api/login")
def login(body: LoginIn):
    conn = _conn()
    row = conn.execute("SELECT id, username, role FROM users WHERE username=?",
                       (body.username,)).fetchone()
    expected = conn.execute("SELECT password_hash FROM users WHERE username=?",
                            (body.username,)).fetchone()
    if row is None or expected is None or expected["password_hash"] != _hash(body.password):
        conn.close()
        raise HTTPException(401, "invalid credentials")
    token = secrets.token_hex(16)
    conn.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
                 (token, row["id"], _now()))
    conn.commit()
    conn.close()
    from fastapi.responses import JSONResponse
    payload = JSONResponse({"token": token, "username": row["username"], "role": row["role"]})
    payload.set_cookie("session", token, httponly=True, samesite="lax")
    return payload


# ------------------------------------------------------------------ requests

class RequestIn(BaseModel):
    title: str = ""
    amount: float = 0.0


def _request_row(conn: sqlite3.Connection, request_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    return dict(row) if row else None


@app.get("/api/requests")
def list_requests(user: dict = Depends(current_user)):
    conn = _conn()
    if user["role"] == "manager":
        rows = conn.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM requests WHERE owner_id=? ORDER BY id DESC",
                            (user["id"],)).fetchall()
    conn.close()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/requests", status_code=201)
def create_request(body: RequestIn, user: dict = Depends(current_user)):
    # R4: no validation happens here.
    # R2: identical submissions are inserted again.
    conn = _conn()
    cursor = conn.execute(
        "INSERT INTO requests (title, amount, status, owner_id, created_at) VALUES (?,?,?,?,?)",
        (body.title, body.amount, "draft", user["id"], _now()))
    conn.commit()
    row = _request_row(conn, cursor.lastrowid)
    conn.close()
    return row


@app.get("/api/requests/{request_id}")
def get_request(request_id: int, user: dict = Depends(current_user)):
    # R1: any authenticated user may read any request.
    conn = _conn()
    row = _request_row(conn, request_id)
    conn.close()
    if row is None:
        raise HTTPException(404, "request not found")
    return row


@app.post("/api/requests/{request_id}/submit")
def submit_request(request_id: int, user: dict = Depends(current_user)):
    conn = _conn()
    row = _request_row(conn, request_id)
    if row is None:
        conn.close()
        raise HTTPException(404, "request not found")
    if row["owner_id"] != user["id"]:
        conn.close()
        raise HTTPException(403, "only the owner may submit")
    if row["status"] != "draft":
        conn.close()
        raise HTTPException(409, f"cannot submit a request in state '{row['status']}'")
    conn.execute("UPDATE requests SET status='submitted' WHERE id=?", (request_id,))
    conn.commit()
    conn.close()
    return {"id": request_id, "status": "submitted"}


@app.post("/api/requests/{request_id}/approve")
def approve_request(request_id: int, user: dict = Depends(current_user)):
    conn = _conn()
    # R7: the audit row is written before the business precondition is checked.
    conn.execute("INSERT INTO audit_log (request_id, actor, action, at) VALUES (?,?,?,?)",
                 (request_id, user["username"], "approve", _now()))
    row = _request_row(conn, request_id)
    if row is None:
        conn.commit()
        conn.close()
        raise HTTPException(404, "request not found")
    if row["owner_id"] != user["id"] and user["role"] != "manager":
        conn.commit()
        conn.close()
        raise HTTPException(403, "only the owner or a manager may approve")
    # R3: the state machine is not checked; a draft can be approved directly.
    conn.execute("UPDATE requests SET status='approved' WHERE id=?", (request_id,))
    conn.commit()
    conn.close()
    return {"id": request_id, "status": "approved"}


# ------------------------------------------------------------------ clock

@app.post("/api/clock/in", status_code=201)
async def clock_in(user: dict = Depends(current_user)):
    conn = _conn()
    open_row = conn.execute(
        "SELECT id FROM clock_shifts WHERE user_id=? AND clock_out IS NULL",
        (user["id"],)).fetchone()
    if open_row is not None:
        conn.close()
        raise HTTPException(409, "an open clock-in already exists")
    # R5: check-then-insert is not atomic; concurrent requests create several open rows.
    await asyncio.sleep(0.1)
    cursor = conn.execute("INSERT INTO clock_shifts (user_id, clock_in, clock_out) VALUES (?,?,NULL)",
                          (user["id"], _now()))
    conn.commit()
    conn.close()
    return {"id": cursor.lastrowid, "status": "clocked_in"}


@app.post("/api/clock/out")
def clock_out(user: dict = Depends(current_user)):
    conn = _conn()
    row = conn.execute(
        "SELECT id FROM clock_shifts WHERE user_id=? AND clock_out IS NULL ORDER BY id DESC LIMIT 1",
        (user["id"],)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(409, "no open clock-in")
    conn.execute("UPDATE clock_shifts SET clock_out=? WHERE id=?", (_now(), row["id"]))
    conn.commit()
    conn.close()
    return {"id": row["id"], "status": "clocked_out"}


# ------------------------------------------------------------------ events / reports

class EventIn(BaseModel):
    label: str = ""
    occurred_at: str = ""


@app.post("/api/events", status_code=201)
def create_event(body: EventIn, user: dict = Depends(current_user)):
    try:
        parsed = datetime.fromisoformat(body.occurred_at.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "occurred_at must be ISO-8601")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    stamp = parsed.astimezone(timezone.utc).isoformat()
    conn = _conn()
    cursor = conn.execute("INSERT INTO events (label, occurred_at) VALUES (?,?)",
                          (body.label, stamp))
    conn.commit()
    conn.close()
    return {"id": cursor.lastrowid, "label": body.label, "occurred_at": stamp}


@app.get("/api/reports/daily")
def daily_report(date: str, user: dict = Depends(current_user)):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    conn = _conn()
    rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    conn.close()
    items = []
    for row in rows:
        occurred = datetime.fromisoformat(row["occurred_at"])
        # R6: the report buckets by a fixed local offset instead of the UTC date.
        local = occurred + LOCAL_OFFSET
        if local.date().isoformat() == date:
            items.append({"id": row["id"], "label": row["label"],
                          "occurred_at": row["occurred_at"]})
    return {"date": date, "items": items}


# ------------------------------------------------------------------ UI

NAV = """
<nav aria-label="Main">
  <a href="/">Dashboard</a> |
  <a href="/requests-page">Requests</a> |
  <a href="/clock-page">Clock</a> |
  <a href="/reports-page">Reports</a>
</nav>
"""

STYLE = """
<style>
  body { font-family: system-ui, sans-serif; color: #111; background: #fff; margin: 16px; }
  table { border-collapse: collapse; }
  th, td { border: 1px solid #333; padding: 6px 10px; text-align: left; }
  label { display: block; margin: 8px 0 2px; }
  .wide { width: 1200px; }
</style>
"""


def _page(title: str, extra: str) -> str:
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{title}</title>{STYLE}</head><body>{NAV}<main>{extra}</main></body></html>")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    # R8: the table is fixed at 1200px, so a 390px viewport scrolls horizontally.
    return _page("Dashboard", """
<h1>Operations dashboard</h1>
<p>Requests, clock-ins and daily reports for the synthetic QA target.</p>
<table class="wide">
  <thead><tr><th scope="col">Feature</th><th scope="col">Rule</th><th scope="col">Status</th></tr></thead>
  <tbody>
    <tr><td>Requests</td><td>Ownership, validation, approval order</td><td>active</td></tr>
    <tr><td>Clock</td><td>One open clock-in per user</td><td>active</td></tr>
    <tr><td>Reports</td><td>UTC day boundary</td><td>active</td></tr>
  </tbody>
</table>
""")


@app.get("/requests-page", response_class=HTMLResponse)
def requests_page():
    return _page("Requests", """
<h1>Requests</h1>
<form id="create-request">
  <label for="title">Title</label>
  <input id="title" name="title" type="text">
  <label for="amount">Amount</label>
  <input id="amount" name="amount" type="number" step="0.01">
  <button type="submit">Create request</button>
</form>
<ul id="requests"></ul>
<script>
  async function load() {
    try {
      const response = await fetch('/api/requests');
      const data = await response.json();
      const list = document.getElementById('requests');
      list.innerHTML = '';
      for (const item of data.items || []) {
        const li = document.createElement('li');
        li.textContent = item.title + ' — ' + item.amount + ' (' + item.status + ')';
        list.appendChild(li);
      }
    } catch (error) { /* page stays usable without data */ }
  }
  document.getElementById('create-request').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await fetch('/api/requests', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          title: document.getElementById('title').value,
          amount: Number(document.getElementById('amount').value || 0),
        }),
      });
      await load();
    } catch (error) { /* keep the form usable */ }
  });
  load();
</script>
""")


@app.get("/clock-page", response_class=HTMLResponse)
def clock_page():
    return _page("Clock", """
<h1>Clock</h1>
<p>Use the API endpoints to clock in and out.</p>
<button id="clock-in" type="button">Clock in</button>
<button id="clock-out" type="button">Clock out</button>
<p id="result"></p>
<script>
  document.getElementById('clock-in').addEventListener('click', async () => {
    try { await fetch('/api/clock/in', {method: 'POST'}); document.getElementById('result').textContent = 'clock-in sent'; }
    catch (error) { document.getElementById('result').textContent = 'clock-in failed'; }
  });
  document.getElementById('clock-out').addEventListener('click', async () => {
    try { await fetch('/api/clock/out', {method: 'POST'}); document.getElementById('result').textContent = 'clock-out sent'; }
    catch (error) { document.getElementById('result').textContent = 'clock-out failed'; }
  });
</script>
""")


@app.get("/reports-page", response_class=HTMLResponse)
def reports_page():
    return _page("Reports", """
<h1>Daily reports</h1>
<label for="date">Date</label>
<input id="date" type="date">
<button id="load-report" type="button">Load report</button>
<pre id="report"></pre>
<script>
  document.getElementById('load-report').addEventListener('click', async () => {
    try {
      const date = document.getElementById('date').value;
      const response = await fetch('/api/reports/daily?date=' + encodeURIComponent(date));
      document.getElementById('report').textContent = JSON.stringify(await response.json(), null, 2);
    } catch (error) { /* leave the panel empty */ }
  });
</script>
""")
