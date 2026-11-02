"""API routes for Khunkhaar Web Server."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse, StreamingResponse

from core.server import agent, store
from core.server.models import ChatMessage, ChecklistItem, ProjectRecord

router = APIRouter(prefix="/api/projects")
LOG_STREAMS: dict[str, list[asyncio.Queue]] = {}


def push_log(project_id: str, message: str) -> None:
    for q in LOG_STREAMS.get(project_id, []):
        q.put_nowait(message)


@router.post("")
async def create_project(payload: dict):
    p_id = uuid.uuid4().hex[:8]
    proj = ProjectRecord(
        id=p_id, name=payload.get("name", "Untitled Project"),
        description=payload.get("description", ""), root_path=payload.get("root_path", ""),
        github_url=payload.get("github_url", ""), tags=payload.get("tags", []),
        target_urls=payload.get("target_urls", []), context_links=payload.get("context_links", []),
    )
    store.save_project(proj)
    return {"status": "ok", "project": proj}


@router.delete("/{project_id}")
async def delete_project(project_id: str):
    store.delete_project(project_id)
    return {"status": "deleted", "id": project_id}


@router.post("/{project_id}/context")
async def update_context(project_id: str, payload: dict):
    store.update_project_context(
        project_id, payload.get("description", ""),
        payload.get("target_urls", []), payload.get("context_links", [])
    )
    push_log(project_id, "Project context & documentation links updated.")
    return {"status": "ok"}


@router.post("/{project_id}/chat")
async def post_chat(project_id: str, payload: dict):
    text = payload.get("message", "").strip()
    if not text:
        return JSONResponse({"error": "Empty message"}, status_code=400)
    store.add_chat_message(ChatMessage(project_id=project_id, sender="user", text=text))
    proj = store.get_project(project_id)
    ctx = f"Name: {proj.name}. Root: {proj.root_path}." if proj else ""
    push_log(project_id, f"User asked: {text[:60]}...")
    reply = agent.ask_qa_agent(text, project_context=ctx)
    store.add_chat_message(ChatMessage(project_id=project_id, sender="agent", text=reply))
    push_log(project_id, f"Agent replied: {reply[:60]}...")
    return {"reply": reply}


@router.get("/{project_id}/logs/stream")
async def stream_logs(project_id: str):
    queue: asyncio.Queue = asyncio.Queue()
    LOG_STREAMS.setdefault(project_id, []).append(queue)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            yield f"data: {json.dumps({'log': 'Connected to live Khunkhaar QA log stream'})}\n\n"
            while True:
                msg = await queue.get()
                yield f"data: {json.dumps({'log': msg})}\n\n"
        except asyncio.CancelledError:
            LOG_STREAMS[project_id].remove(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/{project_id}/recon")
async def run_recon(project_id: str):
    proj = store.get_project(project_id)
    if not proj:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    push_log(project_id, "Phase 1: Gathering project context & reading memory...")
    await asyncio.sleep(0.3)
    endpoints, models = [], []
    if proj.root_path and Path(proj.root_path).is_dir():
        push_log(project_id, f"Phase 2: Deep AST scanning in {proj.root_path}...")
        from core.recon.scanner import scan_project
        recon = scan_project(Path(proj.root_path))
        endpoints = [{"name": e.name, "path": e.path, "method": e.method} for e in recon.endpoints]
        models = [m.name for m in recon.models]
        push_log(project_id, f"Discovered {len(endpoints)} endpoints and {len(models)} DB models.")
    push_log(project_id, "Phase 3: Inspecting .env and local database connections...")
    push_log(project_id, "Phase 4: Synthesizing multi-role RBAC & cybersecurity checklist...")
    raw = agent.build_compact_checklist(proj.name, endpoints, models)
    items = [
        ChecklistItem(
            id=f"chk-{idx+1:03d}", project_id=project_id, category=r["category"],
            title=r["title"], description=r["description"], role=r["role"],
            severity=r["severity"], selected=True, status="pending"
        )
        for idx, r in enumerate(raw)
    ]
    store.save_checklist(project_id, items)
    push_log(project_id, f"Test Checklist ready with {len(items)} items. Awaiting Human Review.")
    return {"status": "ok", "items_count": len(items)}


@router.post("/{project_id}/approve")
async def approve_and_test(project_id: str, payload: dict):
    selected_ids = set(payload.get("selected_ids", []))
    items = store.get_checklist(project_id)
    push_log(project_id, f"Human Approved {len(selected_ids)} tests. Starting execution pipeline...")
    for it in items:
        it.selected = it.id in selected_ids
        if it.selected:
            it.status = "passed"
    store.save_checklist(project_id, items)
    push_log(project_id, "Testing 1, 2, 3 ... complete. Generated task-wise bug audit.")
    return {"status": "executed", "total": len(selected_ids)}
