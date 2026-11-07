/* =========================================================================
   后端接口封装
   所有 fetch 都集中在这里：加新功能时只改这一个文件，页面逻辑不用碰网络细节。
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
  history: (sessionId) =>
    requestJSON(`/chat/history?session_id=${encodeURIComponent(sessionId)}`),

  tools: (withMcp = false) =>
    requestJSON(`/chat/tools${withMcp ? "?with_mcp=1" : ""}`),

  config: () => requestJSON("/chat/config"),

  saveConfig: (cfg) =>
    requestJSON("/chat/config-save", { method: "POST", body: JSON.stringify(cfg) }),

  mcpList: () => requestJSON("/chat/mcp"),

  mcpSave: (server) =>
    requestJSON("/chat/mcp/save", { method: "POST", body: JSON.stringify(server) }),

  mcpDelete: (name) =>
    requestJSON("/chat/mcp/delete", { method: "POST", body: JSON.stringify({ name }) }),

  mcpTest: (server) =>
    requestJSON("/chat/mcp/test", { method: "POST", body: JSON.stringify(server) }),

  /**
   * 发送消息并消费 SSE 流。
   * 用 fetch + ReadableStream 而不是 EventSource：EventSource 只能发 GET，
   * 而这里要把消息体 POST 上去，且需要把 401/500 之类当成错误处理。
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
