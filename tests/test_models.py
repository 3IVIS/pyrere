"""
Tests for pyrere/graph/models.py — Node, Edge, CodeGraph.
"""

from pyrere.graph.models import CodeGraph, Edge, Node

# ─────────────────────────────────────────────────────────────────────────────
# NODE
# ─────────────────────────────────────────────────────────────────────────────


class TestNode:
    def test_required_fields(self):
        node = Node(id="abc", name="foo", type="function", file="/a.py", span=(1, 5))
        assert node.id == "abc"
        assert node.name == "foo"
        assert node.type == "function"
        assert node.file == "/a.py"
        assert node.span == (1, 5)

    def test_optional_fields_default(self):
        node = Node(id="abc", name="foo", type="function", file="/a.py", span=(0, 0))
        assert node.signature is None
        assert node.metadata == {}
        assert node.sources == []

    def test_file_can_be_none(self):
        """Resolver-created nodes have no file on disk."""
        node = Node(id="x", name="external", type="module", file=None, span=(0, 0))
        assert node.file is None

    def test_metadata_is_independent_per_instance(self):
        """Mutable default must not be shared between instances."""
        n1 = Node(id="1", name="a", type="module", file=None, span=(0, 0))
        n2 = Node(id="2", name="b", type="module", file=None, span=(0, 0))
        n1.metadata["x"] = 1
        assert "x" not in n2.metadata

    def test_sources_is_independent_per_instance(self):
        n1 = Node(id="1", name="a", type="module", file=None, span=(0, 0))
        n2 = Node(id="2", name="b", type="module", file=None, span=(0, 0))
        n1.sources.append("tree_sitter")
        assert n2.sources == []


# ─────────────────────────────────────────────────────────────────────────────
# EDGE
# ─────────────────────────────────────────────────────────────────────────────


class TestEdge:
    def test_required_fields(self):
        edge = Edge(id="e1", src="a", dst="b", type="imports")
        assert edge.id == "e1"
        assert edge.src == "a"
        assert edge.dst == "b"
        assert edge.type == "imports"

    def test_confidence_default(self):
        edge = Edge(id="e1", src="a", dst="b", type="calls")
        assert edge.confidence == 1.0

    def test_custom_confidence(self):
        edge = Edge(id="e1", src="a", dst="b", type="calls", confidence=0.75)
        assert edge.confidence == 0.75

    def test_sources_default(self):
        edge = Edge(id="e1", src="a", dst="b", type="imports")
        assert edge.sources == []

    def test_evidence_default(self):
        edge = Edge(id="e1", src="a", dst="b", type="imports")
        assert edge.evidence == {}

    def test_sources_is_independent_per_instance(self):
        e1 = Edge(id="e1", src="a", dst="b", type="imports")
        e2 = Edge(id="e2", src="c", dst="d", type="imports")
        e1.sources.append("tree_sitter")
        assert e2.sources == []


# ─────────────────────────────────────────────────────────────────────────────
# CODEGRAPH
# ─────────────────────────────────────────────────────────────────────────────


class TestCodeGraph:
    def test_empty_on_creation(self, empty_graph):
        assert len(empty_graph.nodes) == 0
        assert len(empty_graph.edges) == 0

    def test_add_node(self, empty_graph):
        node = Node(id="n1", name="foo", type="function", file="/a.py", span=(1, 3))
        empty_graph.add_node(node)
        assert "n1" in empty_graph.nodes
        assert empty_graph.nodes["n1"] is node

    def test_add_node_overwrites_same_id(self, empty_graph):
        n1 = Node(id="n1", name="old", type="function", file="/a.py", span=(0, 0))
        n2 = Node(id="n1", name="new", type="function", file="/a.py", span=(0, 0))
        empty_graph.add_node(n1)
        empty_graph.add_node(n2)
        assert empty_graph.nodes["n1"].name == "new"

    def test_add_edge(self, empty_graph):
        edge = Edge(id="e1", src="a", dst="b", type="imports")
        empty_graph.add_edge(edge)
        assert "e1" in empty_graph.edges
        assert empty_graph.edges["e1"] is edge

    def test_add_edge_overwrites_same_id(self, empty_graph):
        e1 = Edge(id="e1", src="a", dst="b", type="imports", confidence=0.5)
        e2 = Edge(id="e1", src="a", dst="b", type="imports", confidence=0.99)
        empty_graph.add_edge(e1)
        empty_graph.add_edge(e2)
        assert empty_graph.edges["e1"].confidence == 0.99

    def test_simple_graph_fixture(self, simple_graph):
        assert len(simple_graph.nodes) == 2
        assert len(simple_graph.edges) == 1

    def test_nodes_and_edges_are_independent_per_instance(self):
        g1 = CodeGraph()
        g2 = CodeGraph()
        g1.add_node(Node(id="x", name="x", type="module", file=None, span=(0, 0)))
        assert "x" not in g2.nodes
