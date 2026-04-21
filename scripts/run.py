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