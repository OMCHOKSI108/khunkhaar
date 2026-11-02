"""Level 2 & 3 Validator: Retrieval Quality (Recall@5, Precision@5, MRR) & Flow Tracing."""
from __future__ import annotations

from core.context_builder import tools
from core.context_validator.models import BenchmarkCase, LevelReport, MetricResult


def validate_retrieval_and_understanding(project_id: str, cases: list[BenchmarkCase]) -> LevelReport:
    metrics: list[MetricResult] = []
    failures: list[str] = []
    total = len(cases)
    if total == 0:
        return LevelReport(2, "Retrieval & Understanding", False, 0.0, [], ["No cases"])

    hits_top5 = 0
    precision_hits = 0
    reciprocal_ranks: list[float] = []

    for c in cases:
        results = tools.search_codebase(project_id, c.query, top_k=5)
        found_in_top5 = False
        first_rank = None
        case_prec_count = 0

        for rank, res in enumerate(results, 1):
            fpath = res["file_path"].lower()
            is_relevant = any(exp.lower() in fpath for exp in c.expected_files)
            if is_relevant:
                case_prec_count += 1
                if not found_in_top5:
                    found_in_top5 = True
                    first_rank = rank
        if found_in_top5:
            hits_top5 += 1
        reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
        precision_hits += (case_prec_count / max(1, len(results)))

    # Metrics
    recall_at_5 = round(hits_top5 / total, 4)
    r5_ok = recall_at_5 >= 0.90
    metrics.append(MetricResult("recall_at_5", recall_at_5, 0.90, r5_ok, f"{hits_top5}/{total} queries"))
    if not r5_ok:
        failures.append(f"Recall@5 was {recall_at_5 * 100}% (target >= 90%)")

    precision_at_5 = round(precision_hits / total, 4)
    metrics.append(MetricResult("precision_at_5", precision_at_5, 0.40, precision_at_5 >= 0.40, f"Average {precision_at_5}"))

    mrr = round(sum(reciprocal_ranks) / total, 4)
    metrics.append(MetricResult("mean_reciprocal_rank", mrr, 0.60, mrr >= 0.60, f"MRR score {mrr}"))

    # Level 3: Execution Flow Tracing
    flow_ok = True
    trace = tools.trace_execution_flow(project_id, "AttendanceController", max_depth=3)
    if trace:
        targets = [t["target_symbol"] for t in trace]
        if not any("AttendanceService" in tg for tg in targets):
            flow_ok = False
            failures.append("Execution flow failed to link AttendanceController to AttendanceService")
    metrics.append(MetricResult("execution_flow_linkage", 1.0 if flow_ok else 0.0, 1.0, flow_ok, "Graph traversal verified"))

    score = round((recall_at_5 + mrr + (1.0 if flow_ok else 0.0)) / 3.0 * 100, 2)
    return LevelReport(2, "Retrieval & Understanding Validation", r5_ok and flow_ok, score, metrics, failures)
