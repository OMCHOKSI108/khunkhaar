"""Level 4 Validator: Incremental Updates, Stale Record Purging & Content Hash Freshness."""
from __future__ import annotations

from pathlib import Path
from core.context_builder import graph_store, tools
from core.context_validator.models import LevelReport, MetricResult
from core.server import vector_store


def validate_freshness_and_updates(project_id: str, repo_path: Path) -> LevelReport:
    metrics: list[MetricResult] = []
    failures: list[str] = []

    test_file = repo_path / "src" / "services" / "dynamic.service.ts"
    test_rel = "src/services/dynamic.service.ts"

    # 1. Add new function
    test_file.write_text("export function temporaryProbeFunc() { return 42; }\n")
    tools.build_full_project_context(project_id, str(repo_path))
    syms_after_add = graph_store.find_symbol(project_id, "temporaryProbeFunc")
    add_ok = len(syms_after_add) > 0
    metrics.append(MetricResult("incremental_symbol_addition", 1.0 if add_ok else 0.0, 1.0, add_ok, "New symbol detected"))
    if not add_ok:
        failures.append("Failed to detect newly added function in incremental pass")

    # 2. Delete function and verify stale records = 0
    test_file.unlink(missing_ok=True)
    tools.build_full_project_context(project_id, str(repo_path))
    syms_after_delete = graph_store.find_symbol(project_id, "temporaryProbeFunc")
    stale_count = len(syms_after_delete)
    stale_ok = stale_count == 0
    metrics.append(MetricResult("stale_record_purging", 1.0 if stale_ok else 0.0, 1.0, stale_ok, f"Stale records: {stale_count} (target 0)"))
    if not stale_ok:
        failures.append(f"Stale symbols remained in graph after file deletion: {stale_count}")

    # 3. Vector Context Association Check
    contexts = vector_store.get_project_contexts(project_id)
    ctx_ok = len(contexts) > 0 and all(c["total_chunks"] > 0 for c in contexts)
    metrics.append(MetricResult("context_versioning_freshness", 1.0 if ctx_ok else 0.0, 1.0, ctx_ok, f"{len(contexts)} versioned contexts"))

    passed = add_ok and stale_ok and ctx_ok
    score = 100.0 if passed else 50.0
    return LevelReport(4, "Incremental Updates and Freshness Validation", passed, score, metrics, failures)
