/* =========================================================================
   DOM 渲染：只负责把数据变成元素，不碰网络、不碰业务状态。
   加新卡片类型时在这里加一个函数即可。
   ========================================================================= */

const ui = (() => {
  const messagesEl = () => document.getElementById("messages");

  function scrollToBottom() {
    const el = messagesEl();
    el.scrollTop = el.scrollHeight;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /** 移除空状态占位（首次发消息时调用） */
  function clearEmptyState() {
    const placeholder = document.querySelector(".empty-state");
    if (placeholder) placeholder.remove();
  }

  function addBubble(kind, text = "") {
    clearEmptyState();
    const row = el("div", `msg ${kind}`);
    const bubble = el("div", "bubble", text);
    row.appendChild(bubble);
    messagesEl().appendChild(row);
    scrollToBottom();
    return bubble;
  }

  const addUserBubble = (text) => addBubble("user", text);
  const addAssistantBubble = () => addBubble("assistant");
  const addErrorBubble = (text) => addBubble("error", text);

  /**
   * 工具卡片。source: "local" | "mcp"
   * 返回卡片句柄，后续 setToolResult 用它更新同一条记录。
   */
  function createToolCard(id, name, args, status, source = "local") {
    clearEmptyState();
    const details = el("details", "tool-card");
    details.open = status === "running";

    const summary = el("summary");
    summary.appendChild(el("span", "tool-icon", source === "mcp" ? "⇄" : "⚙"));

    const nameEl = el("span", "tool-name", name);
    nameEl.title = name;
    summary.appendChild(nameEl);

    if (source === "mcp") {
      summary.appendChild(el("span", "tool-badge mcp", "MCP"));
    }

    const statusEl = el("span", "tool-status" + (status === "running" ? " running" : ""));
    if (status === "running") {
      statusEl.appendChild(el("span", "spinner"));
      statusEl.appendChild(document.createTextNode("执行中"));
    } else {
      statusEl.textContent = "已完成";
    }
    summary.appendChild(statusEl);

    const body = el("div", "tool-body");
    body.appendChild(el("div", "label", "参数"));
    body.appendChild(el("pre", null, args || "{}"));

    details.append(summary, body);
    messagesEl().appendChild(details);
    scrollToBottom();

    return { el: details, body, statusEl };
  }

  function setToolResult(card, result) {
    if (!card) return;
    card.body.appendChild(el("div", "label", "结果"));
    card.body.appendChild(el("pre", null, result));
    card.statusEl.className = "tool-status";
    card.statusEl.textContent = "已完成";
    card.el.open = false; // 结果就绪后收起，保持聊天流整洁
    scrollToBottom();
  }

  /** 打字光标：流式输出时挂在气泡上 */
  function setTyping(bubble, typing) {
    if (!bubble) return;
    bubble.classList.toggle("caret", typing);
  }

  // ---------- 工具列表（设置面板） ----------
  function renderToolList(container, tools, localCount = null) {
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

  // ---------- MCP 服务卡片 ----------
  function createServerCard(server) {
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
      const editBtn = el("button", "btn btn-ghost btn-sm", "编辑");
      editBtn.dataset.action = "edit";
      editBtn.dataset.name = server.name;

      const delBtn = el("button", "btn btn-danger btn-sm", "删除");
      delBtn.dataset.action = "delete";
      delBtn.dataset.name = server.name;

      actions.append(editBtn, delBtn);
    }

    const testBtn = el("button", "btn btn-ghost btn-sm", "测试");
    testBtn.dataset.action = "test";
    testBtn.dataset.name = server.name;
    actions.appendChild(testBtn);

    head.appendChild(actions);
    card.appendChild(head);

    const cmd = server.transport === "sse"
      ? server.url
      : [server.command, ...(server.args || [])].join(" ");
    card.appendChild(el("div", "server-cmd", cmd || "（未配置）"));

    const toolsBox = el("div", "server-tools");
    toolsBox.style.display = "none";
    card.appendChild(toolsBox);

    return card;
  }

  function setServerTools(card, { tools, error }) {
    const box = card.querySelector(".server-tools");
    box.style.display = "block";
    box.className = "server-tools" + (error ? " err" : "");
    if (error) {
      box.textContent = "连接失败：" + error;
      return;
    }
    if (!tools.length) {
      box.textContent = "已连接，但该服务没有提供任何工具";
      return;
    }
    box.textContent = `已连接，${tools.length} 个工具：` + tools.map((t) => t.name).join("、");
  }

  return {
    el,
    scrollToBottom,
    clearEmptyState,
    addUserBubble,
    addAssistantBubble,
    addErrorBubble,
    createToolCard,
    setToolResult,
    setTyping,
    renderToolList,
    createServerCard,
    setServerTools,
  };
})();
