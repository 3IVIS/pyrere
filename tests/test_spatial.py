"""
Tests for pyrere/utils/spatial.py — build_spatial_index, find_owner,
locate, stamp_issue.
"""

import os

import pytest

from pyrere.graph.models import CodeGraph, Node
from pyrere.symbols.extractor import make_id
from pyrere.utils.spatial import (
    build_spatial_index,
    find_owner,
    locate,
    module_node_for,
    stamp_issue,
)

# ─────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def graph_with_spans():
    """
    Graph with one module node and two function nodes at known spans.

        /repo/a.py
            module   span (0, 20)
            outer()  span (1, 15)
            inner()  span (3,  8)   ← nested inside outer
    """
    g = CodeGraph()
    file_path = os.path.abspath("/repo/a.py")

    mod_id = make_id(file_path)
    outer_id = make_id(file_path, "outer")
    inner_id = make_id(file_path, "inner")

    g.add_node(Node(id=mod_id, name="a.py", type="module", file=file_path, span=(0, 20)))
    g.add_node(Node(id=outer_id, name="outer", type="function", file=file_path, span=(1, 15)))
    g.add_node(Node(id=inner_id, name="inner", type="function", file=file_path, span=(3, 8)))

    return g, file_path, mod_id, outer_id, inner_id


# ─────────────────────────────────────────────────────────────────────────────
# build_spatial_index
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildSpatialIndex:
    def test_returns_dict(self, graph_with_spans):
        g, *_ = graph_with_spans
        idx = build_spatial_index(g)
        assert isinstance(idx, dict)

    def test_keys_are_absolute_paths(self, graph_with_spans):
        g, file_path, *_ = graph_with_spans
        idx = build_spatial_index(g)
        assert file_path in idx

    def test_entries_contain_all_nodes_for_file(self, graph_with_spans):
        g, file_path, mod_id, outer_id, inner_id = graph_with_spans
        idx = build_spatial_index(g)
        node_ids_in_index = {entry[2] for entry in idx[file_path]}
        assert mod_id in node_ids_in_index
        assert outer_id in node_ids_in_index
        assert inner_id in node_ids_in_index

    def test_sorted_by_span_size_ascending(self, graph_with_spans):
        """Smallest span must sort first so innermost scope wins."""
        g, file_path, *_ = graph_with_spans
        idx = build_spatial_index(g)
        entries = idx[file_path]
        spans = [end - start for start, end, _ in entries]
        assert spans == sorted(spans)

    def test_nodes_without_file_are_excluded(self):
        g = CodeGraph()
        g.add_node(Node(id="x", name="ext", type="module", file=None, span=(0, 5)))
        idx = build_spatial_index(g)
        assert idx == {}

    def test_empty_graph_returns_empty_index(self, empty_graph):
        assert build_spatial_index(empty_graph) == {}

    def test_multiple_files_are_grouped(self):
        g = CodeGraph()
        p1 = os.path.abspath("/repo/a.py")
        p2 = os.path.abspath("/repo/b.py")
        g.add_node(Node(id="n1", name="a", type="module", file=p1, span=(0, 10)))
        g.add_node(Node(id="n2", name="b", type="module", file=p2, span=(0, 10)))
        idx = build_spatial_index(g)
        assert p1 in idx
        assert p2 in idx


# ─────────────────────────────────────────────────────────────────────────────
# find_owner
# ─────────────────────────────────────────────────────────────────────────────


class TestFindOwner:
    def test_returns_innermost_node(self, graph_with_spans):
        _g, file_path, _mod_id, _outer_id, inner_id = graph_with_spans
        idx = build_spatial_index(_g)
        entries = idx[file_path]
        # line 5 is inside inner (3-8) which is inside outer (1-15)
        result = find_owner(entries, line=5)
        assert result == inner_id

    def test_returns_outer_when_not_in_inner(self, graph_with_spans):
        _g, file_path, _mod_id, outer_id, _inner_id = graph_with_spans
        idx = build_spatial_index(_g)
        entries = idx[file_path]
        # line 12 is in outer (1-15) but NOT in inner (3-8)
        result = find_owner(entries, line=12)
        assert result == outer_id

    def test_returns_none_when_no_match(self, graph_with_spans):
        g, file_path, *_ = graph_with_spans
        idx = build_spatial_index(g)
        entries = idx[file_path]
        # line 100 is outside all spans
        assert find_owner(entries, line=100) is None

    def test_empty_entries(self):
        assert find_owner([], line=5) is None

    def test_exact_boundary_line(self, graph_with_spans):
        _g, file_path, _mod_id, _outer_id, inner_id = graph_with_spans
        idx = build_spatial_index(_g)
        entries = idx[file_path]
        # line 3 is the start of inner
        result = find_owner(entries, line=3)
        assert result == inner_id


# ─────────────────────────────────────────────────────────────────────────────
# module_node_for
# ─────────────────────────────────────────────────────────────────────────────


class TestModuleNodeFor:
    def test_finds_module_node(self, graph_with_spans):
        g, file_path, mod_id, *_ = graph_with_spans
        result = module_node_for(g, file_path)
        assert result == mod_id

    def test_returns_none_for_unknown_path(self, graph_with_spans):
        g, *_ = graph_with_spans
        assert module_node_for(g, "/nonexistent/file.py") is None

    def test_returns_none_for_non_module_node(self):
        g = CodeGraph()
        p = os.path.abspath("/repo/a.py")
        g.add_node(Node(id="fn1", name="foo", type="function", file=p, span=(1, 5)))
        assert module_node_for(g, p) is None


# ─────────────────────────────────────────────────────────────────────────────
# locate
# ─────────────────────────────────────────────────────────────────────────────


class TestLocate:
    def test_locates_inner_function(self, graph_with_spans):
        g, file_path, _mod_id, _outer_id, inner_id = graph_with_spans
        idx = build_spatial_index(g)
        assert locate(g, idx, file_path, line=5) == inner_id

    def test_falls_back_to_module_node(self, graph_with_spans):
        g, file_path, mod_id, *_ = graph_with_spans
        idx = build_spatial_index(g)
        # line 100 has no symbol match → falls back to module node
        assert locate(g, idx, file_path, line=100) == mod_id

    def test_unknown_file_returns_none(self, graph_with_spans):
        g, *_ = graph_with_spans
        idx = build_spatial_index(g)
        assert locate(g, idx, "/repo/unknown.py", line=1) is None


# ─────────────────────────────────────────────────────────────────────────────
# stamp_issue
# ─────────────────────────────────────────────────────────────────────────────


class TestStampIssue:
    def test_stamps_issue_onto_node(self, graph_with_spans):
        g, _file_path, mod_id, *_ = graph_with_spans
        issue = {"tool": "ruff", "code": "E501", "message": "line too long", "line": 2}
        stamp_issue(g, mod_id, issue)
        assert issue in g.nodes[mod_id].metadata["issues"]

    def test_multiple_issues_appended(self, graph_with_spans):
        g, _file_path, mod_id, *_ = graph_with_spans
        stamp_issue(g, mod_id, {"tool": "ruff", "code": "E501", "message": "x", "line": 1})
        stamp_issue(g, mod_id, {"tool": "bandit", "code": "B101", "message": "y", "line": 2})
        assert len(g.nodes[mod_id].metadata["issues"]) == 2

    def test_none_node_id_is_ignored(self, empty_graph):
        stamp_issue(empty_graph, None, {"tool": "ruff", "code": "E501", "message": "", "line": 1})
        # Should not raise

    def test_unknown_node_id_is_ignored(self, empty_graph):
        stamp_issue(
            empty_graph, "nonexistent", {"tool": "ruff", "code": "E501", "message": "", "line": 1}
        )
        # Should not raise
