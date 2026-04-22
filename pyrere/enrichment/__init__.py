"""
pyrere/enrichment/__init__.py
───────────────────────────
Step 4: Enrichment Layer

Orchestrates three semantic / graph tools and merges their findings back
into the CKG before the flow-analysis (Step 8) and LLM layers run.

  Tool      What it adds
  ────────  ─────────────────────────────────────────────────────────────────
  pyright   Type diagnostics stamped as issues on nodes (tool="pyright")
  grimp     Confirms / adds file-level import edges using the installed
            package graph rather than text scanning
  pycg      Confirms / adds call edges using points-to analysis rather
            than name-matching heuristics

All three tools are optional — missing installs are silently skipped so the
pipeline degrades gracefully.

Install enrichment tools:
    pip install pyright grimp pycg
"""

from __future__ import annotations

from pyrere.enrichment.grimp_ import run_grimp
from pyrere.enrichment.pycg_ import run_pycg
from pyrere.enrichment.pyright import run_pyright
from pyrere.graph.models import CodeGraph
from pyrere.utils.spatial import build_spatial_index


def enrich_graph(graph: CodeGraph, repo_root: str) -> dict[str, int]:
    """
    Run all enrichment tools against `repo_root` and merge results into
    `graph` in-place.

    Returns a summary dict:
        {
            "pyright_issues": int,   # diagnostics stamped as issues
            "grimp_edges":    int,   # new import edges added
            "pycg_edges":     int,   # new call edges added
        }
    """
    # Build the spatial index once; pyright needs it for line → node mapping.
    # (grimp and pycg build their own internal indices.)
    spatial = build_spatial_index(graph)

    pyright_count = run_pyright(repo_root, graph, spatial)
    grimp_count = run_grimp(repo_root, graph)
    pycg_count = run_pycg(repo_root, graph)

    summary = {
        "pyright_issues": pyright_count,
        "grimp_edges": grimp_count,
        "pycg_edges": pycg_count,
    }

    print(
        f"[enrichment] complete — "
        f"pyright={pyright_count} issues  "
        f"grimp={grimp_count} new edges  "
        f"pycg={pycg_count} new edges"
    )
    return summary


__all__ = ["enrich_graph"]
