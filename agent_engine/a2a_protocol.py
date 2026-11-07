"""A2A（Agent2Agent）协议层：消息构造、状态机与 JSON-RPC 封装。

只做"协议翻译"，不碰 Django、也不调 LLM——这样能单独测。

关键差异（与 MCP 对比）：
- MCP：一个调用一个函数，无状态，返回文本
- A2A：一个 **Task** 有生命周期，可多轮、可取消、有产物（artifact）

这里按 A2A v0.2.5 的线上格式实现（JSON-RPC 方法名 `message/send` 等），
同时兼容 v1.0 草案的驼峰方法名（`SendMessage`），因为两类客户端都在用。
"""
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

PROTOCOL_VERSION = "0.2.5"

# Task 状态机（v0.2.5 是小写字符串；v1.0 的 proto 用 TASK_STATE_* 枚举）
SUBMITTED = "submitted"
WORKING = "working"
COMPLETED = "completed"
FAILED = "failed"
CANCELED = "canceled"
INPUT_REQUIRED = "input-required"
REJECTED = "rejected"
AUTH_REQUIRED = "auth-required"

TERMINAL_STATES = {COMPLETED, FAILED, CANCELED, REJECTED}
INTERRUPTED_STATES = {INPUT_REQUIRED, AUTH_REQUIRED}

# v1.0 客户端可能用这些名字调用；映射到内部的一致行为
METHOD_ALIASES = {
    "message/send": "message/send",
    "SendMessage": "message/send",
    "message/stream": "message/stream",
    "SendStreamingMessage": "message/stream",
    "tasks/get": "tasks/get",
    "GetTask": "tasks/get",
    "tasks/list": "tasks/list",
    "ListTasks": "tasks/list",
    "tasks/cancel": "tasks/cancel",
    "CancelTask": "tasks/cancel",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class A2ATask:
    """一个 A2A 任务。contextId 对应本项目的一个 ChatSession，用来延续多轮对话。"""

    task_id: str
    context_id: str
    state: str = SUBMITTED
    status_message: dict | None = None
    artifacts: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # 取消标记：执行线程只检查这个布尔值，不强行杀线程
    cancel_requested: bool = False
    error: str = ""

    def set_state(self, state: str, message: dict | None = None) -> None:
        self.state = state
        self.status_message = message
        self.updated_at = time.time()

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self, history_length: int | None = None) -> dict:
        history = self.history
        if history_length is not None:
            history = history[-history_length:] if history_length > 0 else []
        status = {"state": self.state, "timestamp": now_iso()}
        if self.status_message:
            status["message"] = self.status_message
        return {
            "id": self.task_id,
            "contextId": self.context_id,
            "status": status,
            "artifacts": self.artifacts,
            "history": history,
            "metadata": {"project": "agent-ybh", "protocol": PROTOCOL_VERSION},
        }


# ---------------------------------------------------------------------------
# Message / Part 构造
# ---------------------------------------------------------------------------

def text_part(text: str) -> dict:
    return {"kind": "text", "text": text}


def agent_message(text: str, *, task_id: str, context_id: str, message_id: str | None = None) -> dict:
    return {
        "kind": "message",
        "messageId": message_id or new_id("msg"),
        "role": "agent",
        "parts": [text_part(text)],
        "taskId": task_id,
        "contextId": context_id,
    }


def extract_text(message: dict) -> str:
    """从 A2A Message 里取出纯文本；支持 text / data 两类 part。

    只取文字是有意的：本项目的能力都吃文本输入，文件与结构化输入
    目前没有语义化的处理方式，强行接受反而会误导调用方。
    """
    parts = message.get("parts") or []
    chunks = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("kind") == "text" or "text" in part:
            text = part.get("text")
            if text:
                chunks.append(str(text))
        elif part.get("kind") == "data" and part.get("data") is not None:
            import json
            chunks.append(json.dumps(part["data"], ensure_ascii=False))
    return "\n".join(chunks).strip()


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

def rpc_result(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id, code: int, message: str, data=None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


# 常用错误码：-32602 参数问题，-32601 方法不存在，-32603 内部错误
ERROR_PARSE = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602
ERROR_INTERNAL = -32603
ERROR_TASK_NOT_FOUND = -32001
ERROR_TASK_NOT_CANCELABLE = -32002


def event_task(task: A2ATask, history_length: int | None = None) -> dict:
    return {"kind": "task", "task": task.to_dict(history_length)}


def event_status(task: A2ATask, final: bool = False) -> dict:
    payload = {
        "kind": "status-update",
        "taskId": task.task_id,
        "contextId": task.context_id,
        "status": {"state": task.state, "timestamp": now_iso()},
        "final": final,
    }
    if task.status_message:
        payload["status"]["message"] = task.status_message
    return payload


def event_artifact(task: A2ATask, artifact: dict, append: bool = False, last_chunk: bool = True) -> dict:
    return {
        "kind": "artifact-update",
        "taskId": task.task_id,
        "contextId": task.context_id,
        "artifact": artifact,
        "append": append,
        "lastChunk": last_chunk,
    }


def event_message(message: dict) -> dict:
    return {"kind": "message", "message": message}


def artifact_from_text(text: str, name: str = "answer") -> dict:
    return {
        "artifactId": new_id("art"),
        "name": name,
        "parts": [text_part(text)],
    }


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------

def build_agent_card(base_url: str, model_name: str = "", tool_names: list[str] | None = None) -> dict:
    """生成 AgentCard。skills 描述"能做什么"，由工具清单推导，无需手写维护。"""
    tool_names = tool_names or []
    skills = [
        {
            "id": "agentic-loop",
            "name": "带工具的多步推理",
            "description": "接收一句自然语言任务，自动决定调用哪些工具，多步执行后给出结论。",
            "tags": ["reasoning", "tool-use", "agentic-loop"],
            "examples": [
                "现在几点了？",
                "计算 (3+5)*7 然后告诉我结果",
            ],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
        },
    ]
    if any(name.endswith("__query") or name.endswith("__list_tables") for name in tool_names):
        skills.append({
            "id": "sqlite-query",
            "name": "SQLite 只读查询",
            "description": "在本地 SQLite 数据库上执行只读 SQL，返回表结构或查询结果。",
            "tags": ["sql", "database", "read-only"],
            "examples": ["orders 表有多少条记录？", "列出所有表"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
        })
    if any("list_files" in name or "read_text" in name for name in tool_names):
        skills.append({
            "id": "filesystem",
            "name": "受限目录文件操作",
            "description": "在沙箱目录内列目录、读文本、按内容搜索。",
            "tags": ["files", "search"],
            "examples": ["列一下工作区里有哪些文件", "在文件里搜'预算'"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
        })

    card = {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "智能体 MVP",
        "description": "手写 Agentic Loop 的本地智能体：自带工具与 MCP 客户端，"
                       "能完成计算、取时间、受限文件读写与 SQLite 只读查询。",
        "url": base_url.rstrip("/") + "/a2a",
        "version": "1.0.0",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
        "supportsAuthenticatedExtendedCard": False,
    }
    if model_name:
        card["metadata"] = {"model": model_name}
    return card
