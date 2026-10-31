"""FastAPI application for the private dashboard.

The factory is the only way to build the app; importing this module opens no socket and starts no
server. Every route is read-only over the metadata store and evidence index. The only writes are
the authentication cookie and the token file generated on first start.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.templating import Jinja2Templates

from core.findings import store as findings_store
from core.metadata import store
from core.shared import paths
from dashboard import auth, data

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                "img-src 'self' data:; form-action 'self'; base-uri 'none'; "
                                "frame-ancestors 'none'"),
}
_IMAGE_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}


def _harden(response: Response) -> Response:
    for key, value in _SECURITY_HEADERS.items():
        response.headers[key] = value
    return response


def create_app(token: str | None = None) -> FastAPI:
    store.init()
    findings_store.init()
    secret = token or auth.ensure_token()

    app = FastAPI(title="KHUNKHAAR dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = secret
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    templates.env.globals["version"] = paths.version()

    def page(request: Request, name: str, status_code: int = 200, **context):
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path != "/login" and not auth.valid_session(
                request.cookies.get(auth.COOKIE_NAME), secret):
            return _harden(page(request, "unauthorized.html", status_code=401))
        return _harden(await call_next(request))

    @app.exception_handler(404)
    async def not_found(request: Request, _exc):
        return page(request, "not_found.html", status_code=404)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if auth.valid_session(request.cookies.get(auth.COOKIE_NAME), secret):
            return RedirectResponse("/", status_code=303)
        return page(request, "login.html")

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request):
        form = await request.form()
        supplied = str(form.get("token") or "")
        if not auth.tokens_match(supplied, secret):
            return page(request, "login.html", status_code=401,
                        error="That token did not match.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            auth.COOKIE_NAME, auth.session_value(secret), max_age=auth.SESSION_TTL_SECONDS,
            httponly=True, samesite="strict", secure=False, path="/")
        return response

    @app.post("/logout")
    def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        return page(request, "overview.html", overview=data.overview())

    @app.get("/project/{project_id}", response_class=HTMLResponse)
    def project(request: Request, project_id: str):
        detail = data.project_detail(project_id)
        if detail is None:
            return page(request, "not_found.html", status_code=404)
        return page(request, "project.html", detail=detail)

    @app.get("/execution/{run_id}", response_class=HTMLResponse)
    def execution(request: Request, run_id: str):
        detail = data.execution_detail(run_id)
        if detail is None:
            return page(request, "not_found.html", status_code=404)
        return page(request, "execution.html", detail=detail)

    @app.get("/diff/{project_id}", response_class=HTMLResponse)
    def diff(request: Request, project_id: str):
        detail = data.diff_detail(project_id)
        if detail is None:
            return page(request, "not_found.html", status_code=404)
        return page(request, "diff.html", detail=detail)

    @app.get("/evidence/{ref_id}")
    def evidence(ref_id: int):
        row = store.get_evidence_ref(ref_id)
        if not row:
            return PlainTextResponse("evidence not found", status_code=404)
        relative = str(row.get("path") or "")
        suffix = Path(relative).suffix.lower()
        target = data.resolve_artifact(relative)
        if target is None:
            return PlainTextResponse("evidence not found", status_code=404)
        if suffix in _IMAGE_MEDIA:
            return FileResponse(target, media_type=_IMAGE_MEDIA[suffix], filename=target.name,
                                content_disposition_type="inline")
        text = data.read_text_evidence(relative)
        if text is not None:
            content, truncated = text
            headers = {"Content-Type": "text/plain; charset=utf-8"}
            if truncated:
                headers["X-KHUNKHAAR-Truncated"] = "true"
            return PlainTextResponse(content, headers=headers)
        return FileResponse(target, media_type="application/octet-stream", filename=target.name)

    return app
