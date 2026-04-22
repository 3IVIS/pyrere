/* ─────────────────────────────────────────────────────────────────────────────
   CKG VIEWER  -  app.js
   ───────────────────────────────────────────────────────────────────────────── */

"use strict";

// ── design tokens ─────────────────────────────────────────────────────────────

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
      background: "#F0F9FF",
      border:     "#0284C7",
      highlight:  { background: "#E0F2FE", border: "#0369A1" },
      hover:      { background: "#E0F2FE", border: "#0369A1" },
    },
    shape: "triangleDown",
    font:  { color: "#0C4A6E", size: 11, face: "ui-sans-serif, system-ui, sans-serif" },
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

// Issue severity → border colour override
const SEVERITY_BORDER = {
  error:   "#DC2626",   // red-600
  warning: "#D97706",   // amber-600
  info:    "#0891B2",   // cyan-600
};

// Tool → pill colour (background, text)
const TOOL_PILL = {
  ruff:    { bg: "#EFF6FF", fg: "#1D4ED8", label: "ruff"    },
  vulture: { bg: "#FFF7ED", fg: "#C2410C", label: "vulture" },
  bandit:  { bg: "#FEF2F2", fg: "#991B1B", label: "bandit"  },
};

const EDGE_STYLES = {
  contains:       ["#CBD5E1", false],
  imports:        ["#2563EB", false],
  imports_symbol: ["#93C5FD", true ],
  calls:          ["#EA580C", false],
  inherits:       ["#7C3AED", false],
  decorates:      ["#DB2777", false],
  uses_type:      ["#0891B2", true ],
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

// ── issue helpers ─────────────────────────────────────────────────────────────

/**
 * Return the worst severity present in an issues array, or null if empty.
 * Order: error > warning > info
 */
function worstSeverity(issues) {
  if (!issues || issues.length === 0) return null;
  if (issues.some((i) => i.severity === "error"))   return "error";
  if (issues.some((i) => i.severity === "warning")) return "warning";
  return "info";
}

/**
 * Count issues by severity. Returns { error, warning, info }.
 */
function countBySeverity(issues) {
  const out = { error: 0, warning: 0, info: 0 };
  for (const i of (issues || [])) out[i.severity] = (out[i.severity] || 0) + 1;
  return out;
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

function renderTree(node, container, path = "", depth = 0) {
  const ul = document.createElement("ul");

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

// ── issues sidebar panel ──────────────────────────────────────────────────────

/**
 * Rebuild the "Issues" panel in the sidebar with aggregated counts across
 * all currently visible nodes.
 */
function buildIssuesPanel(visibleNodes) {
  const panel = document.getElementById("issues-panel");
  if (!panel) return;
  panel.innerHTML = "";

  // Collect all issues from visible nodes
  const byTool    = {};
  const bySev     = { error: 0, warning: 0, info: 0 };
  let   total     = 0;

  for (const n of visibleNodes) {
    for (const issue of (n.metadata?.issues ?? [])) {
      byTool[issue.tool] = (byTool[issue.tool] || 0) + 1;
      bySev[issue.severity] = (bySev[issue.severity] || 0) + 1;
      total++;
    }
  }

  if (total === 0) {
    const none = document.createElement("div");
    none.style.cssText = "padding:6px 14px;font-size:12px;color:#94a3b8";
    none.textContent = "No issues in visible nodes";
    panel.appendChild(none);
    return;
  }

  // Severity summary row
  const sevRow = document.createElement("div");
  sevRow.style.cssText = "display:flex;gap:6px;padding:6px 14px 4px;flex-wrap:wrap";

  const sevDefs = [
    { key: "error",   label: "errors",   bg: "#FEF2F2", fg: "#991B1B" },
    { key: "warning", label: "warnings", bg: "#FFFBEB", fg: "#92400E" },
    { key: "info",    label: "info",     bg: "#EFF6FF", fg: "#1E40AF" },
  ];
  for (const { key, label, bg, fg } of sevDefs) {
    if (!bySev[key]) continue;
    const pill = document.createElement("span");
    pill.style.cssText = `background:${bg};color:${fg};font-size:11px;font-weight:600;padding:1px 7px;border-radius:999px`;
    pill.textContent = `${bySev[key]} ${label}`;
    sevRow.appendChild(pill);
  }
  panel.appendChild(sevRow);

  // Per-tool breakdown
  for (const [tool, count] of Object.entries(byTool).sort()) {
    const p = TOOL_PILL[tool] ?? { bg: "#F1F5F9", fg: "#334155", label: tool };
    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:7px;padding:3px 14px";

    const pill = document.createElement("span");
    pill.style.cssText = `background:${p.bg};color:${p.fg};font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;min-width:46px;text-align:center`;
    pill.textContent = p.label;

    const cnt = document.createElement("span");
    cnt.style.cssText = "font-size:12px;color:#374151";
    cnt.textContent = `${count} issue${count !== 1 ? "s" : ""}`;

    row.append(pill, cnt);
    panel.appendChild(row);
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

function nodeOptions(type, isImported = false, issues = []) {
  // Start from the base style for this node type
  const base = {
    ...(NODE_STYLES[type] ?? NODE_STYLES_EXTRA[type] ?? NODE_STYLE_DEFAULT),
  };

  // Apply severity-based border colour override (errors trump warnings)
  const worst = worstSeverity(issues);
  if (worst) {
    const borderCol = SEVERITY_BORDER[worst];
    base.color = {
      ...base.color,
      border:    borderCol,
      highlight: { ...base.color.highlight, border: borderCol },
      hover:     { ...base.color.hover,     border: borderCol },
    };
    // Slightly thicker border so it reads at small sizes
    base.borderWidth = 3;
  }

  if (!isImported) return base;

  return {
    ...base,
    color: {
      ...base.color,
      background: base.color.background,
      border:     base.color.border + "88",
    },
    opacity:      0.55,
    borderDashes: [5, 4],
    borderWidth:  1,
  };
}

/**
 * Build the rich HTML tooltip element for a node.
 */
function buildTooltip(n, isImported) {
  const m      = n.metadata ?? {};
  const issues = m.issues ?? [];
  const el     = document.createElement("div");
  el.style.cssText =
    "font-family:ui-sans-serif,system-ui,sans-serif;font-size:12px;" +
    "line-height:1.5;max-width:340px;padding:2px 0";

  // ── name + type ──────────────────────────────────────────────────────────────
  const heading = document.createElement("div");
  heading.style.cssText = "font-weight:700;font-size:13px;margin-bottom:2px";
  heading.textContent = n.name;
  el.appendChild(heading);

  const sub = document.createElement("div");
  sub.style.cssText = "color:#64748b;font-size:11px";
  sub.textContent = `${n.type} · ${n.file ?? ""}`;
  el.appendChild(sub);

  // ── metadata badges ──────────────────────────────────────────────────────────
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
    ds.style.cssText =
      "color:#64748b;font-size:11px;font-style:italic;margin-top:3px;" +
      "border-top:1px solid #f1f5f9;padding-top:3px";
    const preview = m.docstring.length > 120 ? m.docstring.slice(0, 120) + "…" : m.docstring;
    ds.textContent = preview;
    el.appendChild(ds);
  }

  // ── issues section ───────────────────────────────────────────────────────────
  if (issues.length > 0) {
    const divider = document.createElement("div");
    divider.style.cssText =
      "margin-top:6px;padding-top:5px;border-top:1px solid #fee2e2";
    el.appendChild(divider);

    const issueHdr = document.createElement("div");
    issueHdr.style.cssText = "font-weight:700;font-size:10px;color:#dc2626;margin-bottom:3px;text-transform:uppercase;letter-spacing:.05em";
    issueHdr.textContent = `${issues.length} issue${issues.length !== 1 ? "s" : ""}`;
    divider.appendChild(issueHdr);

    // Show up to 6 issues; summarise the rest
    const shown = issues.slice(0, 6);
    for (const issue of shown) {
      const row = document.createElement("div");
      row.style.cssText = "display:flex;gap:5px;align-items:baseline;margin-bottom:2px";

      const p = TOOL_PILL[issue.tool] ?? { bg: "#F1F5F9", fg: "#334155" };
      const pill = document.createElement("span");
      pill.style.cssText =
        `background:${p.bg};color:${p.fg};font-size:9px;font-weight:700;` +
        "padding:0 4px;border-radius:3px;flex-shrink:0";
      pill.textContent = issue.tool;

      const codeSev = document.createElement("span");
      const sevColour = SEVERITY_BORDER[issue.severity] ?? "#64748b";
      codeSev.style.cssText = `color:${sevColour};font-size:10px;font-weight:600;flex-shrink:0`;
      codeSev.textContent = issue.code;

      const msg = document.createElement("span");
      msg.style.cssText = "color:#374151;font-size:10px;white-space:normal";
      const short = issue.message.length > 70
        ? issue.message.slice(0, 70) + "…"
        : issue.message;
      msg.textContent = `${short}  (L${issue.line})`;

      row.append(pill, codeSev, msg);
      divider.appendChild(row);
    }

    if (issues.length > 6) {
      const more = document.createElement("div");
      more.style.cssText = "color:#94a3b8;font-size:10px;margin-top:2px";
      more.textContent = `+ ${issues.length - 6} more …`;
      divider.appendChild(more);
    }
  }

  return el;
}

function renderGraph() {
  const nodeById       = new Map(fullData.nodes.map((n) => [n.id, n]));
  const moduleIdByFile = new Map();
  const fileByModuleId = new Map();
  for (const n of fullData.nodes) {
    if (n.type === "module" && n.file) {
      moduleIdByFile.set(n.file, n.id);
      fileByModuleId.set(n.id, n.file);
    }
  }

  // One-hop import expansion
  const importAdj = new Map();
  for (const e of fullData.edges) {
    if (e.type !== "imports") continue;
    if (!importAdj.has(e.source)) importAdj.set(e.source, []);
    importAdj.get(e.source).push(e.target);
  }

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

  // Node set
  const nodes = fullData.nodes.filter(
    (n) => allVisibleFiles.has(n.file) && selectedNodeTypes.has(n.type)
  );
  const nodeIds = new Set(nodes.map((n) => n.id));

  // Expand neighbours of selected node
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

  // Edge set
  const edges = fullData.edges.filter(
    (e) => nodeIds.has(e.source) && nodeIds.has(e.target) && selectedEdgeTypes.has(e.type)
  );

  // Status bar
  const statusEl = document.getElementById("status");
  if (statusEl) statusEl.textContent = `${nodes.length} nodes · ${edges.length} edges`;

  // Refresh issues panel
  buildIssuesPanel(nodes);

  // vis datasets
  const visNodes = new vis.DataSet(
    nodes.map((n) => {
      const isImported = importedFiles.has(n.file) && !selectedFiles.has(n.file);
      const issues     = n.metadata?.issues ?? [];
      const counts     = countBySeverity(issues);

      // Build label: name + optional issue badge
      let label = n.name;
      if (counts.error)   label += ` ✖${counts.error}`;
      if (counts.warning) label += ` ⚠${counts.warning}`;

      return {
        id:    n.id,
        label,
        title: buildTooltip(n, isImported),
        ...nodeOptions(n.type, isImported, issues),
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