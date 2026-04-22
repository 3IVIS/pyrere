import os
from typing import List

from pyrere.graph.models import CodeGraph, Node, Edge
from pyrere.parsing.parser import get_parser
from pyrere.ingestion.loader import load_python_files
from pyrere.symbols.extractor import extract_symbols, make_id, ImportRef


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
) -> List[str]:
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
        is_init = (
            caller_module.endswith(".__init__")
            or caller_module == "__init__"
        )
        if is_init:
            caller_module = (
                caller_module.removesuffix(".__init__")
                              .removesuffix("__init__")
            )
            # __init__.py already represents the package, so each dot strips
            # one *fewer* component compared to a regular file.
            adjusted_level = imp.level - 1
        else:
            adjusted_level = imp.level

        parts = caller_module.split(".") if caller_module else []
        if adjusted_level > len(parts):
            return []   # can't go above the repo root
        base_parts = parts[: -adjusted_level] if adjusted_level > 0 else parts

        if imp.module:
            # from .utils import bar  OR  from ..graph.models import Foo
            target = ".".join(base_parts + imp.module.split("."))
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
                    sub = ".".join(base_parts + [name])
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
            with open(file_path, "r", encoding="utf-8") as fh:
                code = fh.read()
        except (OSError, UnicodeDecodeError):
            continue

        tree = parser.parse(bytes(code, "utf-8"))
        file_id = get_file_id(file_path)

        graph.add_node(Node(
            id=file_id,
            name=os.path.basename(file_path),
            type="module",
            file=file_path,
            span=(0, 0),
            sources=["filesystem"],
        ))

        symbols, edges, import_refs, call_refs, inherit_refs, decorator_refs, type_refs = \
            extract_symbols(tree, code, file_path, file_id)

        for n in symbols:
            graph.add_node(n)
            symbol_index.setdefault(n.name, []).append(n.id)

        for e in edges:
            graph.add_edge(e)

        deferred.append((file_id, file_path, import_refs, call_refs,
                         inherit_refs, decorator_refs, type_refs))

    # ── SECOND PASS: resolve all cross-file relationships ────────────────────
    for file_id, file_path, import_refs, call_refs, \
            inherit_refs, decorator_refs, type_refs in deferred:

        # ── imports → file-level edges ───────────────────────────────────────
        for imp in import_refs:
            resolved_paths = resolve_import_ref(imp, file_path, repo_root, module_index)

            for resolved in resolved_paths:
                target_id = get_file_id(resolved)
                if target_id not in graph.nodes:
                    graph.add_node(Node(
                        id=target_id,
                        name=os.path.basename(resolved),
                        type="module",
                        file=resolved,
                        span=(0, 0),
                        sources=["resolver"],
                    ))
                graph.add_edge(Edge(
                    id=make_id(file_id, target_id, "imports"),
                    src=file_id,
                    dst=target_id,
                    type="imports",
                    confidence=0.95,
                    sources=["resolver"],
                ))

            # imports_symbol: `from module import ClassName` → direct edge to the
            # class/function node so the viewer can show fine-grained dependencies
            for name in imp.names:
                if name == "*":
                    continue
                for sym_id in symbol_index.get(name, []):
                    sym_node = graph.nodes.get(sym_id)
                    if sym_node and sym_node.file in resolved_paths:
                        graph.add_edge(Edge(
                            id=make_id(file_id, sym_id, "imports_symbol"),
                            src=file_id,
                            dst=sym_id,
                            type="imports_symbol",
                            confidence=0.95,
                            sources=["resolver"],
                        ))

        # ── call refs → calls edges ──────────────────────────────────────────
        for caller_id, callee_name in call_refs:
            for callee_id in symbol_index.get(callee_name, []):
                graph.add_edge(Edge(
                    id=make_id(caller_id, callee_id, "calls"),
                    src=caller_id,
                    dst=callee_id,
                    type="calls",
                    confidence=0.8,
                    sources=["tree_sitter"],
                ))

        # ── inherit refs → inherits edges ────────────────────────────────────
        for class_id, base_name in inherit_refs:
            for base_id in symbol_index.get(base_name, []):
                node = graph.nodes.get(base_id)
                if node and node.type == "class":
                    graph.add_edge(Edge(
                        id=make_id(class_id, base_id, "inherits"),
                        src=class_id,
                        dst=base_id,
                        type="inherits",
                        confidence=0.9,
                        sources=["tree_sitter"],
                    ))

        # ── decorator refs → decorates edges ─────────────────────────────────
        # Edge direction: decorator → decorated  (reads "X decorates Y")
        for decorated_id, dec_name in decorator_refs:
            for dec_id in symbol_index.get(dec_name, []):
                graph.add_edge(Edge(
                    id=make_id(dec_id, decorated_id, "decorates"),
                    src=dec_id,
                    dst=decorated_id,
                    type="decorates",
                    confidence=0.9,
                    sources=["tree_sitter"],
                ))

        # ── type refs → uses_type edges ───────────────────────────────────────
        for user_id, type_name in type_refs:
            for type_id in symbol_index.get(type_name, []):
                node = graph.nodes.get(type_id)
                if node and node.type == "class":
                    graph.add_edge(Edge(
                        id=make_id(user_id, type_id, "uses_type"),
                        src=user_id,
                        dst=type_id,
                        type="uses_type",
                        confidence=0.85,
                        sources=["tree_sitter"],
                    ))

    return graph