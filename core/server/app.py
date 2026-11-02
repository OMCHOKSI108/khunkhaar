"""FastAPI Web Application for Khunkhaar Interactive Platform."""
from __future__ import annotations

from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.server import store
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
        return templates.TemplateResponse(request, "project.html", {
            "project": proj, "messages": messages, "checklist": checklist
        })

    return app
