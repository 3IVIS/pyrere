

# --- FILE: pyrere/__init__.py ---

"""
pyrere — Code Knowledge Graph builder.

Parses a Python repository with tree-sitter, constructs a typed graph of
modules, classes and functions, optionally enriches it with pyright / grimp /
pycg, and exports it to an interactive viewer.
"""

# pyrere/__init__.py
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pyrere")
except PackageNotFoundError:
    __version__ = "0.0.0"  # dev fallback

__all__ = ["annotate_graph", "build_graph", "enrich_graph"]

from pyrere.aggregator.builder import build_graph
from pyrere.enrichment import enrich_graph
from pyrere.flow import annotate_graph


# --- FILE: pyrere/_viewer/__init__.py ---



# --- FILE: pyrere/aggregator/__init__.py ---



# --- FILE: pyrere/aggregator/builder.py ---

import os

from pyrere.graph.models import CodeGraph, Edge, Node
from pyrere.ingestion.loader import load_python_files
from pyrere.parsing.parser import get_parser
from pyrere.symbols.extractor import ImportRef, extract_symbols, make_id

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def get_file_id(path: str) -> str:
    return make_id(os.path.abspath(path))


def build_module_index(repo_root: str) -> dict:
    """Return a mapping  dotted.module.name → absolute/path/to/file.py"""
    module_index: dict[str, str] = {}
    for file_path in load_python_files(repo_root):
        file_path = os.path.abspath(file_path)
        rel = os.path.relpath(file_path, repo_root)
        # removesuffix avoids corrupting paths like "deploy_python/foo.py"
        module_name = rel.replace(os.sep, ".").removesuffix(".py")
        # Strip ".__init__" to map packages to their directory name
        if module_name.endswith(".__init__"):
            module_name = module_name[: -len(".__init__")]
        module_index[module_name] = file_path
    return module_index


def _resolve_module_name(name: str, module_index: dict) -> str | None:
    """Exact match first, then safe suffix match."""
    if not name:
        return None
    if name in module_index:
        return module_index[name]
    for mod, path in module_index.items():
        if mod.endswith("." + name):
            return path
    return None


# ─────────────────────────────────────────────────────────────────────────────
# IMPORT RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────


def resolve_import_ref(
    imp: ImportRef,
    caller_file: str,
    repo_root: str,
    module_index: dict,
) -> list[str]:
    """
    Resolve an ImportRef to a list of absolute file paths that should receive
    an `imports` edge from caller_file.

    Handles:
      • absolute:        import foo.bar  /  from foo.bar import X
      • relative level 1: from . import X   /  from .utils import Y
      • relative level N: from .. import X  /  from ..pkg.mod import Y
      • __init__.py callers (level semantics shift by 1)
    """
    results: set[str] = set()

    if imp.level == 0:
        # ── absolute import ────────────────────────────────────────────────
        path = _resolve_module_name(imp.module, module_index)
        if path:
            results.add(path)
        # `from foo.bar import sub_module` — sub_module might itself be a file
        for name in imp.names:
            if name == "*":
                continue
            candidate = f"{imp.module}.{name}" if imp.module else name
            path = _resolve_module_name(candidate, module_index)
            if path:
                results.add(path)

    else:
        # ── relative import ────────────────────────────────────────────────
        rel = os.path.relpath(caller_file, repo_root)
        caller_module = rel.replace(os.sep, ".").removesuffix(".py")

        # Determine whether the caller is itself a package __init__.py.
        # For __init__.py: `from .` means THIS package, so level-1 is free.
        # For regular files: `from .` means the current package (strip the filename).
        is_init = caller_module.endswith(".__init__") or caller_module == "__init__"
        if is_init:
            caller_module = caller_module.removesuffix(".__init__").removesuffix("__init__")
            # __init__.py already represents the package, so each dot strips
            # one *fewer* component compared to a regular file.
            adjusted_level = imp.level - 1
        else:
            adjusted_level = imp.level

        parts = caller_module.split(".") if caller_module else []
        if adjusted_level > len(parts):
            return []  # can't go above the repo root
        base_parts = parts[:-adjusted_level] if adjusted_level > 0 else parts

        if imp.module:
            # from .utils import bar  OR  from ..graph.models import Foo
            target = ".".join([*base_parts, *imp.module.split(".")])
            path = _resolve_module_name(target, module_index)
            if path:
                results.add(path)
            # Also check if any imported name is a further sub-module
            for name in imp.names:
                if name == "*":
                    continue
                path = _resolve_module_name(f"{target}.{name}", module_index)
                if path:
                    results.add(path)
        else:
            # from . import foo, bar
            for name in imp.names:
                if name == "*":
                    # Wildcard: resolve the package __init__
                    pkg = ".".join(base_parts)
                    path = _resolve_module_name(pkg, module_index)
                    if path:
                        results.add(path)
                else:
                    # Try name as a sub-module of the base package
                    sub = ".".join([*base_parts, name])
                    path = _resolve_module_name(sub, module_index)
                    if path:
                        results.add(path)
            # Fallback: if no name resolved as a module, point at the package __init__
            if not results and base_parts:
                pkg = ".".join(base_parts)
                path = _resolve_module_name(pkg, module_index)
                if path:
                    results.add(path)

    return list(results)


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH BUILDER  —  two-pass so cross-file symbol resolution is possible
# ─────────────────────────────────────────────────────────────────────────────


def build_graph(repo_path: str) -> CodeGraph:
    parser = get_parser()
    graph = CodeGraph()
    repo_root = os.path.abspath(repo_path)
    module_index = build_module_index(repo_root)

    # Global symbol name → [node_id, …] for call / inherit / decorator / type resolution
    symbol_index: dict[str, list[str]] = {}

    # Per-file data deferred to the second pass
    # Tuple: (file_id, file_path, import_refs, call_refs, inherit_refs, decorator_refs, type_refs)
    deferred: list[tuple] = []

    # ── FIRST PASS: parse, build structural nodes/edges, collect all refs ─────
    for file_path in load_python_files(repo_root):
        file_path = os.path.abspath(file_path)

        try:
            with open(file_path, encoding="utf-8") as fh:
                code = fh.read()
        except (OSError, UnicodeDecodeError):
            continue

        tree = parser.parse(bytes(code, "utf-8"))
        file_id = get_file_id(file_path)

        graph.add_node(
            Node(
                id=file_id,
                name=os.path.basename(file_path),
                type="module",
                file=file_path,
                span=(0, 0),
                sources=["filesystem"],
            )
        )

        symbols, edges, import_refs, call_refs, inherit_refs, decorator_refs, type_refs = (
            extract_symbols(tree, code, file_path, file_id)
        )

        for n in symbols:
            graph.add_node(n)
            symbol_index.setdefault(n.name, []).append(n.id)

        for e in edges:
            graph.add_edge(e)

        deferred.append(
            (file_id, file_path, import_refs, call_refs, inherit_refs, decorator_refs, type_refs)
        )

    # ── SECOND PASS: resolve all cross-file relationships ────────────────────
    for (
        file_id,
        file_path,
        import_refs,
        call_refs,
        inherit_refs,
        decorator_refs,
        type_refs,
    ) in deferred:
        # ── imports → file-level edges ───────────────────────────────────────
        for imp in import_refs:
            resolved_paths = resolve_import_ref(imp, file_path, repo_root, module_index)

            for resolved in resolved_paths:
                target_id = get_file_id(resolved)
                if target_id not in graph.nodes:
                    graph.add_node(
                        Node(
                            id=target_id,
                            name=os.path.basename(resolved),
                            type="module",
                            file=resolved,
                            span=(0, 0),
                            sources=["resolver"],
                        )
                    )
                graph.add_edge(
                    Edge(
                        id=make_id(file_id, target_id, "imports"),
                        src=file_id,
                        dst=target_id,
                        type="imports",
                        confidence=0.95,
                        sources=["resolver"],
                    )
                )

            # imports_symbol: `from module import ClassName` → direct edge to the
            # class/function node so the viewer can show fine-grained dependencies
            for name in imp.names:
                if name == "*":
                    continue
                for sym_id in symbol_index.get(name, []):
                    sym_node = graph.nodes.get(sym_id)
                    if sym_node and sym_node.file in resolved_paths:
                        graph.add_edge(
                            Edge(
                                id=make_id(file_id, sym_id, "imports_symbol"),
                                src=file_id,
                                dst=sym_id,
                                type="imports_symbol",
                                confidence=0.95,
                                sources=["resolver"],
                            )
                        )

        # ── call refs → calls edges ──────────────────────────────────────────
        for caller_id, callee_name in call_refs:
            for callee_id in symbol_index.get(callee_name, []):
                graph.add_edge(
                    Edge(
                        id=make_id(caller_id, callee_id, "calls"),
                        src=caller_id,
                        dst=callee_id,
                        type="calls",
                        confidence=0.8,
                        sources=["tree_sitter"],
                    )
                )

        # ── inherit refs → inherits edges ────────────────────────────────────
        for class_id, base_name in inherit_refs:
            for base_id in symbol_index.get(base_name, []):
                node = graph.nodes.get(base_id)
                if node and node.type == "class":
                    graph.add_edge(
                        Edge(
                            id=make_id(class_id, base_id, "inherits"),
                            src=class_id,
                            dst=base_id,
                            type="inherits",
                            confidence=0.9,
                            sources=["tree_sitter"],
                        )
                    )

        # ── decorator refs → decorates edges ─────────────────────────────────
        # Edge direction: decorator → decorated  (reads "X decorates Y")
        for decorated_id, dec_name in decorator_refs:
            for dec_id in symbol_index.get(dec_name, []):
                graph.add_edge(
                    Edge(
                        id=make_id(dec_id, decorated_id, "decorates"),
                        src=dec_id,
                        dst=decorated_id,
                        type="decorates",
                        confidence=0.9,
                        sources=["tree_sitter"],
                    )
                )

        # ── type refs → uses_type edges ───────────────────────────────────────
        for user_id, type_name in type_refs:
            for type_id in symbol_index.get(type_name, []):
                node = graph.nodes.get(type_id)
                if node and node.type == "class":
                    graph.add_edge(
                        Edge(
                            id=make_id(user_id, type_id, "uses_type"),
                            src=user_id,
                            dst=type_id,
                            type="uses_type",
                            confidence=0.85,
                            sources=["tree_sitter"],
                        )
                    )

    return graph


# --- FILE: pyrere/context/__init__.py ---

"""
pyrere/context/__init__.py
──────────────────────────
Context-window / prompt-assembly layer — not yet implemented.

This package is reserved for a future release.  Accessing any attribute will
raise NotImplementedError with a clear message.  The error is deferred to
attribute access (via __getattr__) rather than raised at import time, so that
``import pyrere`` and ``import pyrere.context`` both succeed without crashing
— only actually *using* something from this package will raise.
"""

from __future__ import annotations


def __getattr__(name: str) -> object:
    raise NotImplementedError(
        f"pyrere.context.{name} is not yet implemented. "
        "pyrere.context will be available in a future release."
    )


# --- FILE: pyrere/enrichment/__init__.py ---

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


# --- FILE: pyrere/enrichment/grimp_.py ---

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
        import grimp  # type: ignore
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


# --- FILE: pyrere/enrichment/pycg_.py ---

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


# --- FILE: pyrere/enrichment/pyright.py ---

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


# --- FILE: pyrere/flow/__init__.py ---

from pyrere.flow.analyzer import annotate_graph

__all__ = ["annotate_graph"]


# --- FILE: pyrere/flow/analyzer.py ---

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


# --- FILE: pyrere/graph/__init__.py ---



# --- FILE: pyrere/graph/models.py ---

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Node:
    id: str
    name: str
    type: str
    # Optional because external/resolver-created nodes may not map to a real
    # file on disk, and several code paths explicitly check ``if node.file``.
    file: str | None
    span: tuple[int, int]
    signature: dict | None = None
    metadata: dict = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    type: str
    confidence: float = 1.0
    sources: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


@dataclass
class CodeGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)

    def add_node(self, node: Node) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        self.edges[edge.id] = edge


# --- FILE: pyrere/ingestion/__init__.py ---



# --- FILE: pyrere/ingestion/loader.py ---

import os

# Directories that are never source code and should never be walked.
# Pruned in-place so os.walk doesn't descend into them.
_SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".ruff_cache",
    ".pytype",
    ".pytest_cache",
    ".hypothesis",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".env",
    "build",
    "dist",
    ".eggs",
    "buck-out",
    ".direnv",
}


def _should_skip(dirname: str) -> bool:
    """Return True for directories that are definitely not user source code."""
    return dirname in _SKIP_DIRS or dirname.endswith(".egg-info")


def load_python_files(repo_path: str):
    """
    Yield absolute paths of every .py file under repo_path, skipping
    virtual-env, cache, build, and VCS directories.
    """
    for root, dirs, files in os.walk(repo_path):
        # Prune dirs in-place; os.walk respects this and won't descend into them
        dirs[:] = [d for d in dirs if not _should_skip(d)]

        for file in files:
            if file.endswith(".py"):
                yield os.path.join(root, file)


# --- FILE: pyrere/llm/__init__.py ---

"""
pyrere/llm/__init__.py
──────────────────────
LLM-assisted refactoring layer — not yet implemented.

This package is reserved for a future release.  Accessing any attribute will
raise NotImplementedError with a clear message.  The error is deferred to
attribute access (via __getattr__) rather than raised at import time, so that
``import pyrere`` and ``import pyrere.llm`` both succeed without crashing —
only actually *using* something from this package will raise.
"""

from __future__ import annotations


def __getattr__(name: str) -> object:
    raise NotImplementedError(
        f"pyrere.llm.{name} is not yet implemented. "
        "pyrere.llm will be available in a future release."
    )


# --- FILE: pyrere/parsing/__init__.py ---



# --- FILE: pyrere/parsing/parser.py ---

from tree_sitter import Parser

# ── Language loading ──────────────────────────────────────────────────────────
# Supports both the legacy tree-sitter-languages bundle (tree-sitter < 0.22)
# and the modern per-language packages (tree-sitter >= 0.22).
try:
    from tree_sitter_languages import get_language  # type: ignore

    PY_LANGUAGE = get_language("python")
    _LEGACY_API = True
except ImportError:
    import tree_sitter_python as tspython  # type: ignore
    from tree_sitter import Language  # type: ignore

    PY_LANGUAGE = Language(tspython.language())
    _LEGACY_API = False


def get_parser() -> Parser:
    if _LEGACY_API:
        # tree-sitter < 0.22: construct Parser then call set_language()
        parser = Parser()
        parser.set_language(PY_LANGUAGE)
    else:
        # tree-sitter >= 0.22: language is passed directly to the constructor
        parser = Parser(PY_LANGUAGE)
    return parser


# --- FILE: pyrere/relationships/__init__.py ---



# --- FILE: pyrere/symbols/__init__.py ---



# --- FILE: pyrere/symbols/extractor.py ---

import hashlib
import re
from typing import NamedTuple

from pyrere.graph.models import Edge, Node

# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────────────────────


def make_id(*parts) -> str:
    # FIX: added usedforsecurity=False so this doesn't raise ValueError on
    # FIPS-mode Linux (common in enterprise/cloud environments).  Bandit B324
    # also flags the previous form.  MD5 is used here purely as a fast
    # deterministic hash for node IDs, not for any security purpose.
    return hashlib.md5(":".join(map(str, parts)).encode(), usedforsecurity=False).hexdigest()


def _text(code_bytes: bytes, node) -> str:
    """Extract UTF-8 text via tree-sitter *byte* offsets (never char offsets)."""
    return code_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _collect_type_names(code_bytes: bytes, node, cache: dict) -> list[str]:
    """
    Recursively collect every identifier inside a type annotation subtree.
    Handles simple names, generics (Optional[X]), unions (X | Y), attributes, etc.
    Built-in names (int, str, …) silently fail to resolve in builder — fine.
    """
    if node is None:
        return []
    names: list[str] = []
    if node.type == "identifier":
        names.append(_text(code_bytes, node))
    for child in node.children:
        names.extend(_collect_type_names(code_bytes, child, cache))
    return names


# ─────────────────────────────────────────────────────────────────────────────
# IMPORT DATA STRUCTURE
# ─────────────────────────────────────────────────────────────────────────────


class ImportRef(NamedTuple):
    """Carries everything needed to fully resolve one import statement."""

    level: int  # 0 = absolute; 1 = from .; 2 = from ..; …
    module: str  # dotted module string after the dots (may be "")
    names: list[str]  # specific symbols imported; ["*"] = wildcard; [] = bare import


# ─────────────────────────────────────────────────────────────────────────────
# METADATA HELPERS  (adopted from external extractor, adapted to our AST model)
# ─────────────────────────────────────────────────────────────────────────────


def _extract_docstring(code_bytes: bytes, body_node, text_cache: dict) -> str:
    """
    Extract the docstring from a function/class body block.
    Looks for the first expression_statement > string child in the block.
    Uses tree-sitter node positions — no line scanning needed.
    """
    if body_node is None or body_node.type != "block":
        return ""
    for child in body_node.children:
        if child.type == "expression_statement" and child.children:
            first = child.children[0]
            if first.type == "string":
                raw = _cached_text(code_bytes, first, text_cache)
                # Strip surrounding triple- or single-quotes
                for q in ('"""', "'''", '"', "'"):
                    if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
                        return raw[len(q) : -len(q)].strip()
                return raw.strip()
        break  # docstring must be the very first statement
    return ""


def _extract_parameters(code_bytes: bytes, params_node, text_cache: dict) -> list[str]:
    """
    Extract parameter names/annotations from a `parameters` or
    `lambda_parameters` node.  Includes *args and **kwargs.
    """
    if params_node is None:
        return []
    params: list[str] = []
    skip = {"(", ")", ","}
    for child in params_node.children:
        if child.type in skip:
            continue
        t = child.type
        if (
            t == "identifier"
            or t in ("typed_parameter", "typed_default_parameter", "default_parameter")
            or t == "list_splat_pattern"
            or t == "dictionary_splat_pattern"
        ):
            params.append(_cached_text(code_bytes, child, text_cache))
    return params


# FIX: tree-sitter node types that create a new branch (i.e. raise cyclomatic
# complexity by 1 each).  Used by _cyclomatic_complexity below.
_BRANCH_NODE_TYPES = frozenset(
    {
        "if_statement",
        "elif_clause",
        "for_statement",
        "while_statement",
        "except_clause",
        "with_statement",
        "match_statement",
        "case_clause",
        "boolean_operator",  # `and` / `or`
        "conditional_expression",  # ternary  a if cond else b
    }
)


def _cyclomatic_complexity(body_node) -> int:
    """
    Approximate cyclomatic complexity by counting branch-creating AST nodes
    inside the function/class body.

    FIX: the previous implementation ran regex over raw source text, which
    caused keywords inside docstrings, f-strings, and comments to inflate the
    score (e.g. a docstring containing "if you call this…" would add +1).
    This version walks tree-sitter nodes directly so only real code branches
    are counted.

    Complexity starts at 1 (the straight-line path) and increments once per
    node type in _BRANCH_NODE_TYPES.
    """
    if body_node is None:
        return 1
    complexity = 1
    stack = list(body_node.children)
    while stack:
        node = stack.pop()
        if node.type in _BRANCH_NODE_TYPES:
            complexity += 1
        stack.extend(node.children)
    return complexity


def _cached_text(code_bytes: bytes, node, cache: dict) -> str:
    """
    Return node text, caching by (start_byte, end_byte) so repeated visits
    of the same byte range don't re-decode.
    Adopted from external extractor's position-keyed _node_text_cache.
    """
    key = (node.start_byte, node.end_byte)
    if key not in cache:
        cache[key] = _text(code_bytes, node)
    return cache[key]


def _decorator_name(dc, code_bytes: bytes, text_cache: dict) -> str | None:
    """Extract the short callable name from a decorator expression child node."""
    if dc.type == "identifier":
        return _cached_text(code_bytes, dc, text_cache)
    if dc.type == "attribute":
        attr = dc.child_by_field_name("attribute")
        return _cached_text(code_bytes, attr, text_cache) if attr else None
    if dc.type == "call":
        fn = dc.child_by_field_name("function")
        return _decorator_name(fn, code_bytes, text_cache) if fn else None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ITERATIVE TRAVERSAL  (replaces recursive process_node)
# ─────────────────────────────────────────────────────────────────────────────
#
# Adopted from external extractor's _traverse_and_extract_iterative.
# Key improvements over our old recursive approach:
#   1. No Python recursion-limit crashes on large/deeply nested files.
#   2. Early-exit for nodes that can't contain any of our targets.
#   3. _processed_nodes set prevents duplicate extraction of the same node.
#
# Stack items: (node, scope_id, pending_decorator_names)
# pending_decorator_names is non-empty only when we're processing the inner
# function_definition / class_definition that sits inside a decorated_definition.

# Node types we must recurse into to find definitions / relationships.
# Anything NOT in this set and NOT a target is pruned entirely — its children
# are never pushed onto the stack.
_CONTAINER_TYPES = {
    "module",
    "block",
    "decorated_definition",
    "function_definition",
    "class_definition",
    "if_statement",
    "elif_clause",
    "else_clause",
    "for_statement",
    "while_statement",
    "with_statement",
    "try_statement",
    "except_clause",
    "raise_statement",
    "expression_statement",
    "assignment",
    "return_statement",
    "yield",
    "call",
    "argument_list",
    "parameters",
    "lambda",
}

# Target node types we want to act on (vs. just traverse through).
_TARGET_TYPES = {
    "function_definition",
    "class_definition",
    "lambda",
    "decorated_definition",
    "import_statement",
    "import_from_statement",
    "call",
    "typed_parameter",
    "typed_default_parameter",
    "assignment",
    "except_clause",
    "raise_statement",
}


# ─────────────────────────────────────────────────────────────────────────────
# MAIN EXTRACTOR
# ─────────────────────────────────────────────────────────────────────────────


def extract_symbols(
    tree,
    code: str,
    file_path: str,
    file_id: str,
) -> tuple:
    """
    Walk the AST iteratively and return:
      nodes          - function / class / variable Node objects
      edges          - contains edges (intra-file structural edges)
      import_refs    - ImportRef list (absolute + relative)
      call_refs      - list of (caller_id, callee_name)
      inherit_refs   - list of (class_id, base_name)
      decorator_refs - list of (decorated_id, decorator_name)
      type_refs      - list of (user_id, type_name)
    """
    nodes: list = []
    edges: list = []
    import_refs: list[ImportRef] = []
    call_refs: list[tuple] = []
    inherit_refs: list[tuple] = []
    decorator_refs: list[tuple] = []
    type_refs: list[tuple] = []

    code_bytes = code.encode("utf-8")

    # ── per-call caches (adopted from external extractor) ─────────────────────
    text_cache: dict = {}  # (start_byte, end_byte) → str
    processed: set = set()  # node ids already handled (avoids duplicates)

    # ── iterative stack ───────────────────────────────────────────────────────
    # Each entry: (node, scope_id, pending_dec_names)
    # pending_dec_names — decorator names collected from an enclosing
    # decorated_definition, to be attached once the inner def/class is emitted.
    stack = [(tree.root_node, file_id, [])]

    while stack:
        node, scope_id, pending_decs = stack.pop()
        ntype = node.type

        # ── early-exit for uninteresting nodes ────────────────────────────────
        if (
            ntype not in _TARGET_TYPES
            and ntype not in _CONTAINER_TYPES
            and node is not tree.root_node
        ):
            continue

        nid = id(node)  # CPython object identity — unique per live node

        # ── @decorator … def/class … ─────────────────────────────────────────
        if ntype == "decorated_definition":
            dec_names: list[str] = []
            inner = node.child_by_field_name("definition")
            for child in node.children:
                if child.type == "decorator":
                    for dc in child.children:
                        name = _decorator_name(dc, code_bytes, text_cache)
                        if name:
                            dec_names.append(name)
            if inner:
                stack.append((inner, scope_id, dec_names))
            continue  # no other children need processing

        # ── function_definition / class_definition / lambda ───────────────────
        if ntype in ("function_definition", "class_definition", "lambda"):
            if nid in processed:
                continue

            if ntype == "lambda":
                # ── lambda ────────────────────────────────────────────────────
                params_node = node.child_by_field_name("parameters")  # lambda_parameters
                params = _extract_parameters(code_bytes, params_node, text_cache)
                body_node = node.child_by_field_name("body")
                node_id = make_id(file_path, "<lambda>", node.start_point)

                nodes.append(
                    Node(
                        id=node_id,
                        name="<lambda>",
                        type="function",
                        file=file_path,
                        span=(node.start_point[0], node.end_point[0]),
                        metadata={
                            "is_lambda": True,
                            "is_async": False,
                            "is_generator": False,
                            "visibility": "private",
                            "parameters": params,
                            "return_type": None,
                            "docstring": "",
                            "complexity": 1,
                        },
                        sources=["tree_sitter"],
                    )
                )
                edges.append(
                    Edge(
                        id=make_id(scope_id, node_id, "contains"),
                        src=scope_id,
                        dst=node_id,
                        type="contains",
                        confidence=1.0,
                        sources=["tree_sitter"],
                    )
                )
                processed.add(nid)
                # Recurse into body for calls/type refs
                if body_node:
                    stack.append((body_node, node_id, []))
                continue

            # ── function_definition ───────────────────────────────────────────
            if ntype == "function_definition":
                name_node = node.child_by_field_name("name")
                if not name_node:
                    for child in node.children:
                        stack.append((child, scope_id, []))
                    processed.add(nid)
                    continue

                name = _cached_text(code_bytes, name_node, text_cache)
                node_id = make_id(file_path, name, node.start_point)
                body = node.child_by_field_name("body")
                params_n = node.child_by_field_name("parameters")

                # is_async: `async def` has an [async] child before [def]
                is_async = any(c.type == "async" for c in node.children)

                # is_generator: any `yield` node in body text
                body_text = _cached_text(code_bytes, body, text_cache) if body else ""
                is_generator = bool(re.search(r"\byield\b", body_text))

                # visibility from name convention
                if name.startswith("__") and name.endswith("__"):
                    visibility = "magic"
                elif name.startswith("_"):
                    visibility = "private"
                else:
                    visibility = "public"

                # is_static / is_classmethod / is_property from pending decorators
                is_static = "staticmethod" in pending_decs
                is_classmethod = "classmethod" in pending_decs
                is_property = "property" in pending_decs

                params = _extract_parameters(code_bytes, params_n, text_cache)

                ret_node = node.child_by_field_name("return_type")
                return_type = (
                    _cached_text(code_bytes, ret_node, text_cache).lstrip("->").strip()
                    if ret_node
                    else None
                )

                docstring = _extract_docstring(code_bytes, body, text_cache)
                # FIX: pass only body (tree-sitter node), not raw text/cache.
                # _cyclomatic_complexity now walks the AST so strings and
                # comments no longer inflate the score.
                complexity = _cyclomatic_complexity(body)

                nodes.append(
                    Node(
                        id=node_id,
                        name=name,
                        type="function",
                        file=file_path,
                        span=(node.start_point[0], node.end_point[0]),
                        metadata={
                            "is_lambda": False,
                            "is_async": is_async,
                            "is_generator": is_generator,
                            "is_static": is_static,
                            "is_classmethod": is_classmethod,
                            "is_property": is_property,
                            "visibility": visibility,
                            "parameters": params,
                            "return_type": return_type,
                            "docstring": docstring,
                            "complexity": complexity,
                        },
                        sources=["tree_sitter"],
                    )
                )
                edges.append(
                    Edge(
                        id=make_id(scope_id, node_id, "contains"),
                        src=scope_id,
                        dst=node_id,
                        type="contains",
                        confidence=1.0,
                        sources=["tree_sitter"],
                    )
                )

                # return-type refs
                if ret_node:
                    for tname in _collect_type_names(code_bytes, ret_node, text_cache):
                        type_refs.append((node_id, tname))

                # decorator refs
                for dn in pending_decs:
                    decorator_refs.append((node_id, dn))

                processed.add(nid)
                if body:
                    stack.append((body, node_id, []))
                if params_n:
                    stack.append((params_n, node_id, []))
                continue

            # ── class_definition ──────────────────────────────────────────────
            if ntype == "class_definition":
                name_node = node.child_by_field_name("name")
                if not name_node:
                    for child in node.children:
                        stack.append((child, scope_id, []))
                    processed.add(nid)
                    continue

                name = _cached_text(code_bytes, name_node, text_cache)
                node_id = make_id(file_path, name, node.start_point)
                body = node.child_by_field_name("body")

                # superclasses
                super_names: list[str] = []
                supers = node.child_by_field_name("superclasses")
                if supers:
                    for base in supers.children:
                        if base.type == "identifier":
                            super_names.append(_cached_text(code_bytes, base, text_cache))
                        elif base.type == "attribute":
                            attr = base.child_by_field_name("attribute")
                            if attr:
                                super_names.append(_cached_text(code_bytes, attr, text_cache))
                        elif base.type == "dotted_name" and base.children:
                            last = base.children[-1]
                            if last.type == "identifier":
                                super_names.append(_cached_text(code_bytes, last, text_cache))

                body_raw = _cached_text(code_bytes, body, text_cache) if body else ""
                docstring = _extract_docstring(code_bytes, body, text_cache)
                # FIX: same as function_definition — use AST-based counter.
                complexity = _cyclomatic_complexity(body)
                is_dataclass = "dataclass" in pending_decs
                is_abstract = (
                    "ABC" in super_names or "ABCMeta" in super_names or "abstractmethod" in body_raw
                )
                is_exception = any("Exception" in s or "Error" in s for s in super_names)

                nodes.append(
                    Node(
                        id=node_id,
                        name=name,
                        type="class",
                        file=file_path,
                        span=(node.start_point[0], node.end_point[0]),
                        metadata={
                            "superclasses": super_names,
                            "is_dataclass": is_dataclass,
                            "is_abstract": is_abstract,
                            "is_exception": is_exception,
                            "docstring": docstring,
                            "complexity": complexity,
                        },
                        sources=["tree_sitter"],
                    )
                )
                edges.append(
                    Edge(
                        id=make_id(scope_id, node_id, "contains"),
                        src=scope_id,
                        dst=node_id,
                        type="contains",
                        confidence=1.0,
                        sources=["tree_sitter"],
                    )
                )

                # inherit refs
                for sn in super_names:
                    inherit_refs.append((node_id, sn))

                # decorator refs
                for dn in pending_decs:
                    decorator_refs.append((node_id, dn))

                # class-level variable nodes (class attributes)
                if body:
                    _extract_class_attributes(
                        code_bytes, body, node_id, file_path, nodes, edges, text_cache
                    )

                processed.add(nid)
                if body:
                    stack.append((body, node_id, []))
                continue

        # ── typed parameter: def foo(x: MyType [= default]) ──────────────────
        if ntype in ("typed_parameter", "typed_default_parameter"):
            type_node = node.child_by_field_name("type")
            if type_node:
                for tname in _collect_type_names(code_bytes, type_node, text_cache):
                    type_refs.append((scope_id, tname))
            # No further recursion needed
            continue

        # ── annotated assignment: x: SomeType [= value] ───────────────────────
        if ntype == "assignment":
            type_node = node.child_by_field_name("type")
            if type_node:
                for tname in _collect_type_names(code_bytes, type_node, text_cache):
                    type_refs.append((scope_id, tname))
            # Still push children for nested calls inside the RHS
            for child in node.children:
                if child.type not in _CONTAINER_TYPES and child.type not in _TARGET_TYPES:
                    continue
                stack.append((child, scope_id, []))
            continue

        # ── except clause ──────────────────────────────────────────────────────
        if ntype == "except_clause":
            for child in node.children:
                if child.type in ("except", ":", "block"):
                    if child.type == "block":
                        stack.append((child, scope_id, []))
                    continue
                exc_type = child
                if child.type == "as_pattern":
                    exc_type = child.children[0] if child.children else None
                if exc_type is None:
                    break
                if exc_type.type == "identifier":
                    type_refs.append((scope_id, _cached_text(code_bytes, exc_type, text_cache)))
                elif exc_type.type in ("tuple", "parenthesized_expression"):
                    for cc in exc_type.children:
                        if cc.type == "identifier":
                            type_refs.append((scope_id, _cached_text(code_bytes, cc, text_cache)))
                break
            continue

        # ── raise statement ────────────────────────────────────────────────────
        if ntype == "raise_statement":
            for child in node.children:
                if child.type == "identifier":
                    type_refs.append((scope_id, _cached_text(code_bytes, child, text_cache)))
                else:
                    stack.append((child, scope_id, []))
            continue

        # ── call expression ────────────────────────────────────────────────────
        if ntype == "call":
            fn = node.child_by_field_name("function")
            if fn:
                if fn.type == "identifier":
                    call_refs.append((scope_id, _cached_text(code_bytes, fn, text_cache)))
                elif fn.type == "attribute":
                    attr = fn.child_by_field_name("attribute")
                    if attr:
                        call_refs.append((scope_id, _cached_text(code_bytes, attr, text_cache)))
            for child in node.children:
                stack.append((child, scope_id, []))
            continue

        # ── import foo.bar [as alias] ──────────────────────────────────────────
        if ntype == "import_statement":
            for name_node in node.children_by_field_name("name"):
                if name_node.type == "dotted_name":
                    import_refs.append(
                        ImportRef(
                            level=0,
                            module=_cached_text(code_bytes, name_node, text_cache),
                            names=[],
                        )
                    )
                elif name_node.type == "aliased_import":
                    n = name_node.child_by_field_name("name")
                    if n:
                        import_refs.append(
                            ImportRef(
                                level=0,
                                module=_cached_text(code_bytes, n, text_cache),
                                names=[],
                            )
                        )
            continue

        # ── from [..][module] import name1, name2, … ──────────────────────────
        if ntype == "import_from_statement":
            level = 0
            mod_str = ""
            mod_field = node.child_by_field_name("module_name")
            if mod_field is not None:
                if mod_field.type == "relative_import":
                    for child in mod_field.children:
                        if child.type == "import_prefix":
                            level = len(_cached_text(code_bytes, child, text_cache))
                        elif child.type == "dotted_name":
                            mod_str = _cached_text(code_bytes, child, text_cache)
                else:
                    mod_str = _cached_text(code_bytes, mod_field, text_cache)
            imported_names: list[str] = []
            for name_node in node.children_by_field_name("name"):
                if name_node.type == "wildcard_import":
                    imported_names.append("*")
                elif name_node.type == "dotted_name":
                    imported_names.append(_cached_text(code_bytes, name_node, text_cache))
                elif name_node.type == "aliased_import":
                    n = name_node.child_by_field_name("name")
                    if n:
                        imported_names.append(_cached_text(code_bytes, n, text_cache))
            import_refs.append(ImportRef(level=level, module=mod_str, names=imported_names))
            continue

        # ── default: push all children ─────────────────────────────────────────
        for child in reversed(node.children):
            stack.append((child, scope_id, []))

    return nodes, edges, import_refs, call_refs, inherit_refs, decorator_refs, type_refs


# ─────────────────────────────────────────────────────────────────────────────
# CLASS ATTRIBUTE EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────
# Adopted from external extractor's _extract_class_attributes.
# Emits "variable" Node objects for class-level assignments.


def _extract_class_attributes(
    code_bytes: bytes,
    body_node,
    class_node_id: str,
    file_path: str,
    nodes: list,
    edges: list,
    text_cache: dict,
) -> None:
    """
    Walk a class body block and emit a 'variable' Node + 'contains' Edge for
    every direct assignment (both plain `x = v` and annotated `x: T = v`).
    Only direct children of the block are considered — method-local variables
    are intentionally excluded.
    """
    for child in body_node.children:
        assignment = None
        if child.type == "expression_statement" and child.children:
            first = child.children[0]
            if first.type == "assignment":
                assignment = first
        elif child.type == "assignment":
            assignment = child

        if assignment is None:
            continue

        # Extract the variable name from the left-hand side
        left = assignment.child_by_field_name("left")
        if left is None:
            continue

        # Skip tuple unpacking and subscript assignments
        if left.type not in ("identifier", "attribute"):
            continue

        # For attribute assignments (self.x), take the attribute part
        if left.type == "attribute":
            attr = left.child_by_field_name("attribute")
            var_name = _cached_text(code_bytes, attr, text_cache) if attr else None
        else:
            var_name = _cached_text(code_bytes, left, text_cache)

        if not var_name:
            continue

        # Optional type annotation
        type_node = assignment.child_by_field_name("type")
        type_str = _cached_text(code_bytes, type_node, text_cache).strip() if type_node else None

        var_id = make_id(file_path, var_name, assignment.start_point)
        nodes.append(
            Node(
                id=var_id,
                name=var_name,
                type="variable",
                file=file_path,
                span=(assignment.start_point[0], assignment.end_point[0]),
                metadata={"annotation": type_str},
                sources=["tree_sitter"],
            )
        )
        edges.append(
            Edge(
                id=make_id(class_node_id, var_id, "contains"),
                src=class_node_id,
                dst=var_id,
                type="contains",
                confidence=1.0,
                sources=["tree_sitter"],
            )
        )


# --- FILE: pyrere/utils/__init__.py ---



# --- FILE: pyrere/utils/spatial.py ---

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


# --- FILE: pyrere_scripts/__init__.py ---



# --- FILE: pyrere_scripts/run.py ---

"""
pyrere_scripts/run.py
──────────────────────────────
CLI entry point for pyrere.

Usage:
    pyrere [REPO_PATH] [--port PORT]   # analyse REPO_PATH (default: current dir)
    python -m pyrere_scripts.run PATH  # equivalent direct invocation
"""

from __future__ import annotations

import functools
import importlib.resources
import json
import os
import shutil
import sys
import tempfile
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler

from pyrere.aggregator.builder import build_graph
from pyrere.enrichment import enrich_graph
from pyrere.flow import annotate_graph

DEFAULT_PORT = 8000


# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────────────


def _viewer_dir() -> str:
    """
    Return the absolute path to the _viewer/ directory whether the package is
    installed (pip install) or run from source.
    """
    # importlib.resources works for installed packages; fall back to __file__
    # for editable installs and source runs.
    try:
        # Python 3.9+: files() returns a Traversable rooted at the package.
        ref = importlib.resources.files("pyrere") / "_viewer"
        if ref.is_dir():
            return str(ref)
    except (TypeError, AttributeError):
        pass

    # FIX: was "viewer" (missing leading underscore) — editable installs always
    # hit this branch and would raise FileNotFoundError immediately.
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(os.path.dirname(here), "_viewer")
    if os.path.isdir(candidate):
        return candidate

    raise FileNotFoundError(
        "Cannot locate the pyrere viewer directory. "
        "Re-install the package or run from the repo root."
    )


def get_user_data_dir() -> str:
    """
    Return (and create if necessary) the OS-appropriate user data directory
    for pyrere:

      macOS   ~/Library/Application Support/pyrere/
      Windows %APPDATA%\\pyrere\\          (falls back to ~/AppData/Roaming/pyrere/)
      Linux   $XDG_DATA_HOME/pyrere/       (falls back to ~/.local/share/pyrere/)
    """
    system = sys.platform

    if system == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    elif system == "win32":
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming"
        )
    else:
        # Linux / BSD / other POSIX
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share"
        )

    data_dir = os.path.join(base, "pyrere")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


# ─────────────────────────────────────────────────────────────────────────────
# EXPORT
# ─────────────────────────────────────────────────────────────────────────────


def make_relative(path: str | None, repo_root: str) -> str:
    if not path:
        return "__external__"
    return os.path.relpath(path, repo_root).replace("\\", "/")


def export_graph(graph, repo_root: str) -> str:
    """
    Serialise *graph* to JSON and write it to the OS user data directory.

    The filename matches the analysed repository folder name so multiple repos
    can be stored side-by-side without collisions, e.g.:

      ~/.local/share/pyrere/myproject.json
      %APPDATA%\\pyrere\\myproject.json
      ~/Library/Application Support/pyrere/myproject.json

    Returns the absolute path of the written file.
    """
    data = {
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "type": n.type,
                "file": make_relative(n.file, repo_root),
                "metadata": n.metadata,
            }
            for n in graph.nodes.values()
        ],
        "edges": [
            {
                "source": e.src,
                "target": e.dst,
                "type": e.type,
                "confidence": round(e.confidence, 4),
                "sources": e.sources,
            }
            for e in graph.edges.values()
        ],
        "repo_root": repo_root,
    }

    repo_name = os.path.basename(os.path.normpath(repo_root)) or "pyrere_graph"
    filename = f"{repo_name}.json"
    out_path = os.path.join(get_user_data_dir(), filename)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# SERVER
# ─────────────────────────────────────────────────────────────────────────────


def _prepare_serve_dir(viewer_dir: str, graph_json_path: str) -> str:
    """
    Create a temporary directory that contains:
      • all static viewer assets (HTML, JS, CSS …) copied from *viewer_dir*
      • *graph_json_path* copied in as ``graph.json``

    Serving from a temp directory means we never write to the (potentially
    read-only) installed package directory.  The caller is responsible for
    cleaning up the directory when the server exits.
    """
    tmp = tempfile.mkdtemp(prefix="pyrere_serve_")

    # Copy every file in viewer_dir (non-recursive; _viewer/ is flat)
    for entry in os.scandir(viewer_dir):
        if entry.is_file():
            shutil.copy2(entry.path, tmp)

    # Place the graph data where the viewer HTML expects it
    shutil.copy2(graph_json_path, os.path.join(tmp, "graph.json"))

    return tmp


def start_server(serve_dir: str, port: int, ready: threading.Event) -> None:
    """
    Start an HTTP server on *port* rooted at *serve_dir*.

    FIX: the previous implementation called os.chdir(serve_dir), which
    permanently changed the working directory of the *entire process* (os.chdir
    is process-wide, not thread-local).  This broke any downstream code that
    relied on relative paths after pyrere returned.

    The fix uses the ``directory`` keyword argument of SimpleHTTPRequestHandler
    (available since Python 3.7) so the server is rooted at *serve_dir* without
    touching the process CWD.

    Sets *ready* once the socket is bound so that the caller can open the
    browser only after the server is actually listening.
    """
    handler = functools.partial(SimpleHTTPRequestHandler, directory=serve_dir)
    try:
        server = HTTPServer(("localhost", port), handler)
    except OSError as exc:
        print(
            f"\n[pyrere] Could not bind to port {port}: {exc}\n"
            f"         Free the port or pick another one:\n"
            f"           pyrere --port {port + 1} .\n"
        )
        ready.set()  # unblock the main thread even on failure
        return
    print(f"Serving at http://localhost:{port}")
    ready.set()  # signal that the socket is bound and ready
    server.serve_forever()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────


def _parse_args(args: list[str]) -> tuple[str | None, int]:
    """
    Minimal arg parser for:
        pyrere [REPO_PATH] [--port PORT | -p PORT]

    Returns (repo_path_or_None, port).
    """
    port: int = DEFAULT_PORT
    repo_path: str | None = None
    i = 0
    while i < len(args):
        if args[i] in ("--port", "-p") and i + 1 < len(args):
            try:
                port = int(args[i + 1])
            except ValueError:
                print(f"[pyrere] Invalid port value: {args[i + 1]!r}")
                sys.exit(1)
            i += 2
        elif args[i].startswith("--port="):
            try:
                port = int(args[i].split("=", 1)[1])
            except ValueError:
                print(f"[pyrere] Invalid port value: {args[i]!r}")
                sys.exit(1)
            i += 1
        else:
            repo_path = args[i]
            i += 1
    return repo_path, port


def main(argv: list[str] | None = None) -> None:
    raw_args = argv if argv is not None else sys.argv[1:]
    repo_path_arg, port = _parse_args(raw_args)
    repo_path = os.path.abspath(repo_path_arg if repo_path_arg else ".")
    viewer_dir = _viewer_dir()

    print("[1/4] Building code knowledge graph …")
    graph = build_graph(repo_path)
    print(f"      {len(graph.nodes)} nodes  {len(graph.edges)} edges")

    print("[2/4] Enriching graph (pyright / grimp / pycg) …")
    enrich_graph(graph, repo_path)
    print(f"      {len(graph.nodes)} nodes  {len(graph.edges)} edges  (after enrichment)")

    print("[3/4] Running static-analysis tools (ruff / vulture / bandit) …")
    annotate_graph(graph, repo_path)

    print("[4/4] Exporting graph + launching viewer …")
    graph_json_path = export_graph(graph, repo_path)
    print(f"      Graph saved to {graph_json_path}")

    serve_dir = _prepare_serve_dir(viewer_dir, graph_json_path)
    ready = threading.Event()
    try:
        thread = threading.Thread(target=start_server, args=(serve_dir, port, ready), daemon=True)
        thread.start()
        ready.wait()  # wait until the socket is actually bound
        webbrowser.open(f"http://localhost:{port}")
        thread.join()
    finally:
        shutil.rmtree(serve_dir, ignore_errors=True)


if __name__ == "__main__":
    main()


# --- FILE: pyrere/_viewer/index.html ---

<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CKG Viewer</title>
<!--
  vis-network is loaded from the unpkg CDN at a pinned version (9.1.9).
  Pinning prevents silent breakage if a future major release changes the API.

  If you need offline support, download the file and place it alongside this
  HTML:
    curl -Lo _viewer/vis-network.min.js \
      https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js

  Then change the src below to: <script src="vis-network.min.js">
-->
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
/* ── reset ─────────────────────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 13px;
  color: #1e293b;
  background: #f1f5f9;
  height: 100vh;
  overflow: hidden;
}

/* ── layout ──────────────────────────────────────────────────────────────── */
#layout { display: flex; height: 100vh; }

#sidebar {
  width: 260px;
  min-width: 180px;
  max-width: 560px;
  display: flex;
  flex-direction: column;
  background: #ffffff;
  border-right: 1px solid #e2e8f0;
  flex-shrink: 0;
  overflow: hidden;
}

/* app name strip at the very top */
#sidebar-header {
  padding: 12px 14px 10px;
  border-bottom: 1px solid #f1f5f9;
  flex-shrink: 0;
}
#sidebar-header h1 {
  font-size: 13px;
  font-weight: 700;
  letter-spacing: .04em;
  color: #0f172a;
  display: flex;
  align-items: center;
  gap: 7px;
}
#sidebar-header h1 .logo { font-size: 16px; }

/* scrollable body */
#sidebar-scroll {
  flex: 1;
  overflow-y: auto;
  padding-bottom: 20px;
}
#sidebar-scroll::-webkit-scrollbar { width: 4px; }
#sidebar-scroll::-webkit-scrollbar-track { background: transparent; }
#sidebar-scroll::-webkit-scrollbar-thumb { background: #e2e8f0; border-radius: 4px; }
#sidebar-scroll::-webkit-scrollbar-thumb:hover { background: #cbd5e1; }

/* status strip at the very bottom */
#sidebar-footer {
  padding: 6px 14px;
  border-top: 1px solid #f1f5f9;
  font-size: 11px;
  color: #94a3b8;
  flex-shrink: 0;
  background: #fafafa;
}

/* ── section headers ─────────────────────────────────────────────────────── */
.section-title {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .1em;
  color: #94a3b8;
  padding: 14px 14px 5px;
}

/* issues section title gets a red tint when there are findings */
.section-title.has-issues {
  color: #dc2626;
}

/* ── resizer ─────────────────────────────────────────────────────────────── */
#resizer {
  width: 4px;
  cursor: col-resize;
  background: #e2e8f0;
  flex-shrink: 0;
  transition: background 0.15s;
  z-index: 10;
}
#resizer:hover, #resizer:active { background: #94a3b8; }

/* ── graph canvas ────────────────────────────────────────────────────────── */
#graph {
  flex: 1;
  background-color: #f8fafc;
  background-image:
    linear-gradient(to right, #e2e8f033 1px, transparent 1px),
    linear-gradient(to bottom, #e2e8f033 1px, transparent 1px);
  background-size: 28px 28px;
}

/* ── file tree ───────────────────────────────────────────────────────────── */
#file-list ul {
  list-style: none;
  padding-left: 0;
}
#file-list li { line-height: 1; }

/* folder */
.tree-folder {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px 4px 14px;
  cursor: pointer;
  color: #374151;
  font-weight: 600;
  font-size: 12.5px;
  border-radius: 0;
  transition: background 0.1s;
  user-select: none;
}
.tree-folder:hover { background: #f8fafc; }
.tree-folder.open { color: #1e293b; }

.chevron {
  display: inline-block;
  width: 10px;
  font-size: 8px;
  color: #cbd5e1;
  transition: transform 0.15s;
  flex-shrink: 0;
  line-height: 1;
}
.chevron::before { content: "▶"; }
.tree-folder.open > .chevron { transform: rotate(90deg); color: #94a3b8; }

.folder-icon { font-size: 13px; flex-shrink: 0; }

.tree-children { padding-left: 16px; }

/* file */
.tree-file {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 3px 10px 3px 14px;
  cursor: pointer;
  border-radius: 0;
  color: #475569;
  transition: background 0.1s;
}
.tree-file:hover { background: #f8fafc; color: #1e293b; }
.tree-file input[type="checkbox"] {
  width: 12px;
  height: 12px;
  cursor: pointer;
  accent-color: #2563EB;
  flex-shrink: 0;
}
.file-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #2563EB;
  flex-shrink: 0;
  opacity: 0.5;
}
.tree-file:has(input:checked) .file-dot { opacity: 1; }
.tree-file:has(input:checked) { color: #1e293b; font-weight: 500; }

.tree-label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 12.5px;
}

/* ── filter rows ─────────────────────────────────────────────────────────── */
.filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 14px;
  cursor: pointer;
  transition: background 0.1s;
}
.filter-row:hover { background: #f8fafc; }
.filter-row input[type="checkbox"] {
  width: 12px;
  height: 12px;
  cursor: pointer;
  flex-shrink: 0;
}
.filter-label {
  color: #374151;
  font-size: 12px;
}

/* node-type swatch */
.swatch {
  display: inline-block;
  width: 13px;
  height: 13px;
  border: 2px solid transparent;
  flex-shrink: 0;
}

/* edge-type swatch */
.swatch-line {
  display: inline-block;
  width: 24px;
  height: 3px;
  border-radius: 2px;
  flex-shrink: 0;
}

/* ── issues panel ────────────────────────────────────────────────────────── */
#issues-panel {
  padding-bottom: 4px;
}

/* vis.js tooltip container */
.vis-tooltip {
  background: #ffffff !important;
  border: 1px solid #e2e8f0 !important;
  border-radius: 6px !important;
  box-shadow: 0 4px 12px rgba(0,0,0,0.10) !important;
  padding: 8px 10px !important;
  color: #1e293b !important;
  font-family: ui-sans-serif, system-ui, sans-serif !important;
  font-size: 12px !important;
  max-width: 360px !important;
  pointer-events: none;
}
</style>
</head>
<body>
<div id="layout">

  <div id="sidebar">

    <div id="sidebar-header">
      <h1><span class="logo">🔍</span> Code Graph</h1>
    </div>

    <div id="sidebar-scroll">

      <div class="section-title">Files</div>
      <div id="file-list"></div>

      <div class="section-title">Issues</div>
      <div id="issues-panel"></div>

      <div class="section-title">Node types</div>
      <div id="node-type-filters"></div>

      <div class="section-title">Edge types</div>
      <div id="edge-type-filters"></div>

    </div>

    <div id="sidebar-footer">
      <span id="status">Loading…</span>
    </div>

  </div>

  <div id="resizer"></div>
  <div id="graph"></div>

</div>
<script src="app.js"></script>
</body>
</html>

# --- FILE: pyrere/_viewer/app.js ---

/* ─────────────────────────────────────────────────────────────────────────────
   CKG VIEWER  -  app.js
   ───────────────────────────────────────────────────────────────────────────── */

"use strict";

// ── design tokens ─────────────────────────────────────────────────────────────

const NODE_STYLES = {
  module: {
    color: {
      background: "#EFF6FF",
      border:     "#2563EB",
      highlight:  { background: "#DBEAFE", border: "#1D4ED8" },
      hover:      { background: "#DBEAFE", border: "#1D4ED8" },
    },
    shape:  "box",
    font:   { color: "#1E3A5F", size: 12, face: "ui-monospace, 'Cascadia Code', monospace", bold: true },
    margin: 8,
  },
  class: {
    color: {
      background: "#FFF7ED",
      border:     "#EA580C",
      highlight:  { background: "#FFEDD5", border: "#C2410C" },
      hover:      { background: "#FFEDD5", border: "#C2410C" },
    },
    shape: "diamond",
    font:  { color: "#431407", size: 12, face: "ui-sans-serif, system-ui, sans-serif" },
    size:  20,
  },
  function: {
    color: {
      background: "#F0FDF4",
      border:     "#16A34A",
      highlight:  { background: "#DCFCE7", border: "#15803D" },
      hover:      { background: "#DCFCE7", border: "#15803D" },
    },
    shape: "ellipse",
    font:  { color: "#14532D", size: 12, face: "ui-sans-serif, system-ui, sans-serif" },
  },
};

const NODE_STYLES_EXTRA = {
  variable: {
    color: {
      background: "#F0F9FF",
      border:     "#0284C7",
      highlight:  { background: "#E0F2FE", border: "#0369A1" },
      hover:      { background: "#E0F2FE", border: "#0369A1" },
    },
    shape: "triangleDown",
    font:  { color: "#0C4A6E", size: 11, face: "ui-sans-serif, system-ui, sans-serif" },
  },
};

const NODE_STYLE_DEFAULT = {
  color: {
    background: "#F8FAFC",
    border:     "#94A3B8",
    highlight:  { background: "#F1F5F9", border: "#64748B" },
    hover:      { background: "#F1F5F9", border: "#64748B" },
  },
  shape: "dot",
  font:  { color: "#334155", size: 11 },
};

// Issue severity → border colour override
const SEVERITY_BORDER = {
  error:   "#DC2626",   // red-600
  warning: "#D97706",   // amber-600
  info:    "#0891B2",   // cyan-600
};

// Tool → pill colour (background, text)
const TOOL_PILL = {
  ruff:    { bg: "#EFF6FF", fg: "#1D4ED8", label: "ruff"    },
  vulture: { bg: "#FFF7ED", fg: "#C2410C", label: "vulture" },
  bandit:  { bg: "#FEF2F2", fg: "#991B1B", label: "bandit"  },
};

const EDGE_STYLES = {
  contains:       ["#CBD5E1", false],
  imports:        ["#2563EB", false],
  imports_symbol: ["#93C5FD", true ],
  calls:          ["#EA580C", false],
  inherits:       ["#7C3AED", false],
  decorates:      ["#DB2777", false],
  uses_type:      ["#0891B2", true ],
};
const EDGE_COLOUR_DEFAULT = "#94A3B8";

// ── state ─────────────────────────────────────────────────────────────────────

let fullData          = null;
let network           = null;
let selectedFiles     = new Set();
let selectedNodeTypes = new Set();
let selectedEdgeTypes = new Set();
let selectedNodeId    = null;
let _rendering        = false;

// ── resizer ───────────────────────────────────────────────────────────────────

function initResizer() {
  const resizer = document.getElementById("resizer");
  const sidebar = document.getElementById("sidebar");
  let dragging  = false;
  resizer.addEventListener("mousedown", () => {
    dragging = true;
    document.body.style.userSelect = "none";
  });
  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    sidebar.style.width = Math.max(180, Math.min(e.clientX, 560)) + "px";
  });
  document.addEventListener("mouseup", () => {
    dragging = false;
    document.body.style.userSelect = "";
  });
}

// ── path helpers ──────────────────────────────────────────────────────────────

function normalizePath(p) {
  return p ? p.replace(/\\/g, "/") : "";
}

// ── issue helpers ─────────────────────────────────────────────────────────────

/**
 * Return the worst severity present in an issues array, or null if empty.
 * Order: error > warning > info
 */
function worstSeverity(issues) {
  if (!issues || issues.length === 0) return null;
  if (issues.some((i) => i.severity === "error"))   return "error";
  if (issues.some((i) => i.severity === "warning")) return "warning";
  return "info";
}

/**
 * Count issues by severity. Returns { error, warning, info }.
 */
function countBySeverity(issues) {
  const out = { error: 0, warning: 0, info: 0 };
  for (const i of (issues || [])) out[i.severity] = (out[i.severity] || 0) + 1;
  return out;
}

// ── file tree ─────────────────────────────────────────────────────────────────

function buildFileTree(files) {
  const root = {};
  for (const file of files) {
    const parts = file.split("/").filter(Boolean);
    let cur = root;
    for (let i = 0; i < parts.length; i++) {
      const key = parts[i];
      if (!cur[key]) cur[key] = { __children: {}, __isFile: false };
      if (i === parts.length - 1) cur[key].__isFile = true;
      cur = cur[key].__children;
    }
  }
  return root;
}

function renderTree(node, container, path = "", depth = 0) {
  const ul = document.createElement("ul");

  const all     = Object.entries(node);
  const folders = all.filter(([, v]) => !v.__isFile || Object.keys(v.__children).length > 0)
                     .sort(([a], [b]) => a.localeCompare(b));
  const files   = all.filter(([, v]) =>  v.__isFile && Object.keys(v.__children).length === 0)
                     .sort(([a], [b]) => a.localeCompare(b));

  for (const [key, item] of [...folders, ...files]) {
    const fullPath = path ? `${path}/${key}` : key;
    const li       = document.createElement("li");
    const isFolder = !item.__isFile || Object.keys(item.__children).length > 0;

    if (isFolder) {
      const header = document.createElement("div");
      header.className = "tree-folder";

      const chevron = document.createElement("span");
      chevron.className = "chevron";

      const icon = document.createElement("span");
      icon.className = "folder-icon";
      icon.textContent = "📁";

      const label = document.createElement("span");
      label.className = "tree-label";
      label.textContent = key;

      header.append(chevron, icon, label);

      const childWrap = document.createElement("div");
      childWrap.className = "tree-children";
      const open = depth === 0;
      childWrap.style.display = open ? "block" : "none";
      if (open) header.classList.add("open");

      header.addEventListener("click", (e) => {
        e.stopPropagation();
        const isOpen = childWrap.style.display !== "none";
        childWrap.style.display = isOpen ? "none" : "block";
        header.classList.toggle("open", !isOpen);
      });

      renderTree(item.__children, childWrap, fullPath, depth + 1);
      li.append(header, childWrap);

    } else {
      const row = document.createElement("label");
      row.className = "tree-file";

      const cb = document.createElement("input");
      cb.type    = "checkbox";
      cb.checked = selectedFiles.has(fullPath);
      cb.onchange = () => {
        if (cb.checked) selectedFiles.add(fullPath);
        else            selectedFiles.delete(fullPath);
        renderGraph();
      };

      const dot = document.createElement("span");
      dot.className = "file-dot";

      const label = document.createElement("span");
      label.className = "tree-label";
      label.textContent = key;
      label.title = fullPath;

      row.append(cb, dot, label);
      li.appendChild(row);
    }

    ul.appendChild(li);
  }

  container.appendChild(ul);
}

function buildFileList() {
  const container = document.getElementById("file-list");
  container.innerHTML = "";
  const files = [...new Set(fullData.nodes.map((n) => n.file).filter(Boolean))].sort();
  renderTree(buildFileTree(files), container);
}

// ── type filters ──────────────────────────────────────────────────────────────

function makeNodeSwatch(type) {
  const s  = NODE_STYLES[type] ?? NODE_STYLE_DEFAULT;
  const el = document.createElement("span");
  el.className = "swatch";
  el.style.background   = s.color.background;
  el.style.borderColor  = s.color.border;
  el.style.borderRadius = (s.shape === "ellipse") ? "50%"
                        : (s.shape === "diamond") ? "2px"
                        : "3px";
  if (s.shape === "diamond") el.style.transform = "rotate(45deg)";
  return el;
}

function makeEdgeSwatch(type) {
  const [colour, dashed] = EDGE_STYLES[type] ?? [EDGE_COLOUR_DEFAULT, false];
  const el = document.createElement("span");
  el.className = "swatch-line";
  el.style.background = dashed
    ? `repeating-linear-gradient(90deg,${colour} 0,${colour} 5px,transparent 5px,transparent 9px)`
    : colour;
  return el;
}

function makeFilterRow(labelText, checked, swatchEl, onChange) {
  const row = document.createElement("label");
  row.className = "filter-row";

  const cb = document.createElement("input");
  cb.type    = "checkbox";
  cb.checked = checked;
  cb.onchange = onChange;

  const txt = document.createElement("span");
  txt.className  = "filter-label";
  txt.textContent = labelText;

  row.append(cb, swatchEl, txt);
  return row;
}

function buildTypeFilters() {
  const nodeDiv = document.getElementById("node-type-filters");
  const edgeDiv = document.getElementById("edge-type-filters");
  nodeDiv.innerHTML = "";
  edgeDiv.innerHTML = "";

  const nodeTypes = [...new Set(fullData.nodes.map((n) => n.type))].sort();
  const edgeTypes = [...new Set(fullData.edges.map((e) => e.type))].sort();

  selectedNodeTypes = new Set(nodeTypes);
  selectedEdgeTypes = new Set(edgeTypes);

  for (const t of nodeTypes) {
    nodeDiv.appendChild(makeFilterRow(t, true, makeNodeSwatch(t),
      (e) => { e.target.checked ? selectedNodeTypes.add(t) : selectedNodeTypes.delete(t); renderGraph(); }
    ));
  }
  for (const t of edgeTypes) {
    edgeDiv.appendChild(makeFilterRow(t, true, makeEdgeSwatch(t),
      (e) => { e.target.checked ? selectedEdgeTypes.add(t) : selectedEdgeTypes.delete(t); renderGraph(); }
    ));
  }
}

// ── issues sidebar panel ──────────────────────────────────────────────────────

/**
 * Rebuild the "Issues" panel in the sidebar with aggregated counts across
 * all currently visible nodes.
 */
function buildIssuesPanel(visibleNodes) {
  const panel = document.getElementById("issues-panel");
  if (!panel) return;
  panel.innerHTML = "";

  // Collect all issues from visible nodes
  const byTool    = {};
  const bySev     = { error: 0, warning: 0, info: 0 };
  let   total     = 0;

  for (const n of visibleNodes) {
    for (const issue of (n.metadata?.issues ?? [])) {
      byTool[issue.tool] = (byTool[issue.tool] || 0) + 1;
      bySev[issue.severity] = (bySev[issue.severity] || 0) + 1;
      total++;
    }
  }

  if (total === 0) {
    const none = document.createElement("div");
    none.style.cssText = "padding:6px 14px;font-size:12px;color:#94a3b8";
    none.textContent = "No issues in visible nodes";
    panel.appendChild(none);
    return;
  }

  // Severity summary row
  const sevRow = document.createElement("div");
  sevRow.style.cssText = "display:flex;gap:6px;padding:6px 14px 4px;flex-wrap:wrap";

  const sevDefs = [
    { key: "error",   label: "errors",   bg: "#FEF2F2", fg: "#991B1B" },
    { key: "warning", label: "warnings", bg: "#FFFBEB", fg: "#92400E" },
    { key: "info",    label: "info",     bg: "#EFF6FF", fg: "#1E40AF" },
  ];
  for (const { key, label, bg, fg } of sevDefs) {
    if (!bySev[key]) continue;
    const pill = document.createElement("span");
    pill.style.cssText = `background:${bg};color:${fg};font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px`;
    pill.textContent = `${bySev[key]} ${label}`;
    sevRow.appendChild(pill);
  }
  panel.appendChild(sevRow);

  // Per-tool breakdown
  for (const [tool, count] of Object.entries(byTool).sort()) {
    const p = TOOL_PILL[tool] ?? { bg: "#F1F5F9", fg: "#334155", label: tool };
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:7px;padding:3px 14px";

    const pill = document.createElement("span");
    pill.style.cssText = `background:${p.bg};color:${p.fg};font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;min-width:46px;text-align:center`;
    pill.textContent = p.label;

    const cnt = document.createElement("span");
    cnt.style.cssText = "font-size:12px;color:#374151";
    cnt.textContent = `${count} issue${count !== 1 ? "s" : ""}`;

    row.append(pill, cnt);
    panel.appendChild(row);
  }
}

// ── graph rendering ───────────────────────────────────────────────────────────

function edgeOptions(type) {
  const [colour, dashed] = EDGE_STYLES[type] ?? [EDGE_COLOUR_DEFAULT, false];
  return {
    color:  { color: colour, highlight: colour, hover: colour },
    dashes: dashed,
    width:  type === "contains" ? 1 : 2,
    arrows: { to: { enabled: true, scaleFactor: 0.6 } },
    smooth: { type: "dynamic" },
  };
}

function nodeOptions(type, isImported = false, issues = []) {
  // Start from the base style for this node type
  const base = {
    ...(NODE_STYLES[type] ?? NODE_STYLES_EXTRA[type] ?? NODE_STYLE_DEFAULT),
  };

  // Apply severity-based border colour override (errors trump warnings)
  const worst = worstSeverity(issues);
  if (worst) {
    const borderCol = SEVERITY_BORDER[worst];
    base.color = {
      ...base.color,
      border:    borderCol,
      highlight: { ...base.color.highlight, border: borderCol },
      hover:     { ...base.color.hover,     border: borderCol },
    };
    // Slightly thicker border so it reads at small sizes
    base.borderWidth = 3;
  }

  if (!isImported) return base;

  return {
    ...base,
    color: {
      ...base.color,
      background: base.color.background,
      border:     base.color.border + "88",
    },
    opacity:      0.55,
    borderDashes: [5, 4],
    borderWidth:  1,
  };
}

/**
 * Build the rich HTML tooltip element for a node.
 */
function buildTooltip(n, isImported) {
  const m      = n.metadata ?? {};
  const issues = m.issues ?? [];
  const el     = document.createElement("div");
  el.style.cssText =
    "font-family:ui-sans-serif,system-ui,sans-serif;font-size:12px;" +
    "line-height:1.5;max-width:340px;padding:2px 0";

  // ── name + type ──────────────────────────────────────────────────────────────
  const heading = document.createElement("div");
  heading.style.cssText = "font-weight:700;font-size:13px;margin-bottom:2px";
  heading.textContent = n.name;
  el.appendChild(heading);

  const sub = document.createElement("div");
  sub.style.cssText = "color:#64748b;font-size:11px";
  sub.textContent = `${n.type} · ${n.file ?? ""}`;
  el.appendChild(sub);

  // ── metadata badges ──────────────────────────────────────────────────────────
  const badges = [];
  if (m.is_async)       badges.push("async");
  if (m.is_generator)   badges.push("generator");
  if (m.is_static)      badges.push("static");
  if (m.is_classmethod) badges.push("classmethod");
  if (m.is_property)    badges.push("property");
  if (m.is_lambda)      badges.push("lambda");
  if (m.is_dataclass)   badges.push("dataclass");
  if (m.is_abstract)    badges.push("abstract");
  if (m.is_exception)   badges.push("exception");
  if (isImported)       badges.push("imported");
  if (m.visibility)     badges.push(m.visibility);

  if (badges.length) {
    const bd = document.createElement("div");
    bd.style.cssText = "color:#94a3b8;font-size:10px;margin-top:2px";
    bd.textContent = badges.join(" · ");
    el.appendChild(bd);
  }

  if (m.return_type) {
    const rt = document.createElement("div");
    rt.style.cssText = "color:#94a3b8;font-size:10px";
    rt.textContent = `→ ${m.return_type}`;
    el.appendChild(rt);
  }

  if (m.complexity != null && m.complexity > 1) {
    const cx = document.createElement("div");
    cx.style.cssText = "color:#94a3b8;font-size:10px";
    cx.textContent = `complexity: ${m.complexity}`;
    el.appendChild(cx);
  }

  if (m.docstring) {
    const ds = document.createElement("div");
    ds.style.cssText =
      "color:#64748b;font-size:11px;font-style:italic;margin-top:3px;" +
      "border-top:1px solid #f1f5f9;padding-top:3px";
    const preview = m.docstring.length > 120 ? m.docstring.slice(0, 120) + "…" : m.docstring;
    ds.textContent = preview;
    el.appendChild(ds);
  }

  // ── issues section ───────────────────────────────────────────────────────────
  if (issues.length > 0) {
    const divider = document.createElement("div");
    divider.style.cssText =
      "margin-top:6px;padding-top:5px;border-top:1px solid #fee2e2";
    el.appendChild(divider);

    const issueHdr = document.createElement("div");
    issueHdr.style.cssText = "font-weight:700;font-size:10px;color:#dc2626;margin-bottom:3px;text-transform:uppercase;letter-spacing:.05em";
    issueHdr.textContent = `${issues.length} issue${issues.length !== 1 ? "s" : ""}`;
    divider.appendChild(issueHdr);

    // Show up to 6 issues; summarise the rest
    const shown = issues.slice(0, 6);
    for (const issue of shown) {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;gap:5px;align-items:baseline;margin-bottom:2px";

      const p = TOOL_PILL[issue.tool] ?? { bg: "#F1F5F9", fg: "#334155" };
      const pill = document.createElement("span");
      pill.style.cssText =
        `background:${p.bg};color:${p.fg};font-size:9px;font-weight:700;` +
        "padding:0 4px;border-radius:3px;flex-shrink:0";
      pill.textContent = issue.tool;

      const codeSev = document.createElement("span");
      const sevColour = SEVERITY_BORDER[issue.severity] ?? "#64748b";
      codeSev.style.cssText = `color:${sevColour};font-size:10px;font-weight:600;flex-shrink:0`;
      codeSev.textContent = issue.code;

      const msg = document.createElement("span");
      msg.style.cssText = "color:#374151;font-size:10px;white-space:normal";
      const short = issue.message.length > 70
        ? issue.message.slice(0, 70) + "…"
        : issue.message;
      msg.textContent = `${short}  (L${issue.line})`;

      row.append(pill, codeSev, msg);
      divider.appendChild(row);
    }

    if (issues.length > 6) {
      const more = document.createElement("div");
      more.style.cssText = "color:#94a3b8;font-size:10px;margin-top:2px";
      more.textContent = `+ ${issues.length - 6} more …`;
      divider.appendChild(more);
    }
  }

  return el;
}

function renderGraph() {
  const nodeById       = new Map(fullData.nodes.map((n) => [n.id, n]));
  const moduleIdByFile = new Map();
  const fileByModuleId = new Map();
  for (const n of fullData.nodes) {
    if (n.type === "module" && n.file) {
      moduleIdByFile.set(n.file, n.id);
      fileByModuleId.set(n.id, n.file);
    }
  }

  // One-hop import expansion
  const importAdj = new Map();
  for (const e of fullData.edges) {
    if (e.type !== "imports") continue;
    if (!importAdj.has(e.source)) importAdj.set(e.source, []);
    importAdj.get(e.source).push(e.target);
  }

  const importedFiles = new Set();
  for (const selFile of selectedFiles) {
    const modId = moduleIdByFile.get(selFile);
    if (!modId) continue;
    for (const targetId of (importAdj.get(modId) ?? [])) {
      const tf = fileByModuleId.get(targetId);
      if (tf && !selectedFiles.has(tf)) importedFiles.add(tf);
    }
  }

  const allVisibleFiles = new Set([...selectedFiles, ...importedFiles]);

  // Node set
  const nodes = fullData.nodes.filter(
    (n) => allVisibleFiles.has(n.file) && selectedNodeTypes.has(n.type)
  );
  const nodeIds = new Set(nodes.map((n) => n.id));

  // Expand neighbours of selected node
  if (selectedNodeId && nodeIds.has(selectedNodeId)) {
    for (const e of fullData.edges) {
      if (!selectedEdgeTypes.has(e.type)) continue;
      const otherId = e.source === selectedNodeId ? e.target
                    : e.target === selectedNodeId ? e.source : null;
      if (!otherId || nodeIds.has(otherId)) continue;
      const other = nodeById.get(otherId);
      if (other && selectedNodeTypes.has(other.type)) {
        nodes.push(other);
        nodeIds.add(otherId);
      }
    }
  }

  // Edge set
  const edges = fullData.edges.filter(
    (e) => nodeIds.has(e.source) && nodeIds.has(e.target) && selectedEdgeTypes.has(e.type)
  );

  // Status bar
  const statusEl = document.getElementById("status");
  if (statusEl) statusEl.textContent = `${nodes.length} nodes · ${edges.length} edges`;

  // Refresh issues panel
  buildIssuesPanel(nodes);

  // vis datasets
  const visNodes = new vis.DataSet(
    nodes.map((n) => {
      const isImported = importedFiles.has(n.file) && !selectedFiles.has(n.file);
      const issues     = n.metadata?.issues ?? [];
      const counts     = countBySeverity(issues);

      // Build label: name + optional issue badge
      let label = n.name;
      if (counts.error)   label += ` ✖${counts.error}`;
      if (counts.warning) label += ` ⚠${counts.warning}`;

      return {
        id:    n.id,
        label,
        title: buildTooltip(n, isImported),
        ...nodeOptions(n.type, isImported, issues),
      };
    })
  );

  const visEdges = new vis.DataSet(
    edges.map((e) => ({
      id:    `${e.source}_${e.target}_${e.type}`,
      from:  e.source,
      to:    e.target,
      label: e.type,
      font:  { size: 9, color: "#94A3B8", align: "middle", strokeWidth: 0 },
      ...edgeOptions(e.type),
    }))
  );

  const container = document.getElementById("graph");

  if (network) {
    network.setData({ nodes: visNodes, edges: visEdges });
  } else {
    network = new vis.Network(container, { nodes: visNodes, edges: visEdges }, {
      layout: { improvedLayout: true },
      physics: {
        stabilization: { iterations: 300, fit: true },
        barnesHut: {
          gravitationalConstant: -6000,
          centralGravity:        0.1,
          springLength:          140,
          springConstant:        0.04,
          damping:               0.12,
        },
      },
      interaction: {
        hover:           true,
        tooltipDelay:    60,
        hideEdgesOnDrag: true,
      },
      nodes: {
        borderWidth:         2,
        borderWidthSelected: 3,
        shadow:              { enabled: true, color: "rgba(0,0,0,0.08)", size: 8, x: 0, y: 2 },
      },
      edges: {
        font: { size: 9, color: "#94A3B8", strokeWidth: 0 },
      },
    });

    network.on("selectNode", ({ nodes: ns }) => {
      if (_rendering) return;
      selectedNodeId = ns[0] ?? null;
      renderGraph();
    });
    network.on("deselectNode", () => {
      if (_rendering) return;
      selectedNodeId = null;
      renderGraph();
    });
  }

  if (selectedNodeId && nodeIds.has(selectedNodeId)) {
    _rendering = true;
    network.selectNodes([selectedNodeId]);
    _rendering = false;
  }
}

// ── init ──────────────────────────────────────────────────────────────────────

async function loadGraph() {
  const res = await fetch("graph.json");
  fullData  = await res.json();
  fullData.nodes.forEach((n) => { n.file = normalizePath(n.file); });

  const allFiles  = [...new Set(fullData.nodes.map((n) => n.file).filter(Boolean))];
  const mainFiles = allFiles.filter((f) => f.endsWith("__main__.py"));
  selectedFiles   = new Set(mainFiles.length ? mainFiles : allFiles.slice(0, 1));

  buildFileList();
  buildTypeFilters();
  renderGraph();
}

loadGraph();
initResizer();

# --- FILE: CONTRIBUTING.md ---

# Contributing to pyrere

Thank you for your interest in contributing! pyrere is an open-source project and contributions of all kinds are welcome — bug fixes, new features, documentation improvements, new skills, and additional backend support.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Project Structure](#project-structure)
- [Adding a New Skill](#adding-a-new-skill)
- [Adding a New Backend](#adding-a-new-backend)
- [Testing](#testing)
- [Commit Style](#commit-style)
- [Pull Request Guidelines](#pull-request-guidelines)
- [Reporting Bugs](#reporting-bugs)
- [Requesting Features](#requesting-features)

---

## Code of Conduct

Be respectful, constructive, and collaborative. We're all here to build something useful together.

---

## Ways to Contribute

You don't have to write code to contribute meaningfully:

- **Fix a bug** — browse [open issues](https://github.com/3IVIS/pyrere/issues) labelled `bug`
- **Implement a feature** — pick up an issue labelled `enhancement` or `help wanted`
- **Add a skill** — the easiest entry point; no core code changes required (see [Adding a New Skill](#adding-a-new-skill))
- **Add a backend** — bring support for a new LLM provider (see [Adding a New Backend](#adding-a-new-backend))
- **Improve documentation** — fix typos, clarify explanations, add examples
- **Share a demo run** — submit an interesting interactive snapshot as an example
- **Report a bug** — a well-written issue is a genuine contribution
- **Propose a feature** — open a discussion before building something large

---

## Getting Started

### Prerequisites

- Python 3.11 or higher
- `git` on your PATH
- At least one of: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or a local llama.cpp installation

### Fork and clone

```bash
# 1. Fork the repo on GitHub, then clone your fork
git clone https://github.com/<your-username>/pyrere.git
cd pyrere

# 2. Add the upstream remote so you can pull future changes
git remote add upstream https://github.com/3IVIS/pyrere.git
```

### Install in development mode

```bash
# Install all extras plus development dependencies
pip install -e ".[all,dev]"
```

### Verify your setup

```bash
# Run the test suite to confirm everything is working
pytest

# Run a quick smoke test against your preferred backend
export ANTHROPIC_API_KEY=sk-ant-...
pyrere "List three ways to learn Python"
```

---

## Development Workflow

pyrere follows a standard **fork → branch → PR** flow.

```
upstream/main  ←─────────── your PR
      │
      └──→ your fork/main
                 │
                 └──→ feature/your-branch  ← your work lives here
```

### Step by step

```bash
# 1. Keep your fork's main up to date
git fetch upstream
git checkout main
git merge upstream/main

# 2. Create a focused branch for your change
git checkout -b feature/my-new-skill
# or: fix/crash-on-empty-graph
# or: docs/clarify-config-options

# 3. Make your changes, commit often
git add .
git commit -m "feat(skills): add web-scraping skill with BeautifulSoup"

# 4. Push and open a PR against upstream/main
git push origin feature/my-new-skill
```

Then open a pull request on GitHub from your branch to `3IVIS/pyrere:main`.

---

## Project Structure

Understanding where things live will help you find the right place for your change:

```
pyrere/
├── core/
│   └── task_graph.py          # TaskGraph — the mutable DAG at the heart of everything
├── planning/
│   ├── llm_interface.py       # create_llm_client() — backend abstraction layer
│   ├── llm_planner.py         # LLMPlanner — goal → DAG decomposition
│   └── llm_executor.py        # LLMExecutor — per-task LLM execution loop
├── engine/
│   ├── llm_orchestrator.py    # Orchestrator — concurrency, scheduling, event loop
│   └── quality_gate.py        # QualityGate — output verification + gap bridging
├── skills/
│   ├── skill_loader.py        # Discovers and loads SKILL.md skill folders
│   └── builtin/               # Built-in skills (code execution, file I/O, web access)
├── ui/
│   ├── terminal/              # Curses-based terminal UI
│   └── web/                   # Web UI — task graph visualiser, node editor
├── config.py                  # Config loading, CONFIG_PATH, defaults
docs/
├── architecture.md
├── configuration.md
├── skills.md
└── api.md
skills/                        # Drop custom skill folders here
tests/
```

---

## Adding a New Skill

Skills are the easiest way to extend pyrere — no changes to core code required. A skill is a folder containing two files:

```
skills/
└── my_skill/
    ├── SKILL.md     # Description of what the skill does (shown to the LLM planner)
    └── tools.py     # Tool implementations exposed to the LLM executor
```

### SKILL.md

Write a clear, concise description of what the skill does and when to use it. This text is injected directly into the planner's prompt, so plain English works best:

```markdown
# my_skill

Use this skill when the task requires [describe the use case].

## Available tools

- `tool_name(arg1, arg2)` — does X, returns Y
- `another_tool(arg)` — does Z
```

### tools.py

Implement your tools as plain Python functions decorated with `@tool`:

```python
from pyrere.skills.registry import tool

@tool
def tool_name(arg1: str, arg2: int) -> str:
    """Does X. Returns Y as a string."""
    # your implementation
    return result
```

### Testing your skill

Drop the folder into the `skills/` directory at the project root and run a goal that would naturally invoke it:

```bash
pyrere "Your goal that exercises the new skill"
```

Check that the planner picks up the skill in its summary and that the executor calls it correctly.

---

## Adding a New Backend

Backends live behind the `create_llm_client()` abstraction in `pyrere/planning/llm_interface.py`. To add support for a new LLM provider:

1. **Add a client class** in `llm_interface.py` that implements the same interface as the existing `ClaudeClient` and `OpenAIClient` classes (i.e. a `complete()` method with the same signature).

2. **Register the backend name** in the `create_llm_client()` factory function.

3. **Add a pip extra** in `pyproject.toml` (e.g. `[project.optional-dependencies]` → `myprovider = ["their-sdk"]`).

4. **Update the backends table** in `README.md` and `docs/configuration.md`.

5. **Add an integration test** in `tests/test_backends.py` that skips unless the relevant API key is present.

Please open an issue first if you're planning a new backend — it's a good way to align on the interface before writing code.

---

## Testing

```bash
# Run the full test suite
pytest

# Run a specific file
pytest tests/test_task_graph.py

# Run with coverage
pytest --cov=pyrere

# Run only fast unit tests (skip integration tests that hit real APIs)
pytest -m "not integration"
```

If you're adding new functionality, please include:
- **Unit tests** for logic that can be tested in isolation
- **An integration test** (marked `@pytest.mark.integration`) for anything that exercises a real LLM call, gated on the relevant env var being set

---

## Commit Style

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>

[optional body]

[optional footer]
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`

**Scopes** (use the relevant module): `planner`, `executor`, `graph`, `orchestrator`, `quality-gate`, `skills`, `ui`, `config`, `backends`

**Examples:**

```
feat(skills): add PDF extraction skill using pdfplumber
fix(orchestrator): prevent duplicate task dispatch on fast resume
docs(skills): add example SKILL.md template
refactor(planner): extract context extraction into its own method
test(graph): add edge cases for cycle detection in PlanConstraintChecker
```

Keep the subject line under 72 characters. Use the body to explain *why*, not *what*.

---

## Pull Request Guidelines

- **Keep PRs focused** — one logical change per PR makes review much faster
- **Reference the issue** — include `Closes #123` or `Fixes #123` in the PR description if applicable
- **Fill in the PR template** — describe what changed, why, and how you tested it
- **Pass CI** — all tests must pass before a PR will be reviewed
- **Keep commits clean** — squash fixup commits before requesting review (`git rebase -i`)
- **Be responsive** — if a reviewer asks a question, try to respond within a few days

### PR description template

```markdown
## What does this PR do?
<!-- A concise summary of the change -->

## Why?
<!-- The motivation — link to issue if applicable -->

## How was it tested?
<!-- What did you run to verify it works? -->

## Checklist
- [ ] Tests added or updated
- [ ] Docs updated if behaviour changed
- [ ] `CONTRIBUTING.md` updated if new patterns introduced
```

---

## Reporting Bugs

Please use the [GitHub Issues](https://github.com/3IVIS/pyrere/issues) page and include:

- **pyrere version** (`pip show pyrere`)
- **Python version** (`python --version`)
- **Backend and model** (e.g. `claude / claude-opus-4-6`)
- **Operating system**
- **What you did** — the goal you ran, or the code you called
- **What you expected** to happen
- **What actually happened** — include the full error traceback if there is one
- **Event log** if available — the JSONL file from the run's output directory (remove any API keys first)

---

## Requesting Features

Open a [GitHub Issue](https://github.com/3IVIS/pyrere/issues) with the label `enhancement`. Describe:

- The problem you're trying to solve (not just the solution)
- How you'd expect it to work from a user perspective
- Any ideas you have on implementation

For large changes (new subsystems, breaking changes to the API), please open a discussion issue before writing code so we can align on the design first.

---

## Questions?

Open a [GitHub Discussion](https://github.com/3IVIS/pyrere/discussions) or file an issue labelled `question`. We're happy to help.

# --- FILE: README.md ---

# pyrere (Python Repo Review)
 
**Code Knowledge Graph** — static analysis + LLM-assisted refactoring pipeline for Python repositories.
 
`pyrere` parses a Python repository with [tree-sitter](https://tree-sitter.github.io/tree-sitter/), builds a typed graph of modules, classes and functions, optionally enriches it with [pyright](https://github.com/microsoft/pyright) / [grimp](https://github.com/seddonym/grimp) / [pycg](https://github.com/vitsalis/PyCG), annotates it with static-analysis issues (ruff, vulture, bandit), and opens an interactive in-browser viewer.
 
---
 
## Installation
 
```bash
pip install pyrere
```
 
With optional enrichment / flow tools:
 
```bash
pip install "pyrere[all]"          # everything
pip install "pyrere[flow]"         # ruff + vulture + bandit
pip install "pyrere[enrichment]"   # pyright + grimp + pycg
```

> **Note — pyright requires Node.js:**  `pyright` is a Node.js program installed
> via a thin pip shim.  `pip install pyrere[enrichment]` will succeed on machines
> without Node.js, but running the enrichment step will fail at runtime.  Install
> Node.js 18+ from <https://nodejs.org> before using the `enrichment` extra.
> pyrere degrades gracefully when pyright is absent — the enrichment step is
> simply skipped with a printed warning.

> **Note — pycg compatibility:**  `pycg` (a points-to call-graph tool included in
> the `enrichment` extra) is a research project with limited maintenance and known
> install issues on Python 3.11+.  pyrere handles a missing or broken pycg
> gracefully — the call-graph enrichment step is skipped automatically.  If you
> hit install errors with `pip install "pyrere[enrichment]"` try omitting pycg:
> ```bash
> pip install "pyrere[flow]" pyright grimp
> ```
 
---
 
## Quick start
 
```bash
# Analyse the current directory and open the viewer
pyrere .
 
# Analyse a specific repo
pyrere /path/to/your/repo
```
 
The viewer will open at `http://localhost:8000`.
 
---
 
## Python API
 
```python
from pyrere import build_graph, enrich_graph, annotate_graph
 
graph = build_graph("/path/to/repo")
enrich_graph(graph, "/path/to/repo")   # optional: pyright/grimp/pycg
annotate_graph(graph, "/path/to/repo") # optional: ruff/vulture/bandit
 
print(f"{len(graph.nodes)} nodes, {len(graph.edges)} edges")
```
 
---
 
## tree-sitter version note
 
The default install uses the **legacy bundle** (`tree-sitter < 0.22`, `tree-sitter-languages`) which ships pre-compiled grammars for ~100 languages and requires no compiler.

Pre-built wheels for `tree-sitter-languages` are available for the most common
platforms (CPython 3.10-3.12 on Linux x86_64/arm64, macOS, Windows x64).  On
other platforms (e.g. Alpine Linux, RISC-V, PyPy) `pip` will attempt to build
from source and will need a C compiler (`gcc` / `clang`) plus the Python headers
(`python3-dev` / `python3-devel`).
 
To use the **modern per-language packages** instead (no compiler required on all
platforms that have a `tree-sitter-python` wheel), install manually:
 
```bash
pip install "tree-sitter>=0.22" tree-sitter-python
```
 
and update `dependencies` in your local `pyproject.toml` accordingly.
 
---

## Viewer and internet access

The interactive graph viewer loads [vis-network](https://visjs.github.io/vis-network/)
from the unpkg CDN at a pinned version.  An active internet connection is
required the first time the viewer is opened in each browser session.

For **offline use**, download the JS file once and place it alongside the viewer:

```bash
curl -Lo "$(python -c "import pyrere, os; print(os.path.join(os.path.dirname(pyrere.__file__), '_viewer', 'vis-network.min.js'))")" \
  https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js
```

Then edit `_viewer/index.html` to change the `<script src="...">` line to
`<script src="vis-network.min.js">`.

---
 
## Development
 
```bash
git clone https://github.com/3ivis/pyrere
cd pyrere
pip install -e ".[dev]"
pytest
```
 
---
 
## License
 
MIT

# --- FILE: pyproject.toml ---

[build-system]
requires      = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"


# ══════════════════════════════════════════════════════════════════════════════
# PROJECT METADATA
# ══════════════════════════════════════════════════════════════════════════════

[project]
name        = "pyrere"
version     = "0.1.0"
description = "Code Knowledge Graph: static analysis + LLM-assisted refactoring pipeline"
readme      = "README.md"
license     = { text = "MIT" }

requires-python = ">=3.10"

authors = [
    { name = "3IVIS GmbH", email = "contact@3ivis.com" },
]

keywords = [
    "code analysis", "knowledge graph", "tree-sitter",
    "static analysis", "refactoring", "LLM",
]

classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Topic :: Software Development :: Libraries :: Python Modules",
    "Topic :: Software Development :: Quality Assurance",
    # PEP 561: this package ships inline type annotations
    "Typing :: Typed",
]

# Uses the legacy tree-sitter bundle (< 0.22) with pre-compiled grammars.
# See README for instructions on switching to the modern per-language packages.
dependencies = [
    "tree-sitter>=0.21,<0.22",
    "tree-sitter-languages>=1.10.2",
]

[project.urls]
Homepage      = "https://github.com/3ivis/pyrere"
Documentation = "https://github.com/3ivis/pyrere#readme"
"Bug Tracker" = "https://github.com/3ivis/pyrere/issues"


# ── Optional dependency groups ────────────────────────────────────────────────
# Install with:
#   pip install "pyrere[flow]"
#   pip install "pyrere[enrichment]"
#   pip install "pyrere[all]"

[project.optional-dependencies]

# Step 8: static linters and security scanners
flow = [
    "ruff>=0.4.0",
    "vulture>=2.11",
    "bandit>=1.7.9",
]

# Step 4: semantic enrichment tools
# NOTE: pyright requires Node.js on your PATH at runtime (it is a Node.js
#       program wrapped by a thin pip shim).  Installation will succeed
#       without Node.js but running pyrere[enrichment] will fail.
# NOTE: pycg (>=0.0.7) is a research tool with limited maintenance.  It may
#       not install cleanly on Python 3.11+.  pyrere degrades gracefully when
#       pycg is absent — the call-graph enrichment step is simply skipped.
enrichment = [
    "pyright>=1.1.0",
    "grimp>=3.4",
    "pycg>=0.0.7",
]

# FIX: self-referential extras ("pyrere[flow,enrichment]") fail silently on
# pip < 21.2.  List all deps explicitly so any pip version works.
all = [
    "ruff>=0.4.0",
    "vulture>=2.11",
    "bandit>=1.7.9",
    "pyright>=1.1.0",
    "grimp>=3.4",
    "pycg>=0.0.7",
]

dev = [
    "ruff>=0.4.0",
    "vulture>=2.11",
    "bandit>=1.7.9",
    "pyright>=1.1.0",
    "grimp>=3.4",
    "pycg>=0.0.7",
    "pytest>=8.0",
    "pytest-cov>=5.0",
    "mypy>=1.10",
]


# ── Entry points ──────────────────────────────────────────────────────────────
[project.scripts]
pyrere = "pyrere_scripts.run:main"


# ══════════════════════════════════════════════════════════════════════════════
# PACKAGE DISCOVERY
# ══════════════════════════════════════════════════════════════════════════════

[tool.setuptools.packages.find]
where   = ["."]
include = ["pyrere*", "pyrere_scripts*"]
# Exclusions:
#   *.egg-info        — build artefacts
#   _viewer*          — top-level _viewer (safety net; not a real package)
#   pyrere._viewer    — _viewer/ is a static-asset directory, not a Python
#                       package; its __init__.py exists only as a placeholder.
#                       Excluding it here prevents an empty pyrere._viewer
#                       package from appearing in the installed wheel.
#                       The actual HTML/JS/CSS files are shipped via package-data.
#   tests*            — test suite, not for distribution
#   pyrere.relationships — empty placeholder, not yet implemented
exclude = [
    "*.egg-info",
    "_viewer*",
    "pyrere._viewer",
    "pyrere._viewer.*",
    "tests*",
    "pyrere.relationships",
    "pyrere.relationships.*",
]

[tool.setuptools.package-data]
"pyrere" = [
    # PEP 561 marker — tells mypy/pyright that this package ships type stubs
    "py.typed",
    # Interactive viewer static assets
    "_viewer/*.html",
    "_viewer/*.js",
    "_viewer/*.css",
]


# ══════════════════════════════════════════════════════════════════════════════
# TOOL CONFIGURATIONS
# ══════════════════════════════════════════════════════════════════════════════

# ── ruff ──────────────────────────────────────────────────────────────────────
[tool.ruff]
target-version = "py310"
line-length    = 100
exclude = [
    ".git", ".hg", ".tox", ".venv", "venv", "env",
    "build", "dist", "__pycache__",
    "*.egg-info",
    "_viewer",
]

[tool.ruff.lint]
select = [
    "E",   # pycodestyle errors
    "W",   # pycodestyle warnings
    "F",   # pyflakes
    "I",   # isort
    "UP",  # pyupgrade
    "B",   # flake8-bugbear
    "C4",  # flake8-comprehensions
    "SIM", # flake8-simplify
    "RUF", # ruff-specific rules
]
ignore = [
    "E501",   # line-too-long — handled by formatter
    "B008",   # do-not-perform-function-call-in-default-argument
    "SIM108", # ternary — sometimes less readable
]

[tool.ruff.lint.isort]
known-first-party = ["pyrere", "pyrere_scripts"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
line-ending  = "auto"


# ── bandit ────────────────────────────────────────────────────────────────────
[tool.bandit]
exclude_dirs = ["tests", "_viewer", ".venv", "venv", "build", "dist"]
skips = ["B101", "B603", "B607"]


# ── vulture ───────────────────────────────────────────────────────────────────
[tool.vulture]
min_confidence = 70
paths          = ["pyrere", "pyrere_scripts"]
exclude        = ["_viewer/", ".venv/", "venv/", "build/", "dist/"]


# ── mypy ──────────────────────────────────────────────────────────────────────
[tool.mypy]
python_version         = "3.10"
strict                 = false
ignore_missing_imports = true
warn_unused_ignores    = true
warn_return_any        = false
exclude                = ["_viewer/", "build/", "dist/"]


# ── pytest ────────────────────────────────────────────────────────────────────
[tool.pytest.ini_options]
testpaths    = ["tests"]
addopts      = "-ra -q --tb=short"
python_files = ["test_*.py", "*_test.py"]

# --- FILE: .gitignore ---

### Python template
# Byte-compiled / optimized / DLL files
__pycache__/
*.py[cod]
*$py.class

# C extensions
*.so

# Distribution / packaging
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# files and folders
.idea/
access/
data_intelligence/
download_models_hf.py
python-client-fixed.zip
python-client-generated.zip
refresh_token.txt
temp
test_users.json
rest_db_and_import_output.txt

# PyInstaller
#  Usually these files are written by a python script from a template
#  before PyInstaller builds the exe, so as to inject date/other infos into it.
*.manifest
*.spec

# Installer logs
pip-log.txt
pip-delete-this-directory.txt

# Unit test / coverage reports
htmlcov/
.tox/
.coverage
.coverage.*
.cache
nosetests.xml
coverage.xml
*.cover
.hypothesis/

# Translations
*.mo
*.pot

# Django stuff:
staticfiles/

# Sphinx documentation
docs/_build/

# PyBuilder
target/

# pyenv
.python-version

# celery beat schedule file
celerybeat-schedule

# Environments
.venv
.env
venv/
ENV/

# Rope project settings
.ropeproject

# mkdocs documentation
/site

# mypy
.mypy_cache/


### Node template
# Logs
logs
*.log
npm-debug.log*
yarn-debug.log*
yarn-error.log*

# Runtime data
pids
*.pid
*.seed
*.pid.lock

# Directory for instrumented libs generated by jscoverage/JSCover
lib-cov

# Coverage directory used by tools like istanbul
coverage

# nyc test coverage
.nyc_output

# Bower dependency directory (https://bower.io/)
bower_components

# node-waf configuration
.lock-wscript

# Compiled binary addons (http://nodejs.org/api/addons.html)
build/Release

# Dependency directories
node_modules/
jspm_packages/

# Typescript v1 declaration files
typings/

# Optional npm cache directory
.npm

# Optional eslint cache
.eslintcache

# Optional REPL history
.node_repl_history

# Output of 'npm pack'
*.tgz

# Yarn Integrity file
.yarn-integrity


### Linux template
*~

# temporary files which can be created if a process still has a handle open of a deleted file
.fuse_hidden*

# KDE directory preferences
.directory

# Linux trash folder which might appear on any partition or disk
.Trash-*

# .nfs files are created when an open file is removed but is still being accessed
.nfs*


### VisualStudioCode template
.vscode/*
!.vscode/settings.json
!.vscode/tasks.json
!.vscode/launch.json
!.vscode/extensions.json
*.code-workspace

# Local History for devcontainer
.devcontainer/bash_history




### Windows template
# Windows thumbnail cache files
Thumbs.db
ehthumbs.db
ehthumbs_vista.db

# Dump file
*.stackdump

# Folder config file
Desktop.ini

# Recycle Bin used on file shares
$RECYCLE.BIN/

# Windows Installer files
*.cab
*.msi
*.msm
*.msp

# Windows shortcuts
*.lnk


### macOS template
# General
*.DS_Store
.AppleDouble
.LSOverride

# Icon must end with two \r
Icon

# Thumbnails
._*

# Files that might appear in the root of a volume
.DocumentRevisions-V100
.fseventsd
.Spotlight-V100
.TemporaryItems
.Trashes
.VolumeIcon.icns
.com.apple.timemachine.donotpresent

# Directories potentially created on remote AFP share
.AppleDB
.AppleDesktop
Network Trash Folder
Temporary Items
.apdisk


### SublimeText template
# Cache files for Sublime Text
*.tmlanguage.cache
*.tmPreferences.cache
*.stTheme.cache

# Workspace files are user-specific
*.sublime-workspace

# Project files should be checked into the repository, unless a significant
# proportion of contributors will probably not be using Sublime Text
# *.sublime-project

# SFTP configuration file
sftp-config.json

# Package control specific files
Package Control.last-run
Package Control.ca-list
Package Control.ca-bundle
Package Control.system-ca-bundle
Package Control.cache/
Package Control.ca-certs/
Package Control.merged-ca-bundle
Package Control.user-ca-bundle
oscrypto-ca-bundle.crt
bh_unicode_properties.cache

# Sublime-github package stores a github token in this file
# https://packagecontrol.io/packages/sublime-github
GitHub.sublime-settings


### Vim template
# Swap
[._]*.s[a-v][a-z]
[._]*.sw[a-p]
[._]s[a-v][a-z]
[._]sw[a-p]

# Session
Session.vim

# Temporary
.netrwhist

# Auto-generated tag files
tags

# Redis dump file
dump.rdb

### Project template
iinfii/media/

.pytest_cache/

dag_repo/
llamacpp_cache.json
models/
# Run data (user-specific, can be large)
runs/
assessments/
task_id_map.json

# FileBasedLLM communication files
llm_prompts.txt
llm_responses.txt
concat.py
bundle.py


# --- FILE: .grimp_cache/.gitignore ---

# Automatically created by Grimp.
*

# --- FILE: .ruff_cache/.gitignore ---

# Automatically created by ruff.
*


# --- FILE: LICENSE ---

MIT License

Copyright (c) 2026 3IVIS

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
