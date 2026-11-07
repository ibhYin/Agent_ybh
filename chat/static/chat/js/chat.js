/* =========================================================================
   中栏：对话
   会话切换、消息渲染、SSE 事件映射（聊天文本 + 右侧执行过程）。
   ========================================================================= */

const chat = (() => {
  let sessionId = null;
  let busy = false;
  let controller = null;
  let currentBubble = null;

  // ---------- 头部与状态 ----------

  function setHeader(session) {
    const title = document.getElementById("chat-title");
    const meta = document.getElementById("chat-meta");
    if (!session) {
      title.textContent = "新对话";
      meta.textContent = "开始输入吧";
      return;
    }
    title.textContent = session.title || "新对话";
    const turns = session.turn_count ?? session.message_count;
    meta.textContent = `${turns} 轮对话 · ${ui.relativeTime(session.updated_at)}`;
  }

  function updateMeta(session) {
    setHeader(session);
  }

  function setStatus(text, kind = "") {
    const el = document.getElementById("composer-status");
    el.textContent = text;
    el.className = "composer-status" + (kind ? " " + kind : "");
  }

  function setBusy(value) {
    busy = value;
    const input = document.getElementById("input");
    const sendBtn = document.getElementById("send");
    input.disabled = value;
    sendBtn.innerHTML = ui.icon(value ? "stop" : "send");
    sendBtn.title = value ? "停止生成" : "发送";
    sendBtn.setAttribute("aria-label", value ? "停止生成" : "发送");
    if (!value) input.focus();
  }

  // ---------- 会话 ----------

  async function openSession(id) {
    if (busy) stop();

    sessionId = id;
    currentBubble = null;

    const meta = sessions.find(id);
    setHeader(meta);
    ui.clearMessages();
    trace.reset();

    try {
      const data = await api.history(id);
      renderHistory(data.messages || []);
      trace.renderHistory(data.messages || []);
      setStatus("就绪");
    } catch (e) {
      ui.addError("无法加载历史记录：" + e.message);
      setStatus("加载失败", "error");
    }

    document.getElementById("messages").scrollTop = 0;
  }

  function renderHistory(messages) {
    for (const m of messages) {
      if (m.role === "user") {
        ui.addUser(m.content || "");
      } else if (m.role === "assistant" && m.content) {
        ui.setRich(ui.addAssistant(), m.content);
      }
      // role="tool" 的内容归右侧过程面板展示
    }
    if (messages.length) ui.setEmpty(false);
  }

  // ---------- 发送与流式 ----------

  function handleEvent(event) {
    switch (event.type) {
      case "text_delta":
        if (!currentBubble) currentBubble = ui.addAssistant();
        ui.appendRich(currentBubble, event.content);
        ui.setTyping(currentBubble, true);
        ui.scrollToBottom();
        break;

      case "tool_start":
        ui.setTyping(currentBubble, false);
        currentBubble = null;
        trace.addTool({
          id: event.id,
          name: event.name,
          args: event.args,
          source: event.source,
        });
        setStatus(`调用 ${event.name}…`, "busy");
        break;

      case "thought":
        // 模型在调工具前的思考，展示在右栏，不打断对话流
        trace.addThought(event.content);
        break;

      case "tool_result":
        trace.finishTool(event.id, event.result);
        setStatus("工具已返回，继续生成…", "busy");
        break;

      case "error":
        ui.setTyping(currentBubble, false);
        currentBubble = null;
        ui.addError(event.message);
        trace.addError(event.message);
        trace.endTurn("error");
        setStatus("出错", "error");
        break;

      case "done":
        ui.setTyping(currentBubble, false);
        currentBubble = null;
        trace.endTurn("ok");
        setStatus("就绪");
        break;

      // "message"：后端落库用，界面已实时呈现
    }
  }

  async function send() {
    const input = document.getElementById("input");
    const text = input.value.trim();
    if (!text || busy) return;

    if (!sessionId) {
      await sessions.newSession();
      if (!sessionId) return;
    }

    input.value = "";
    autoGrow(input);
    ui.addUser(text);
    setBusy(true);
    setStatus("正在生成…", "busy");
    currentBubble = null;
    trace.startTurn(text);

    controller = new AbortController();

    try {
      await api.streamChat({
        sessionId,
        message: text,
        onEvent: handleEvent,
        signal: controller.signal,
      });
    } catch (e) {
      if (e.name === "AbortError") {
        trace.endTurn("stopped");
        setStatus("已停止");
      } else {
        ui.addError("请求失败：" + e.message);
        trace.addError("请求失败：" + e.message);
        trace.endTurn("error");
        setStatus("请求失败", "error");
      }
    } finally {
      ui.setTyping(currentBubble, false);
      setBusy(false);
      controller = null;
      // 会话标题/条数可能被后端更新，刷新左栏与头部
      sessions.refresh().catch(() => {});
    }
  }

  function stop() {
    if (controller) controller.abort();
  }

  function autoGrow(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 160) + "px";
  }

  // ---------- 初始化 ----------

  function init() {
    const input = document.getElementById("input");
    const sendBtn = document.getElementById("send");

    sendBtn.addEventListener("click", () => (busy ? stop() : send()));
    input.addEventListener("input", () => autoGrow(input));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send();
      }
    });

    document.getElementById("sidebar-toggle").addEventListener("click", () => ui.openPanel("sidebar"));
    document.getElementById("trace-toggle").addEventListener("click", () => ui.openPanel("trace"));
    document.getElementById("trace-close").addEventListener("click", ui.closeDrawers);

    document.getElementById("model-chip").addEventListener("click", () => settings.open("llm"));
    document.getElementById("banner-settings").addEventListener("click", () => settings.open("llm"));

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") ui.closeDrawers();
    });
  }

  return { init, openSession, updateMeta, send, stop };
})();
