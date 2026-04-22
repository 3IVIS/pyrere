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
