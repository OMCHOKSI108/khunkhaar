"""FastAPI Web Application for Khunkhaar Interactive Platform."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncGenerator
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from core.server import logger, store
from core.server.routes import router

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def create_web_app() -> FastAPI:
    store.init_db()
    app = FastAPI(title="KHUNKHAAR QA Web Platform")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.include_router(router)

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        projects = store.get_projects()
        return templates.TemplateResponse(request, "home.html", {"projects": projects})

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    async def project_page(request: Request, project_id: str):
        proj = store.get_project(project_id)
        if not proj:
            return HTMLResponse("<h1>Project Not Found</h1>", status_code=404)
        messages = store.get_chat_messages(project_id)
        checklist = store.get_checklist(project_id)
        from core.server import store_logs
        logs = store_logs.get_project_logs(project_id, limit=80)
        return templates.TemplateResponse(request, "project.html", {
            "project": proj, "messages": messages, "checklist": checklist, "logs": logs
        })

    @app.get("/logs", response_class=HTMLResponse)
    async def centralized_logs(request: Request):
        from core.server import store_logs
        history = store_logs.get_all_logs(limit=200)
        return templates.TemplateResponse(request, "logs.html", {"history": history})

    @app.get("/api/logs/stream")
    async def global_logs_stream():
        queue: asyncio.Queue = asyncio.Queue()
        logger.register_global_listener(queue)

        async def stream():
            try:
                yield f"data: {json.dumps({'message': 'Connected to Khunkhaar centralized telemetry feed', 'level': 'INFO', 'project_id': 'system', 'timestamp': '00:00:00.000'})}\n\n"
                while True:
                    entry = await queue.get()
                    yield f"data: {json.dumps(entry)}\n\n"
            except asyncio.CancelledError:
                logger.unregister_global_listener(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
