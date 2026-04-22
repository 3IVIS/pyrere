# pyrere (Python Repo Review)

[![PyPI version](https://img.shields.io/pypi/v/pyrere)](https://pypi.org/project/pyrere/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue)](https://pypi.org/project/pyrere/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![CI](https://github.com/3IVIS/pyrere/actions/workflows/ci.yml/badge.svg)](https://github.com/3IVIS/pyrere/actions)

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

# Use a custom port (default: 8000)
pyrere /path/to/your/repo --port 8080
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