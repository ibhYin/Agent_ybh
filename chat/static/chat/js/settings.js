/* =========================================================================
   设置抽屉：LLM 配置 / MCP 服务管理 / 可用工具
   ========================================================================= */

const settings = (() => {
  let servers = [];
  let toolsRequest = null;   // 并发去重：启动与打开设置可能同时触发
  let toolsCache = null;     // 连接 MCP 要起子进程（约 2s），成功后缓存，避免每次开设置都重连

  const THEMES = ["light", "warm", "dark", "navy"];

  const drawer = () => document.getElementById("settings-drawer");
  const overlay = () => document.getElementById("settings-overlay");

  // ---------- 打开 / 关闭 ----------

  function open(tabName = "llm") {
    drawer().classList.remove("hidden");
    overlay().classList.remove("hidden");
    switchTab(tabName);
    syncThemeCards();
    loadConfig();
    loadTools();
    loadServers();
  }

  function close() {
    drawer().classList.add("hidden");
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

  // ---------- 外观 ----------

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") || "light";
  }

  function syncThemeCards() {
    for (const card of document.querySelectorAll(".theme-card")) {
      const active = card.dataset.theme === currentTheme();
      card.classList.toggle("active", active);
      card.setAttribute("aria-pressed", active ? "true" : "false");
    }
  }

  function applyTheme(name) {
    if (!THEMES.includes(name)) return;
    document.documentElement.setAttribute("data-theme", name);
    try {
      localStorage.setItem("ui_theme", name);
    } catch (e) { /* 隐私模式忽略 */ }
    syncThemeCards();
  }

  // ---------- LLM ----------

  async function loadConfig() {
    const current = document.getElementById("llm-current");
    try {
      const c = await api.config();

      const modelChip = document.getElementById("model-chip");
      modelChip.classList.toggle("is-ok", c.api_key_configured);
      modelChip.classList.toggle("is-warn", !c.api_key_configured);
      modelChip.querySelector("span").textContent = c.model || "未配置模型";

      current.textContent =
        `${c.base_url || "（未设置）"} · ${c.model || "（未设置）"} · ` +
        `API Key ${c.api_key_configured ? "已配置" : "未配置"}（来源：${c.source}）`;

      document.getElementById("banner").classList.toggle("visible", !c.api_key_configured);
      setChip("llm", c.api_key_configured);
    } catch (e) {
      current.textContent = "无法读取当前配置：" + e.message;
    }
  }

  function setChip(kind, ok, label) {
    const chip = document.querySelector(`[data-chip="${kind}"]`);
    if (!chip) return;
    chip.classList.toggle("is-ok", ok);
    chip.classList.toggle("is-warn", !ok);
    chip.querySelector(".chip-label").textContent = label || (ok ? "已就绪" : "未配置");
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
      status.textContent = "已保存，立即生效";
      document.getElementById("cfg-api-key").value = "";
      await loadConfig();
    } catch (e) {
      status.className = "status-line err";
      status.textContent = "保存失败：" + e.message;
    }
  }

  // ---------- 工具 ----------

  function renderTools(data) {
    const list = document.getElementById("tool-list");
    const count = document.getElementById("tool-count");
    const errorBox = document.getElementById("tool-errors");

    ui.renderToolList(list, data.tools);
    count.textContent = `${data.local_count} 本地` + (data.mcp_count ? ` + ${data.mcp_count} MCP` : "");

    const errors = Object.entries(data.mcp_errors || {});
    errorBox.textContent = errors.length
      ? "部分服务连接失败：" + errors.map(([n, e]) => `${n}: ${e}`).join("；")
      : "";

    setChip("mcp", data.mcp_count > 0, data.mcp_count ? `MCP ${data.mcp_count}` : "MCP 0");
    const chip = document.querySelector('[data-chip="mcp"]');
    chip.title = errors.length ? errorBox.textContent : "";
  }

  function loadTools(force = false) {
    // 启动时会调一次，打开设置又会调一次：并发时复用同一个请求，
    // 已成功过则直接用缓存（刷新按钮可强制重连）。
    if (!force && toolsCache) {
      renderTools(toolsCache);
      return Promise.resolve(toolsCache);
    }
    if (toolsRequest) return toolsRequest;

    const refreshBtn = document.getElementById("tools-refresh");
    if (refreshBtn && force) {
      refreshBtn.disabled = true;
      refreshBtn.textContent = "刷新中…";
    }

    toolsRequest = (async () => {
      document.getElementById("tool-count").textContent = "…";
      try {
        const data = await api.tools(true);
        toolsCache = data;
        renderTools(data);
        return data;
      } catch (e) {
        document.getElementById("tool-list").textContent = "工具列表加载失败：" + e.message;
        document.getElementById("tool-count").textContent = "—";
        setChip("mcp", false, "MCP ?");
        return null;
      }
    })().finally(() => {
      toolsRequest = null;
      if (refreshBtn) {
        refreshBtn.disabled = false;
        refreshBtn.textContent = "刷新";
      }
    });

    return toolsRequest;
  }

  // ---------- MCP 服务 ----------

  async function loadServers() {
    const box = document.getElementById("server-list");
    box.innerHTML = '<div class="empty-note">加载中…</div>';
    try {
      const data = await api.mcpList();
      servers = data.servers || [];
      box.innerHTML = "";
      if (!servers.length) {
        box.innerHTML =
          '<div class="empty-note">还没有配置外部 MCP 服务<br>点「添加服务」，或写项目根目录的 mcp.json</div>';
        return;
      }
      for (const s of servers) box.appendChild(ui.renderServerCard(s));
    } catch (e) {
      box.innerHTML = "";
      box.appendChild(ui.el("div", "empty-note", "加载失败：" + e.message));
    }
  }

  function toggleTransportFields() {
    const transport = document.getElementById("mcp-transport").value;
    document.getElementById("field-stdio").style.display = transport === "stdio" ? "" : "none";
    document.getElementById("field-sse").style.display = transport === "sse" ? "" : "none";
  }

  function fillForm(server) {
    document.getElementById("mcp-name").value = server?.name || "";
    document.getElementById("mcp-transport").value = server?.transport || "stdio";
    document.getElementById("mcp-command").value = server?.command || "";
    document.getElementById("mcp-args").value = (server?.args || []).join("\n");
    document.getElementById("mcp-cwd").value = server?.cwd || "";
    document.getElementById("mcp-url").value = server?.url || "";
    document.getElementById("mcp-status").textContent = "";
    toggleTransportFields();
  }

  function readForm() {
    const transport = document.getElementById("mcp-transport").value;
    return {
      name: document.getElementById("mcp-name").value.trim(),
      transport,
      command: transport === "stdio" ? document.getElementById("mcp-command").value.trim() : "",
      // 一行一个参数，不做 shell 拆分：参数里的空格可能是合法的
      args: transport === "stdio"
        ? document.getElementById("mcp-args").value.split("\n").map((s) => s.trim()).filter(Boolean)
        : [],
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
      await loadServers();
      toolsCache = null;
      await loadTools(true);
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
      await loadServers();
      toolsCache = null;
      await loadTools(true);
      ui.toast("已删除", "ok");
    } catch (e) {
      ui.toast("删除失败：" + e.message, "err");
    }
  }

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
      document.getElementById("mcp-name").focus();
    }
  }

  // ---------- 初始化 ----------

  function init() {
    document.getElementById("settings-btn").addEventListener("click", () => open());
    document.getElementById("close-settings").addEventListener("click", close);
    overlay().addEventListener("click", close);

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !drawer().classList.contains("hidden")) close();
    });

    for (const tab of document.querySelectorAll(".tab")) {
      tab.addEventListener("click", () => switchTab(tab.dataset.tab));
    }

    document.getElementById("theme-grid").addEventListener("click", (e) => {
      const card = e.target.closest(".theme-card");
      if (card) applyTheme(card.dataset.theme);
    });

    document.getElementById("save-config").addEventListener("click", saveConfig);
    document.getElementById("mcp-transport").addEventListener("change", toggleTransportFields);
    document.getElementById("mcp-save").addEventListener("click", saveServer);
    document.getElementById("mcp-reset").addEventListener("click", () => fillForm(null));
    document.getElementById("mcp-add").addEventListener("click", () => {
      switchTab("mcp");
      fillForm(null);
      document.getElementById("mcp-name").focus();
    });
    document.getElementById("server-list").addEventListener("click", onServerListClick);
    document.getElementById("tools-refresh").addEventListener("click", () => loadTools(true));

    // 首屏点亮左栏状态灯
    syncThemeCards();
    loadConfig();
    loadTools();
  }

  return { init, open, close, loadConfig, loadTools, loadServers };
})();
