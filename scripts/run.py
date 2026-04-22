import os
import sys
import json
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, HTTPServer

from pyrere.aggregator.builder import build_graph
from pyrere.enrichment import enrich_graph
from pyrere.flow import annotate_graph

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
                "id":       n.id,
                "name":     n.name,
                "type":     n.type,
                "file":     make_relative(n.file, repo_root),
                # metadata carries complexity, docstring, visibility flags,
                # return_type, parameters, AND the issues list from Steps 4+8
                "metadata": n.metadata,
            }
            for n in graph.nodes.values()
        ],
        "edges": [
            {
                "source":     e.src,
                "target":     e.dst,
                "type":       e.type,
                # confidence and sources are set by both tree-sitter and the
                # enrichment layer; useful for the LLM context builder (Step 9)
                "confidence": round(e.confidence, 4),
                "sources":    e.sources,
            }
            for e in graph.edges.values()
        ],
        "repo_root": "",  # kept for display only; node paths are already relative
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
    repo_path = os.path.abspath(repo_path)

    print("[1/4] Building code knowledge graph …")
    graph = build_graph(repo_path)
    print(f"      {len(graph.nodes)} nodes  {len(graph.edges)} edges")

    print("[2/4] Enriching graph (pyright / grimp / pycg) …")
    enrich_graph(graph, repo_path)
    print(f"      {len(graph.nodes)} nodes  {len(graph.edges)} edges  (after enrichment)")

    print("[3/4] Running static-analysis tools (ruff / vulture / bandit) …")
    annotate_graph(graph, repo_path)

    print("[4/4] Exporting graph + launching viewer …")
    export_graph(graph, repo_path)
    print(f"      Written to {os.path.join(VIEWER_DIR, 'graph.json')}")

    thread = threading.Thread(target=start_server, daemon=True)
    thread.start()

    webbrowser.open(f"http://localhost:{PORT}")

    thread.join()