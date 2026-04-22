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