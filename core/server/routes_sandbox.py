"""API routes for Ephemeral Code Execution Sandbox."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.sandbox import tools

sandbox_router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


class SandboxRunRequest(BaseModel):
    script: str
    language: str = "python"
    execution_profile: str = "python_qa"
    network_policy: str = "none"
    target_url: str | None = None
    target_id: str | None = None
    timeout_seconds: int = Field(default=120, ge=1, le=300)
    collect_artifacts: bool = True
    memory_mb: int = Field(default=512, ge=64, le=1024)


@sandbox_router.post("/run")
async def run_sandbox_job(payload: SandboxRunRequest) -> dict[str, Any]:
    res = tools.run_in_sandbox(
        script=payload.script,
        language=payload.language,
        execution_profile=payload.execution_profile,
        network_policy=payload.network_policy,
        target_url=payload.target_url,
        target_id=payload.target_id,
        timeout_seconds=payload.timeout_seconds,
        collect_artifacts=payload.collect_artifacts,
        memory_mb=payload.memory_mb,
    )
    return res


@sandbox_router.get("/{execution_id}/status")
async def get_status(execution_id: str) -> dict[str, Any]:
    record = tools.get_execution_status(execution_id)
    if record.get("status") == "NOT_FOUND":
        return JSONResponse({"error": "Execution not found"}, status_code=404)
    return record


@sandbox_router.get("/{execution_id}/logs")
async def get_logs(execution_id: str) -> dict[str, Any]:
    return tools.get_execution_logs(execution_id)


@sandbox_router.get("/{execution_id}/artifacts")
async def get_artifacts(execution_id: str) -> dict[str, Any]:
    items = tools.get_execution_artifacts(execution_id)
    return {"execution_id": execution_id, "artifacts": items, "count": len(items)}


@sandbox_router.delete("/{execution_id}")
async def terminate_job(execution_id: str) -> dict[str, Any]:
    return tools.terminate_execution(execution_id)
