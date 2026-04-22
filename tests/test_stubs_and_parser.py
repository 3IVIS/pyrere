"""
Tests for:
  - pyrere/context/__init__.py  — not-yet-implemented stub
  - pyrere/llm/__init__.py      — not-yet-implemented stub
  - pyrere/parsing/parser.py    — get_parser()
"""

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# pyrere.context — stub module
# ─────────────────────────────────────────────────────────────────────────────


class TestContextStub:
    def test_import_succeeds(self):
        import pyrere.context  # noqa: F401

    def test_attribute_access_raises(self):
        import pyrere.context

        with pytest.raises(NotImplementedError, match=r"pyrere\.context"):
            _ = pyrere.context.anything

    def test_error_message_contains_attribute_name(self):
        import pyrere.context

        with pytest.raises(NotImplementedError, match="build_context"):
            _ = pyrere.context.build_context


# ─────────────────────────────────────────────────────────────────────────────
# pyrere.llm — stub module
# ─────────────────────────────────────────────────────────────────────────────


class TestLlmStub:
    def test_import_succeeds(self):
        import pyrere.llm  # noqa: F401

    def test_attribute_access_raises(self):
        import pyrere.llm

        with pytest.raises(NotImplementedError, match=r"pyrere\.llm"):
            _ = pyrere.llm.anything

    def test_error_message_contains_attribute_name(self):
        import pyrere.llm

        with pytest.raises(NotImplementedError, match="refactor"):
            _ = pyrere.llm.refactor


# ─────────────────────────────────────────────────────────────────────────────
# pyrere.parsing.parser — get_parser()
# ─────────────────────────────────────────────────────────────────────────────


class TestGetParser:
    def test_returns_parser(self):
        from tree_sitter import Parser

        from pyrere.parsing.parser import get_parser

        parser = get_parser()
        assert isinstance(parser, Parser)

    def test_parser_can_parse_python(self):
        from pyrere.parsing.parser import get_parser

        parser = get_parser()
        tree = parser.parse(b"x = 1 + 2\n")
        assert tree is not None
        assert tree.root_node.type == "module"

    def test_parser_handles_empty_input(self):
        from pyrere.parsing.parser import get_parser

        parser = get_parser()
        tree = parser.parse(b"")
        assert tree.root_node is not None

    def test_parser_handles_syntax_error_gracefully(self):
        """tree-sitter is error-tolerant — it should not raise on bad syntax."""
        from pyrere.parsing.parser import get_parser

        parser = get_parser()
        tree = parser.parse(b"def (((broken syntax !!!")
        assert tree is not None
