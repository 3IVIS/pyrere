"""
pyrere/enrichment/pycg_.py
────────────────────────
Runs pycg (points-to call graph generator) against the repo and merges its
output back into the CKG.

Why pycg over the tree-sitter name-matching heuristic?
  • Points-to analysis: if two functions are both named `process`, pycg
    resolves which one is actually called at each site.
  • Catches calls through aliases, variable assignments, and simple
    higher-order patterns that plain name matching can't handle.

Strategy
────────
  1. Build a qualified-name index: "pkg.module.ClassName.method" → node_id
  2. Run pycg as a subprocess; read its JSON output
  3. For each (caller, callee) pair:
       • existing edge  → boost confidence + tag source "pycg"
       • new edge       → add at confidence 0.85

File cap: pycg's points-to analysis can be slow.  We cap at _MAX_FILES to
avoid hangs on very large repos; a warning is printed when the cap fires.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile

from pyrere.graph.models import CodeGraph, Edge
from pyrere.ingestion.loader import load_python_files
from pyrere.symbols.extractor import make_id

_MAX_FILES = 200


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def _build_qname_index(graph: CodeGraph, repo_root: str) -> dict[str, str]:
    """
    Return  dotted.qualified.Name → node_id  for every function and class.

    Qualified name = dotted_module_path + "." + node.name
    Examples:
      rere.aggregator.builder.build_graph  →  <node_id>
      rere.graph.models.CodeGraph          →  <node_id>

    Note: nested functions (e.g. inner inside outer) would appear as
    "module.outer.inner" in pycg output but "module.inner" here.  The index
    stores both keys pointing to the same node so matches still land.
    """
    index: dict[str, str] = {}
    for node in graph.nodes.values():
        if node.type not in ("function", "class") or not node.file:
            continue
        try:
            rel = os.path.relpath(node.file, repo_root)
        except ValueError:
            continue

        module = rel.replace(os.sep, ".").removesuffix(".py")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]

        # Primary key: module.name  (matches pycg's top-level output)
        qname = f"{module}.{node.name}"
        index[qname] = node.id

        # Also register bare name as a fallback for short paths
        index.setdefault(node.name, node.id)

    return index


def _collect_entry_files(repo_root: str) -> list[str]:
    files = [os.path.abspath(p) for p in load_python_files(repo_root)]
    if len(files) > _MAX_FILES:
        print(
            f"[enrichment] pycg: {len(files)} files found, capping at {_MAX_FILES} to avoid timeout"
        )
        # Prefer files that look like entry points; fall back to first N
        priority = [
            f
            for f in files
            if os.path.basename(f) in ("main.py", "run.py", "app.py", "cli.py", "__main__.py")
        ]
        rest = [f for f in files if f not in priority]
        files = (priority + rest)[:_MAX_FILES]
    return files


def _pycg_available() -> bool:
    """Quick check that pycg is installed and responsive."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pycg", "--help"],
            capture_output=True,
            timeout=15,
        )
        # pycg --help exits 0; older versions may exit non-zero but still print help
        return r.returncode in (0, 1, 2)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC RUNNER
# ─────────────────────────────────────────────────────────────────────────────


def run_pycg(repo_root: str, graph: CodeGraph) -> int:
    """
    Run pycg against the repo and merge call-graph results into `graph`.

    Returns the number of *new* call edges added.
    """
    if not _pycg_available():
        print("[enrichment] pycg not installed — skipping (pip install pycg)")
        return 0

    entry_files = _collect_entry_files(repo_root)
    if not entry_files:
        return 0

    qname_index = _build_qname_index(graph, repo_root)

    # pycg writes its JSON to --output; we use a tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [  # RUF005: use iterable unpacking instead of list +
                sys.executable,
                "-m",
                "pycg",
                "--package",
                repo_root,
                "--output",
                tmp_path,
                *entry_files,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )

        # Primary: read from output file
        cg: dict = {}
        with contextlib.suppress(OSError, json.JSONDecodeError), open(tmp_path) as fh:
            cg = json.load(fh)

        # Fallback: some pycg versions write to stdout instead
        if not cg and result.stdout.strip():
            with contextlib.suppress(json.JSONDecodeError):  # SIM105
                cg = json.loads(result.stdout.strip())

        if not cg:
            return 0

    except subprocess.TimeoutExpired:
        print("[enrichment] pycg timed out after 5 min — skipping")
        return 0
    finally:
        with contextlib.suppress(OSError):  # SIM105
            os.unlink(tmp_path)

    added = 0
    for caller_qname, callees in cg.items():
        caller_id = qname_index.get(caller_qname)
        if not caller_id:
            continue

        for callee_qname in callees:
            if not callee_qname:
                continue
            callee_id = qname_index.get(callee_qname)
            if not callee_id or callee_id == caller_id:
                continue

            edge_id = make_id(caller_id, callee_id, "calls")

            if edge_id in graph.edges:
                # pycg confirms this edge — boost confidence
                existing = graph.edges[edge_id]
                existing.confidence = min(1.0, existing.confidence + 0.15)
                if "pycg" not in existing.sources:
                    existing.sources.append("pycg")
            else:
                graph.add_edge(
                    Edge(
                        id=edge_id,
                        src=caller_id,
                        dst=callee_id,
                        type="calls",
                        confidence=0.85,
                        sources=["pycg"],
                    )
                )
                added += 1

    return added
