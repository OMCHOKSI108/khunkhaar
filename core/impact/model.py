"""Typed nodes and edges for the dependency graph.

The graph answers one question precisely: if node X changes, which nodes depend on X? Every edge
therefore points from the dependent to the dependency (`controller imports service`), and impact
propagation walks edges backwards. Mixing the two directions is the usual way a dependency graph
starts lying, so the direction is stated once here and every producer follows it.

Node identifiers are deterministic strings (`file:src/app.py`, `api:GET /users/{}`), so a graph can
be rebuilt, diffed and inspected without a database. The metadata store scopes them per project.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

NODE_TYPES = ("file", "class", "symbol", "service", "api", "entity", "config", "test",
              "workflow", "journey")
RELATIONS = ("imports", "calls", "implements", "exposes", "reads", "writes", "triggers", "tests")

# `{id}`, `<id>` and `:id` are the three ways the same path parameter is spelled by OpenAPI/FastAPI,
# Flask and Express. They are collapsed to `{}` so one endpoint is one node regardless of the
# library that declared it.
_PARAM_SEGMENT = re.compile(r"^(?:\{[^/]*\}|<[^/]*>|:[^/]+)$")


def normalize_path(raw: str) -> str:
    path = (raw or "").strip().split("?", 1)[0]
    parts = [part for part in path.split("/") if part]
    normalized = ["{}" if _PARAM_SEGMENT.match(part) else part for part in parts]
    return "/" + "/".join(normalized) if normalized else "/"


def file_id(path: str) -> str:
    return f"file:{path}"


def class_id(path: str, name: str) -> str:
    return f"class:{path}:{name}"


def service_id(path: str, name: str) -> str:
    return f"service:{path}:{name}"


def symbol_id(path: str, qualified: str) -> str:
    return f"symbol:{path}:{qualified}"


def api_id(method: str, path: str) -> str:
    return f"api:{(method or 'ANY').upper()} {normalize_path(path)}"


def entity_id(name: str) -> str:
    return f"entity:{name}"


def config_id(path: str) -> str:
    return f"config:{path}"


def test_id(path: str) -> str:
    return f"test:{path}"


def workflow_id(feature: str) -> str:
    return f"workflow:{feature}"


def journey_id(value: str) -> str:
    return f"journey:{value}"


def path_of(node_id: str) -> str | None:
    """The repository path a node lives at, or None for graph-level nodes."""
    kind, _, rest = node_id.partition(":")
    if kind in ("file", "config", "test"):
        return rest
    if kind in ("class", "symbol", "service"):
        return rest.rsplit(":", 1)[0]
    return None


@dataclass(frozen=True)
class Node:
    node_id: str
    type: str
    name: str
    path: str | None = None
    meta: dict[str, Any] = field(default_factory=dict, compare=False)
    confidence: float = 1.0
    origin: str = "explicit"

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "type": self.type, "name": self.name,
                "path": self.path, "meta": self.meta, "confidence": self.confidence,
                "origin": self.origin}


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    relation: str
    confidence: float = 1.0
    evidence: str = ""
    origin: str = "explicit"  # explicit | inferred

    def to_dict(self) -> dict[str, Any]:
        return {"src": self.src, "dst": self.dst, "relation": self.relation,
                "confidence": self.confidence, "evidence": self.evidence,
                "origin": self.origin}


@dataclass
class Graph:
    project_id: str
    root: str = ""
    generated_at: str = ""
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    files_scanned: int = 0
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    resolution: dict[str, Any] = field(default_factory=dict)
    _out: dict[str, list[Edge]] = field(default_factory=dict, init=False, repr=False)
    _in: dict[str, list[Edge]] = field(default_factory=dict, init=False, repr=False)
    _edge_keys: dict[tuple[str, str, str], int] = field(default_factory=dict, init=False,
                                                       repr=False)

    def __post_init__(self) -> None:
        for edge in self.edges:
            self._index(edge)

    def _index(self, edge: Edge) -> None:
        self._out.setdefault(edge.src, []).append(edge)
        self._in.setdefault(edge.dst, []).append(edge)

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.node_id)
        if existing is not None:
            return existing
        self.nodes[node.node_id] = node
        return node

    def annotate(self, node_id: str, **meta: Any) -> None:
        """Add facts discovered after the node was created (a parse failure, a line number)."""
        node = self.nodes.get(node_id)
        if node is not None:
            self.nodes[node_id] = replace(node, meta={**node.meta, **meta})

    def has(self, node_id: str) -> bool:
        return node_id in self.nodes

    def node(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def add_edge(self, src: str, dst: str, relation: str, *, confidence: float = 1.0,
                 evidence: str = "", origin: str = "explicit") -> Edge | None:
        """Add an edge between existing nodes.

        The same triple is stored once. A stronger observation replaces a weaker one; an equal
        one keeps the first evidence, because the first is the most direct statement of the
        relationship and later passes are refinements, not new facts.
        """
        if relation not in RELATIONS:
            raise ValueError(f"unknown relation '{relation}'")
        if src not in self.nodes or dst not in self.nodes:
            return None
        key = (src, dst, relation)
        edge = Edge(src, dst, relation, confidence, evidence, origin)
        index = self._edge_keys.get(key)
        if index is None:
            self._edge_keys[key] = len(self.edges)
            self.edges.append(edge)
            self._index(edge)
            return edge
        current = self.edges[index]
        if confidence > current.confidence:
            self.edges[index] = edge
            # The adjacency lists hold the old object; replace it in place.
            self._out[src] = [edge if item is current else item for item in self._out.get(src, [])]
            self._in[dst] = [edge if item is current else item for item in self._in.get(dst, [])]
            return edge
        return current

    def dependencies(self, node_id: str) -> list[Edge]:
        return list(self._out.get(node_id, []))

    def dependents(self, node_id: str) -> list[Edge]:
        return list(self._in.get(node_id, []))

    def by_type(self, *types: str) -> list[Node]:
        wanted = set(types)
        return [node for node in self.nodes.values() if not wanted or node.type in wanted]

    def path_index(self) -> dict[str, str]:
        """Repository path -> canonical node id, for relating a change to the graph."""
        index: dict[str, str] = {}
        for node in self.nodes.values():
            if node.path and node.type in ("file", "config", "test"):
                index.setdefault(node.path, node.node_id)
        return index

    def stats(self) -> dict[str, Any]:
        nodes = Counter(node.type for node in self.nodes.values())
        edges = Counter(edge.relation for edge in self.edges)
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "nodes_by_type": dict(sorted(nodes.items())),
            "edges_by_relation": dict(sorted(edges.items())),
            "inferred_nodes": sum(1 for n in self.nodes.values() if n.origin == "inferred"),
            "inferred_edges": sum(1 for e in self.edges if e.origin == "inferred"),
            "files_scanned": self.files_scanned,
            "truncated": self.truncated,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "root": self.root,
            "generated_at": self.generated_at,
            "stats": self.stats(),
            "nodes": [node.to_dict() for node in sorted(self.nodes.values(),
                                                        key=lambda n: n.node_id)],
            "edges": [edge.to_dict() for edge in sorted(
                self.edges, key=lambda e: (e.src, e.dst, e.relation))],
            "resolution": self.resolution,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Graph":
        stats = data.get("stats") or {}
        graph = cls(project_id=str(data.get("project_id") or ""),
                    root=str(data.get("root") or ""),
                    generated_at=str(data.get("generated_at") or ""),
                    files_scanned=int(data.get("files_scanned") or stats.get("files_scanned") or 0),
                    truncated=bool(data.get("truncated", stats.get("truncated", False))),
                    notes=list(data.get("notes") or []),
                    resolution=dict(data.get("resolution") or {}))
        for item in data.get("nodes") or []:
            graph.add_node(Node(node_id=item["node_id"], type=item["type"], name=item["name"],
                                path=item.get("path"), meta=dict(item.get("meta") or {}),
                                confidence=float(item.get("confidence", 1.0)),
                                origin=item.get("origin", "explicit")))
        for item in data.get("edges") or []:
            graph.add_edge(item["src"], item["dst"], item["relation"],
                           confidence=float(item.get("confidence", 1.0)),
                           evidence=item.get("evidence", ""),
                           origin=item.get("origin", "explicit"))
        return graph

    def merge(self, other: "Graph") -> None:
        """Fold another extraction into this graph. Used by tests and incremental tooling."""
        for node in other.nodes.values():
            self.add_node(node)
        for edge in other.edges:
            self.add_edge(edge.src, edge.dst, edge.relation, confidence=edge.confidence,
                          evidence=edge.evidence, origin=edge.origin)

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self) -> Iterable[Node]:
        return iter(self.nodes.values())
