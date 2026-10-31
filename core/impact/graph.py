"""Build, persist and reload the dependency graph.

A build is three passes and two outputs. Extraction reads the checkout, resolution links it to the
Phase 2 application map, journeys and schema declarations, and the finished graph is written twice:
as rows in the metadata store (the queryable index, scoped per project) and as one JSON document
under `intelligence/dependency-graphs/` (the readable artifact that keeps the extraction notes the
tables do not have a column for).

Rebuilding never touches a repository: every read goes through the same bounded walker used by the
application map, and nothing outside KHUNKHAAR's own directories is written.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from core.impact import extract, model, resolve
from core.metadata import store
from core.shared import evidence, paths
from core.shared.config import current
from core.shared.redaction import redact_obj


def limits_from_config() -> extract.Limits:
    """Extraction bounds as configured; every value has a safe default in DEFAULTS."""
    return extract.Limits(
        max_files=int(current().get("impact", "max_files", 4000)),
        max_file_bytes=int(current().get("impact", "max_source_bytes", 250_000)),
        max_symbols_per_file=int(current().get("impact", "max_symbols_per_file", 500)),
        parse_js_ts=bool(current().get("impact", "parse_js_ts", True)),
    )


def graph_path(project_id: str) -> Path:
    return paths.dependency_graphs_dir() / f"{paths.slug_segment(project_id)}.json"


def build(project_id: str, root: Path, *, limits: extract.Limits | None = None,
          app_map: dict[str, Any] | None = None, persist: bool = True) -> dict[str, Any]:
    """Extraction + resolution, then the store rows and the JSON artifact when `persist`."""
    root = Path(root).resolve()
    extraction = extract.extract(root, project_id, limits=limits or limits_from_config())
    graph = extraction.graph
    links = resolve.apply(graph, root=root, project_id=project_id, app_map=app_map)
    graph.generated_at = _now()

    artifact = None
    stored = {"nodes": 0, "edges": 0}
    if persist:
        stored = store.replace_impact_graph(
            project_id,
            [node.to_dict() for node in graph.nodes.values()],
            [edge.to_dict() for edge in graph.edges],
        )
        artifact = str(_write_json(graph_path(project_id), graph.to_dict()))
    return {
        "graph": graph,
        "artifact": artifact,
        "stored": stored,
        "generated_at": graph.generated_at,
        "stats": graph.stats(),
        "extraction": extraction.languages,
        "links": links,
    }


def load(project_id: str) -> model.Graph | None:
    """The stored graph, from the JSON artifact when it is readable and from the rows otherwise.

    The artifact is preferred because it also carries the extraction notes and the resolution
    summary. If it is missing or unreadable the rows still answer every graph question, so a lost
    artifact degrades the report's commentary, not its correctness.
    """
    target = graph_path(project_id)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("nodes"):
            graph = model.Graph.from_dict(data)
            if graph.project_id == project_id:
                return graph
    except (OSError, ValueError, TypeError):
        pass
    stored = store.load_impact_graph(project_id)
    if not stored["nodes"]:
        return None
    return model.Graph.from_dict({
        "project_id": project_id, "generated_at": stored.get("updated_at") or "",
        "nodes": stored["nodes"], "edges": stored["edges"],
        "notes": ["loaded from the metadata store; the JSON artifact was missing or unreadable, "
                  "so extraction notes from the original build are not available"],
    })


def info(project_id: str) -> dict[str, Any]:
    """Counts and timestamps without loading the graph itself."""
    details = store.impact_graph_info(project_id)
    target = graph_path(project_id)
    details["artifact"] = evidence.relative(target) if target.exists() else None
    return details


def _write_json(target: Path, payload: dict[str, Any]) -> Path:
    """Write 0600 via a temporary file, so a reader never sees a half-written graph."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(redact_obj(payload), handle, indent=2, default=str, ensure_ascii=False)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    target.chmod(0o600)
    return target


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
