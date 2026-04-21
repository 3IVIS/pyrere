/* ─────────────────────────────────────────────────────────────────────────────
   CKG VIEWER  –  app.js
   ───────────────────────────────────────────────────────────────────────────── */

"use strict";

// ── design tokens ─────────────────────────────────────────────────────────────
//
// Light-background nodes: pastel fill + strong-coloured border + dark text.
// This reads better at small label sizes and keeps the canvas feeling airy.

const NODE_STYLES = {
  module: {
    color: {
      background: "#EFF6FF",
      border:     "#2563EB",
      highlight:  { background: "#DBEAFE", border: "#1D4ED8" },
      hover:      { background: "#DBEAFE", border: "#1D4ED8" },
    },
    shape:  "box",
    font:   { color: "#1E3A5F", size: 12, face: "ui-monospace, 'Cascadia Code', monospace", bold: true },
    margin: 8,
  },
  class: {
    color: {
      background: "#FFF7ED",
      border:     "#EA580C",
      highlight:  { background: "#FFEDD5", border: "#C2410C" },
      hover:      { background: "#FFEDD5", border: "#C2410C" },
    },
    shape: "diamond",
    font:  { color: "#431407", size: 12, face: "ui-sans-serif, system-ui, sans-serif" },
    size:  20,
  },
  function: {
    color: {
      background: "#F0FDF4",
      border:     "#16A34A",
      highlight:  { background: "#DCFCE7", border: "#15803D" },
      hover:      { background: "#DCFCE7", border: "#15803D" },
    },
    shape: "ellipse",
    font:  { color: "#14532D", size: 12, face: "ui-sans-serif, system-ui, sans-serif" },
  },
};

const NODE_STYLES_EXTRA = {
  variable: {
    color: {
      background: '#F0F9FF',
      border: '#0284C7',
      highlight: { background: '#E0F2FE', border: '#0369A1' },
      hover:     { background: '#E0F2FE', border: '#0369A1' },
    },
    shape: 'triangleDown',
    font:  { color: '#0C4A6E', size: 11, face: 'ui-sans-serif, system-ui, sans-serif' },
  },
};

const NODE_STYLE_DEFAULT = {
  color: {
    background: "#F8FAFC",
    border:     "#94A3B8",
    highlight:  { background: "#F1F5F9", border: "#64748B" },
    hover:      { background: "#F1F5F9", border: "#64748B" },
  },
  shape: "dot",
  font:  { color: "#334155", size: 11 },
};

// Semantic edge palette:
//  slate  = structural containment (quiet, doesn't compete)
//  blue   = import relationships
//  orange = execution / calls
//  violet = inheritance hierarchy
//  pink   = decoration / modification
//  cyan   = type-system usage
const EDGE_STYLES = {
  contains:       ["#CBD5E1", false],  // slate-300   — thin, structural
  imports:        ["#2563EB", false],  // blue-600    — file dependency
  imports_symbol: ["#93C5FD", true ],  // blue-300    — symbol import, dashed
  calls:          ["#EA580C", false],  // orange-600  — function call
  inherits:       ["#7C3AED", false],  // violet-600  — class hierarchy
  decorates:      ["#DB2777", false],  // pink-600    — decorator
  uses_type:      ["#0891B2", true ],  // cyan-600    — type annotation, dashed
};
const EDGE_COLOUR_DEFAULT = "#94A3B8";

// ── state ─────────────────────────────────────────────────────────────────────

let fullData          = null;
let network           = null;
let selectedFiles     = new Set();
let selectedNodeTypes = new Set();
let selectedEdgeTypes = new Set();
let selectedNodeId    = null;
let _rendering        = false;

// ── resizer ───────────────────────────────────────────────────────────────────

function initResizer() {
  const resizer = document.getElementById("resizer");
  const sidebar = document.getElementById("sidebar");
  let dragging  = false;
  resizer.addEventListener("mousedown", () => {
    dragging = true;
    document.body.style.userSelect = "none";
  });
  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    sidebar.style.width = Math.max(180, Math.min(e.clientX, 560)) + "px";
  });
  document.addEventListener("mouseup", () => {
    dragging = false;
    document.body.style.userSelect = "";
  });
}

// ── path helpers ──────────────────────────────────────────────────────────────

function normalizePath(p) {
  return p ? p.replace(/\\/g, "/") : "";
}

// ── file tree ─────────────────────────────────────────────────────────────────

function buildFileTree(files) {
  const root = {};
  for (const file of files) {
    const parts = file.split("/").filter(Boolean);
    let cur = root;
    for (let i = 0; i < parts.length; i++) {
      const key = parts[i];
      if (!cur[key]) cur[key] = { __children: {}, __isFile: false };
      if (i === parts.length - 1) cur[key].__isFile = true;
      cur = cur[key].__children;
    }
  }
  return root;
}

/**
 * Renders a tree level into `container`.
 * Folders always appear before files; each group is sorted alphabetically.
 * Top-level folders (depth 0) are expanded by default.
 */
function renderTree(node, container, path = "", depth = 0) {
  const ul = document.createElement("ul");

  // Separate folders from files, sort each group alpha, then concat
  const all     = Object.entries(node);
  const folders = all.filter(([, v]) => !v.__isFile || Object.keys(v.__children).length > 0)
                     .sort(([a], [b]) => a.localeCompare(b));
  const files   = all.filter(([, v]) =>  v.__isFile && Object.keys(v.__children).length === 0)
                     .sort(([a], [b]) => a.localeCompare(b));

  for (const [key, item] of [...folders, ...files]) {
    const fullPath = path ? `${path}/${key}` : key;
    const li       = document.createElement("li");
    const isFolder = !item.__isFile || Object.keys(item.__children).length > 0;

    if (isFolder) {
      // ── folder ──────────────────────────────────────────────────────────────
      const header = document.createElement("div");
      header.className = "tree-folder";

      const chevron = document.createElement("span");
      chevron.className = "chevron";

      const icon = document.createElement("span");
      icon.className = "folder-icon";
      icon.textContent = "📁";

      const label = document.createElement("span");
      label.className = "tree-label";
      label.textContent = key;

      header.append(chevron, icon, label);

      const childWrap = document.createElement("div");
      childWrap.className = "tree-children";
      const open = depth === 0;
      childWrap.style.display = open ? "block" : "none";
      if (open) header.classList.add("open");

      header.addEventListener("click", (e) => {
        e.stopPropagation();
        const isOpen = childWrap.style.display !== "none";
        childWrap.style.display = isOpen ? "none" : "block";
        header.classList.toggle("open", !isOpen);
      });

      renderTree(item.__children, childWrap, fullPath, depth + 1);
      li.append(header, childWrap);

    } else {
      // ── file ─────────────────────────────────────────────────────────────────
      const row = document.createElement("label");
      row.className = "tree-file";

      const cb = document.createElement("input");
      cb.type    = "checkbox";
      cb.checked = selectedFiles.has(fullPath);
      cb.onchange = () => {
        if (cb.checked) selectedFiles.add(fullPath);
        else            selectedFiles.delete(fullPath);
        renderGraph();
      };

      const dot = document.createElement("span");
      dot.className = "file-dot";

      const label = document.createElement("span");
      label.className = "tree-label";
      label.textContent = key;
      label.title = fullPath;

      row.append(cb, dot, label);
      li.appendChild(row);
    }

    ul.appendChild(li);
  }

  container.appendChild(ul);
}

function buildFileList() {
  const container = document.getElementById("file-list");
  container.innerHTML = "";
  const files = [...new Set(fullData.nodes.map((n) => n.file).filter(Boolean))].sort();
  renderTree(buildFileTree(files), container);
}

// ── type filters ──────────────────────────────────────────────────────────────

function makeNodeSwatch(type) {
  const s  = NODE_STYLES[type] ?? NODE_STYLE_DEFAULT;
  const el = document.createElement("span");
  el.className = "swatch";
  el.style.background   = s.color.background;
  el.style.borderColor  = s.color.border;
  // Mirror the vis.js shape loosely
  el.style.borderRadius = (s.shape === "ellipse") ? "50%"
                        : (s.shape === "diamond") ? "2px"
                        : "3px";
  if (s.shape === "diamond") el.style.transform = "rotate(45deg)";
  return el;
}

function makeEdgeSwatch(type) {
  const [colour, dashed] = EDGE_STYLES[type] ?? [EDGE_COLOUR_DEFAULT, false];
  const el = document.createElement("span");
  el.className = "swatch-line";
  el.style.background = dashed
    ? `repeating-linear-gradient(90deg,${colour} 0,${colour} 5px,transparent 5px,transparent 9px)`
    : colour;
  return el;
}

function makeFilterRow(labelText, checked, swatchEl, onChange) {
  const row = document.createElement("label");
  row.className = "filter-row";

  const cb = document.createElement("input");
  cb.type    = "checkbox";
  cb.checked = checked;
  cb.onchange = onChange;

  const txt = document.createElement("span");
  txt.className  = "filter-label";
  txt.textContent = labelText;

  row.append(cb, swatchEl, txt);
  return row;
}

function buildTypeFilters() {
  const nodeDiv = document.getElementById("node-type-filters");
  const edgeDiv = document.getElementById("edge-type-filters");
  nodeDiv.innerHTML = "";
  edgeDiv.innerHTML = "";

  const nodeTypes = [...new Set(fullData.nodes.map((n) => n.type))].sort();
  const edgeTypes = [...new Set(fullData.edges.map((e) => e.type))].sort();

  selectedNodeTypes = new Set(nodeTypes);
  selectedEdgeTypes = new Set(edgeTypes);

  for (const t of nodeTypes) {
    nodeDiv.appendChild(makeFilterRow(t, true, makeNodeSwatch(t),
      (e) => { e.target.checked ? selectedNodeTypes.add(t) : selectedNodeTypes.delete(t); renderGraph(); }
    ));
  }
  for (const t of edgeTypes) {
    edgeDiv.appendChild(makeFilterRow(t, true, makeEdgeSwatch(t),
      (e) => { e.target.checked ? selectedEdgeTypes.add(t) : selectedEdgeTypes.delete(t); renderGraph(); }
    ));
  }
}

// ── graph rendering ───────────────────────────────────────────────────────────

function edgeOptions(type) {
  const [colour, dashed] = EDGE_STYLES[type] ?? [EDGE_COLOUR_DEFAULT, false];
  return {
    color:  { color: colour, highlight: colour, hover: colour },
    dashes: dashed,
    width:  type === "contains" ? 1 : 2,
    arrows: { to: { enabled: true, scaleFactor: 0.6 } },
    smooth: { type: "dynamic" },
  };
}

function nodeOptions(type, isImported = false) {
  const base = { ...(NODE_STYLES[type] ?? NODE_STYLES_EXTRA[type] ?? NODE_STYLE_DEFAULT) };
  if (!isImported) return base;
  return {
    ...base,
    color: {
      ...base.color,
      background: base.color.background,
      border:     base.color.border + "88",   // semi-transparent border
    },
    opacity:      0.55,
    borderDashes: [5, 4],
    borderWidth:  1,
  };
}

function renderGraph() {
  // ── lookup tables ──────────────────────────────────────────────────────────
  const nodeById       = new Map(fullData.nodes.map((n) => [n.id, n]));
  const moduleIdByFile = new Map();
  const fileByModuleId = new Map();
  for (const n of fullData.nodes) {
    if (n.type === "module" && n.file) {
      moduleIdByFile.set(n.file, n.id);
      fileByModuleId.set(n.id, n.file);
    }
  }

  // Adjacency list: moduleId → [targetModuleId]
  const importAdj = new Map();
  for (const e of fullData.edges) {
    if (e.type !== "imports") continue;
    if (!importAdj.has(e.source)) importAdj.set(e.source, []);
    importAdj.get(e.source).push(e.target);
  }

  // ── one-hop import expansion ───────────────────────────────────────────────
  const importedFiles = new Set();
  for (const selFile of selectedFiles) {
    const modId = moduleIdByFile.get(selFile);
    if (!modId) continue;
    for (const targetId of (importAdj.get(modId) ?? [])) {
      const tf = fileByModuleId.get(targetId);
      if (tf && !selectedFiles.has(tf)) importedFiles.add(tf);
    }
  }

  const allVisibleFiles = new Set([...selectedFiles, ...importedFiles]);

  // ── node set ───────────────────────────────────────────────────────────────
  const nodes = fullData.nodes.filter(
    (n) => allVisibleFiles.has(n.file) && selectedNodeTypes.has(n.type)
  );
  const nodeIds = new Set(nodes.map((n) => n.id));

  // Clicked node: pull in all directly connected neighbours
  if (selectedNodeId && nodeIds.has(selectedNodeId)) {
    for (const e of fullData.edges) {
      if (!selectedEdgeTypes.has(e.type)) continue;
      const otherId = e.source === selectedNodeId ? e.target
                    : e.target === selectedNodeId ? e.source : null;
      if (!otherId || nodeIds.has(otherId)) continue;
      const other = nodeById.get(otherId);
      if (other && selectedNodeTypes.has(other.type)) {
        nodes.push(other);
        nodeIds.add(otherId);
      }
    }
  }

  // ── edge set ───────────────────────────────────────────────────────────────
  const edges = fullData.edges.filter(
    (e) => nodeIds.has(e.source) && nodeIds.has(e.target) && selectedEdgeTypes.has(e.type)
  );

  // ── status bar ─────────────────────────────────────────────────────────────
  const statusEl = document.getElementById("status");
  if (statusEl) statusEl.textContent = `${nodes.length} nodes · ${edges.length} edges`;

  // ── vis datasets ───────────────────────────────────────────────────────────
  const visNodes = new vis.DataSet(
    nodes.map((n) => {
      const isImported = importedFiles.has(n.file) && !selectedFiles.has(n.file);
      return {
        id:    n.id,
        label: n.name,
        title: (() => {
          // vis.js renders a DOM element as HTML; a plain string is plain text.
          const m = n.metadata ?? {};
          const el = document.createElement("div");
          el.style.cssText = "font-family:ui-sans-serif,system-ui,sans-serif;font-size:12px;line-height:1.5;max-width:320px;padding:2px 0";

          const heading = document.createElement("div");
          heading.style.cssText = "font-weight:700;font-size:13px;margin-bottom:2px";
          heading.textContent = n.name;
          el.appendChild(heading);

          const sub = document.createElement("div");
          sub.style.cssText = "color:#64748b;font-size:11px";
          sub.textContent = `${n.type} · ${n.file ?? ""}`;
          el.appendChild(sub);

          const badges = [];
          if (m.is_async)       badges.push("async");
          if (m.is_generator)   badges.push("generator");
          if (m.is_static)      badges.push("static");
          if (m.is_classmethod) badges.push("classmethod");
          if (m.is_property)    badges.push("property");
          if (m.is_lambda)      badges.push("lambda");
          if (m.is_dataclass)   badges.push("dataclass");
          if (m.is_abstract)    badges.push("abstract");
          if (m.is_exception)   badges.push("exception");
          if (isImported)       badges.push("imported");
          if (m.visibility)     badges.push(m.visibility);

          if (badges.length) {
            const bd = document.createElement("div");
            bd.style.cssText = "color:#94a3b8;font-size:10px;margin-top:2px";
            bd.textContent = badges.join(" · ");
            el.appendChild(bd);
          }

          if (m.return_type) {
            const rt = document.createElement("div");
            rt.style.cssText = "color:#94a3b8;font-size:10px";
            rt.textContent = `→ ${m.return_type}`;
            el.appendChild(rt);
          }

          if (m.complexity != null && m.complexity > 1) {
            const cx = document.createElement("div");
            cx.style.cssText = "color:#94a3b8;font-size:10px";
            cx.textContent = `complexity: ${m.complexity}`;
            el.appendChild(cx);
          }

          if (m.docstring) {
            const ds = document.createElement("div");
            ds.style.cssText = "color:#64748b;font-size:11px;font-style:italic;margin-top:3px;border-top:1px solid #f1f5f9;padding-top:3px";
            const preview = m.docstring.length > 120 ? m.docstring.slice(0, 120) + "…" : m.docstring;
            ds.textContent = preview;
            el.appendChild(ds);
          }

          return el;
        })(),
        ...nodeOptions(n.type, isImported),
      };
    })
  );

  const visEdges = new vis.DataSet(
    edges.map((e) => ({
      id:    `${e.source}_${e.target}_${e.type}`,
      from:  e.source,
      to:    e.target,
      label: e.type,
      font:  { size: 9, color: "#94A3B8", align: "middle", strokeWidth: 0 },
      ...edgeOptions(e.type),
    }))
  );

  const container = document.getElementById("graph");

  if (network) {
    network.setData({ nodes: visNodes, edges: visEdges });
  } else {
    network = new vis.Network(container, { nodes: visNodes, edges: visEdges }, {
      layout: { improvedLayout: true },
      physics: {
        stabilization: { iterations: 300, fit: true },
        barnesHut: {
          gravitationalConstant: -6000,
          centralGravity:        0.1,
          springLength:          140,
          springConstant:        0.04,
          damping:               0.12,
        },
      },
      interaction: {
        hover:           true,
        tooltipDelay:    60,
        hideEdgesOnDrag: true,
      },
      nodes: {
        borderWidth:         2,
        borderWidthSelected: 3,
        shadow:              { enabled: true, color: "rgba(0,0,0,0.08)", size: 8, x: 0, y: 2 },
      },
      edges: {
        font: { size: 9, color: "#94A3B8", strokeWidth: 0 },
      },
    });

    network.on("selectNode", ({ nodes: ns }) => {
      if (_rendering) return;
      selectedNodeId = ns[0] ?? null;
      renderGraph();
    });
    network.on("deselectNode", () => {
      if (_rendering) return;
      selectedNodeId = null;
      renderGraph();
    });
  }

  if (selectedNodeId && nodeIds.has(selectedNodeId)) {
    _rendering = true;
    network.selectNodes([selectedNodeId]);
    _rendering = false;
  }
}

// ── init ──────────────────────────────────────────────────────────────────────

async function loadGraph() {
  const res = await fetch("graph.json");
  fullData  = await res.json();
  fullData.nodes.forEach((n) => { n.file = normalizePath(n.file); });

  const allFiles  = [...new Set(fullData.nodes.map((n) => n.file).filter(Boolean))];
  const mainFiles = allFiles.filter((f) => f.endsWith("__main__.py"));
  selectedFiles   = new Set(mainFiles.length ? mainFiles : allFiles.slice(0, 1));

  buildFileList();
  buildTypeFilters();
  renderGraph();
}

loadGraph();
initResizer();