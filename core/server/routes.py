"""API routes for Khunkhaar Web Server."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncGenerator
from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from core.server import agent, logger, store
from core.server.models import ChatMessage, ProjectRecord

router = APIRouter(prefix="/api/projects")


def push_log(project_id: str, message: str, level: str = "INFO") -> None:
    logger.record_log(project_id, message, level=level)


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
    logger.register_project_listener(project_id, queue)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            yield f"data: {json.dumps({'message': 'Connected to live Khunkhaar QA log stream', 'level': 'INFO', 'project_id': project_id})}\n\n"
            while True:
                entry = await queue.get()
                yield f"data: {json.dumps(entry)}\n\n"
        except asyncio.CancelledError:
            logger.unregister_project_listener(project_id, queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/{project_id}/recon")
async def run_recon(project_id: str):
    proj = store.get_project(project_id)
    if not proj:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    from core.server import recon_service
    items = await recon_service.execute_project_recon(project_id, proj, push_log)
    return {"status": "ok", "items_count": len(items)}


@router.post("/{project_id}/tests/{test_id}/run")
async def run_single_test_route(project_id: str, test_id: str):
    proj = store.get_project(project_id)
    if not proj:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    items = store.get_checklist(project_id)
    target_item = next((it for it in items if it.id == test_id), None)
    if not target_item:
        return JSONResponse({"error": "Test item not found"}, status_code=404)

    target_url = (proj.target_urls[0] if proj.target_urls else "http://localhost:8000/api/graphql")
    push_log(project_id, f"⚡ [Single Test] [{target_item.category.upper()}] {target_item.title} (Role: {target_item.role})", level="TEST")

    from core.server import engine_bridge
    status, reason, _reproduction = engine_bridge.execute_checklist_item(
        project_id, target_item, target_url, codebase_dir=proj.root_path)
    target_item.status = status
    target_item.comment = reason
    store.save_checklist(project_id, items)

    lvl = "PASS" if status == "passed" else "FAIL"
    prefix = "✅ [PASS]" if status == "passed" else "🚨 [FAIL]"
    push_log(project_id, f"{prefix} {target_item.title} | {reason}", level=lvl)
    return {"status": status, "reason": reason, "item_id": test_id}


@router.post("/{project_id}/auth/suggest")
async def suggest_auth_config(project_id: str):
    """Draft an auth.db_mint config block from the project's own schema/auth source via the
    configured LLM harness. Returns a suggestion only - nothing is written to
    config/khunkhaar.yaml; the operator reviews and applies it manually."""
    proj = store.get_project(project_id)
    if not proj:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    from core.server import engine_bridge
    from core.server.path_resolver import resolve_codebase_path

    codebase_path = resolve_codebase_path(proj.root_path)
    if not codebase_path:
        return JSONResponse({"error": "Project root_path is not a resolvable local directory"}, status_code=400)

    push_log(project_id, "🔎 Gathering schema/auth source for LLM-assisted auth config draft...")
    app_map = engine_bridge.build_app_map(codebase_path)
    schema_text = engine_bridge.gather_auth_schema_text(codebase_path, app_map)
    if not schema_text:
        push_log(project_id, "⚠️ No schema/auth files found to infer from.", level="WARN")
        return {"status": "empty", "draft": {}}

    draft = agent.suggest_auth_config(schema_text)
    if not draft:
        push_log(project_id, "⚠️ LLM auth config draft failed or was unparseable.", level="WARN")
        return {"status": "failed", "draft": {}}

    push_log(project_id, f"✅ Drafted auth.db_mint with {len(draft.get('roles') or [])} role(s) "
                        f"- review before enabling in config/khunkhaar.yaml.")
    return {"status": "ok", "draft": draft}


@router.post("/{project_id}/approve")
async def approve_and_test(project_id: str, payload: dict):
    selected_ids = payload.get("selected_ids", [])
    from core.server import test_runner
    res = await test_runner.execute_test_pipeline(project_id, selected_ids)
    return res
