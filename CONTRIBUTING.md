# Contributing to pyrere

Thank you for your interest in contributing! pyrere is an open-source project and contributions of all kinds are welcome — bug fixes, new features, documentation improvements, and additional tool integrations.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Project Structure](#project-structure)
- [Adding a New Enrichment Tool](#adding-a-new-enrichment-tool)
- [Adding a New Flow Analyser](#adding-a-new-flow-analyser)
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
- **Add an enrichment tool** — bring in a new semantic analysis backend (see [Adding a New Enrichment Tool](#adding-a-new-enrichment-tool))
- **Add a flow analyser** — integrate a new static linter or security scanner (see [Adding a New Flow Analyser](#adding-a-new-flow-analyser))
- **Improve documentation** — fix typos, clarify explanations, add examples
- **Share a demo run** — submit an interesting interactive snapshot as an example
- **Report a bug** — a well-written issue is a genuine contribution
- **Propose a feature** — open a discussion before building something large

---

## Getting Started

### Prerequisites

- Python 3.10 or higher
- `git` on your PATH
- Node.js 18+ (only required if you want to run the `pyright` enrichment step)

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

# Run a quick smoke test against this repo itself
pyrere .
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
git checkout -b feature/my-new-enrichment-tool
# or: fix/crash-on-empty-graph
# or: docs/clarify-config-options

# 3. Make your changes, commit often
git add .
git commit -m "feat(enrichment): add semgrep integration"

# 4. Push and open a PR against upstream/main
git push origin feature/my-new-enrichment-tool
```

Then open a pull request on GitHub from your branch to `3IVIS/pyrere:main`.

---

## Project Structure

Understanding where things live will help you find the right place for your change:

```
pyrere/
├── __init__.py              # Public API: build_graph, enrich_graph, annotate_graph
├── _viewer/                 # Interactive in-browser graph viewer (HTML/JS/CSS)
│   ├── index.html
│   └── app.js
├── aggregator/
│   └── builder.py           # build_graph() — two-pass tree-sitter graph builder
├── context/
│   └── __init__.py          # Reserved for future context-window layer (not yet implemented)
├── enrichment/
│   ├── __init__.py          # enrich_graph() — orchestrates pyright / grimp / pycg
│   ├── grimp_.py            # Import-graph enrichment via grimp
│   ├── pycg_.py             # Call-graph enrichment via pycg (points-to analysis)
│   └── pyright.py           # Type-diagnostic enrichment via pyright
├── flow/
│   ├── __init__.py          # annotate_graph() — runs static analysis tools
│   └── analyzer.py          # ruff / vulture / bandit integration
├── graph/
│   └── models.py            # CodeGraph, Node, Edge data models
├── ingestion/
│   └── loader.py            # load_python_files() — recursive .py file discovery
├── llm/
│   └── __init__.py          # LLM integration layer
├── parsing/
│   └── parser.py            # get_parser() — tree-sitter parser initialisation
├── relationships/
│   └── __init__.py          # Placeholder (not yet implemented)
├── symbols/
│   └── extractor.py         # extract_symbols(), ImportRef, make_id
└── utils/
    └── spatial.py           # build_spatial_index() — line-number → node lookup
pyrere_scripts/
└── run.py                   # CLI entry point: pyrere [REPO_PATH] [--port PORT]
tests/
docs/
├── architecture.md
├── configuration.md
└── api.md
```

---

## Adding a New Enrichment Tool

Enrichment tools live in `pyrere/enrichment/` and plug into `enrich_graph()` in
`pyrere/enrichment/__init__.py`.  Each tool is optional — a missing install must
be detected at runtime and skipped gracefully with a printed warning.

### Steps

1. **Create `pyrere/enrichment/mytool_.py`** with a single public function:

   ```python
   from pyrere.graph.models import CodeGraph

   def run_mytool(repo_root: str, graph: CodeGraph) -> int:
       """
       Run mytool against repo_root and merge results into graph in-place.
       Returns the number of new edges (or issues) added.
       """
       try:
           import mytool
       except ImportError:
           print("[enrichment] mytool not installed — skipping (pip install mytool)")
           return 0

       # ... your implementation ...
       return added
   ```

2. **Register it in `pyrere/enrichment/__init__.py`**:

   ```python
   from pyrere.enrichment.mytool_ import run_mytool

   def enrich_graph(graph: CodeGraph, repo_root: str) -> dict[str, int]:
       ...
       mytool_count = run_mytool(repo_root, graph)
       return {
           ...,
           "mytool_edges": mytool_count,
       }
   ```

3. **Add a pip extra** in `pyproject.toml`:

   ```toml
   [project.optional-dependencies]
   enrichment = [
       ...,
       "mytool>=1.0",
   ]
   all = [
       ...,
       "mytool>=1.0",
   ]
   ```

4. **Update `README.md`** — add a note to the enrichment table.

5. **Add tests** in `tests/test_enrichment.py`, gated on the tool being installed.

Please open an issue first if you're planning a new enrichment integration — it's a good way to align on the interface before writing code.

---

## Adding a New Flow Analyser

Static analysis / flow tools live in `pyrere/flow/analyzer.py` and are called
from `annotate_graph()`.  Like enrichment tools, each analyser must degrade
gracefully when not installed.

1. Add a runner function in `analyzer.py` that stamps `Node.issues` with
   structured issue dicts (matching the existing `pyright` / `ruff` / `bandit`
   schema).

2. Call it from `annotate_graph()` and include it in the returned summary.

3. Add the dependency to the `flow` and `all` extras in `pyproject.toml`.

4. Add tests in `tests/test_flow.py`.

---

## Testing

```bash
# Run the full test suite
pytest

# Run a specific file
pytest tests/test_graph.py

# Run with coverage
pytest --cov=pyrere

# Run only fast unit tests (skip integration tests that hit real tools)
pytest -m "not integration"
```

If you're adding new functionality, please include:
- **Unit tests** for logic that can be tested in isolation
- **An integration test** (marked `@pytest.mark.integration`) for anything that
  invokes an external tool (pyright, grimp, pycg, ruff, etc.), gated on that
  tool being installed

---

## Commit Style

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>

[optional body]

[optional footer]
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`

**Scopes** (use the relevant module or component):
`aggregator`, `enrichment`, `flow`, `graph`, `ingestion`, `parsing`, `symbols`, `utils`, `viewer`, `context`, `llm`, `cli`

**Examples:**

```
feat(enrichment): add semgrep integration for security annotations
fix(aggregator): handle circular imports in two-pass resolver
docs(flow): document bandit severity mapping
refactor(symbols): extract type-ref collection into its own pass
test(graph): add edge cases for disconnected subgraphs
chore(cli): bump vis-network CDN pin to 9.1.9
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
- **Operating system**
- **What you ran** — the command or Python code you used, and the repo path you pointed it at
- **What you expected** to happen
- **What actually happened** — include the full error traceback if there is one
- **Which optional extras are installed** (`pip show pyright grimp pycg ruff vulture bandit`)

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