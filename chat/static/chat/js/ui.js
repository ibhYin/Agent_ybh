/* =========================================================================
   DOM 渲染与通用交互
   只负责把数据变成元素、以及抽屉/提示这类壳层交互；不碰网络与业务状态。
   ========================================================================= */

const ui = (() => {
  const messagesEl = () => document.getElementById("messages");
  const scrim = () => document.getElementById("scrim");
  const inner = () => document.querySelector(".messages-inner");

  // ---------- 基础 ----------

  function icon(name) {
    return `<svg class="ico" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function init() {
    // 统一装进一个限宽容器：空状态与消息共用同一列宽
    const wrap = el("div", "messages-inner");
    const empty = document.getElementById("empty-state");
    if (empty) wrap.appendChild(empty);
    messagesEl().appendChild(wrap);

    scrim().addEventListener("click", closeDrawers);
    for (const s of document.querySelectorAll(".suggestion")) {
      s.addEventListener("click", () => {
        const input = document.getElementById("input");
        input.value = s.dataset.prompt;
        input.focus();
      });
    }
  }

  function scrollToBottom() {
    const el = messagesEl();
    el.scrollTop = el.scrollHeight;
  }

  function setEmpty(show) {
    const empty = document.getElementById("empty-state");
    if (empty) empty.style.display = show ? "" : "none";
  }

  // ---------- 抽屉 ----------

  function openPanel(id) {
    const panel = document.getElementById(id);
    if (!panel) return;
    panel.classList.add("open");
    scrim().classList.remove("hidden");
  }

  function closeDrawers() {
    for (const id of ["sidebar", "trace"]) {
      document.getElementById(id)?.classList.remove("open");
    }
    scrim().classList.add("hidden");
  }

  // ---------- 消息 ----------

  function addMessage(kind, text = "") {
    setEmpty(false);
    const row = el("div", `msg ${kind}`);

    const avatar = el("div", "msg-avatar");
    if (kind === "assistant") avatar.innerHTML = icon("spark");
    else if (kind === "error") avatar.textContent = "!";
    else avatar.textContent = "你";

    const main = el("div", "msg-main");
    main.appendChild(el("div", "msg-role",
      kind === "user" ? "YOU" : kind === "assistant" ? "AGENT" : "ERROR"));

    const bubble = el("div", "bubble", text);
    main.appendChild(bubble);
    row.append(avatar, main);
    inner().appendChild(row);
    scrollToBottom();
    return bubble;
  }

  const addUser = (text) => addMessage("user", text);
  const addAssistant = () => addMessage("assistant");
  const addError = (text) => addMessage("error", text);

  function setTyping(bubble, on) {
    if (!bubble) return;
    bubble.classList.toggle("typing", !!on);
  }

  // ---------- 轻量 Markdown ----------
  // 只支持对话里最常见的两种：**加粗** 与 `代码`。
  // 先整体转义，再插入固定标签，避免模型输出里的 HTML 被执行。

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderRich(text) {
    let html = escapeHtml(text);
    html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^\n]+?)\*\*/g, "<strong>$1</strong>");
    return html;
  }

  function setRich(bubble, text) {
    if (!bubble) return;
    bubble.__raw = String(text ?? "");
    bubble.innerHTML = renderRich(bubble.__raw);
  }

  function appendRich(bubble, delta) {
    if (!bubble) return;
    bubble.__raw = (bubble.__raw || "") + String(delta ?? "");
    bubble.innerHTML = renderRich(bubble.__raw);
  }

  function clearMessages() {
    for (const node of inner().querySelectorAll(".msg")) node.remove();
    setEmpty(true);
  }

  // ---------- 轻提示 ----------

  let toastTimer = null;
  function toast(message, kind = "") {
    const node = document.getElementById("toast");
    node.textContent = message;
    node.className = `toast ${kind}`.trim();
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.add("hidden"), 2600);
  }

  // ---------- 时间 ----------

  function relativeTime(iso) {
    if (!iso) return "";
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return "";
    const diff = (Date.now() - t) / 1000;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    if (diff < 172800) return "昨天";
    const d = new Date(t);
    return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
  }

  // ---------- 设置面板：工具列表 ----------

  function renderToolList(container, tools) {
    container.innerHTML = "";
    if (!tools.length) {
      container.appendChild(el("div", "empty-note", "暂无工具"));
      return;
    }
    for (const t of tools) {
      const f = t.function;
      const details = el("details", "tool-item");
      const summary = el("summary");
      summary.appendChild(el("span", "t-name", f.name));
      summary.appendChild(el("span", "t-desc", f.description || "（无描述）"));
      details.appendChild(summary);
      details.appendChild(el("pre", null, JSON.stringify(f.parameters, null, 2)));
      container.appendChild(details);
    }
  }

  // ---------- 设置面板：MCP 服务卡片 ----------

  function renderServerCard(server) {
    const card = el("div", "server-card" + (server.enabled ? "" : " disabled"));

    const head = el("div", "server-head");
    head.appendChild(el("span", "server-name", server.name));
    head.appendChild(el("span", `server-tag ${server.transport}`, server.transport));

    if (server.source === "file") {
      const tag = el("span", "server-tag file", "mcp.json");
      tag.title = "来自项目根目录的 mcp.json，页面上不能编辑或删除";
      head.appendChild(tag);
    }

    const actions = el("div", "server-actions");
    if (server.source === "database") {
      const edit = el("button", "btn ghost sm", "编辑");
      edit.dataset.action = "edit";
      edit.dataset.name = server.name;
      const del = el("button", "btn danger sm", "删除");
      del.dataset.action = "delete";
      del.dataset.name = server.name;
      actions.append(edit, del);
    }
    const test = el("button", "btn ghost sm", "测试");
    test.dataset.action = "test";
    test.dataset.name = server.name;
    actions.appendChild(test);

    head.appendChild(actions);
    card.appendChild(head);

    const cmd = server.transport === "sse"
      ? server.url
      : [server.command, ...(server.args || [])].join(" ");
    card.appendChild(el("div", "server-cmd", cmd || "（未配置）"));

    const tools = el("div", "server-tools");
    card.appendChild(tools);
    return card;
  }

  function setServerTools(card, { tools, error }) {
    const box = card.querySelector(".server-tools");
    box.classList.add("show");
    if (error) {
      box.classList.add("err");
      box.textContent = "连接失败：" + error;
      return;
    }
    if (!tools.length) {
      box.textContent = "已连接，但该服务没有提供工具";
      return;
    }
    box.textContent = `已连接，${tools.length} 个工具：` + tools.map((t) => t.name).join("、");
  }

  return {
    icon,
    el,
    init,
    scrollToBottom,
    setEmpty,
    addUser,
    addAssistant,
    addError,
    setTyping,
    setRich,
    appendRich,
    clearMessages,
    toast,
    relativeTime,
    renderToolList,
    renderServerCard,
    setServerTools,
    openPanel,
    closeDrawers,
  };
})();
