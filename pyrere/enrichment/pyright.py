"""
pyrere/enrichment/pyright.py
──────────────────────────
Runs pyright --outputjson and stamps type-level diagnostics onto CKG nodes
as issues (tool="pyright").

pyright surfaces things ruff/bandit miss:
  • type mismatches and invalid argument types
  • unreachable code blocks
  • undefined names and missing imports
  • return-type violations

Supports both install modes:
  pip install pyright   →  python -m pyright  (wraps the npm package)
  npm install -g pyright →  bare `pyright` binary
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from pyrere.graph.models import CodeGraph
from pyrere.utils.spatial import locate, stamp_issue

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def _severity(sev: str) -> str:
    """Map pyright severity strings to our three-level scheme."""
    if sev == "error":
        return "error"
    if sev == "warning":
        return "warning"
    return "info"  # pyright uses "information"


def _find_pyright_cmd() -> list[str] | None:
    """
    Return the command list that successfully runs pyright, or None if
    pyright is not installed.
    Tries `python -m pyright` (pip install) before bare `pyright` (npm global).
    """
    candidates = [
        [sys.executable, "-m", "pyright"],
        ["pyright"],
    ]
    for cmd in candidates:
        try:
            r = subprocess.run(
                [*cmd, "--version"],  # RUF005: unpacking instead of concatenation
                capture_output=True,
                timeout=15,
            )
            if r.returncode == 0:
                return cmd
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC RUNNER
# ─────────────────────────────────────────────────────────────────────────────


def run_pyright(repo_root: str, graph: CodeGraph, spatial: dict) -> int:
    """
    Run pyright against `repo_root`, then stamp each diagnostic onto the
    innermost CKG node that owns the reported source location.

    Returns the number of diagnostics stamped (0 if pyright is absent or
    produces no output).
    """
    cmd = _find_pyright_cmd()
    if cmd is None:
        print("[enrichment] pyright not found — skipping (pip install pyright)")
        return 0

    try:
        result = subprocess.run(
            [*cmd, "--outputjson", repo_root],  # RUF005: unpacking instead of concatenation
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        print("[enrichment] pyright timed out after 5 min — skipping")
        return 0

    raw = result.stdout.strip()
    if not raw:
        return 0

    # pyright can emit startup warnings before the JSON blob; skip to first '{'
    brace = raw.find("{")
    if brace == -1:
        return 0
    try:
        data = json.loads(raw[brace:])
    except json.JSONDecodeError:
        return 0

    count = 0
    for diag in data.get("generalDiagnostics", []):
        # pyright reports absolute file paths
        abs_path = os.path.abspath(diag.get("file", ""))
        # pyright line numbers are 0-indexed
        line = diag.get("range", {}).get("start", {}).get("line", 0) + 1
        sev = diag.get("severity", "information")
        # rule is the pyright check name e.g. "reportMissingImports"
        rule = diag.get("rule") or "pyright"
        msg = diag.get("message", "").strip()

        owner = locate(graph, spatial, abs_path, line)
        stamp_issue(
            graph,
            owner,
            {
                "tool": "pyright",
                "code": rule,
                "message": msg,
                "line": line,
                "severity": _severity(sev),
            },
        )
        count += 1

    return count
