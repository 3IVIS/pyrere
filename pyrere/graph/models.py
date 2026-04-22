from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Node:
    id: str
    name: str
    type: str
    # Optional because external/resolver-created nodes may not map to a real
    # file on disk, and several code paths explicitly check ``if node.file``.
    file: str | None
    span: tuple[int, int]
    signature: dict | None = None
    metadata: dict = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    type: str
    confidence: float = 1.0
    sources: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


@dataclass
class CodeGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)

    def add_node(self, node: Node) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        self.edges[edge.id] = edge
