"""
pyrere/utils/spatial.py
────────────────────
Shared spatial index: maps file+line → the innermost CKG node that owns
that source location.  Used by both the enrichment layer (pyright) and the
flow/issue layer (ruff, bandit, vulture).
"""

from __future__ import annotations

import os

from pyrere.graph.models import CodeGraph


def build_spatial_index(graph: CodeGraph) -> dict[str, list[tuple[int, int, str]]]:
    """
    Return  abs_file_path → [(start_line, end_line, node_id), …]
    sorted by span size ascending so the *smallest* (most specific) scope
    sorts first — enabling innermost-scope lookups in O(n) per file.
    """
    index: dict[str, list[tuple[int, int, str]]] = {}
    for node in graph.nodes.values():
        if not node.file or not node.span:
            continue
        path = os.path.abspath(node.file)
        index.setdefault(path, []).append((node.span[0], node.span[1], node.id))
    for entries in index.values():
        entries.sort(key=lambda t: t[1] - t[0])
    return index


def find_owner(
    entries: list[tuple[int, int, str]],
    line: int,
) -> str | None:
    """
    Walk the sorted entry list and return the node_id of the innermost span
    containing `line`.  Returns None if no span matches.
    """
    best_id: str | None = None
    best_span = 10**9
    for start, end, node_id in entries:
        if start <= line <= end:
            span = end - start
            if span < best_span:
                best_span = span
                best_id = node_id
    return best_id


def module_node_for(graph: CodeGraph, abs_path: str) -> str | None:
    """Return the file-level module node ID for `abs_path`, or None."""
    for node in graph.nodes.values():
        if node.type == "module" and node.file and os.path.abspath(node.file) == abs_path:
            return node.id
    return None


def locate(
    graph: CodeGraph,
    spatial: dict[str, list[tuple[int, int, str]]],
    abs_path: str,
    line: int,
) -> str | None:
    """
    Resolve (file, line) → node_id.
    Tries the innermost symbol first; falls back to the module node.
    """
    entries = spatial.get(abs_path, [])
    return find_owner(entries, line) or module_node_for(graph, abs_path)


def stamp_issue(graph: CodeGraph, node_id: str | None, issue: dict) -> None:
    """Append `issue` to `node.metadata['issues']` for the given node_id."""
    if not node_id or node_id not in graph.nodes:
        return
    graph.nodes[node_id].metadata.setdefault("issues", []).append(issue)
