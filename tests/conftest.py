"""
Shared fixtures for the pyrere test suite.
"""

import os
import textwrap
import pytest

from pyrere.graph.models import CodeGraph, Edge, Node
from pyrere.symbols.extractor import make_id


# ─────────────────────────────────────────────────────────────────────────────
# FILESYSTEM HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def write_file(base: str, rel: str, content: str) -> str:
    """Write *content* to <base>/<rel>, creating directories as needed."""
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(content))
    return path


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH FIXTURES
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def empty_graph() -> CodeGraph:
    return CodeGraph()


@pytest.fixture()
def simple_graph() -> CodeGraph:
    """A graph with two module nodes and one imports edge."""
    graph = CodeGraph()
    a_id = make_id("/repo/a.py")
    b_id = make_id("/repo/b.py")
    graph.add_node(Node(id=a_id, name="a.py", type="module", file="/repo/a.py", span=(0, 10)))
    graph.add_node(Node(id=b_id, name="b.py", type="module", file="/repo/b.py", span=(0, 5)))
    graph.add_edge(
        Edge(
            id=make_id(a_id, b_id, "imports"),
            src=a_id,
            dst=b_id,
            type="imports",
            confidence=0.95,
        )
    )
    return graph


# ─────────────────────────────────────────────────────────────────────────────
# REPO FIXTURES
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def simple_repo(tmp_path):
    """
    Minimal repo layout:

        mypkg/
            __init__.py
            utils.py
            sub/
                __init__.py
                helper.py
    """
    write_file(str(tmp_path), "mypkg/__init__.py", "from mypkg.utils import helper\n")
    write_file(str(tmp_path), "mypkg/utils.py", "def helper(): pass\n")
    write_file(str(tmp_path), "mypkg/sub/__init__.py", "")
    write_file(str(tmp_path), "mypkg/sub/helper.py", "def sub_help(): pass\n")
    return tmp_path
