"""Orchestrates test pipeline execution, live telemetry logging, and status persistence."""
from __future__ import annotations

import asyncio
from core.server import logger, store
from core.server.test_executor import execute_single_test, resolve_target_url


async def execute_test_pipeline(project_id: str, selected_ids: list[str]) -> dict[str, int]:
    """Execute selected test cases sequentially with live log streaming and state updates."""
    proj = store.get_project(project_id)
    target_url = (proj.target_urls[0] if proj and proj.target_urls else "http://localhost:8000/api/graphql")
    items = store.get_checklist(project_id)
    
    selected_set = set(selected_ids)
    target_items = [it for it in items if it.id in selected_set]
    total = len(target_items)

    logger.record_log(
        project_id,
        f"🚀 [Pipeline Start] Initializing live test suite ({total} tests) against {target_url}",
        level="INFO"
    )

    passed_count = 0
    failed_count = 0

    for idx, item in enumerate(target_items, start=1):
        item.selected = True
        logger.record_log(
            project_id,
            f"⚡ [Test {idx}/{total}] [{item.category.upper()}] {item.title} (Role: {item.role})",
            level="TEST"
        )
        await asyncio.sleep(0.2)

        status, reason, code = execute_single_test(item, target_url)
        item.status = status

        if status == "passed":
            passed_count += 1
            logger.record_log(project_id, f"✅ [PASS] {item.title} | {reason}", level="PASS")
        else:
            failed_count += 1
            logger.record_log(project_id, f"🚨 [FAIL] {item.title} | {reason}", level="FAIL")

        # Save progressive state to SQLite
        store.save_checklist(project_id, items)
        await asyncio.sleep(0.15)

    summary_msg = f"🏁 [Pipeline Complete] {passed_count} Passed, {failed_count} Failed out of {total} tests executed."
    logger.record_log(project_id, summary_msg, level="INFO")

    return {
        "total": total,
        "passed": passed_count,
        "failed": failed_count
    }
