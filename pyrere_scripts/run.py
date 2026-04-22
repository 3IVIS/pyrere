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
