/* =========================================================================
   设置面板：LLM 配置 / MCP 服务 / 工具列表
   ========================================================================= */

const settings = (() => {
  const overlay = () => document.getElementById("settings-modal");

  function open(tabName) {
    overlay().classList.remove("hidden");
    switchTab(tabName || "llm");
    loadConfig();
    loadTools();
    loadServers();
  }

  function close() {
    overlay().classList.add("hidden");
  }

  function switchTab(name) {
    for (const tab of document.querySelectorAll(".tab")) {
      tab.classList.toggle("active", tab.dataset.tab === name);
    }
    for (const panel of document.querySelectorAll(".tab-panel")) {
      panel.classList.toggle("active", panel.dataset.panel === name);
    }
  }

  // ---------- LLM 配置 ----------

  async function loadConfig() {
    const status = document.getElementById("llm-current");
    try {
      const c = await api.config();
      status.textContent =
        `${c.base_url || "（未设置）"} · ${c.model || "（未设置）"} · ` +
        `API Key ${c.api_key_configured ? "已配置" : "未配置"}（来源：${c.source}）`;
      setChip("llm", c.api_key_configured);
      document.getElementById("config-banner").classList.toggle("visible", !c.api_key_configured);
    } catch (e) {
      status.textContent = "无法读取当前配置：" + e.message;
    }
  }

  function setChip(kind, ok) {
    const chip = document.querySelector(`[data-chip="${kind}"]`);
    if (!chip) return;
    chip.classList.toggle("is-ok", ok);
    chip.classList.toggle("is-warn", !ok);
    chip.querySelector(".chip-label").textContent =
      kind === "llm" ? (ok ? "LLM 已就绪" : "LLM 未配置") : (ok ? "MCP 已连接" : "MCP 未配置");
  }

  async function saveConfig() {
    const status = document.getElementById("llm-status");
    const cfg = {};
    const baseUrl = document.getElementById("cfg-base-url").value.trim();
    const apiKey = document.getElementById("cfg-api-key").value.trim();
    const model = document.getElementById("cfg-model").value.trim();
    if (baseUrl) cfg.base_url = baseUrl;
    if (apiKey) cfg.api_key = apiKey;
    if (model) cfg.model = model;

    if (!Object.keys(cfg).length) {
      status.className = "status-line err";
      status.textContent = "没有可保存的内容";
      return;
    }

    try {
      await api.saveConfig(cfg);
      status.className = "status-line ok";
      status.textContent = "已保存到服务器，立即生效";
      document.getElementById("cfg-api-key").value = "";
      loadConfig();
    } catch (e) {
      status.className = "status-line err";
      status.textContent = "保存失败：" + e.message;
    }
  }

  // ---------- 工具列表 ----------

  async function loadTools() {
    const list = document.getElementById("tool-list");
    const count = document.getElementById("tool-count");
    count.textContent = "…";
    try {
      // 带上 with_mcp 才会真的去连外部服务，可能慢一点，所以单独标出来
      const data = await api.tools(true);
      ui.renderToolList(list, data.tools);
      count.textContent = `${data.local_count} 本地` +
        (data.mcp_count ? ` + ${data.mcp_count} MCP` : "");
      const errors = Object.entries(data.mcp_errors || {});
      if (errors.length) {
        count.title = "部分服务连接失败：" + errors.map(([n, e]) => `${n}: ${e}`).join("；");
      }
      setChip("mcp", data.mcp_count > 0);
    } catch (e) {
      list.textContent = "工具列表加载失败：" + e.message;
      count.textContent = "—";
    }
  }

  // ---------- MCP 服务 ----------

  let servers = [];

  async function loadServers() {
    const box = document.getElementById("server-list");
    box.innerHTML = '<div class="empty-note">加载中…</div>';
    try {
      const data = await api.mcpList();
      servers = data.servers || [];
      box.innerHTML = "";
      if (!servers.length) {
        box.innerHTML =
          '<div class="empty-note">还没有配置外部 MCP 服务。<br>' +
          "点上方【添加服务】填写，或在项目根目录建一个 mcp.json。</div>";
        return;
      }
      for (const s of servers) box.appendChild(ui.createServerCard(s));
    } catch (e) {
      box.innerHTML = "";
      box.appendChild(ui.el("div", "empty-note", "加载失败：" + e.message));
    }
  }

  function fillForm(server) {
    document.getElementById("mcp-name").value = server?.name || "";
    document.getElementById("mcp-transport").value = server?.transport || "stdio";
    document.getElementById("mcp-command").value = server?.command || "";
    document.getElementById("mcp-args").value = (server?.args || []).join("\n");
    document.getElementById("mcp-cwd").value = server?.cwd || "";
    document.getElementById("mcp-url").value = server?.url || "";
    toggleTransportFields();
    document.getElementById("mcp-status").textContent = "";
  }

  function toggleTransportFields() {
    const transport = document.getElementById("mcp-transport").value;
    document.getElementById("field-stdio").style.display = transport === "stdio" ? "" : "none";
    document.getElementById("field-sse").style.display = transport === "sse" ? "" : "none";
  }

  function readForm() {
    const transport = document.getElementById("mcp-transport").value;
    const argsRaw = document.getElementById("mcp-args").value;
    return {
      name: document.getElementById("mcp-name").value.trim(),
      transport,
      command: transport === "stdio" ? document.getElementById("mcp-command").value.trim() : "",
      // 一行一个参数，保持原样（不做 shell 拆分：参数里的空格可能是合法的）
      args: transport === "stdio" ? argsRaw.split("\n").map((s) => s.trim()).filter(Boolean) : [],
      cwd: transport === "stdio" ? document.getElementById("mcp-cwd").value.trim() : "",
      url: transport === "sse" ? document.getElementById("mcp-url").value.trim() : "",
      enabled: true,
    };
  }

  async function saveServer() {
    const status = document.getElementById("mcp-status");
    const server = readForm();
    try {
      await api.mcpSave(server);
      status.className = "status-line ok";
      status.textContent = `已保存服务「${server.name}」`;
      fillForm(null);
      loadServers();
      loadTools();
    } catch (e) {
      status.className = "status-line err";
      status.textContent = "保存失败：" + e.message;
    }
  }

  async function testServer(server) {
    const status = document.getElementById("mcp-status");
    status.className = "status-line";
    status.textContent = "正在连接…";
    try {
      const r = await api.mcpTest(server);
      if (r.ok) {
        status.className = "status-line ok";
        status.textContent = `连接成功，${r.tools.length} 个工具：` +
          r.tools.map((t) => t.name).join("、");
      } else {
        status.className = "status-line err";
        status.textContent = "连接失败：" + r.error;
      }
    } catch (e) {
      status.className = "status-line err";
      status.textContent = "连接失败：" + e.message;
    }
  }

  async function deleteServer(name) {
    if (!confirm(`确定删除 MCP 服务「${name}」？`)) return;
    try {
      await api.mcpDelete(name);
      loadServers();
      loadTools();
    } catch (e) {
      alert("删除失败：" + e.message);
    }
  }

  /** 事件委托：服务卡片上的按钮 */
  function onServerListClick(e) {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const server = servers.find((s) => s.name === btn.dataset.name);
    if (!server) return;

    if (btn.dataset.action === "test") testServer(server);
    else if (btn.dataset.action === "delete") deleteServer(server.name);
    else if (btn.dataset.action === "edit") {
      switchTab("mcp");
      fillForm(server);
      document.getElementById("mcp-name").scrollIntoView({ block: "center" });
    }
  }

  // ---------- 初始化 ----------

  function init() {
    document.getElementById("settings-btn").addEventListener("click", () => open());
    document.getElementById("banner-settings").addEventListener("click", () => open());
    document.getElementById("close-settings").addEventListener("click", close);
    overlay().addEventListener("click", (e) => { if (e.target === overlay()) close(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !overlay().classList.contains("hidden")) close();
    });

    for (const tab of document.querySelectorAll(".tab")) {
      tab.addEventListener("click", () => switchTab(tab.dataset.tab));
    }

    document.getElementById("save-config").addEventListener("click", saveConfig);
    document.getElementById("mcp-transport").addEventListener("change", toggleTransportFields);
    document.getElementById("mcp-save").addEventListener("click", saveServer);
    document.getElementById("mcp-add").addEventListener("click", () => {
      fillForm(null);
      switchTab("mcp");
      document.getElementById("mcp-name").focus();
    });
    document.getElementById("mcp-reset").addEventListener("click", () => fillForm(null));
    document.getElementById("server-list").addEventListener("click", onServerListClick);

    // 首屏也要知道 LLM / MCP 状态，才能点亮顶栏那两个 chip
    loadConfig();
    loadTools();
  }

  return { init, open, close, loadConfig, loadTools, loadServers };
})();
