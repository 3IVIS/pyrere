"""
pyrere/flow/analyzer.py
─────────────────────
Step 8: Flow + Issue Analysis

Runs ruff, vulture, and bandit against the repo, then stamps each CKG node
with an `issues` list inside its metadata.

Each issue dict:
    {
        "tool":     "ruff" | "vulture" | "bandit",
        "code":     str,           # e.g. "E501", "B101", "unused-import"
        "message":  str,
        "line":     int,
        "severity": "error" | "warning" | "info",
    }

All three tools are optional — if a tool is not installed or times out it is
silently skipped so the rest of the pipeline still runs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from pyrere.graph.models import CodeGraph
from pyrere.utils.spatial import build_spatial_index, locate, stamp_issue

# ─────────────────────────────────────────────────────────────────────────────
# RUFF
# ─────────────────────────────────────────────────────────────────────────────


def _ruff_severity(code: str) -> str:
    if code.startswith(("E", "F")):
        return "error"
    if code.startswith("W"):
        return "warning"
    return "info"


def run_ruff(repo_root: str, graph: CodeGraph, spatial: dict) -> int:
    """
    Invoke  `python -m ruff check --output-format=json`  and stamp findings.
    Returns the number of issues attached; 0 if ruff is unavailable.
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--output-format=json",
                "--no-cache",
                repo_root,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[flow] ruff skipped: {exc}")
        return 0

    raw = result.stdout.strip()
    if not raw:
        return 0

    try:
        findings = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    count = 0
    for f in findings:
        abs_path = os.path.abspath(f.get("filename", ""))
        line = (f.get("location") or {}).get("row", 0)
        code = f.get("code") or "?"
        msg = f.get("message", "")
        owner = locate(graph, spatial, abs_path, line)
        stamp_issue(
            graph,
            owner,
            {
                "tool": "ruff",
                "code": code,
                "message": msg,
                "line": line,
                "severity": _ruff_severity(code),
            },
        )
        count += 1
    return count


# ─────────────────────────────────────────────────────────────────────────────
# VULTURE
# ─────────────────────────────────────────────────────────────────────────────

# e.g.  "pyrere/foo.py:42: unused variable 'x' (60% confidence)"
_VULTURE_RE = re.compile(r"^(.+?):(\d+):\s+(unused\s.+?)\s+\((\d+)%\s+confidence\)\s*$")


def run_vulture(repo_root: str, graph: CodeGraph, spatial: dict) -> int:
    """
    Invoke  `python -m vulture <repo> --min-confidence 60`  and parse stdout.
    Returns the number of issues attached; 0 if vulture is unavailable.
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "vulture",
                repo_root,
                "--min-confidence",
                "60",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[flow] vulture skipped: {exc}")
        return 0

    count = 0
    for raw_line in result.stdout.splitlines():
        m = _VULTURE_RE.match(raw_line.strip())
        if not m:
            continue
        abs_path = os.path.abspath(m.group(1))
        line = int(m.group(2))
        message = m.group(3)
        conf = int(m.group(4))
        owner = locate(graph, spatial, abs_path, line)
        stamp_issue(
            graph,
            owner,
            {
                "tool": "vulture",
                "code": "unused",
                "message": f"{message} ({conf}% confidence)",
                "line": line,
                "severity": "warning",
            },
        )
        count += 1
    return count


# ─────────────────────────────────────────────────────────────────────────────
# BANDIT
# ─────────────────────────────────────────────────────────────────────────────


def _bandit_severity(level: str) -> str:
    level = (level or "").upper()
    if level == "HIGH":
        return "error"
    if level == "MEDIUM":
        return "warning"
    return "info"


def run_bandit(repo_root: str, graph: CodeGraph, spatial: dict) -> int:
    """
    Invoke  `python -m bandit -r <repo> -f json -q`  and stamp findings.
    Returns the number of issues attached; 0 if bandit is unavailable.
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "bandit",
                "-r",
                repo_root,
                "-f",
                "json",
                "-q",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[flow] bandit skipped: {exc}")
        return 0

    # bandit exits with code 1 when it finds issues — still parse the output
    raw = result.stdout.strip()
    if not raw:
        return 0

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    count = 0
    for finding in data.get("results", []):
        abs_path = os.path.abspath(finding.get("filename", ""))
        line = finding.get("line_number", 0)
        code = finding.get("test_id", "?")
        msg = finding.get("issue_text", "").strip()
        sev = finding.get("issue_severity", "LOW")
        owner = locate(graph, spatial, abs_path, line)
        stamp_issue(
            graph,
            owner,
            {
                "tool": "bandit",
                "code": code,
                "message": msg,
                "line": line,
                "severity": _bandit_severity(sev),
            },
        )
        count += 1
    return count


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────


def annotate_graph(graph: CodeGraph, repo_root: str) -> dict[str, int]:
    """
    Run all three static-analysis tools against `repo_root` and stamp issues
    onto the nodes of `graph`.

    Returns a summary dict  {"ruff": N, "vulture": N, "bandit": N}.
    Tools that are not installed or time out are silently skipped.
    """
    spatial = build_spatial_index(graph)
    summary = {
        "ruff": run_ruff(repo_root, graph, spatial),
        "vulture": run_vulture(repo_root, graph, spatial),
        "bandit": run_bandit(repo_root, graph, spatial),
    }
    total = sum(summary.values())
    print(
        f"[flow] annotated {total} issues  "
        f"(ruff={summary['ruff']}  vulture={summary['vulture']}  bandit={summary['bandit']})"
    )
    return summary
