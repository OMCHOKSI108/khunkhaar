"""API routes for Tool Context Builder operations."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.context_builder import benchmark, tools
from core.server import store

context_router = APIRouter(prefix="/api/projects/{project_id}/context", tags=["context"])


@context_router.post("/build")
async def build_context(project_id: str):
    proj = store.get_project(project_id)
    if not proj:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    res = tools.build_full_project_context(project_id, proj.root_path)
    return {"status": "ok", "result": res}


@context_router.post("/assemble")
async def assemble_context(project_id: str, payload: dict):
    task = payload.get("task", "").strip()
    if not task:
        return JSONResponse({"error": "Task description is required"}, status_code=400)
    budget = int(payload.get("token_budget", 12000))
    include = payload.get("include")
    bundle = tools.assemble_context(project_id, task, include=include, token_budget=budget)
    return {"status": "ok", "bundle": bundle}


@context_router.post("/search")
async def search_code(project_id: str, payload: dict):
    query = payload.get("query", "").strip()
    if not query:
        return {"status": "ok", "results": []}
    top_k = int(payload.get("top_k", 10))
    hits = tools.search_codebase(project_id, query, top_k=top_k)
    return {"status": "ok", "query": query, "results": hits}


@context_router.get("/symbols")
async def find_symbols(project_id: str, name: str = ""):
    hits = tools.find_symbol(project_id, name)
    return {"status": "ok", "symbols": hits}


@context_router.get("/trace")
async def trace_flow(project_id: str, entry_symbol: str, depth: int = 3):
    flow = tools.trace_execution_flow(project_id, entry_symbol, max_depth=depth)
    return {"status": "ok", "entry": entry_symbol, "flow": flow}


@context_router.post("/benchmark")
async def run_benchmark(project_id: str, payload: dict):
    queries = payload.get("queries", [])
    report = benchmark.run_accuracy_benchmark(project_id, queries)
    return {"status": "ok", "report": report}
