"""MCP 客户端：让本项目的 agent 反过来调用外部 MCP 服务。

难点不在协议，在同步/异步的接缝：
- `run_agent()` 是**同步生成器**（Django StreamingHttpResponse 直接消费它）
- MCP SDK 是**全异步**的（`async with Client(...)`）

三个可选方案：
| 方案 | 做法 | 代价 |
|---|---|---|
| A 每次新建 | 每次调用 `asyncio.run(...)` | stdio 服务每次重开会话，npx 类服务要 1–2s，有状态的 server 用不了 |
| B 常驻连接 + 后台线程（本文件采用） | 一条线程跑一个长生命周期任务，对外暴露同步 `call()` | 要管生命周期与超时，约 100 行 |
| C 全链路异步 | 把 run_agent 改 async、视图改 async、换 ASGI | 动到教学核心代码，runserver 也要换成 uvicorn |

选 B：`run_agent` 一行不用改，stdio 连接只建一次，有状态的服务也能用。

**两个必须遵守的约束**（都是 anyio 的机制，不看源码会踩）：

1. `Client.__aenter__()` 内部建立 cancel scope，anyio 要求"在哪个任务里进入，
   就在哪个任务里退出"。所以连接、请求、关闭**全部在同一个 `_serve()` 任务内**完成，
   跨线程只通过队列投递请求。否则 close 时抛
   `RuntimeError: Attempted to exit cancel scope in a different task`。

2. 关闭不能靠 `loop.stop()`：那会在 `__aexit__` 执行到一半时把循环打断，
   留下 "async generator is already running" / "loop is closed" 之类的残局。
   正确做法是往队列投一个哨兵，让 `_serve()` 自己走完 `__aexit__` 再退出，
   线程主函数随后 `run_until_complete` 收尾。
"""
import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass, field

from mcp import Client
from mcp.client.stdio import StdioServerParameters

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


@dataclass
class MCPServerSpec:
    """一个外部 MCP 服务的连接描述。name 用作工具名前缀，必须唯一。"""

    name: str
    transport: str = "stdio"          # "stdio" | "sse"
    command: str = ""                  # stdio 必填，如 "npx"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""                      # sse 必填，如 "http://127.0.0.1:8022/sse"
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "MCPServerSpec":
        return cls(
            name=str(data.get("name", "")).strip(),
            transport=str(data.get("transport", "stdio")),
            command=str(data.get("command", "")),
            args=list(data.get("args") or []),
            env=dict(data.get("env") or {}),
            cwd=data.get("cwd") or None,
            url=str(data.get("url", "")),
            enabled=bool(data.get("enabled", True)),
        )

    def validate(self) -> str:
        """返回错误信息，空字符串表示合法。"""
        if not self.name:
            return "缺少服务名"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", self.name):
            return "服务名只能用字母、数字、下划线和连字符，且不超过 32 字符"
        if self.transport not in ("stdio", "sse"):
            return f"不支持的传输方式：{self.transport}"
        if self.transport == "stdio" and not self.command:
            return "stdio 方式必须填写启动命令"
        if self.transport == "sse" and not self.url:
            return "sse 方式必须填写 URL"
        return ""


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """把远程工具映射成本地唯一的工具名。

    OpenAI function calling 要求函数名匹配 ^[a-zA-Z0-9_-]{1,64}$，
    所以把其他字符换成下划线，再用 `__` 双重分隔避免歧义。
    """
    def clean(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", s)

    return f"{clean(server_name)}__{clean(tool_name)}"[:64]


def _describe_error(err: BaseException) -> str:
    """把异常翻译成一句话。

    anyio 会把子任务里的异常包在 ExceptionGroup 里，直接 str() 只会得到
    "unhandled errors in a TaskGroup"——对使用者毫无信息量。
    这里向下挖一层，把最里层真正的错误拿出来。

    BaseExceptionGroup 是 3.11 才内置的，3.10 要从 exceptiongroup 包拿
    （anyio 的依赖，装 mcp 时就有了）。
    """
    try:
        from exceptiongroup import BaseExceptionGroup as _Group
    except ImportError:  # 3.11+
        _Group = BaseExceptionGroup  # noqa: F821  (3.11+ 内置)

    if isinstance(err, _Group) and err.exceptions:
        for sub in err.exceptions:
            return _describe_error(sub)
    msg = str(err).strip()
    return f"{type(err).__name__}: {msg}" if msg else type(err).__name__


class MCPClient:
    """一条常驻连接：后台线程跑单个长任务，对外暴露同步方法。

    公开方法都是阻塞的，且只能从**非**后台线程调用。
    """

    def __init__(self, spec: MCPServerSpec, timeout: float = DEFAULT_TIMEOUT):
        self.spec = spec
        self.timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client = None
        self._tools: list[dict] = []
        self._lock = threading.RLock()
        self._queue: asyncio.Queue | None = None
        self._ready = threading.Event()
        self._closed = False
        # 握手失败时由 _serve() 记录，connect() 负责抛出可读错误
        self._connect_error: BaseException | None = None

    # ---------- 对外：连接 / 调用 / 关闭 ----------

    def connect(self) -> list[dict]:
        """建立连接并返回工具清单（OpenAI tools schema 格式）。失败抛 RuntimeError。"""
        with self._lock:
            if self._client is not None:
                return self._tools

            self._thread = threading.Thread(
                target=self._thread_main, daemon=True, name=f"mcp-{self.spec.name}"
            )
            self._thread.start()

            if not self._ready.wait(timeout=self.timeout):
                self._force_stop()
                raise RuntimeError(
                    f"MCP 服务 {self.spec.name} 连接超时（{self.timeout:.0f}s）："
                    "命令没有响应，请检查能否手动执行它，以及 URL 是否可达。"
                )

            if self._connect_error is not None:
                err = self._connect_error
                self._join_thread()
                raise RuntimeError(
                    f"MCP 服务 {self.spec.name} 连接失败：{_describe_error(err)}"
                ) from err

            return self._tools

    def call(self, tool_name: str, arguments: dict) -> tuple[str, bool]:
        """调用远程工具，返回 (结果文本, 是否失败)。异常一律转文本，绝不外抛。"""
        if self._client is None:
            return f"MCP 服务 {self.spec.name} 未连接", True
        try:
            return self._call_in_loop(self._do_call_tool(tool_name, arguments))
        except Exception as e:  # 超时、断线、服务崩溃都走这里
            return f"MCP 服务 {self.spec.name} 调用失败：{type(e).__name__}: {e}", True

    def close(self) -> None:
        """关闭连接并停掉后台线程。可重复调用。"""
        with self._lock:
            if self._thread is None or self._closed:
                return
            self._closed = True
            try:
                if self._loop is not None and self._queue is not None:
                    # 投哨兵，让 _serve() 自己走完 __aexit__
                    asyncio.run_coroutine_threadsafe(
                        self._queue.put(None), self._loop
                    ).result(timeout=self.timeout)
            except Exception as e:
                logger.warning("通知 MCP 服务 %s 关闭时出错：%s", self.spec.name, e)
            self._join_thread()
            self._client = None
            self._tools = []

    def _join_thread(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=max(self.timeout, 5))
        self._thread = None
        self._loop = None
        self._queue = None

    def _force_stop(self) -> None:
        """超时兜底：线程卡在握手时只能强停，避免进程退不出去。"""
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass
        self._join_thread()
        self._client = None
        self._tools = []

    # ---------- 后台线程：一个任务包住整个连接生命周期 ----------

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    async def _serve(self) -> None:
        """连接 -> 处理请求 -> 收到哨兵后关闭。全程同一个任务（见模块注释约束 1）。"""
        self._queue = asyncio.Queue()
        try:
            self._client = self._make_client()
            await self._client.__aenter__()
            self._tools = await self._list_tools_async()
        except Exception as e:
            self._connect_error = e
            self._ready.set()
            # 握手可能已经建了一半，尽力收尾
            if self._client is not None:
                try:
                    await self._client.__aexit__(None, None, None)
                except Exception:
                    pass
            return

        self._ready.set()
        try:
            while True:
                item = await self._queue.get()
                if item is None:  # 关闭信号
                    break
                coro_factory, fut = item
                try:
                    fut.set_result(await coro_factory())
                except Exception as e:  # 单个请求失败不影响整条连接
                    fut.set_exception(e)
        finally:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception as e:
                logger.warning("关闭 MCP 服务 %s 连接时出错：%s", self.spec.name, e)
            self._client = None

    def _call_in_loop(self, coro_factory, timeout: float | None = None):
        """把一个协程工厂投递给后台长任务，阻塞等结果。"""
        loop = self._loop
        if loop is None or self._queue is None:
            raise RuntimeError(f"MCP 服务 {self.spec.name} 尚未连接")

        async def _enqueue():
            fut = asyncio.get_running_loop().create_future()
            await self._queue.put((coro_factory, fut))
            return await fut

        fut = asyncio.run_coroutine_threadsafe(_enqueue(), loop)
        return fut.result(timeout=timeout or self.timeout)

    # ---------- 协议操作 ----------

    def _make_client(self) -> Client:
        """按传输方式构造 Client；它本身就是异步上下文管理器。

        注意 SSE 分支必须显式传 `sse_client(...)`，不能写 `Client(url)`：
        Client 拿到裸 URL 会按 Streamable HTTP 协议探测（先 server/discover、
        再 initialize），对本项目的 /sse 端点两条都会失败并报
        "Server returned an error response"——看起来像服务端坏了，其实是传输选错了。
        """
        if self.spec.transport == "sse":
            from mcp.client.sse import sse_client

            return Client(sse_client(self.spec.url))
        params = StdioServerParameters(
            command=self.spec.command,
            args=self.spec.args,
            env=self.spec.env or None,
            cwd=self.spec.cwd,
        )
        return Client(params)

    async def _list_tools_async(self) -> list[dict]:
        result = await self._client.list_tools()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": mcp_tool_name(self.spec.name, t.name),
                    "description": (t.description or "")[:1024],
                    "parameters": t.input_schema or {"type": "object", "properties": {}},
                },
                # 记下真实工具名，执行时按前缀路由回来
                "_mcp": {"server": self.spec.name, "tool": t.name},
            }
            for t in result.tools
        ]
        logger.info("MCP 服务 %s 已连接，工具 %d 个", self.spec.name, len(tools))
        return tools

    def _do_call_tool(self, tool_name: str, arguments: dict):
        async def _run() -> tuple[str, bool]:
            result = await self._client.call_tool(tool_name, arguments)
            text = "\n".join(
                block.text for block in result.content if getattr(block, "type", "") == "text"
            )
            if not text and result.structured_content:
                text = json.dumps(result.structured_content, ensure_ascii=False)
            return text, bool(result.is_error)

        return _run
