/* =========================================================================
   后端接口封装
   页面逻辑不直接碰网络：加接口只改这一个文件。
   ========================================================================= */

async function requestJSON(url, options = {}) {
  const res = await fetch(url, {
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    ...options,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data.error) detail = data.error;
    } catch (_) { /* 响应不是 JSON，保留状态码 */ }
    throw new Error(detail);
  }
  return res.json();
}

const api = {
  // ---------- 会话 ----------
  sessions: () => requestJSON("/chat/sessions"),

  createSession: (title = "") =>
    requestJSON("/chat/sessions/create", { method: "POST", body: JSON.stringify({ title }) }),

  renameSession: (session_id, title) =>
    requestJSON("/chat/sessions/rename", {
      method: "POST",
      body: JSON.stringify({ session_id, title }),
    }),

  deleteSession: (session_id) =>
    requestJSON("/chat/sessions/delete", {
      method: "POST",
      body: JSON.stringify({ session_id }),
    }),

  // ---------- 对话 ----------
  history: (sessionId) =>
    requestJSON(`/chat/history?session_id=${encodeURIComponent(sessionId)}`),

  tools: (withMcp = false) => requestJSON(`/chat/tools${withMcp ? "?with_mcp=1" : ""}`),

  config: () => requestJSON("/chat/config"),

  saveConfig: (cfg) =>
    requestJSON("/chat/config-save", { method: "POST", body: JSON.stringify(cfg) }),

  // ---------- MCP 服务 ----------
  mcpList: () => requestJSON("/chat/mcp"),

  mcpSave: (server) =>
    requestJSON("/chat/mcp/save", { method: "POST", body: JSON.stringify(server) }),

  mcpDelete: (name) =>
    requestJSON("/chat/mcp/delete", { method: "POST", body: JSON.stringify({ name }) }),

  mcpTest: (server) =>
    requestJSON("/chat/mcp/test", { method: "POST", body: JSON.stringify(server) }),

  // ---------- MCP 管理页 ----------
  mcpBuiltin: () => requestJSON("/chat/mcp/builtin"),

  mcpStatus: () => requestJSON("/chat/mcp/status"),

  mcpToggle: (name, enabled) =>
    requestJSON("/chat/mcp/toggle", {
      method: "POST",
      body: JSON.stringify({ name, enabled }),
    }),

  mcpTestStored: (name) =>
    requestJSON("/chat/mcp/test-stored", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  /**
   * 发送消息并消费 SSE 流。
   * 用 fetch + ReadableStream 而不是 EventSource：EventSource 只能 GET，
   * 而这里要 POST 消息体，也需要把 4xx/5xx 当成错误处理。
   */
  async streamChat({ sessionId, message, onEvent, signal }) {
    const res = await fetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message }),
      signal,
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const data = await res.json();
        if (data.error) detail = data.error;
      } catch (_) { /* ignore */ }
      throw new Error(detail);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE：事件之间以空行分隔，每行形如 "data: {...}"
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        try {
          onEvent(JSON.parse(line.slice(6)));
        } catch (e) {
          console.error("无法解析 SSE 事件", e, chunk);
        }
      }
    }
  },
};
