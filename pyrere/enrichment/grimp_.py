"""
pyrere/enrichment/grimp_.py
─────────────────────────
Uses grimp to build an accurate installed-package import graph and merges
any edges it finds that the tree-sitter text-scanning resolver missed.

Why grimp over our resolver?
  • Respects __all__ and re-exports in __init__.py files
  • Handles namespace packages and editable installs correctly
  • Confirms existing edges (boosts confidence) and surfaces new ones

grimp needs the repo packages to be importable (i.e. installed in editable
mode with `pip install -e .`, or the repo root on sys.path).  If a package
can't be loaded it is silently skipped.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

from pyrere.aggregator.builder import build_module_index
from pyrere.graph.models import CodeGraph, Edge
from pyrere.symbols.extractor import make_id

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────


@contextmanager
def _repo_on_path(repo_root: str):
    """
    Temporarily insert *repo_root* at the front of sys.path so that grimp can
    import the packages it needs to analyse, then restore sys.path on exit.

    Using a context manager (rather than a bare sys.path.insert) prevents the
    permanent mutation of the caller's import environment — important when
    pyrere is used as a library inside another application.
    """
    inserted = False
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
        inserted = True
    try:
        yield
    finally:
        if inserted and repo_root in sys.path:
            sys.path.remove(repo_root)


def _find_top_level_packages(repo_root: str) -> list[str]:
    """
    Return names of top-level Python packages (directories that contain an
    __init__.py at their root).  Excludes hidden dirs and common non-code dirs.
    """
    skip = {"build", "dist", ".git", ".hg", "node_modules", "__pycache__"}
    packages: list[str] = []
    try:
        for name in sorted(os.listdir(repo_root)):
            if name.startswith(".") or name in skip:
                continue
            path = os.path.join(repo_root, name)
            init = os.path.join(path, "__init__.py")
            if os.path.isdir(path) and os.path.exists(init):
                packages.append(name)
    except OSError:
        pass
    return packages


def _module_node_id(graph: CodeGraph, abs_path: str) -> str | None:
    """Return the module node ID for `abs_path`, or None."""
    for node in graph.nodes.values():
        if node.type == "module" and node.file and os.path.abspath(node.file) == abs_path:
            return node.id
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC RUNNER
# ─────────────────────────────────────────────────────────────────────────────


def run_grimp(repo_root: str, graph: CodeGraph) -> int:
    """
    Build a grimp import graph for every top-level package in `repo_root`,
    then for each import pair:

      • If the edge already exists in the CKG → boost its confidence slightly
        and add "grimp" to its sources list.
      • If it's new → add it at confidence 0.98.

    Returns the number of *new* edges added.
    """
    try:
        import grimp  
    except ImportError:
        print("[enrichment] grimp not installed — skipping (pip install grimp)")
        return 0

    packages = _find_top_level_packages(repo_root)
    if not packages:
        print("[enrichment] grimp: no top-level packages found — skipping")
        return 0

    # Build the dotted-name → abs-path index once (shared with builder)
    module_index = build_module_index(repo_root)

    added = 0

    # sys.path is only modified for the duration of this block and is
    # restored to its original state when the context manager exits.
    with _repo_on_path(repo_root):
        for pkg in packages:
            try:
                # include_external_packages=False keeps the graph within the repo
                ig = grimp.build_graph(pkg, include_external_packages=False)
            except Exception as exc:
                print(f"[enrichment] grimp: skipping package '{pkg}': {exc}")
                continue

            for importer_mod in ig.modules:
                try:
                    imported_mods = ig.find_modules_directly_imported_by(importer_mod)
                except Exception:
                    continue

                importer_file = module_index.get(importer_mod)
                if not importer_file:
                    continue
                src_id = _module_node_id(graph, os.path.abspath(importer_file))
                if not src_id:
                    continue

                for imported_mod in imported_mods:
                    importee_file = module_index.get(imported_mod)
                    if not importee_file:
                        continue
                    dst_id = _module_node_id(graph, os.path.abspath(importee_file))
                    if not dst_id or dst_id == src_id:
                        continue

                    edge_id = make_id(src_id, dst_id, "imports")

                    if edge_id in graph.edges:
                        existing = graph.edges[edge_id]
                        existing.confidence = min(1.0, existing.confidence + 0.05)
                        if "grimp" not in existing.sources:
                            existing.sources.append("grimp")
                    else:
                        graph.add_edge(
                            Edge(
                                id=edge_id,
                                src=src_id,
                                dst=dst_id,
                                type="imports",
                                confidence=0.98,
                                sources=["grimp"],
                            )
                        )
                        added += 1

    return added
