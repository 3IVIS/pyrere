

# --- FILE: scripts/__init__.py ---



# --- FILE: scripts/run.py ---

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

# --- FILE: src/aggregator/__init__.py ---



# --- FILE: src/aggregator/builder.py ---

import os

from src.graph.models import CodeGraph, Node, Edge
from src.parsing.parser import get_parser
from src.ingestion.loader import load_python_files
from src.symbols.extractor import extract_symbols, make_id


# -----------------------------
# FILE ID (CRITICAL FIX)
# -----------------------------
def get_file_id(path: str):
    return make_id(os.path.abspath(path))


# -----------------------------
# MODULE INDEX
# -----------------------------
def build_module_index(repo_root):
    module_index = {}

    for file_path in load_python_files(repo_root):
        file_path = os.path.abspath(file_path)
        rel = os.path.relpath(file_path, repo_root)

        module_name = rel.replace(os.sep, ".").replace(".py", "")

        if module_name.endswith("__init__"):
            module_name = module_name.rsplit(".", 1)[0]

        module_index[module_name] = file_path

    return module_index


# -----------------------------
# IMPORT RESOLUTION (SAFE)
# -----------------------------
def resolve_import(import_str: str, module_index: dict):
    if not import_str:
        return None

    name = import_str.strip().lstrip(".")

    # exact match
    if name in module_index:
        return module_index[name]

    # safe suffix match (NOT dangerous fuzzy matching)
    for mod, path in module_index.items():
        if mod == name or mod.endswith("." + name):
            return path

    return None


# -----------------------------
# GRAPH BUILDER
# -----------------------------
def build_graph(repo_path: str) -> CodeGraph:
    parser = get_parser()
    graph = CodeGraph()

    repo_root = os.path.abspath(repo_path)
    module_index = build_module_index(repo_root)

    for file_path in load_python_files(repo_root):
        file_path = os.path.abspath(file_path)

        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()

        tree = parser.parse(bytes(code, "utf-8"))

        file_id = get_file_id(file_path)

        # ------------------------
        # FILE NODE
        # ------------------------
        graph.add_node(
            Node(
                id=file_id,
                name=os.path.basename(file_path),
                type="module",
                file=file_path,
                span=(0, 0),
                sources=["filesystem"],
            )
        )

        # ------------------------
        # EXTRACT SYMBOLS
        # ------------------------
        symbols, edges, imports = extract_symbols(
            tree, code, file_path, file_id
        )

        for n in symbols:
            graph.add_node(n)

        for e in edges:
            graph.add_edge(e)

        # ------------------------
        # IMPORT RESOLUTION
        # ------------------------
        for imp in imports:
            resolved = resolve_import(imp, module_index)

            if not resolved:
                continue  # drop external imports for now

            target_id = get_file_id(resolved)

            # ensure target node exists
            if target_id not in graph.nodes:
                graph.add_node(
                    Node(
                        id=target_id,
                        name=os.path.basename(resolved),
                        type="module",
                        file=resolved,
                        span=(0, 0),
                        sources=["resolver"],
                    )
                )

            graph.add_edge(
                Edge(
                    id=make_id(file_id, target_id, "imports"),
                    src=file_id,
                    dst=target_id,
                    type="imports",
                    confidence=0.95,
                    sources=["resolver"],
                )
            )

    return graph

# --- FILE: src/coddingtoddly.egg-info/__init__.py ---



# --- FILE: src/context/__init__.py ---



# --- FILE: src/enrichment/__init__.py ---



# --- FILE: src/flow/__init__.py ---



# --- FILE: src/graph/__init__.py ---



# --- FILE: src/graph/models.py ---

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional


@dataclass
class Node:
    id: str
    name: str
    type: str
    file: str
    span: Tuple[int, int]
    signature: Optional[dict] = None
    metadata: dict = field(default_factory=dict)
    sources: List[str] = field(default_factory=list)


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    type: str
    confidence: float = 1.0
    sources: List[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


@dataclass
class CodeGraph:
    nodes: Dict[str, Node] = field(default_factory=dict)
    edges: Dict[str, Edge] = field(default_factory=dict)

    def add_node(self, node: Node):
        self.nodes[node.id] = node

    def add_edge(self, edge: Edge):
        self.edges[edge.id] = edge

# --- FILE: src/ingestion/__init__.py ---



# --- FILE: src/ingestion/loader.py ---

import os

def load_python_files(repo_path: str):
    for root, _, files in os.walk(repo_path):
        for file in files:
            if file.endswith(".py"):
                yield os.path.join(root, file)

# --- FILE: src/llm/__init__.py ---



# --- FILE: src/parsing/__init__.py ---



# --- FILE: src/parsing/parser.py ---

from tree_sitter import Parser
from tree_sitter_languages import get_language

PY_LANGUAGE = get_language("python")

def get_parser():
    parser = Parser()
    parser.set_language(PY_LANGUAGE)
    return parser

# --- FILE: src/relationships/__init__.py ---



# --- FILE: src/symbols/__init__.py ---



# --- FILE: src/symbols/extractor.py ---

from src.graph.models import Node, Edge
import hashlib


def make_id(*parts):
    return hashlib.md5(":".join(map(str, parts)).encode()).hexdigest()


# -----------------------------
# TREE-SITTER EXTRACTOR (PURE SYNTAX)
# -----------------------------
def extract_symbols(tree, code: str, file_path: str, file_id: str):
    nodes = []
    edges = []
    imports = []

    root = tree.root_node

    def walk(n):
        yield n
        for c in n.children:
            yield from walk(c)

    for node in walk(root):

        # ------------------------
        # FUNCTIONS / CLASSES
        # ------------------------
        if node.type in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue

            name = code[name_node.start_byte:name_node.end_byte]

            node_id = make_id(file_path, name, node.start_point)

            nodes.append(
                Node(
                    id=node_id,
                    name=name,
                    type="function" if node.type == "function_definition" else "class",
                    file=file_path,
                    span=(node.start_point[0], node.end_point[0]),
                    sources=["tree_sitter"],
                )
            )

            edges.append(
                Edge(
                    id=make_id(file_id, node_id, "contains"),
                    src=file_id,
                    dst=node_id,
                    type="contains",
                    confidence=1.0,
                    sources=["tree_sitter"],
                )
            )

        # ------------------------
        # IMPORTS (RAW ONLY)
        # ------------------------
        elif node.type in ("import_statement", "import_from_statement"):
            raw = code[node.start_byte:node.end_byte].strip()
            imports.append(raw)

    return nodes, edges, imports

# --- FILE: src/utils/__init__.py ---



# --- FILE: viewer/__init__.py ---



# --- FILE: viewer/index.html ---

<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
  <title>CKG Viewer</title>
  <script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
  <style>
    body { margin: 0; }
    #graph { width: 100vw; height: 100vh; }
.file-icon::before {
  content: "📄";
  margin-right: 6px;
}

.folder-icon::before {
  content: "📁";
  margin-right: 6px;
}

/* clean tree spacing */
#file-list ul {
  list-style: none;
  padding-left: 16px;
  margin: 4px 0;
}

#file-list li {
  margin: 2px 0;
  line-height: 1.4;
}

.folder {
  user-select: none;
}

#resizer {
  width: 5px;
  cursor: col-resize;
  background: #eee;
}

#resizer:hover {
  background: #ccc;
}
  </style>
</head>
<body>
    <div style="display: flex; height: 100vh;">
    
    <div id="sidebar" style="width: 260px; overflow: auto; border-right: 1px solid #ccc; padding: 10px;">
        <h3>Files</h3>
        <div id="file-list"></div>

        <hr>

        <h3>Filters</h3>
        <div id="type-filters"></div>
    </div>

    <!-- 👇 draggable divider -->
    <div id="resizer"></div>

    <div id="graph" style="flex: 1;"></div>
    </div>

  <script src="app.js"></script>
</body>
</html>

# --- FILE: viewer/app.js ---

let fullData = null;
let network = null;
let selectedFiles = new Set();
let selectedNodeTypes = new Set();
let selectedEdgeTypes = new Set();

function initResizer() {
  const resizer = document.getElementById("resizer");
  const sidebar = document.getElementById("sidebar");

  let isDragging = false;

  resizer.addEventListener("mousedown", () => {
    isDragging = true;
  });

  document.addEventListener("mousemove", (e) => {
    if (!isDragging) return;

    const newWidth = e.clientX;
    sidebar.style.width = newWidth + "px";
  });

  document.addEventListener("mouseup", () => {
    isDragging = false;
  });
}

/* -----------------------------
   PATH NORMALIZATION
------------------------------*/
function normalizePath(filePath, repoRoot = "") {
  if (!filePath) return "";
  if (!repoRoot) return filePath;

  return filePath.replace(repoRoot, "").replace(/^\/+/, "").replace(/\\/g, "/");
}

function buildTypeFilters() {
  const container = document.getElementById("type-filters");
  container.innerHTML = "";

  const nodeTypes = [...new Set(fullData.nodes.map((n) => n.type))];
  const edgeTypes = [...new Set(fullData.edges.map((e) => e.type))];

  // default: all selected
  selectedNodeTypes = new Set(nodeTypes);
  selectedEdgeTypes = new Set(edgeTypes);

  const section = (title) => {
    const h = document.createElement("div");
    h.textContent = title;
    h.style.fontWeight = "bold";
    h.style.marginTop = "8px";
    return h;
  };

  container.appendChild(section("Node Types"));

  nodeTypes.forEach((type) => {
    const label = document.createElement("label");

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;

    cb.onchange = () => {
      if (cb.checked) selectedNodeTypes.add(type);
      else selectedNodeTypes.delete(type);
      updateFilter();
    };

    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + type));
    container.appendChild(label);
    container.appendChild(document.createElement("br"));
  });

  container.appendChild(section("Edge Types"));

  edgeTypes.forEach((type) => {
    const label = document.createElement("label");

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = true;

    cb.onchange = () => {
      if (cb.checked) selectedEdgeTypes.add(type);
      else selectedEdgeTypes.delete(type);
      updateFilter();
    };

    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + type));
    container.appendChild(label);
    container.appendChild(document.createElement("br"));
  });
}

/* -----------------------------
   LOAD GRAPH
------------------------------*/
async function loadGraph() {
  const res = await fetch("graph.json");
  fullData = await res.json();

  const allFiles = fullData.nodes
    .map((n) => normalizePath(n.file, fullData.repo_root))
    .filter((f) => f);

  const uniqueFiles = [...new Set(allFiles)];

  const mainFiles = uniqueFiles.filter((f) => f.endsWith("__main__.py"));

  const initialFiles =
    mainFiles.length > 0 ? mainFiles : uniqueFiles.slice(0, 1);

  // ✅ SET STATE FIRST
  selectedFiles = new Set(initialFiles);

  // ✅ THEN BUILD UI
  buildFileList();
  buildTypeFilters();

  // ✅ THEN RENDER GRAPH
  renderGraph(initialFiles);
}

/* -----------------------------
   FILE TREE BUILD
------------------------------*/
function buildFileTree(files) {
  const root = {};

  for (const file of files) {
    const parts = file.split("/").filter(Boolean);

    let current = root;

    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];

      if (!current[part]) {
        current[part] = {
          __children: {},
          __isFile: false,
        };
      }

      if (i === parts.length - 1) {
        current[part].__isFile = true;
      }

      current = current[part].__children;
    }
  }

  return root;
}

/* -----------------------------
   RENDER TREE (SIDEBAR)
------------------------------*/
function renderTree(node, container, path = "") {
  const ul = document.createElement("ul");

  for (const key in node) {
    const item = node[key];
    const fullPath = path ? `${path}/${key}` : key;

    const li = document.createElement("li");

    /* ---------------- FILE ---------------- */
    if (item.__isFile) {
      const label = document.createElement("label");

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = selectedFiles.has(fullPath);
      checkbox.value = fullPath;
      checkbox.onchange = (e) => {
        if (e.target.checked) {
          selectedFiles.add(fullPath);
        } else {
          selectedFiles.delete(fullPath);
        }
        updateFilter();
      };

      const icon = document.createElement("span");
      icon.className = "file-icon";

      const text = document.createElement("span");
      text.textContent = " " + key;

      label.appendChild(checkbox);
      label.appendChild(icon);
      label.appendChild(text);

      li.appendChild(label);
    } else {

    /* ---------------- FOLDER ---------------- */
      const folderHeader = document.createElement("div");
      folderHeader.className = "folder";

      const icon = document.createElement("span");
      icon.className = "folder-icon";

      const text = document.createElement("span");
      text.textContent = " " + key;

      folderHeader.appendChild(icon);
      folderHeader.appendChild(text);

      const childContainer = document.createElement("div");
      childContainer.style.display = "none";
      childContainer.style.paddingLeft = "14px";

      folderHeader.onclick = (e) => {
        e.stopPropagation();
        childContainer.style.display =
          childContainer.style.display === "none" ? "block" : "none";
      };

      renderTree(item.__children, childContainer, fullPath);

      li.appendChild(folderHeader);
      li.appendChild(childContainer);
    }

    ul.appendChild(li);
  }

  container.appendChild(ul);
}

/* -----------------------------
   BUILD SIDEBAR
------------------------------*/
function buildFileList() {
  const container = document.getElementById("file-list");
  container.innerHTML = "";

  const files = [
    ...new Set(
      fullData.nodes
        .map((n) => normalizePath(n.file, fullData.repo_root))
        .filter((f) => f),
    ),
  ];

  const tree = buildFileTree(files);
  renderTree(tree, container);
}

/* -----------------------------
   FILE FILTER
------------------------------*/
function getSelectedFiles() {
  return Array.from(selectedFiles);
}

function updateFilter() {
  const selectedFiles = getSelectedFiles();
  renderGraph(selectedFiles);
}

/* -----------------------------
   GRAPH RENDERING
------------------------------*/
function renderGraph(selectedFiles) {
  const nodes = fullData.nodes.filter(
    (n) =>
      selectedFiles.includes(normalizePath(n.file, fullData.repo_root)) &&
      selectedNodeTypes.has(n.type),
  );

  const nodeIds = new Set(nodes.map((n) => n.id));

  const edges = fullData.edges.filter(
    (e) =>
      nodeIds.has(e.source) &&
      nodeIds.has(e.target) &&
      selectedEdgeTypes.has(e.type),
  );

  const visNodes = new vis.DataSet(
    nodes.map((n) => ({
      id: n.id,
      label: `${n.name}\n(${n.type})`,
      group: n.type,
    })),
  );

  const visEdges = new vis.DataSet(
    edges.map((e) => ({
      from: e.source,
      to: e.target,
      label: e.type,
      arrows: "to",
    })),
  );

  const container = document.getElementById("graph");

  network = new vis.Network(
    container,
    { nodes: visNodes, edges: visEdges },
    {
      layout: {
        improvedLayout: false,
      },
      physics: {
        stabilization: false,
      },
      groups: {
        module: { color: "#6baed6" },
        function: { color: "#74c476" },
        class: { color: "#fd8d3c" },
        import: { color: "#9e9ac8" },
      },
    },
  );
}

/* -----------------------------
   INIT
------------------------------*/
loadGraph();
initResizer();


# --- FILE: pyproject.toml ---

[project]
name = "coddingtoddly"
version = "0.1.0"
dependencies = [
    "tree-sitter",
    "tree-sitter-languages"
]