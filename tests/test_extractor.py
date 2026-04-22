"""
Tests for pyrere/symbols/extractor.py — make_id, ImportRef,
_cyclomatic_complexity, extract_symbols.
"""

import pytest

from pyrere.symbols.extractor import ImportRef, _cyclomatic_complexity, extract_symbols, make_id
from pyrere.parsing.parser import get_parser

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def parse_and_extract(code: str, file_path: str = "/repo/test.py", file_id: str = "test_id"):
    """Parse *code* and run extract_symbols, returning all seven result lists."""
    parser = get_parser()
    tree = parser.parse(bytes(code, "utf-8"))
    return extract_symbols(tree, code, file_path, file_id)


# ─────────────────────────────────────────────────────────────────────────────
# make_id
# ─────────────────────────────────────────────────────────────────────────────


class TestMakeId:
    def test_returns_hex_string(self):
        result = make_id("a", "b", "c")
        assert isinstance(result, str)
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self):
        assert make_id("x", "y") == make_id("x", "y")

    def test_different_inputs_different_ids(self):
        assert make_id("a") != make_id("b")
        assert make_id("a", "b") != make_id("b", "a")

    def test_single_part(self):
        result = make_id("/path/to/file.py")
        assert len(result) == 32  # MD5 hex digest length

    def test_fips_safe(self):
        """Should not raise ValueError on FIPS-mode systems."""
        make_id("safe", "hash")


# ─────────────────────────────────────────────────────────────────────────────
# ImportRef
# ─────────────────────────────────────────────────────────────────────────────


class TestImportRef:
    def test_absolute_import(self):
        ref = ImportRef(level=0, module="os.path", names=[])
        assert ref.level == 0
        assert ref.module == "os.path"
        assert ref.names == []

    def test_relative_import(self):
        ref = ImportRef(level=1, module="utils", names=["helper"])
        assert ref.level == 1

    def test_wildcard_import(self):
        ref = ImportRef(level=0, module="os", names=["*"])
        assert ref.names == ["*"]

    def test_is_namedtuple(self):
        ref = ImportRef(level=0, module="foo", names=["bar"])
        assert ref[0] == 0
        assert ref[1] == "foo"
        assert ref[2] == ["bar"]


# ─────────────────────────────────────────────────────────────────────────────
# _cyclomatic_complexity  (via extract_symbols since it operates on AST nodes)
# ─────────────────────────────────────────────────────────────────────────────


class TestCyclomaticComplexity:
    def _complexity_of(self, code: str) -> int:
        """Extract the complexity of the first function found in *code*."""
        nodes, *_ = parse_and_extract(code)
        funcs = [n for n in nodes if n.type == "function" and n.name != "<lambda>"]
        assert funcs, "No function node found"
        return funcs[0].metadata["complexity"]

    def test_simple_function_is_one(self):
        code = "def foo():\n    return 1\n"
        assert self._complexity_of(code) == 1

    def test_if_increments(self):
        code = "def foo(x):\n    if x:\n        return 1\n    return 0\n"
        assert self._complexity_of(code) == 2

    def test_for_loop_increments(self):
        code = "def foo(xs):\n    for x in xs:\n        pass\n"
        assert self._complexity_of(code) == 2

    def test_while_increments(self):
        code = "def foo():\n    while True:\n        break\n"
        assert self._complexity_of(code) == 2

    def test_boolean_operator_increments(self):
        code = "def foo(a, b):\n    return a and b\n"
        assert self._complexity_of(code) == 2

    def test_docstring_does_not_inflate(self):
        """Keywords inside docstrings must NOT count as branches."""
        code = (
            'def foo():\n'
            '    """if you call this while for is in the loop, it works"""\n'
            '    return 1\n'
        )
        assert self._complexity_of(code) == 1

    def test_nested_if_counts_each(self):
        code = (
            "def foo(a, b):\n"
            "    if a:\n"
            "        if b:\n"
            "            return 1\n"
            "    return 0\n"
        )
        assert self._complexity_of(code) == 3


# ─────────────────────────────────────────────────────────────────────────────
# extract_symbols — function extraction
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractFunctions:
    def test_simple_function(self):
        nodes, edges, *_ = parse_and_extract("def foo():\n    pass\n")
        funcs = [n for n in nodes if n.type == "function"]
        assert any(n.name == "foo" for n in funcs)

    def test_function_visibility_public(self):
        nodes, *_ = parse_and_extract("def public_fn():\n    pass\n")
        fn = next(n for n in nodes if n.name == "public_fn")
        assert fn.metadata["visibility"] == "public"

    def test_function_visibility_private(self):
        nodes, *_ = parse_and_extract("def _private():\n    pass\n")
        fn = next(n for n in nodes if n.name == "_private")
        assert fn.metadata["visibility"] == "private"

    def test_function_visibility_magic(self):
        nodes, *_ = parse_and_extract("def __init__(self):\n    pass\n")
        fn = next(n for n in nodes if n.name == "__init__")
        assert fn.metadata["visibility"] == "magic"

    def test_async_function(self):
        nodes, *_ = parse_and_extract("async def fetch():\n    pass\n")
        fn = next(n for n in nodes if n.name == "fetch")
        assert fn.metadata["is_async"] is True

    def test_sync_function_not_async(self):
        nodes, *_ = parse_and_extract("def sync():\n    pass\n")
        fn = next(n for n in nodes if n.name == "sync")
        assert fn.metadata["is_async"] is False

    def test_generator_function(self):
        nodes, *_ = parse_and_extract("def gen():\n    yield 1\n")
        fn = next(n for n in nodes if n.name == "gen")
        assert fn.metadata["is_generator"] is True

    def test_non_generator_function(self):
        nodes, *_ = parse_and_extract("def normal():\n    return 1\n")
        fn = next(n for n in nodes if n.name == "normal")
        assert fn.metadata["is_generator"] is False

    def test_function_parameters(self):
        nodes, *_ = parse_and_extract("def foo(a, b, c):\n    pass\n")
        fn = next(n for n in nodes if n.name == "foo")
        assert "a" in fn.metadata["parameters"]
        assert "b" in fn.metadata["parameters"]

    def test_function_return_type(self):
        nodes, *_ = parse_and_extract("def foo() -> int:\n    return 1\n")
        fn = next(n for n in nodes if n.name == "foo")
        assert fn.metadata["return_type"] == "int"

    def test_function_no_return_type(self):
        nodes, *_ = parse_and_extract("def foo():\n    pass\n")
        fn = next(n for n in nodes if n.name == "foo")
        assert fn.metadata["return_type"] is None

    def test_function_docstring(self):
        code = 'def foo():\n    """Does something."""\n    pass\n'
        nodes, *_ = parse_and_extract(code)
        fn = next(n for n in nodes if n.name == "foo")
        assert "Does something" in fn.metadata["docstring"]

    def test_staticmethod_decorator(self):
        code = "@staticmethod\ndef foo():\n    pass\n"
        nodes, *_ = parse_and_extract(code)
        fn = next(n for n in nodes if n.name == "foo")
        assert fn.metadata["is_static"] is True

    def test_classmethod_decorator(self):
        code = "@classmethod\ndef foo(cls):\n    pass\n"
        nodes, *_ = parse_and_extract(code)
        fn = next(n for n in nodes if n.name == "foo")
        assert fn.metadata["is_classmethod"] is True

    def test_contains_edge_created(self):
        _, edges, *_ = parse_and_extract("def foo():\n    pass\n", file_id="mod_id")
        contains = [e for e in edges if e.type == "contains"]
        assert len(contains) >= 1
        assert all(e.src == "mod_id" for e in contains)

    def test_lambda_node(self):
        nodes, *_ = parse_and_extract("f = lambda x: x + 1\n")
        lambdas = [n for n in nodes if n.name == "<lambda>"]
        assert len(lambdas) == 1
        assert lambdas[0].metadata["is_lambda"] is True


# ─────────────────────────────────────────────────────────────────────────────
# extract_symbols — class extraction
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractClasses:
    def test_simple_class(self):
        nodes, *_ = parse_and_extract("class Foo:\n    pass\n")
        classes = [n for n in nodes if n.type == "class"]
        assert any(n.name == "Foo" for n in classes)

    def test_class_superclasses(self):
        nodes, *_ = parse_and_extract("class Child(Base):\n    pass\n")
        cls = next(n for n in nodes if n.name == "Child")
        assert "Base" in cls.metadata["superclasses"]

    def test_abstract_class_via_abc(self):
        nodes, *_ = parse_and_extract("class Foo(ABC):\n    pass\n")
        cls = next(n for n in nodes if n.name == "Foo")
        assert cls.metadata["is_abstract"] is True

    def test_exception_class(self):
        nodes, *_ = parse_and_extract("class MyError(Exception):\n    pass\n")
        cls = next(n for n in nodes if n.name == "MyError")
        assert cls.metadata["is_exception"] is True

    def test_dataclass_decorator(self):
        code = "@dataclass\nclass Point:\n    x: int\n    y: int\n"
        nodes, *_ = parse_and_extract(code)
        cls = next(n for n in nodes if n.name == "Point")
        assert cls.metadata["is_dataclass"] is True

    def test_class_attribute_extracted(self):
        code = "class Foo:\n    x = 1\n"
        nodes, *_ = parse_and_extract(code)
        var_nodes = [n for n in nodes if n.type == "variable"]
        assert any(n.name == "x" for n in var_nodes)

    def test_inherit_refs_populated(self):
        _, _, _, _, inherit_refs, *_ = parse_and_extract("class Child(Base):\n    pass\n")
        assert any(base == "Base" for _, base in inherit_refs)

    def test_decorator_refs_populated(self):
        code = "@dataclass\nclass Foo:\n    pass\n"
        _, _, _, _, _, decorator_refs, _ = parse_and_extract(code)
        assert any(dec == "dataclass" for _, dec in decorator_refs)


# ─────────────────────────────────────────────────────────────────────────────
# extract_symbols — import extraction
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractImports:
    def test_absolute_import(self):
        _, _, import_refs, *_ = parse_and_extract("import os\n")
        assert any(r.level == 0 and r.module == "os" for r in import_refs)

    def test_from_import(self):
        _, _, import_refs, *_ = parse_and_extract("from os.path import join\n")
        ref = next(r for r in import_refs if r.module == "os.path")
        assert "join" in ref.names

    def test_relative_import_level_1(self):
        _, _, import_refs, *_ = parse_and_extract("from . import utils\n")
        ref = next(r for r in import_refs if r.level == 1)
        assert "utils" in ref.names

    def test_relative_import_level_2(self):
        _, _, import_refs, *_ = parse_and_extract("from .. import base\n")
        ref = next(r for r in import_refs if r.level == 2)
        assert "base" in ref.names

    def test_wildcard_import(self):
        _, _, import_refs, *_ = parse_and_extract("from os import *\n")
        ref = next(r for r in import_refs if r.module == "os")
        assert "*" in ref.names

    def test_aliased_import(self):
        _, _, import_refs, *_ = parse_and_extract("import numpy as np\n")
        assert any(r.module == "numpy" for r in import_refs)

    def test_multiple_imports(self):
        code = "import os\nimport sys\nfrom pathlib import Path\n"
        _, _, import_refs, *_ = parse_and_extract(code)
        modules = {r.module for r in import_refs}
        assert "os" in modules
        assert "sys" in modules
        assert "pathlib" in modules


# ─────────────────────────────────────────────────────────────────────────────
# extract_symbols — call & type ref extraction
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractRefs:
    def test_call_ref_captured(self):
        code = "def foo():\n    bar()\n"
        _, _, _, call_refs, *_ = parse_and_extract(code)
        assert any(callee == "bar" for _, callee in call_refs)

    def test_type_ref_from_annotation(self):
        code = "def foo(x: MyClass) -> None:\n    pass\n"
        _, _, _, _, _, _, type_refs = parse_and_extract(code)
        assert any(tname == "MyClass" for _, tname in type_refs)

    def test_type_ref_from_return_annotation(self):
        code = "def foo() -> MyType:\n    pass\n"
        _, _, _, _, _, _, type_refs = parse_and_extract(code)
        assert any(tname == "MyType" for _, tname in type_refs)


# ─────────────────────────────────────────────────────────────────────────────
# edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractEdgeCases:
    def test_empty_file(self):
        nodes, edges, import_refs, call_refs, inherit_refs, decorator_refs, type_refs = (
            parse_and_extract("")
        )
        assert nodes == []
        assert edges == []

    def test_deeply_nested_functions(self):
        code = (
            "def outer():\n"
            "    def middle():\n"
            "        def inner():\n"
            "            pass\n"
        )
        nodes, *_ = parse_and_extract(code)
        names = {n.name for n in nodes if n.type == "function"}
        assert {"outer", "middle", "inner"} == names

    def test_no_duplicate_nodes(self):
        code = "def foo():\n    pass\n\ndef bar():\n    pass\n"
        nodes, *_ = parse_and_extract(code)
        ids = [n.id for n in nodes]
        assert len(ids) == len(set(ids))
