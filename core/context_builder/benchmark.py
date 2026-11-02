"""Benchmark Evaluator: measures retrieval accuracy, MRR, Recall@K, and citation accuracy."""
from __future__ import annotations

from typing import Any

from core.context_builder import tools


def run_accuracy_benchmark(project_id: str, test_queries: list[dict[str, Any]]) -> dict[str, Any]:
    """Runs standard benchmark queries against Context Builder.
    Each query dict: {"query": str, "expected_files": list[str], "expected_symbols": list[str]}"""
    total = len(test_queries)
    if total == 0:
        return {"error": "No benchmark queries provided"}

    hits_at_1 = 0
    hits_at_3 = 0
    hits_at_5 = 0
    reciprocal_ranks: list[float] = []
    citation_validations = 0
    total_citations = 0

    for item in test_queries:
        q = item["query"]
        expected = [f.lower() for f in item.get("expected_files", [])]
        results = tools.search_codebase(project_id, q, top_k=5)

        first_rank: int | None = None
        for rank, res in enumerate(results, 1):
            fpath = res["file_path"].lower()
            # Check file match
            matched = any(exp in fpath for exp in expected)
            if matched:
                if first_rank is None:
                    first_rank = rank
                if rank == 1:
                    hits_at_1 += 1
                if rank <= 3:
                    hits_at_3 += 1
                if rank <= 5:
                    hits_at_5 += 1

            # Validate citation line integrity
            total_citations += 1
            if res.get("start_line", 0) > 0 and res.get("end_line", 0) >= res.get("start_line", 0):
                citation_validations += 1

        reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)

    recall_at_1 = round(hits_at_1 / total, 4)
    recall_at_3 = round(hits_at_3 / total, 4)
    recall_at_5 = round(hits_at_5 / total, 4)
    mrr = round(sum(reciprocal_ranks) / total, 4)
    citation_acc = round(citation_validations / max(1, total_citations), 4)

    return {
        "benchmark_queries_evaluated": total,
        "metrics": {
            "recall_at_1": recall_at_1,
            "recall_at_3": recall_at_3,
            "recall_at_5": recall_at_5,
            "mean_reciprocal_rank_mrr": mrr,
            "citation_accuracy": citation_acc,
        },
        "status": "PASS" if mrr >= 0.5 and recall_at_5 >= 0.7 else "REVIEW",
    }
