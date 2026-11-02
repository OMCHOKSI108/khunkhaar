"""Level 1 Validator: Ingestion and AST Structural Correctness."""
from __future__ import annotations

from pathlib import Path
from core.context_builder import ast_analyzer, scanner
from core.context_validator.models import LevelReport, MetricResult


def validate_ingestion_and_indexing(root_path: Path) -> LevelReport:
    metrics: list[MetricResult] = []
    failures: list[str] = []

    # 1. File Discovery
    files = scanner.scan_directory(root_path)
    discovered_rel = {f.relative_path for f in files}
    expected_files = {
        "src/controllers/attendance.controller.ts",
        "src/services/attendance.service.ts",
        "src/repositories/attendance.repository.ts",
        "src/models/attendance.model.ts",
        "src/middleware/auth.middleware.ts",
    }
    found_files = expected_files.intersection(discovered_rel)
    disc_acc = round(len(found_files) / max(1, len(expected_files)), 4)
    file_ok = disc_acc >= 0.99
    metrics.append(MetricResult("file_discovery_accuracy", disc_acc, 0.99, file_ok, f"{len(found_files)}/{len(expected_files)} files"))
    if not file_ok:
        failures.append(f"Missing files: {expected_files - discovered_rel}")

    # 2. Symbol Extraction
    all_symbols = []
    for f in files:
        syms, _ = ast_analyzer.analyze_file("bench_proj", Path(f.path), f.relative_path)
        all_symbols.extend(syms)

    extracted_names = {s.symbol_name for s in all_symbols}
    expected_symbols = {
        "AttendanceController", "AttendanceService", "calculateLateArrival",
        "AttendanceRepository", "AttendanceModel", "verifyAuthToken", "POST /api/attendance/punch"
    }
    found_syms = expected_symbols.intersection(extracted_names)
    sym_acc = round(len(found_syms) / max(1, len(expected_symbols)), 4)
    sym_ok = sym_acc >= 0.98
    metrics.append(MetricResult("symbol_extraction_accuracy", sym_acc, 0.98, sym_ok, f"{len(found_syms)}/{len(expected_symbols)} symbols"))
    if not sym_ok:
        failures.append(f"Missing symbols: {expected_symbols - extracted_names}")

    # 3. Line Number Integrity
    valid_lines = sum(1 for s in all_symbols if s.start_line > 0 and s.end_line >= s.start_line)
    line_acc = round(valid_lines / max(1, len(all_symbols)), 4)
    line_ok = line_acc == 1.0
    metrics.append(MetricResult("citation_line_integrity", line_acc, 1.0, line_ok, f"{valid_lines}/{len(all_symbols)} valid"))
    if not line_ok:
        failures.append("Some extracted symbols had corrupted start_line/end_line ranges")

    passed = file_ok and sym_ok and line_ok
    score = round((disc_acc + sym_acc + line_acc) / 3.0 * 100, 2)
    return LevelReport(1, "Ingestion and Indexing Validation", passed, score, metrics, failures)
