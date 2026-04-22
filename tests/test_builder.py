"""
Tests for pyrere/aggregator/builder.py — build_module_index,
resolve_import_ref, and build_graph.
"""

import os
import textwrap

import pytest

from pyrere.aggregator.builder import (
    _resolve_module_name,
    build_graph,
    build_module_index,
    resolve_import_ref,
)
from pyrere.symbols.extractor import ImportRef


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def write(base, rel, content=""):
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(textwrap.dedent(content))
    return path


# ─────────────────────────────────────────────────────────────────────────────
# build_module_index
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildModuleIndex:
    def test_simple_package(self, tmp_path):
        write(str(tmp_path), "mypkg/__init__.py")
        write(str(tmp_path), "mypkg/utils.py")
        idx = build_module_index(str(tmp_path))
        assert "mypkg" in idx
        assert "mypkg.utils" in idx

    def test_init_maps_to_package_name(self, tmp_path):
        """mypkg/__init__.py → key is 'mypkg', not 'mypkg.__init__'"""
        write(str(tmp_path), "mypkg/__init__.py")
        idx = build_module_index(str(tmp_path))
        assert "mypkg.__init__" not in idx
        assert "mypkg" in idx

    def test_nested_package(self, tmp_path):
        write(str(tmp_path), "pkg/sub/__init__.py")
        write(str(tmp_path), "pkg/sub/module.py")
        idx = build_module_index(str(tmp_path))
        assert "pkg.sub" in idx
        assert "pkg.sub.module" in idx

    def test_values_are_absolute_paths(self, tmp_path):
        write(str(tmp_path), "mypkg/__init__.py")
        idx = build_module_index(str(tmp_path))
        for path in idx.values():
            assert os.path.isabs(path)

    def test_skips_venv(self, tmp_path):
        write(str(tmp_path), "mypkg/real.py")
        write(str(tmp_path), ".venv/lib/something.py")
        idx = build_module_index(str(tmp_path))
        assert not any(".venv" in k for k in idx)

    def test_empty_repo(self, tmp_path):
        idx = build_module_index(str(tmp_path))
        assert idx == {}


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_module_name
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveModuleName:
    def setup_method(self):
        self.index = {
            "mypkg": "/repo/mypkg/__init__.py",
            "mypkg.utils": "/repo/mypkg/utils.py",
            "mypkg.sub": "/repo/mypkg/sub/__init__.py",
        }

    def test_exact_match(self):
        assert _resolve_module_name("mypkg", self.index) == "/repo/mypkg/__init__.py"

    def test_dotted_exact_match(self):
        assert _resolve_module_name("mypkg.utils", self.index) == "/repo/mypkg/utils.py"

    def test_suffix_match(self):
        # "utils" matches "mypkg.utils" via suffix
        result = _resolve_module_name("utils", self.index)
        assert result == "/repo/mypkg/utils.py"

    def test_no_match_returns_none(self):
        assert _resolve_module_name("nonexistent", self.index) is None

    def test_empty_string_returns_none(self):
        assert _resolve_module_name("", self.index) is None


# ─────────────────────────────────────────────────────────────────────────────
# resolve_import_ref
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveImportRef:
    @pytest.fixture()
    def repo(self, tmp_path):
        write(str(tmp_path), "mypkg/__init__.py", "")
        write(str(tmp_path), "mypkg/utils.py", "")
        write(str(tmp_path), "mypkg/sub/__init__.py", "")
        write(str(tmp_path), "mypkg/sub/helper.py", "")
        return tmp_path

    def test_absolute_import(self, repo):
        idx = build_module_index(str(repo))
        caller = str(repo / "mypkg" / "utils.py")
        imp = ImportRef(level=0, module="mypkg.sub", names=[])
        result = resolve_import_ref(imp, caller, str(repo), idx)
        assert any("sub" in r for r in result)

    def test_absolute_from_import(self, repo):
        idx = build_module_index(str(repo))
        caller = str(repo / "mypkg" / "utils.py")
        imp = ImportRef(level=0, module="mypkg", names=["sub"])
        result = resolve_import_ref(imp, caller, str(repo), idx)
        assert len(result) >= 1

    def test_relative_import_level_1(self, repo):
        idx = build_module_index(str(repo))
        caller = str(repo / "mypkg" / "utils.py")
        imp = ImportRef(level=1, module="", names=["sub"])
        result = resolve_import_ref(imp, caller, str(repo), idx)
        assert any("sub" in r for r in result)

    def test_relative_import_with_module(self, repo):
        idx = build_module_index(str(repo))
        caller = str(repo / "mypkg" / "utils.py")
        imp = ImportRef(level=1, module="sub", names=["helper"])
        result = resolve_import_ref(imp, caller, str(repo), idx)
        assert any("sub" in r for r in result)

    def test_level_too_deep_returns_empty(self, repo):
        idx = build_module_index(str(repo))
        caller = str(repo / "mypkg" / "utils.py")
        imp = ImportRef(level=99, module="", names=["x"])
        result = resolve_import_ref(imp, caller, str(repo), idx)
        assert result == []

    def test_unresolvable_returns_empty(self, repo):
        idx = build_module_index(str(repo))
        caller = str(repo / "mypkg" / "utils.py")
        imp = ImportRef(level=0, module="totally.unknown", names=[])
        result = resolve_import_ref(imp, caller, str(repo), idx)
        assert result == []

    def test_init_caller_level_1(self, repo):
        """from . import X inside __init__.py means THIS package."""
        idx = build_module_index(str(repo))
        caller = str(repo / "mypkg" / "__init__.py")
        imp = ImportRef(level=1, module="", names=["utils"])
        result = resolve_import_ref(imp, caller, str(repo), idx)
        assert any("utils" in r for r in result)


# ─────────────────────────────────────────────────────────────────────────────
# build_graph — integration-level
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildGraph:
    def test_returns_code_graph(self, simple_repo):
        from pyrere.graph.models import CodeGraph

        graph = build_graph(str(simple_repo))
        assert isinstance(graph, CodeGraph)

    def test_module_nodes_created(self, simple_repo):
        graph = build_graph(str(simple_repo))
        module_nodes = [n for n in graph.nodes.values() if n.type == "module"]
        assert len(module_nodes) >= 4  # __init__, utils, sub/__init__, helper

    def test_function_nodes_created(self, simple_repo):
        graph = build_graph(str(simple_repo))
        fn_nodes = [n for n in graph.nodes.values() if n.type == "function"]
        names = {n.name for n in fn_nodes}
        assert "helper" in names
        assert "sub_help" in names

    def test_import_edges_created(self, simple_repo):
        graph = build_graph(str(simple_repo))
        import_edges = [e for e in graph.edges.values() if e.type == "imports"]
        assert len(import_edges) >= 1

    def test_contains_edges_created(self, simple_repo):
        graph = build_graph(str(simple_repo))
        contains_edges = [e for e in graph.edges.values() if e.type == "contains"]
        assert len(contains_edges) >= 1

    def test_empty_repo_builds_empty_graph(self, tmp_path):
        graph = build_graph(str(tmp_path))
        assert len(graph.nodes) == 0

    def test_graph_node_ids_are_unique(self, simple_repo):
        graph = build_graph(str(simple_repo))
        ids = list(graph.nodes.keys())
        assert len(ids) == len(set(ids))

    def test_all_edge_endpoints_exist(self, simple_repo):
        """Every edge src and dst must point to a node that exists in the graph."""
        graph = build_graph(str(simple_repo))
        for edge in graph.edges.values():
            assert edge.src in graph.nodes, f"Missing src node: {edge.src}"
            assert edge.dst in graph.nodes, f"Missing dst node: {edge.dst}"

    def test_call_edges_created(self, tmp_path):
        write(str(tmp_path), "pkg/__init__.py", "")
        write(str(tmp_path), "pkg/a.py", "def foo():\n    bar()\n\ndef bar():\n    pass\n")
        graph = build_graph(str(tmp_path))
        call_edges = [e for e in graph.edges.values() if e.type == "calls"]
        assert len(call_edges) >= 1

    def test_inherit_edges_created(self, tmp_path):
        write(str(tmp_path), "pkg/__init__.py", "")
        write(str(tmp_path), "pkg/models.py", "class Base:\n    pass\n\nclass Child(Base):\n    pass\n")
        graph = build_graph(str(tmp_path))
        inherit_edges = [e for e in graph.edges.values() if e.type == "inherits"]
        assert len(inherit_edges) >= 1

    def test_unicode_source_file(self, tmp_path):
        """Files with non-ASCII content should not crash the builder."""
        write(str(tmp_path), "pkg/__init__.py", "")
        write(str(tmp_path), "pkg/i18n.py", '# -*- coding: utf-8 -*-\nNAME = "héllo wörld"\n')
        graph = build_graph(str(tmp_path))
        assert len(graph.nodes) >= 1
