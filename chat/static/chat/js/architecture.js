/* =========================================================================
   功能架构页 /architecture
   数据全部来自 GET /architecture/data（agent_engine/architecture.py）。
   本文件只负责渲染与交互：改图不需要动这里。
   ========================================================================= */

const architecturePage = (() => {
  let data = null;
  let nodeById = {};
  let layerById = {};
  let nodeEls = {};          // node id -> button
  let edgeEls = [];          // { el, from, to }
  let canvas = null;
  let svg = null;
  let selectedId = null;
  let focusedId = null;
  let flowTimer = null;
  let flowIndex = -1;

  // ---------- 基础 ----------

  const $ = (sel) => document.querySelector(sel);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function icon(name) {
    return `<svg class="ico" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  }

  function nodeChip(nodeId) {
    const node = nodeById[nodeId];
    if (!node) return el("span", "node-chip", nodeId);
    const chip = el("button", "node-chip", node.title);
    chip.type = "button";
    chip.dataset.node = nodeId;
    return chip;
  }

  // ---------- 分层图 ----------

  function renderGraph() {
    canvas = $("#arch-canvas");
    canvas.innerHTML = "";
    nodeEls = {};

    for (const layer of data.layers) {
      const band = el("section", `arch-layer layer-${layer.id}`);
      const label = el("div", "layer-label");
      label.appendChild(el("strong", null, layer.title));
      label.appendChild(el("small", null, layer.hint));
      const layerNodes = data.nodes.filter((n) => n.layer === layer.id);
      label.appendChild(el("span", "layer-count", `${layerNodes.length} 个节点`));
      band.appendChild(label);

      const nodesWrap = el("div", "layer-nodes");
      for (const node of layerNodes) {
        const btn = el("button", "arch-node");
        btn.type = "button";
        btn.dataset.node = node.id;
        btn.appendChild(el("span", "node-kicker", layer.title));
        btn.appendChild(el("span", "node-title", node.title));
        btn.appendChild(el("span", "node-summary", node.summary));
        nodesWrap.appendChild(btn);
        nodeEls[node.id] = btn;
      }
      band.appendChild(nodesWrap);
      canvas.appendChild(band);
    }

    svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "arch-edges");
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    defs.innerHTML =
      '<marker id="arch-arrow" viewBox="0 0 10 10" refX="8" refY="5" ' +
      'markerWidth="5" markerHeight="5" orient="auto-start-reverse">' +
      '<path d="M0 0 L10 5 L0 10 z" fill="#7f8aa3"/></marker>';
    svg.appendChild(defs);
    canvas.appendChild(svg);

    requestAnimationFrame(() => requestAnimationFrame(drawEdges));
  }

  function pointOf(nodeEl, side) {
    const rect = nodeEl.getBoundingClientRect();
    const base = canvas.getBoundingClientRect();
    const x = rect.left - base.left;
    const y = rect.top - base.top;
    return {
      left: { x, y: y + rect.height / 2 },
      right: { x: x + rect.width, y: y + rect.height / 2 },
      top: { x: x + rect.width / 2, y },
      bottom: { x: x + rect.width / 2, y: y + rect.height },
      center: { x: x + rect.width / 2, y: y + rect.height / 2 },
      height: rect.height,
    };
  }

  function drawEdges() {
    if (!svg || !canvas || canvas.clientWidth === 0) return;
    // 只清掉路径，保留 defs（箭头 marker）
    for (const path of svg.querySelectorAll(".arch-edge")) path.remove();
    edgeEls = [];

    const source = data.nodes.reduce((acc, n) => {
      acc[n.id] = n;
      return acc;
    }, {});
    const layerIndex = data.layers.reduce((acc, l, i) => {
      acc[l.id] = i;
      return acc;
    }, {});

    for (const edge of data.edges) {
      const a = nodeEls[edge.from];
      const b = nodeEls[edge.to];
      if (!a || !b) continue;

      const fromLayer = layerIndex[source[edge.from].layer];
      const toLayer = layerIndex[source[edge.to].layer];
      const pa = pointOf(a, "top");
      const pb = pointOf(b, "top");

      let d;
      if (fromLayer === toLayer) {
        // 同层：横向曲线
        const leftToRight = pa.center.x <= pb.center.x;
        const start = leftToRight ? pa.right : pa.left;
        const end = leftToRight ? pb.left : pb.right;
        const dx = Math.min(50, Math.max(18, Math.abs(end.x - start.x) / 2)) * (leftToRight ? 1 : -1);
        d = `M ${start.x} ${start.y} C ${start.x + dx} ${start.y}, ${end.x - dx} ${end.y}, ${end.x} ${end.y}`;
      } else {
        // 跨层：上往下或下往上
        const downward = toLayer > fromLayer;
        const start = downward ? pa.bottom : pa.top;
        const end = downward ? pb.top : pb.bottom;
        const span = Math.abs(end.y - start.y);
        const bend = Math.min(70, Math.max(20, span / 2)) * (downward ? 1 : -1);
        d = `M ${start.x} ${start.y} C ${start.x} ${start.y + bend}, ${end.x} ${end.y - bend}, ${end.x} ${end.y}`;
      }

      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      path.setAttribute("class", `arch-edge ${edge.kind === "dashed" ? "dashed" : "primary"}`);
      path.setAttribute("data-from", edge.from);
      path.setAttribute("data-to", edge.to);
      path.setAttribute("marker-end", "url(#arch-arrow)");
      path.setAttribute("title", edge.label || "");
      svg.appendChild(path);
      edgeEls.push({ el: path, from: edge.from, to: edge.to, label: edge.label });
    }

    applyFocus(focusedId);
  }

  function relatedTo(nodeId) {
    const related = new Set([nodeId]);
    for (const edge of edgeEls) {
      if (edge.from === nodeId) related.add(edge.to);
      if (edge.to === nodeId) related.add(edge.from);
    }
    return related;
  }

  function applyFocus(nodeId) {
    const related = nodeId ? relatedTo(nodeId) : null;
    for (const [id, nodeEl] of Object.entries(nodeEls)) {
      const dim = !!nodeId && !related.has(id);
      nodeEl.classList.toggle("is-dim", dim);
      nodeEl.classList.toggle("is-selected", id === selectedId);
    }
    for (const edge of edgeEls) {
      const hot = !!nodeId && (edge.from === nodeId || edge.to === nodeId);
      edge.el.classList.toggle("is-hot", hot);
      edge.el.classList.toggle("is-dim", !!nodeId && !hot);
    }
  }

  function setFocus(nodeId) {
    focusedId = nodeId;
    applyFocus(nodeId);
  }

  function clearSelection() {
    selectedId = null;
    focusedId = null;
    applyFocus(null);
    closeDetail();
  }

  // ---------- 详情 ----------

  function openDetail(nodeId) {
    const node = nodeById[nodeId];
    if (!node) return;

    selectedId = nodeId;
    focusedId = nodeId;
    applyFocus(nodeId);

    const layer = layerById[node.layer];
    const layerEl = $("#detail-layer");
    layerEl.textContent = layer ? layer.title : node.layer;
    layerEl.className = "detail-layer " + node.layer;

    const body = $("#detail-body");
    body.innerHTML = "";

    body.appendChild(el("h2", "detail-title", node.title));
    body.appendChild(el("div", "detail-summary", node.summary));
    body.appendChild(el("div", "detail-role", node.role));

    if (node.files?.length) {
      const section = el("section", "detail-section");
      section.appendChild(el("h3", null, "关键文件"));
      const list = el("div", "file-list");
      for (const file of node.files) {
        const link = el("a", "file-link");
        link.href = `${data.repo_url}/blob/main/${file}`;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.innerHTML = icon("file");
        link.appendChild(el("span", null, file));
        list.appendChild(link);
      }
      section.appendChild(list);
      body.appendChild(section);
    }

    if (node.symbols?.length) {
      const section = el("section", "detail-section");
      section.appendChild(el("h3", null, "关键符号"));
      const wrap = el("div", "symbol-list");
      for (const symbol of node.symbols) wrap.appendChild(el("span", "symbol", symbol));
      section.appendChild(wrap);
      body.appendChild(section);
    }

    if (node.inputs?.length || node.outputs?.length) {
      const section = el("section", "detail-section");
      section.appendChild(el("h3", null, "输入 / 输出"));
      const list = el("dl", "kv-list");
      if (node.inputs?.length) {
        list.appendChild(el("dt", null, "输入"));
        list.appendChild(el("dd", null, node.inputs.join("；")));
      }
      if (node.outputs?.length) {
        list.appendChild(el("dt", null, "输出"));
        list.appendChild(el("dd", null, node.outputs.join("；")));
      }
      section.appendChild(list);
      body.appendChild(section);
    }

    if (node.notes) {
      body.appendChild(el("div", "detail-note", node.notes));
    }

    // 同一对节点可能有双向边，按节点去重，标签合并显示
    const related = new Map();
    for (const edge of data.edges) {
      let other = null;
      if (edge.from === nodeId) other = edge.to;
      else if (edge.to === nodeId) other = edge.from;
      if (!other) continue;
      if (!related.has(other)) related.set(other, new Set());
      if (edge.label) related.get(other).add(edge.label);
    }
    if (related.size) {
      const section = el("section", "detail-section");
      section.appendChild(el("h3", null, `关联节点（${related.size}）`));
      const wrap = el("div", "chips");
      for (const [otherId, labels] of related) {
        const chip = nodeChip(otherId);
        chip.title = Array.from(labels).join(" / ");
        wrap.appendChild(chip);
      }
      section.appendChild(wrap);
      body.appendChild(section);
    }

    $("#arch-detail").classList.remove("hidden");
    $("#detail-scrim").classList.remove("hidden");
    $("#detail-body").scrollTop = 0;
  }

  function closeDetail() {
    $("#arch-detail").classList.add("hidden");
    $("#detail-scrim").classList.add("hidden");
    selectedId = null;
    focusedId = null;
    applyFocus(null);
  }

  // ---------- 功能地图 ----------

  function renderFeatures() {
    const grid = $("#feature-grid");
    grid.innerHTML = "";
    data.features.forEach((feature, index) => {
      const card = el("article", "feature-card");
      const title = el("h2");
      title.appendChild(el("span", "feature-index", String(index + 1).padStart(2, "0")));
      title.appendChild(el("span", null, feature.title));
      card.appendChild(title);
      card.appendChild(el("div", "feature-summary", feature.summary));

      const items = el("div", "feature-items");
      for (const item of feature.items) {
        const row = el("div", "feature-item");
        row.appendChild(el("div", null, item.text));
        const chips = el("div", "chips");
        for (const nodeId of item.nodes) chips.appendChild(nodeChip(nodeId));
        row.appendChild(chips);
        items.appendChild(row);
      }
      card.appendChild(items);
      grid.appendChild(card);
    });
  }

  // ---------- 数据流 ----------

  function renderFlow() {
    const list = $("#flow-list");
    list.innerHTML = "";
    data.flow.forEach((step, index) => {
      const card = el("div", "flow-step");
      card.dataset.index = String(index);
      card.appendChild(el("div", "flow-index", String(index + 1).padStart(2, "0")));
      const main = el("div");
      main.appendChild(el("h3", null, step.title));
      main.appendChild(el("p", null, step.detail));
      const chips = el("div", "chips");
      for (const nodeId of step.nodes) chips.appendChild(nodeChip(nodeId));
      main.appendChild(chips);
      card.appendChild(main);
      list.appendChild(card);
    });

    list.addEventListener("click", (e) => {
      const chip = e.target.closest(".node-chip");
      if (chip) { openDetail(chip.dataset.node); return; }
      const card = e.target.closest(".flow-step");
      if (card) setFlowActive(Number(card.dataset.index), { scroll: false, stop: true });
    });

    $("#flow-prev").addEventListener("click", () => {
      stopFlowPlay();
      setFlowActive(Math.max(0, flowIndex - 1));
    });
    $("#flow-next").addEventListener("click", () => {
      stopFlowPlay();
      setFlowActive(Math.min(data.flow.length - 1, Math.max(0, flowIndex + 1)));
    });
    $("#flow-reset").addEventListener("click", () => {
      stopFlowPlay();
      setFlowActive(-1);
    });
    $("#flow-play").addEventListener("click", toggleFlowPlay);
  }

  function setFlowActive(index, { scroll = true, stop = false } = {}) {
    flowIndex = index;
    const cards = document.querySelectorAll(".flow-step");
    cards.forEach((card, i) => {
      card.classList.toggle("active", i === index);
      card.classList.toggle("dim", index >= 0 && i !== index);
    });
    if (index >= 0 && scroll) cards[index].scrollIntoView({ block: "center", behavior: "smooth" });
    if (stop) stopFlowPlay();
  }

  function toggleFlowPlay() {
    if (flowTimer) { stopFlowPlay(); return; }
    const play = $("#flow-play");
    play.innerHTML = icon("pause") + " 暂停";
    if (flowIndex < 0) setFlowActive(0, { scroll: true });
    flowTimer = setInterval(() => {
      const next = (flowIndex + 1) % data.flow.length;
      setFlowActive(next);
    }, 1800);
  }

  function stopFlowPlay() {
    if (flowTimer) { clearInterval(flowTimer); flowTimer = null; }
    $("#flow-play").innerHTML = icon("play") + " 播放";
  }

  // ---------- MCP 双向 ----------

  function mcpNode(nodeId, label) {
    const node = nodeById[nodeId];
    if (!node) return el("span", "mcp-node", nodeId);
    const btn = el("button", "mcp-node");
    btn.type = "button";
    btn.dataset.node = nodeId;
    btn.appendChild(el("span", "node-title", node.title));
    btn.appendChild(el("span", "node-label", label || node.summary));
    return btn;
  }

  function renderMCP() {
    const container = $("#mcp-paths");
    container.innerHTML = "";

    data.mcp_paths.forEach((path) => {
      const card = el("section", "mcp-path");
      card.appendChild(el("h2", null, path.title));
      card.appendChild(el("div", "caption", path.caption));

      const chain = el("div", "mcp-chain");
      path.steps.forEach((step, index) => {
        chain.appendChild(mcpNode(step.node, step.label));
        if (index < path.steps.length - 1) {
          const arrow = el("span", "mcp-arrow");
          arrow.innerHTML = icon("arrow-right");
          chain.appendChild(arrow);
        }
      });
      card.appendChild(chain);

      if (path.branches?.length) {
        const branches = el("div", "mcp-branches");
        branches.appendChild(el("span", "mcp-branch-label", "分支"));
        for (const branch of path.branches) {
          branches.appendChild(mcpNode(branch.node, branch.label));
        }
        card.appendChild(branches);
      }
      container.appendChild(card);
    });

    $("#shared-note-text").textContent = data.shared_note;
  }

  // ---------- 视图切换 ----------

  function switchView(view) {
    for (const tab of document.querySelectorAll(".arch-tab")) {
      tab.classList.toggle("active", tab.dataset.view === view);
    }
    for (const section of document.querySelectorAll(".arch-view")) {
      section.classList.toggle("active", section.dataset.view === view);
    }
    if (view === "overview") requestAnimationFrame(drawEdges);
    if (view !== "flow") stopFlowPlay();
  }

  // ---------- 初始化 ----------

  async function init() {
    try {
      const res = await fetch("/architecture/data", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      data = await res.json();
    } catch (e) {
      $("#arch-canvas").textContent = "架构数据加载失败：" + e.message;
      return;
    }

    nodeById = data.nodes.reduce((acc, n) => { acc[n.id] = n; return acc; }, {});
    layerById = data.layers.reduce((acc, l) => { acc[l.id] = l; return acc; }, {});

    renderGraph();
    renderFeatures();
    renderFlow();
    renderMCP();
    $("#shared-note-text").textContent = data.shared_note;

    for (const tab of document.querySelectorAll(".arch-tab")) {
      tab.addEventListener("click", () => switchView(tab.dataset.view));
    }

    canvas.addEventListener("click", (e) => {
      const node = e.target.closest(".arch-node");
      if (node) { openDetail(node.dataset.node); return; }
      if (e.target === canvas || e.target.closest(".arch-layer")) clearSelection();
    });
    canvas.addEventListener("mouseover", (e) => {
      const node = e.target.closest(".arch-node");
      if (node) setFocus(node.dataset.node);
    });
    canvas.addEventListener("mouseout", (e) => {
      const node = e.target.closest(".arch-node");
      if (node && !node.contains(e.relatedTarget)) {
        setFocus(selectedId && nodeEls[selectedId] ? selectedId : null);
      }
    });

    $("#detail-close").addEventListener("click", closeDetail);
    $("#detail-scrim").addEventListener("click", closeDetail);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDetail();
    });

    document.addEventListener("click", (e) => {
      const chip = e.target.closest(".node-chip");
      if (chip) openDetail(chip.dataset.node);
    });
    $("#mcp-paths").addEventListener("click", (e) => {
      const node = e.target.closest(".mcp-node");
      if (node) openDetail(node.dataset.node);
    });

    window.addEventListener("resize", () => {
      clearTimeout(init.resizeTimer);
      init.resizeTimer = setTimeout(drawEdges, 120);
    });
  }

  return { init };
})();

window.addEventListener("DOMContentLoaded", () => architecturePage.init());
