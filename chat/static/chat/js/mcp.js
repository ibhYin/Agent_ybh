/* =========================================================================
   MCP 管理页（/mcp）
   卡片网格：配置来自 /chat/mcp，连接状态与工具来自 /chat/mcp/status。
   ========================================================================= */

const mcpPage = (() => {
  let servers = [];
  let builtins = [];
  let statusByName = {};
  let lastCheckedAt = null;
  let detecting = false;
  let editingName = null;

  const grid = () => document.getElementById("server-grid");
  const builtinGrid = () => document.getElementById("builtin-grid");
  const drawer = () => document.getElementById("server-drawer");
  const scrim = () => document.getElementById("drawer-scrim");

  // ---------- 基础 ----------

  function icon(name) {
    return `<svg class="ico" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  let toastTimer = null;
  function toast(message, kind = "") {
    const node = document.getElementById("toast");
    node.textContent = message;
    node.className = `toast ${kind}`.trim();
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.add("hidden"), 2600);
  }

  function commandOf(server) {
    if (server.transport === "sse") return server.url || "（未填 URL）";
    return [server.command, ...(server.args || [])].join(" ") || "（未填命令）";
  }

  function statusOf(server) {
    return statusByName[server.name] || null;
  }

  // ---------- 数据 ----------

  async function loadAll() {
    try {
      const [list, catalog] = await Promise.all([api.mcpList(), api.mcpBuiltin()]);
      servers = list.servers || [];
      builtins = catalog.builtins || [];
      renderServers();
      renderBuiltins();
      updateStats();
      await checkAll();
    } catch (e) {
      document.getElementById("server-note").textContent = "加载失败：" + e.message;
      toast("加载失败：" + e.message, "err");
    }
  }

  async function checkAll() {
    const enabled = servers.filter((s) => s.enabled !== false);
    if (!enabled.length) {
      statusByName = {};
      setNote("没有启用中的服务");
      renderServers();
      updateStats();
      return;
    }

    detecting = true;
    setNote("检测中…（每个服务要起一次进程）");
    document.getElementById("refresh-all").disabled = true;
    renderServers();

    try {
      const data = await api.mcpStatus();
      statusByName = {};
      for (const item of data.servers) statusByName[item.name] = item;
      lastCheckedAt = Date.now();
      setNote(`已检测 ${data.servers.length} 个服务`);
    } catch (e) {
      setNote("检测失败：" + e.message);
      toast("检测失败：" + e.message, "err");
    } finally {
      detecting = false;
      document.getElementById("refresh-all").disabled = false;
      renderServers();
      updateStats();
    }
  }

  function setNote(text) {
    document.getElementById("server-note").textContent = text;
  }

  function updateStats() {
    const enabled = servers.filter((s) => s.enabled !== false);
    const online = enabled.filter((s) => statusOf(s)?.ok).length;
    const toolCount = enabled.reduce((sum, s) => sum + (statusOf(s)?.tools?.length || 0), 0);

    document.getElementById("stat-servers").textContent = servers.length;
    const onlineEl = document.getElementById("stat-online");
    onlineEl.textContent = online;
    onlineEl.className = online ? "ok" : "warn";
    document.getElementById("stat-tools").textContent = toolCount;
    document.getElementById("stat-time").textContent = lastCheckedAt ? "刚刚" : "—";
  }

  // ---------- 已配置服务卡片 ----------

  function renderServers() {
    const box = grid();
    box.innerHTML = "";

    if (!servers.length) {
      box.appendChild(el("div", "empty-note",
        "还没有配置服务。点右上角「新建服务」，或从下方内置服务里添加。"));
      return;
    }

    for (const server of servers) box.appendChild(serverCard(server));
  }

  function serverCard(server) {
    const enabled = server.enabled !== false;
    const status = statusOf(server);
    const card = el("article", "mcp-card"
      + (enabled ? "" : " is-off")
      + (status && !status.ok ? " is-error" : ""));

    // 头部
    const head = el("div", "mc-head");
    const dotState = !enabled ? "" : detecting ? "busy" : !status ? "" : status.ok ? "ok" : "err";
    const dot = el("span", `dot ${dotState}`.trim());
    head.appendChild(dot);
    head.appendChild(el("h3", null, server.name));
    head.appendChild(el("span", `tag ${server.source === "file" ? "file" : "db"}`,
      server.source === "file" ? "mcp.json" : "数据库"));
    head.appendChild(el("span", `tag ${server.transport}`, server.transport));
    head.appendChild(el("span", "mc-spacer"));

    const sw = el("label", "switch");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = enabled;
    input.dataset.name = server.name;
    input.dataset.act = "toggle";
    if (server.source === "file") {
      input.disabled = true;
      sw.title = "mcp.json 里的服务请在文件里启停";
    }
    sw.append(input, el("span", "track"));
    head.appendChild(sw);

    // 命令
    const cmd = el("div", "mc-cmd", commandOf(server));
    cmd.title = cmd.textContent;

    // 工具区
    const tools = el("div", "mc-tools");
    if (!enabled) {
      tools.appendChild(el("span", "mc-msg", "已停用，不参与对话"));
    } else if (status?.ok) {
      if (!status.tools.length) {
        tools.appendChild(el("span", "mc-msg", "已连接，但该服务没有提供工具"));
      } else {
        for (const tool of status.tools) {
          const chip = el("span", "tool-chip", tool.name);
          chip.title = tool.description || tool.name;
          tools.appendChild(chip);
        }
      }
    } else if (status && !status.ok) {
      tools.appendChild(el("span", "mc-msg err", "连接失败：" + status.error));
    } else if (detecting) {
      tools.appendChild(el("span", "mc-msg", "检测中…"));
    } else {
      tools.appendChild(el("span", "mc-msg", "未检测"));
    }

    // 底部
    const foot = el("div", "mc-foot");
    const when = el("span", "when",
      status ? `${status.elapsed_ms} ms` : enabled ? "待检测" : "—");
    foot.appendChild(when);

    const test = el("button", "btn ghost sm", "测试");
    test.dataset.act = "test";
    test.dataset.name = server.name;
    test.disabled = !enabled;  // 文件来源也能测：后端按名字会带上它的 env
    foot.appendChild(test);

    if (server.source !== "file") {
      const edit = el("button", "btn ghost sm", "编辑");
      edit.dataset.act = "edit";
      edit.dataset.name = server.name;
      const del = el("button", "btn danger sm", "删除");
      del.dataset.act = "delete";
      del.dataset.name = server.name;
      foot.append(edit, del);
    }

    card.append(head, cmd, tools, foot);
    return card;
  }

  // ---------- 内置服务 ----------

  function renderBuiltins() {
    const box = builtinGrid();
    box.innerHTML = "";

    if (!builtins.length) {
      box.appendChild(el("div", "empty-note", "没有内置服务"));
      return;
    }

    for (const item of builtins) {
      const card = el("article", "mcp-card builtin-card");
      const head = el("div", "mc-head");
      head.appendChild(el("h3", null, item.title));
      head.appendChild(el("span", "tag builtin", "内置"));
      head.appendChild(el("span", "mc-spacer"));
      card.appendChild(head);

      card.appendChild(el("p", null, item.description));
      card.appendChild(el("div", "module", item.module));

      const chips = el("div", "chips");
      for (const tool of item.tools || []) chips.appendChild(el("span", "tool-chip", tool));
      card.appendChild(chips);

      const foot = el("div", "mc-foot");
      const btn = el("button", item.installed ? "btn ghost sm" : "btn sm", item.installed ? "已添加" : "添加");
      btn.dataset.act = "add-builtin";
      btn.dataset.name = item.name;
      btn.disabled = !!item.installed;
      foot.appendChild(btn);
      card.appendChild(foot);

      box.appendChild(card);
    }
  }

  // ---------- 操作 ----------

  async function toggleServer(name, enabled) {
    try {
      await api.mcpToggle(name, enabled);
      const server = servers.find((s) => s.name === name);
      if (server) server.enabled = enabled;
      if (!enabled) delete statusByName[name];
      renderServers();
      updateStats();
      toast(enabled ? `已启用 ${name}` : `已停用 ${name}`, "ok");
      await checkAll();
    } catch (e) {
      toast("切换失败：" + e.message, "err");
      await loadAll();
    }
  }

  async function testServer(name) {
    try {
      const result = await api.mcpTestStored(name);
      statusByName[name] = {
        name,
        ok: result.ok,
        tools: result.tools || [],
        error: result.ok ? "" : result.error,
        elapsed_ms: statusByName[name]?.elapsed_ms || 0,
      };
      renderServers();
      updateStats();
      toast(result.ok ? `${name}：${result.tools.length} 个工具` : `${name} 连接失败`,
        result.ok ? "ok" : "err");
    } catch (e) {
      toast("测试失败：" + e.message, "err");
    }
  }

  async function deleteServer(name) {
    if (!confirm(`删除 MCP 服务「${name}」？`)) return;
    try {
      await api.mcpDelete(name);
      toast("已删除", "ok");
      await loadAll();
    } catch (e) {
      toast("删除失败：" + e.message, "err");
    }
  }

  async function addBuiltin(name) {
    const item = builtins.find((b) => b.name === name);
    if (!item) return;
    try {
      await api.mcpSave({
        name: item.name,
        transport: item.transport || "stdio",
        command: item.command,
        args: item.args,
        cwd: item.cwd,
        env: item.env,
        enabled: true,
      });
      toast(`已添加内置服务 ${item.title}`, "ok");
      await loadAll();
    } catch (e) {
      toast("添加失败：" + e.message, "err");
    }
  }

  // ---------- 编辑抽屉 ----------

  function toggleTransportFields() {
    const transport = document.getElementById("f-transport").value;
    document.getElementById("f-stdio-fields").style.display = transport === "stdio" ? "" : "none";
    document.getElementById("f-sse-fields").style.display = transport === "sse" ? "" : "none";
  }

  function fillForm(server) {
    editingName = server?.name || null;
    document.getElementById("drawer-title").textContent = server ? `编辑 ${server.name}` : "添加服务";
    document.getElementById("f-name").value = server?.name || "";
    document.getElementById("f-name").disabled = !!server;
    document.getElementById("f-transport").value = server?.transport || "stdio";
    document.getElementById("f-command").value = server?.command || "";
    document.getElementById("f-args").value = (server?.args || []).join("\n");
    document.getElementById("f-cwd").value = server?.cwd || "";
    document.getElementById("f-url").value = server?.url || "";
    document.getElementById("f-env").value = "";

    const hint = document.getElementById("env-hint");
    if (server && server.env_keys?.length) {
      hint.textContent = `已保存 ${server.env_keys.length} 个环境变量（${server.env_keys.join("、")}）；留空表示保留，不会回显原值。`;
    } else {
      hint.textContent = "可留空；需要时一行一个 KEY=VALUE。";
    }

    document.getElementById("form-status").textContent = "";
    toggleTransportFields();
  }

  function openEditor(server) {
    fillForm(server);
    drawer().classList.remove("hidden");
    scrim().classList.remove("hidden");
    setTimeout(() => document.getElementById("f-name").focus(), 50);
  }

  function closeEditor() {
    drawer().classList.add("hidden");
    scrim().classList.add("hidden");
  }

  function readEnv() {
    const lines = document.getElementById("f-env").value
      .split("\n").map((s) => s.trim()).filter(Boolean);
    const env = {};
    for (const line of lines) {
      const idx = line.indexOf("=");
      if (idx <= 0) return { error: `环境变量格式应为 KEY=VALUE：${line}` };
      env[line.slice(0, idx)] = line.slice(idx + 1);
    }
    return { env };
  }

  async function saveServer() {
    const status = document.getElementById("form-status");
    const parsed = readEnv();
    if (parsed.error) {
      status.className = "status-line err";
      status.textContent = parsed.error;
      return;
    }

    const transport = document.getElementById("f-transport").value;
    const payload = {
      name: document.getElementById("f-name").value.trim(),
      transport,
      command: transport === "stdio" ? document.getElementById("f-command").value.trim() : "",
      args: transport === "stdio"
        ? document.getElementById("f-args").value.split("\n").map((s) => s.trim()).filter(Boolean)
        : [],
      cwd: transport === "stdio" ? document.getElementById("f-cwd").value.trim() : "",
      url: transport === "sse" ? document.getElementById("f-url").value.trim() : "",
    };
    // 环境变量留空 -> 不传，后端保留原值
    if (Object.keys(parsed.env).length) payload.env = parsed.env;

    try {
      await api.mcpSave(payload);
      status.className = "status-line ok";
      status.textContent = "已保存";
      toast("已保存", "ok");
      closeEditor();
      await loadAll();
    } catch (e) {
      status.className = "status-line err";
      status.textContent = "保存失败：" + e.message;
    }
  }

  // ---------- 事件 ----------

  function init() {
    document.getElementById("new-server").addEventListener("click", () => openEditor(null));
    document.getElementById("refresh-all").addEventListener("click", checkAll);
    document.getElementById("close-drawer").addEventListener("click", closeEditor);
    document.getElementById("reset-form").addEventListener("click", () => fillForm(null));
    document.getElementById("save-server").addEventListener("click", saveServer);
    document.getElementById("f-transport").addEventListener("change", toggleTransportFields);
    scrim().addEventListener("click", closeEditor);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !drawer().classList.contains("hidden")) closeEditor();
    });

    grid().addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-act]");
      if (btn) {
        const { act, name } = btn.dataset;
        if (act === "test") testServer(name);
        else if (act === "delete") deleteServer(name);
        else if (act === "edit") {
          const server = servers.find((s) => s.name === name);
          if (server) openEditor(server);
        }
        return;
      }
    });
    grid().addEventListener("change", (e) => {
      const input = e.target.closest('input[data-act="toggle"]');
      if (input) toggleServer(input.dataset.name, input.checked);
    });

    builtinGrid().addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-act]");
      if (btn && btn.dataset.act === "add-builtin") addBuiltin(btn.dataset.name);
    });

    loadAll();
  }

  return { init };
})();

window.addEventListener("DOMContentLoaded", () => mcpPage.init());
