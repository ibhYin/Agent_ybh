/* =========================================================================
   会话与聊天流
   职责：session 管理、SSE 事件 → UI 映射、历史回放。
   ========================================================================= */

const chat = (() => {
  // 会话 ID：首次访问生成 UUID 存 localStorage，刷新后靠它从后端恢复历史
  let sessionId = localStorage.getItem("session_id");
  if (!sessionId) {
    sessionId = crypto.randomUUID();
    localStorage.setItem("session_id", sessionId);
  }

  // tool_call_id -> 卡片句柄（实时渲染与历史回放共用）
  const toolCards = new Map();

  let currentBubble = null;   // 当前正在流式输出的助手气泡
  let busy = false;
  let abortController = null;

  function setBusy(value) {
    busy = value;
    const input = document.getElementById("input");
    const sendBtn = document.getElementById("send");
    input.disabled = value;
    sendBtn.disabled = value;
    sendBtn.textContent = value ? "生成中" : "发送";
    if (!value) input.focus();
  }

  /** 从一个 SSE 事件更新界面 */
  function handleEvent(event) {
    switch (event.type) {
      case "text_delta":
        if (!currentBubble) currentBubble = ui.addAssistantBubble();
        currentBubble.textContent += event.content;
        ui.setTyping(currentBubble, true);
        ui.scrollToBottom();
        break;

      case "tool_start":
        ui.setTyping(currentBubble, false);
        currentBubble = null; // 工具调用打断本轮文本
        toolCards.set(
          event.id,
          ui.createToolCard(event.id, event.name, event.args, "running", event.source || "local")
        );
        break;

      case "tool_result":
        ui.setToolResult(toolCards.get(event.id), event.result);
        break;

      case "error":
        ui.setTyping(currentBubble, false);
        currentBubble = null;
        ui.addErrorBubble(event.message);
        break;

      case "done":
        ui.setTyping(currentBubble, false);
        currentBubble = null;
        break;

      // "message"：后端落库用，界面已经实时显示过了
    }
  }

  /** 回放历史消息（刷新页面后） */
  function renderHistory(messages) {
    for (const m of messages) {
      if (m.role === "user") {
        ui.addUserBubble(m.content);
      } else if (m.role === "assistant") {
        if (m.content) ui.addAssistantBubble().textContent = m.content;
        for (const tc of m.tool_calls || []) {
          // 历史里没有 source 字段，按工具名推断（MCP 工具带 __ 前缀）
          const source = tc.function.name.includes("__") ? "mcp" : "local";
          toolCards.set(
            tc.id,
            ui.createToolCard(tc.id, tc.function.name, tc.function.arguments, "done", source)
          );
        }
      } else if (m.role === "tool") {
        ui.setToolResult(toolCards.get(m.tool_call_id), m.content);
      }
    }
  }

  async function loadHistory() {
    try {
      const data = await api.history(sessionId);
      renderHistory(data.messages || []);
    } catch (e) {
      ui.addErrorBubble("无法加载历史记录：" + e.message);
    }
  }

  async function send() {
    const input = document.getElementById("input");
    const text = input.value.trim();
    if (!text || busy) return;

    input.value = "";
    autoGrow(input);
    ui.addUserBubble(text);
    setBusy(true);
    currentBubble = null;
    abortController = new AbortController();

    try {
      await api.streamChat({
        sessionId,
        message: text,
        onEvent: handleEvent,
        signal: abortController.signal,
      });
    } catch (e) {
      if (e.name !== "AbortError") ui.addErrorBubble("请求失败：" + e.message);
    } finally {
      ui.setTyping(currentBubble, false);
      setBusy(false);
      abortController = null;
    }
  }

  function stop() {
    if (abortController) abortController.abort();
  }

  /** 输入框随内容自动增高 */
  function autoGrow(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 160) + "px";
  }

  function init() {
    const input = document.getElementById("input");
    const sendBtn = document.getElementById("send");

    sendBtn.addEventListener("click", () => (busy ? stop() : send()));

    input.addEventListener("input", () => autoGrow(input));
    input.addEventListener("keydown", (e) => {
      // Enter 发送，Shift+Enter 换行
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send();
      }
    });

    loadHistory();
  }

  return { init, send, stop, get busy() { return busy; } };
})();
