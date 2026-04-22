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
    stack: list[tuple] = [(tree.root_node, file_id, [])]

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
            imported_names: list[str] = []

            # Find the index of the "import" keyword to split the statement
            # into the module half (left) and the names half (right).
            # This is stable across tree-sitter-python versions, unlike field names.
            import_kw_idx = next(
                (i for i, c in enumerate(node.children) if c.type == "import"),
                None,
            )

            for i, child in enumerate(node.children):
                ctype = child.type

                if ctype in ("from", "import", ",", "(", ")"):
                    continue

                if import_kw_idx is not None and i < import_kw_idx:
                    # ── module / level half ───────────────────────────────────
                    if ctype == "relative_import":
                        # Older tree-sitter-python wraps dots + name here
                        for rc in child.children:
                            if rc.type == "import_prefix":
                                level = len(_cached_text(code_bytes, rc, text_cache))
                            elif rc.type == "dotted_name":
                                mod_str = _cached_text(code_bytes, rc, text_cache)
                    elif ctype == "dotted_name":
                        mod_str = _cached_text(code_bytes, child, text_cache)
                    elif ctype == "import_prefix":
                        # Newer versions may expose dots directly without wrapper
                        level = len(_cached_text(code_bytes, child, text_cache))
                else:
                    # ── names half ────────────────────────────────────────────
                    if ctype == "wildcard_import":
                        imported_names.append("*")
                    elif ctype == "dotted_name":
                        imported_names.append(_cached_text(code_bytes, child, text_cache))
                    elif ctype == "aliased_import":
                        n = child.child_by_field_name("name")
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
