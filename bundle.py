

# --- FILE: scripts/__init__.py ---



# --- FILE: scripts/run.py ---

import os
import sys
import json
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, HTTPServer

from src.aggregator.builder import build_graph

VIEWER_DIR = os.path.abspath("viewer")
PORT = 8000

def make_relative(path, repo_root):
    if not path:
        return "__external__"
    return os.path.relpath(path, repo_root).replace("\\", "/")

def export_graph(graph, repo_root):
    data = {
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "type": n.type,
                "file": make_relative(n.file, repo_root),
            }
            for n in graph.nodes.values()
        ],
        "edges": [
            {
                "source": e.src,
                "target": e.dst,
                "type": e.type,
            }
            for e in graph.edges.values()
        ],
        "repo_root": "",  # now unnecessary OR keep as display only
    }

    with open(os.path.join(VIEWER_DIR, "graph.json"), "w") as f:
        json.dump(data, f, indent=2)


def start_server():
    os.chdir(VIEWER_DIR)
    server = HTTPServer(("localhost", PORT), SimpleHTTPRequestHandler)
    print(f"Serving at http://localhost:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    repo_path = sys.argv[1] if len(sys.argv) > 1 else "."

    graph = build_graph(repo_path)
    export_graph(graph, repo_path)

    thread = threading.Thread(target=start_server, daemon=True)
    thread.start()

    webbrowser.open(f"http://localhost:{PORT}")

    thread.join()

# --- FILE: src/aggregator/__init__.py ---



# --- FILE: src/aggregator/builder.py ---

import os
from typing import List

from src.graph.models import CodeGraph, Node, Edge
from src.parsing.parser import get_parser
from src.ingestion.loader import load_python_files
from src.symbols.extractor import extract_symbols, make_id, ImportRef


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

# --- FILE: src/coddingtoddly.egg-info/__init__.py ---



# --- FILE: src/context/__init__.py ---



# --- FILE: src/enrichment/__init__.py ---



# --- FILE: src/flow/__init__.py ---



# --- FILE: src/graph/__init__.py ---



# --- FILE: src/graph/models.py ---

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional


@dataclass
class Node:
    id: str
    name: str
    type: str
    file: str
    span: Tuple[int, int]
    signature: Optional[dict] = None
    metadata: dict = field(default_factory=dict)
    sources: List[str] = field(default_factory=list)


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    type: str
    confidence: float = 1.0
    sources: List[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


@dataclass
class CodeGraph:
    nodes: Dict[str, Node] = field(default_factory=dict)
    edges: Dict[str, Edge] = field(default_factory=dict)

    def add_node(self, node: Node):
        self.nodes[node.id] = node

    def add_edge(self, edge: Edge):
        self.edges[edge.id] = edge

# --- FILE: src/ingestion/__init__.py ---



# --- FILE: src/ingestion/loader.py ---

import os

# Directories that are never source code and should never be walked.
# Pruned in-place so os.walk doesn't descend into them.
_SKIP_DIRS = {
    "__pycache__",
    ".git", ".hg", ".svn",
    ".tox", ".nox",
    ".mypy_cache", ".ruff_cache", ".pytype",
    ".pytest_cache", ".hypothesis",
    "node_modules",
    "venv", ".venv", "env", ".env",
    "build", "dist",
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

# --- FILE: src/llm/__init__.py ---



# --- FILE: src/parsing/__init__.py ---



# --- FILE: src/parsing/parser.py ---

from tree_sitter import Parser

# ── Language loading ──────────────────────────────────────────────────────────
# Supports both the legacy tree-sitter-languages bundle (tree-sitter < 0.22)
# and the modern per-language packages (tree-sitter >= 0.22).
try:
    from tree_sitter_languages import get_language  # type: ignore
    PY_LANGUAGE = get_language("python")
    _LEGACY_API = True
except ImportError:
    from tree_sitter import Language  # type: ignore
    import tree_sitter_python as tspython  # type: ignore
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

# --- FILE: src/relationships/__init__.py ---



# --- FILE: src/symbols/__init__.py ---



# --- FILE: src/symbols/extractor.py ---

from typing import List, NamedTuple
from src.graph.models import Node, Edge
import hashlib
import re


# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def make_id(*parts) -> str:
    return hashlib.md5(":".join(map(str, parts)).encode()).hexdigest()


def _text(code_bytes: bytes, node) -> str:
    """Extract UTF-8 text via tree-sitter *byte* offsets (never char offsets)."""
    return code_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _collect_type_names(code_bytes: bytes, node, cache: dict) -> List[str]:
    """
    Recursively collect every identifier inside a type annotation subtree.
    Handles simple names, generics (Optional[X]), unions (X | Y), attributes, etc.
    Built-in names (int, str, …) silently fail to resolve in builder — fine.
    """
    if node is None:
        return []
    names: List[str] = []
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
    level:  int        # 0 = absolute; 1 = from .; 2 = from ..; …
    module: str        # dotted module string after the dots (may be "")
    names:  List[str]  # specific symbols imported; ["*"] = wildcard; [] = bare import


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
                        return raw[len(q):-len(q)].strip()
                return raw.strip()
        break  # docstring must be the very first statement
    return ""


def _extract_parameters(code_bytes: bytes, params_node, text_cache: dict) -> List[str]:
    """
    Extract parameter names/annotations from a `parameters` or
    `lambda_parameters` node.  Includes *args and **kwargs.
    """
    if params_node is None:
        return []
    params: List[str] = []
    skip = {"(", ")", ","}
    for child in params_node.children:
        if child.type in skip:
            continue
        t = child.type
        if t == "identifier":
            params.append(_cached_text(code_bytes, child, text_cache))
        elif t in ("typed_parameter", "typed_default_parameter",
                   "default_parameter"):
            params.append(_cached_text(code_bytes, child, text_cache))
        elif t == "list_splat_pattern":   # *args
            params.append(_cached_text(code_bytes, child, text_cache))
        elif t == "dictionary_splat_pattern":  # **kwargs
            params.append(_cached_text(code_bytes, child, text_cache))
    return params


def _cyclomatic_complexity(code_bytes: bytes, body_node, text_cache: dict) -> int:
    """
    Approximate cyclomatic complexity by counting branch-creating keywords
    in the function/class body text.
    Adopted from external extractor's _calculate_complexity_optimized.
    """
    if body_node is None:
        return 1
    body_text = _cached_text(code_bytes, body_node, text_cache).lower()
    keywords = ["if", "elif", "while", "for", "except", "and", "or",
                "with", "match", "case"]
    complexity = 1
    for kw in keywords:
        complexity += len(re.findall(rf"\b{kw}\b", body_text))
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
      nodes          – function / class / variable Node objects
      edges          – contains edges (intra-file structural edges)
      import_refs    – ImportRef list (absolute + relative)
      call_refs      – list of (caller_id, callee_name)
      inherit_refs   – list of (class_id, base_name)
      decorator_refs – list of (decorated_id, decorator_name)
      type_refs      – list of (user_id, type_name)
    """
    nodes:          list = []
    edges:          list = []
    import_refs:    List[ImportRef] = []
    call_refs:      List[tuple] = []
    inherit_refs:   List[tuple] = []
    decorator_refs: List[tuple] = []
    type_refs:      List[tuple] = []

    code_bytes = code.encode("utf-8")

    # ── per-call caches (adopted from external extractor) ─────────────────────
    text_cache: dict  = {}   # (start_byte, end_byte) → str
    processed:  set   = set()   # node ids already handled (avoids duplicates)

    # ── iterative stack ───────────────────────────────────────────────────────
    # Each entry: (node, scope_id, pending_dec_names)
    # pending_dec_names — decorator names collected from an enclosing
    # decorated_definition, to be attached once the inner def/class is emitted.
    stack = [(tree.root_node, file_id, [])]

    while stack:
        node, scope_id, pending_decs = stack.pop()
        ntype = node.type

        # ── early-exit for uninteresting nodes ────────────────────────────────
        if (ntype not in _TARGET_TYPES
                and ntype not in _CONTAINER_TYPES
                and node is not tree.root_node):
            continue

        nid = id(node)  # CPython object identity — unique per live node

        # ── @decorator … def/class … ─────────────────────────────────────────
        if ntype == "decorated_definition":
            dec_names: List[str] = []
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

                nodes.append(Node(
                    id=node_id,
                    name="<lambda>",
                    type="function",
                    file=file_path,
                    span=(node.start_point[0], node.end_point[0]),
                    metadata={
                        "is_lambda":    True,
                        "is_async":     False,
                        "is_generator": False,
                        "visibility":   "private",
                        "parameters":   params,
                        "return_type":  None,
                        "docstring":    "",
                        "complexity":   1,
                    },
                    sources=["tree_sitter"],
                ))
                edges.append(Edge(
                    id=make_id(scope_id, node_id, "contains"),
                    src=scope_id, dst=node_id, type="contains",
                    confidence=1.0, sources=["tree_sitter"],
                ))
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

                name     = _cached_text(code_bytes, name_node, text_cache)
                node_id  = make_id(file_path, name, node.start_point)
                body     = node.child_by_field_name("body")
                params_n = node.child_by_field_name("parameters")

                # is_async: `async def` has an [async] child before [def]
                is_async = any(c.type == "async" for c in node.children)

                # is_generator: any `yield` node in body text
                body_text    = _cached_text(code_bytes, body, text_cache) if body else ""
                is_generator = bool(re.search(r"\byield\b", body_text))

                # visibility from name convention
                if name.startswith("__") and name.endswith("__"):
                    visibility = "magic"
                elif name.startswith("_"):
                    visibility = "private"
                else:
                    visibility = "public"

                # is_static / is_classmethod / is_property from pending decorators
                is_static      = "staticmethod"  in pending_decs
                is_classmethod = "classmethod"   in pending_decs
                is_property    = "property"      in pending_decs

                params = _extract_parameters(code_bytes, params_n, text_cache)

                ret_node    = node.child_by_field_name("return_type")
                return_type = _cached_text(code_bytes, ret_node, text_cache).lstrip("->").strip() if ret_node else None

                docstring   = _extract_docstring(code_bytes, body, text_cache)
                complexity  = _cyclomatic_complexity(code_bytes, body, text_cache)

                nodes.append(Node(
                    id=node_id, name=name, type="function",
                    file=file_path,
                    span=(node.start_point[0], node.end_point[0]),
                    metadata={
                        "is_lambda":     False,
                        "is_async":      is_async,
                        "is_generator":  is_generator,
                        "is_static":     is_static,
                        "is_classmethod":is_classmethod,
                        "is_property":   is_property,
                        "visibility":    visibility,
                        "parameters":    params,
                        "return_type":   return_type,
                        "docstring":     docstring,
                        "complexity":    complexity,
                    },
                    sources=["tree_sitter"],
                ))
                edges.append(Edge(
                    id=make_id(scope_id, node_id, "contains"),
                    src=scope_id, dst=node_id, type="contains",
                    confidence=1.0, sources=["tree_sitter"],
                ))

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

                name    = _cached_text(code_bytes, name_node, text_cache)
                node_id = make_id(file_path, name, node.start_point)
                body    = node.child_by_field_name("body")

                # superclasses
                super_names: List[str] = []
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

                body_raw     = _cached_text(code_bytes, body, text_cache) if body else ""
                docstring    = _extract_docstring(code_bytes, body, text_cache)
                complexity   = _cyclomatic_complexity(code_bytes, body, text_cache)
                is_dataclass = "dataclass" in pending_decs
                is_abstract  = ("ABC" in super_names
                                or "ABCMeta" in super_names
                                or "abstractmethod" in body_raw)
                is_exception = any("Exception" in s or "Error" in s
                                   for s in super_names)

                nodes.append(Node(
                    id=node_id, name=name, type="class",
                    file=file_path,
                    span=(node.start_point[0], node.end_point[0]),
                    metadata={
                        "superclasses":  super_names,
                        "is_dataclass":  is_dataclass,
                        "is_abstract":   is_abstract,
                        "is_exception":  is_exception,
                        "docstring":     docstring,
                        "complexity":    complexity,
                    },
                    sources=["tree_sitter"],
                ))
                edges.append(Edge(
                    id=make_id(scope_id, node_id, "contains"),
                    src=scope_id, dst=node_id, type="contains",
                    confidence=1.0, sources=["tree_sitter"],
                ))

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
                    import_refs.append(ImportRef(
                        level=0, module=_cached_text(code_bytes, name_node, text_cache), names=[],
                    ))
                elif name_node.type == "aliased_import":
                    n = name_node.child_by_field_name("name")
                    if n:
                        import_refs.append(ImportRef(
                            level=0, module=_cached_text(code_bytes, n, text_cache), names=[],
                        ))
            continue

        # ── from [..][module] import name1, name2, … ──────────────────────────
        if ntype == "import_from_statement":
            level   = 0
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
            imported_names: List[str] = []
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
        type_str  = _cached_text(code_bytes, type_node, text_cache).strip() if type_node else None

        var_id = make_id(file_path, var_name, assignment.start_point)
        nodes.append(Node(
            id=var_id,
            name=var_name,
            type="variable",
            file=file_path,
            span=(assignment.start_point[0], assignment.end_point[0]),
            metadata={"annotation": type_str},
            sources=["tree_sitter"],
        ))
        edges.append(Edge(
            id=make_id(class_node_id, var_id, "contains"),
            src=class_node_id, dst=var_id, type="contains",
            confidence=1.0, sources=["tree_sitter"],
        ))

# --- FILE: src/utils/__init__.py ---



# --- FILE: viewer/__init__.py ---



# --- FILE: viewer/index.html ---

<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CKG Viewer</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
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
/* small coloured dot indicating it's a .py file */
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

/* node-type swatch: a small shape mirroring vis.js */
.swatch {
  display: inline-block;
  width: 13px;
  height: 13px;
  border: 2px solid transparent;
  flex-shrink: 0;
}

/* edge-type swatch: a short coloured line */
.swatch-line {
  display: inline-block;
  width: 24px;
  height: 3px;
  border-radius: 2px;
  flex-shrink: 0;
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
  max-width: 340px !important;
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

# --- FILE: viewer/app.js ---

/* ─────────────────────────────────────────────────────────────────────────────
   CKG VIEWER  –  app.js
   ───────────────────────────────────────────────────────────────────────────── */

"use strict";

// ── design tokens ─────────────────────────────────────────────────────────────
//
// Light-background nodes: pastel fill + strong-coloured border + dark text.
// This reads better at small label sizes and keeps the canvas feeling airy.

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
      background: '#F0F9FF',
      border: '#0284C7',
      highlight: { background: '#E0F2FE', border: '#0369A1' },
      hover:     { background: '#E0F2FE', border: '#0369A1' },
    },
    shape: 'triangleDown',
    font:  { color: '#0C4A6E', size: 11, face: 'ui-sans-serif, system-ui, sans-serif' },
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

// Semantic edge palette:
//  slate  = structural containment (quiet, doesn't compete)
//  blue   = import relationships
//  orange = execution / calls
//  violet = inheritance hierarchy
//  pink   = decoration / modification
//  cyan   = type-system usage
const EDGE_STYLES = {
  contains:       ["#CBD5E1", false],  // slate-300   — thin, structural
  imports:        ["#2563EB", false],  // blue-600    — file dependency
  imports_symbol: ["#93C5FD", true ],  // blue-300    — symbol import, dashed
  calls:          ["#EA580C", false],  // orange-600  — function call
  inherits:       ["#7C3AED", false],  // violet-600  — class hierarchy
  decorates:      ["#DB2777", false],  // pink-600    — decorator
  uses_type:      ["#0891B2", true ],  // cyan-600    — type annotation, dashed
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

/**
 * Renders a tree level into `container`.
 * Folders always appear before files; each group is sorted alphabetically.
 * Top-level folders (depth 0) are expanded by default.
 */
function renderTree(node, container, path = "", depth = 0) {
  const ul = document.createElement("ul");

  // Separate folders from files, sort each group alpha, then concat
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
      // ── folder ──────────────────────────────────────────────────────────────
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
      // ── file ─────────────────────────────────────────────────────────────────
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
  // Mirror the vis.js shape loosely
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

function nodeOptions(type, isImported = false) {
  const base = { ...(NODE_STYLES[type] ?? NODE_STYLES_EXTRA[type] ?? NODE_STYLE_DEFAULT) };
  if (!isImported) return base;
  return {
    ...base,
    color: {
      ...base.color,
      background: base.color.background,
      border:     base.color.border + "88",   // semi-transparent border
    },
    opacity:      0.55,
    borderDashes: [5, 4],
    borderWidth:  1,
  };
}

function renderGraph() {
  // ── lookup tables ──────────────────────────────────────────────────────────
  const nodeById       = new Map(fullData.nodes.map((n) => [n.id, n]));
  const moduleIdByFile = new Map();
  const fileByModuleId = new Map();
  for (const n of fullData.nodes) {
    if (n.type === "module" && n.file) {
      moduleIdByFile.set(n.file, n.id);
      fileByModuleId.set(n.id, n.file);
    }
  }

  // Adjacency list: moduleId → [targetModuleId]
  const importAdj = new Map();
  for (const e of fullData.edges) {
    if (e.type !== "imports") continue;
    if (!importAdj.has(e.source)) importAdj.set(e.source, []);
    importAdj.get(e.source).push(e.target);
  }

  // ── one-hop import expansion ───────────────────────────────────────────────
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

  // ── node set ───────────────────────────────────────────────────────────────
  const nodes = fullData.nodes.filter(
    (n) => allVisibleFiles.has(n.file) && selectedNodeTypes.has(n.type)
  );
  const nodeIds = new Set(nodes.map((n) => n.id));

  // Clicked node: pull in all directly connected neighbours
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

  // ── edge set ───────────────────────────────────────────────────────────────
  const edges = fullData.edges.filter(
    (e) => nodeIds.has(e.source) && nodeIds.has(e.target) && selectedEdgeTypes.has(e.type)
  );

  // ── status bar ─────────────────────────────────────────────────────────────
  const statusEl = document.getElementById("status");
  if (statusEl) statusEl.textContent = `${nodes.length} nodes · ${edges.length} edges`;

  // ── vis datasets ───────────────────────────────────────────────────────────
  const visNodes = new vis.DataSet(
    nodes.map((n) => {
      const isImported = importedFiles.has(n.file) && !selectedFiles.has(n.file);
      return {
        id:    n.id,
        label: n.name,
        title: (() => {
          // vis.js renders a DOM element as HTML; a plain string is plain text.
          const m = n.metadata ?? {};
          const el = document.createElement("div");
          el.style.cssText = "font-family:ui-sans-serif,system-ui,sans-serif;font-size:12px;line-height:1.5;max-width:320px;padding:2px 0";

          const heading = document.createElement("div");
          heading.style.cssText = "font-weight:700;font-size:13px;margin-bottom:2px";
          heading.textContent = n.name;
          el.appendChild(heading);

          const sub = document.createElement("div");
          sub.style.cssText = "color:#64748b;font-size:11px";
          sub.textContent = `${n.type} · ${n.file ?? ""}`;
          el.appendChild(sub);

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
            ds.style.cssText = "color:#64748b;font-size:11px;font-style:italic;margin-top:3px;border-top:1px solid #f1f5f9;padding-top:3px";
            const preview = m.docstring.length > 120 ? m.docstring.slice(0, 120) + "…" : m.docstring;
            ds.textContent = preview;
            el.appendChild(ds);
          }

          return el;
        })(),
        ...nodeOptions(n.type, isImported),
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

# --- FILE: pyproject.toml ---

[project]
name = "coddingtoddly"
version = "0.1.0"
dependencies = [
    # ── Option A: legacy bundle (simpler install) ───────────────────────────
    # tree-sitter-languages only works with tree-sitter < 0.22.
    # parser.py will use the Parser() + set_language() API.
    "tree-sitter>=0.21,<0.22",
    "tree-sitter-languages>=1.10.2",

    # ── Option B: modern per-language packages ──────────────────────────────
    # Uncomment these and comment out Option A to use tree-sitter >= 0.22.
    # parser.py auto-detects which API to use.
    # "tree-sitter>=0.22",
    # "tree-sitter-python>=0.23",
]

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
