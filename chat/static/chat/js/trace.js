/* =========================================================================
   右栏：执行过程
   实时展示模型每一轮调用了哪些工具（本地 / MCP）、参数、返回与耗时。
   历史回放时从消息里重建（没有耗时数据，只显示结构）。
   ========================================================================= */

const trace = (() => {
  const body = () => document.getElementById("trace-body");
  const emptyEl = () => document.getElementById("trace-empty");
  const statsEl = () => document.getElementById("trace-stats");

  let turnIndex = 0;
  let current = null;          // 正在进行的一轮
  let timer = null;
  let totalTools = 0;
  let totalMcp = 0;
  let lastDuration = null;

  const toolSteps = new Map(); // tool_call_id -> step 对象

  // ---------- 小工具 ----------

  function fmtDuration(ms) {
    if (ms == null) return "—";
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  }

  function preview(text, limit = 400) {
    const s = String(text ?? "");
    return s.length > limit ? s.slice(0, limit) + `\n…（共 ${s.length} 字符）` : s;
  }

  function scrollBottom() {
    const el = body();
    el.scrollTop = el.scrollHeight;
  }

  function showEmpty(show) {
    const el = emptyEl();
    if (el) el.style.display = show ? "" : "none";
  }

  function updateStats() {
    const stats = statsEl().querySelectorAll(".stat em");
    stats[0].textContent = totalTools;
    stats[1].textContent = totalMcp;
    stats[2].textContent = lastDuration != null ? fmtDuration(lastDuration) : "—";
    statsEl().querySelectorAll(".stat")[2].classList.toggle("done", lastDuration != null);
  }

  function refreshTurnLabels(turn) {
    turn.sum.textContent = turn.tools
      ? `工具 ${turn.tools} · MCP ${turn.mcp}`
      : "未调用工具";
  }

  // ---------- 结构 ----------

  function createTurn(prompt) {
    turnIndex += 1;
    const root = ui.el("div", "turn live");
    const head = ui.el("div", "turn-head");
    head.appendChild(ui.el("span", "turn-index", `#${turnIndex}`));
    head.appendChild(ui.el("span", "turn-prompt", prompt || "（无输入）"));
    const state = ui.el("span", "turn-state running");
    head.appendChild(state);

    const steps = ui.el("div", "turn-steps");
    const empty = ui.el("div", "turn-empty", "本轮没有调用工具");
    const foot = ui.el("div", "turn-foot");
    const sum = ui.el("span", "turn-sum", "运行中…");
    const time = ui.el("span", "turn-time", "—");
    foot.append(sum, time);

    root.append(head, steps, empty, foot);
    body().appendChild(root);

    return {
      index: turnIndex,
      root,
      state,
      steps,
      empty,
      sum,
      time,
      tools: 0,
      mcp: 0,
      startedAt: performance.now(),
      ended: false,
    };
  }

  function createStep({ id, name, args, source }) {
    const root = ui.el("div", "step running");
    const head = ui.el("button", "step-head");
    head.type = "button";

    head.appendChild(ui.el("span", "step-dot"));

    const nameEl = ui.el("span", "step-name", name);
    nameEl.title = name;
    head.appendChild(nameEl);

    const isMcp = source === "mcp" || (source == null && String(name).includes("__"));
    head.appendChild(ui.el("span", `badge ${isMcp ? "mcp" : "local"}`, isMcp ? "MCP" : "LOCAL"));

    const time = ui.el("span", "step-time", "");
    head.appendChild(time);

    const chevron = ui.el("span", "step-chevron");
    chevron.innerHTML = ui.icon("chevron");
    head.appendChild(chevron);

    const bodyEl = ui.el("div", "step-body");
    const argsKv = ui.el("div", "kv");
    argsKv.appendChild(ui.el("div", "kv-label", "参数"));
    argsKv.appendChild(ui.el("pre", null, preview(args || "{}")));
    bodyEl.appendChild(argsKv);

    root.append(head, bodyEl);
    root.classList.add("open");  // 进行中的步骤默认展开，方便看参数
    head.addEventListener("click", () => root.classList.toggle("open"));

    return { id, root, time, bodyEl, startedAt: performance.now(), isMcp };
  }

  // ---------- 对外：一轮的生命周期 ----------

  function startTurn(prompt, { historical = false } = {}) {
    showEmpty(false);
    current = createTurn(prompt);
    lastDuration = null;
    updateStats();
    scrollBottom();

    if (!historical) {
      if (timer) clearInterval(timer);
      timer = setInterval(() => {
        if (!current || current.ended) return;
        lastDuration = performance.now() - current.startedAt;
        current.time.textContent = fmtDuration(lastDuration);
        updateStats();
      }, 120);
    }
    return current;
  }

  function addTool({ id, name, args, source }) {
    if (!current) startTurn("");

    const step = createStep({ id, name, args, source });
    current.steps.appendChild(step.root);

    current.tools += 1;
    totalTools += 1;
    if (step.isMcp) {
      current.mcp += 1;
      totalMcp += 1;
    }
    refreshTurnLabels(current);
    updateStats();

    if (id) toolSteps.set(id, step);
    scrollBottom();
    return step;
  }

  function addThought(text, { historical = false } = {}) {
    // 思考块：插在工具步骤之前，展示模型"为什么调这个工具"。
    // 默认折叠，避免右栏被思考文字刷屏；用户点开才看。
    if (!text) return;
    if (!current) startTurn("");

    const root = ui.el("div", "thought" + (historical ? "" : ""));
    const head = ui.el("button", "thought-head");
    head.type = "button";

    head.appendChild(ui.el("span", "thought-dot"));
    head.appendChild(ui.el("span", "thought-label", "思考"));
    const preview = ui.el("span", "thought-preview", text);
    preview.title = text;
    head.appendChild(preview);

    const chevron = ui.el("span", "step-chevron");
    chevron.innerHTML = ui.icon("chevron");
    head.appendChild(chevron);

    const body = ui.el("div", "thought-body");
    body.appendChild(ui.el("div", "thought-text", text));

    root.append(head, body);
    head.addEventListener("click", () => root.classList.toggle("open"));

    current.steps.appendChild(root);
    scrollBottom();
  }

  function finishTool(id, result, { historical = false } = {}) {
    const step = toolSteps.get(id);
    if (!step) return;

    step.root.classList.remove("running");
    step.root.classList.add("ok");

    if (historical) {
      step.time.textContent = "";
      step.root.classList.remove("open");  // 历史步骤默认收起，避免右栏过长
    } else {
      step.time.textContent = fmtDuration(performance.now() - step.startedAt);
      step.root.classList.add("open");
    }

    if (result !== undefined && result !== null && result !== "") {
      let kv = step.bodyEl.querySelector(".kv-result");
      if (!kv) {
        kv = ui.el("div", "kv kv-result");
        kv.appendChild(ui.el("div", "kv-label", "结果"));
        kv.appendChild(ui.el("pre", null, preview(result)));
        step.bodyEl.appendChild(kv);
      }
    }
    scrollBottom();
  }

  function addError(message) {
    if (!current) startTurn("");
    const root = ui.el("div", "step error open");
    const head = ui.el("div", "step-head");
    head.appendChild(ui.el("span", "step-dot"));
    head.appendChild(ui.el("span", "step-name", "执行出错"));

    const bodyEl = ui.el("div", "step-body");
    bodyEl.appendChild(ui.el("pre", null, preview(message)));
    root.append(head, bodyEl);

    current.steps.appendChild(root);
    scrollBottom();
  }

  function endTurn(status = "ok") {
    if (!current || current.ended) return;
    current.ended = true;
    if (timer) { clearInterval(timer); timer = null; }

    lastDuration = performance.now() - current.startedAt;

    current.state.className = `turn-state ${status}`;
    current.root.classList.remove("live");
    current.empty.classList.toggle("show", current.tools === 0);
    refreshTurnLabels(current);
    current.time.textContent = fmtDuration(lastDuration);
    updateStats();

    current = null;
    scrollBottom();
  }

  // ---------- 对外：历史回放 ----------

  function reset() {
    toolSteps.clear();
    turnIndex = 0;
    current = null;
    totalTools = 0;
    totalMcp = 0;
    lastDuration = null;
    if (timer) { clearInterval(timer); timer = null; }

    body().querySelectorAll(".turn").forEach((n) => n.remove());
    showEmpty(true);
    updateStats();
  }

  function finalizeHistoricalTurn() {
    if (!current) return;
    current.ended = true;
    current.root.classList.remove("live");
    current.state.className = "turn-state ok";
    current.empty.classList.toggle("show", current.tools === 0);
    refreshTurnLabels(current);
    current.time.textContent = "";
    current = null;
  }

  function renderHistory(messages) {
    reset();
    showEmpty(true);

    for (const m of messages || []) {
      if (m.role === "user") {
        finalizeHistoricalTurn();  // 上一轮到此为止
        startTurn(m.content || "", { historical: true });
      } else if (m.role === "assistant") {
        // 思考：历史消息里存了 reasoning 私有字段
        if (m.reasoning) {
          addThought(m.reasoning, { historical: true });
        }
        for (const tc of m.tool_calls || []) {
          const fn = tc.function || {};
          addTool({
            id: tc.id,
            name: fn.name || "unknown",
            args: fn.arguments || "{}",
            source: String(fn.name || "").includes("__") ? "mcp" : "local",
          });
        }
      } else if (m.role === "tool") {
        finishTool(m.tool_call_id, m.content, { historical: true });
      }
    }

    finalizeHistoricalTurn();
    if (timer) { clearInterval(timer); timer = null; }
    updateStats();
    showEmpty(turnIndex === 0);
    if (turnIndex) scrollBottom();
  }

  function init() {
    updateStats();
  }

  return { init, startTurn, addTool, addThought, finishTool, addError, endTurn, reset, renderHistory };
})();
