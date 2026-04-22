from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional


@dataclass
class Node:
    id: str
    name: str
    type: str
    file: str
    span: Tuple[int, int]
    signature: Optional[dict] = None
    metadata: dict = field(default_factory=dict)
    sources: List[str] = field(default_factory=list)


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    type: str
    confidence: float = 1.0
    sources: List[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


@dataclass
class CodeGraph:
    nodes: Dict[str, Node] = field(default_factory=dict)
    edges: Dict[str, Edge] = field(default_factory=dict)

    def add_node(self, node: Node):
        self.nodes[node.id] = node

    def add_edge(self, edge: Edge):
        self.edges[edge.id] = edge