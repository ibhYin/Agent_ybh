"""把 agent_engine 的工具注册表暴露成一个 MCP 服务（stdio / SSE 两种传输）。

谁在用：
- Claude Desktop / Cursor 等 MCP 客户端把本模块当成子进程拉起，就能调用
  calculate / get_current_time / write_file / read_file
- 远程或多客户端场景走 SSE：一次起服务，多个客户端各占一条长连接
- 本项目的 agent 客户端（M2）也可以连它，用来做端到端自测

为什么工具只有一份：
  build_tools_schema() 产出的就是标准 JSON Schema，MCP 的 inputSchema 直接复用；
  以后加一个 @tool，两种传输一行都不用改。

运行：
  python -m agent_engine.mcp_server                  # stdio，由 MCP 客户端拉起
  python -m agent_engine.mcp_server --transport sse  # SSE，监听 127.0.0.1:8022
  python -m agent_engine.mcp_server --transport sse --port 9000 --host 0.0.0.0

硬约束：stdio 模式下 stdout 是协议通道，只能出现 JSON-RPC 帧。
任何 print / 日志写到 stdout 都会让客户端解析失败，所以日志一律走 stderr。
SSE 模式没有这个限制（stdout 是普通终端输出），但下面把日志同样收在 stderr，
这样两种模式下日志行为一致，切传输方式时不会突然冒出未配置的 handler。
"""
import argparse
import asyncio
import logging
import os
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .tools import build_tools_schema, execute_tool_result

SERVER_NAME = "agent-ybh"
SERVER_VERSION = "1.0.0"
SERVER_INSTRUCTIONS = (
    "智能体 MVP 的本地工具：安全计算、取当前时间、读写 workspace/ 内的文件。"
    "文件工具被限制在项目 workspace/ 目录内，越界路径会返回错误文本而不是抛异常。"
)

# stdio 模式下绝不能往 stdout 写东西。
# 日志配置只在 main() 里做，避免 import 本模块就改掉宿主进程的 logging。
logger = logging.getLogger(__name__)


async def _list_tools(ctx, params) -> types.ListToolsResult:
    """MCP 的 tools/list：把 OpenAI 格式的 tools schema 翻译成 MCP Tool。"""
    tools = []
    for item in build_tools_schema():
        fn = item["function"]
        tools.append(
            types.Tool(
                name=fn["name"],
                description=fn["description"] or None,
                input_schema=fn["parameters"],
            )
        )
    return types.ListToolsResult(tools=tools)


async def _call_tool(ctx, params) -> types.CallToolResult:
    """MCP 的 tools/call：执行本地注册表里的函数。

    按 MCP 规范：工具自身的错误（除零、文件不存在、越界路径）放在内容里、
    用 isError=True 返回，客户端模型看得见也能自己纠正；
    协议级错误只用于"找不到工具"这类框架问题。
    """
    text, failed = execute_tool_result(params.name, params.arguments or {})
    if failed:
        logger.info("tool %s failed: %s", params.name, text.replace("\n", " ")[:200])
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        is_error=failed,
    )


def build_server() -> Server:
    """构造 MCP Server。测试里也直接用它做 in-process 连接，不用起子进程。"""
    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )


async def _serve_stdio() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# ---------------------------------------------------------------------------
# SSE 传输：一次起服务，多个客户端各占一条长连接
# ---------------------------------------------------------------------------
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8022
SSE_ENDPOINT = "/sse"
MESSAGE_ENDPOINT = "/messages/"


def build_security_settings(host: str) -> "TransportSecuritySettings":
    """按监听地址生成 DNS rebinding 防护白名单。

    规范要求服务端校验 Host / Origin：本地服务默认只听 127.0.0.1，
    浏览器里恶意网页却可以往 http://127.0.0.1:8022 发请求（DNS rebinding），
    所以这里显式列出允许的 Host，非法 Host 直接 421、非法 Origin 直接 403。
    """
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    if host in ("0.0.0.0", "::"):
        # 对外监听时把本机网卡地址也放进来；这里无法枚举，交给调用方追加
        logger.warning(
            "监听 %s 意味着局域网可访问；允许的 Host 仅限 %s，"
            "如需对外提供请自行扩展 allowed_hosts",
            host,
            hosts,
        )
    elif host not in ("127.0.0.1", "localhost", "::1"):
        hosts.append(f"{host}:*")

    origins = [h.replace(":*", "") for h in hosts]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{o}:*" for o in origins] + [f"https://{o}:*" for o in origins],
    )


class _SseEndpoint:
    """GET /sse：建立长连接，之后所有请求都是客户端 POST 到 /messages/。

    注意：这里刻意写成裸 ASGI 可调用对象，而不是 `async def handler(request)`。
    SDK 文档示例用的是 `request._send`，但 starlette 1.6 起 Request 不再带 `_send`，
    照抄会 AttributeError；直接拿 ASGI 的 (scope, receive, send) 就不受影响。

    每条连接 new 一个 Server：Server.run() 会进入 lifespan，
    共用实例意味着多连接同时进 lifespan，语义不清楚也没必要冒这个险。
    """

    def __init__(self, transport) -> None:
        self._transport = transport

    async def __call__(self, scope, receive, send) -> None:
        # 延迟 import：stdio 模式下不必把 starlette 拉进进程
        from starlette.responses import Response

        if scope["type"] == "lifespan":  # 由 uvicorn 的 lifespan 通道触发，直接放行
            return

        server = build_server()
        try:
            async with self._transport.connect_sse(scope, receive, send) as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())
        except ValueError as exc:
            # connect_sse 已经自己回过 421/403 响应了，这里再抛会被 uvicorn 当成未处理异常
            # 打一整页 traceback——那只是正常的拒绝路径，不该污染日志。
            if "Request validation failed" not in str(exc):
                raise
            logger.warning("拒绝连接：Host/Origin 不在白名单内（scope client=%s）", scope.get("client"))
            return
        # 连接结束后必须回一个 Response，否则 starlette 会报 "'NoneType' object is not callable"
        await Response()(scope, receive, send)


def build_asgi_app(host: str = DEFAULT_HOST):
    """构造 SSE 用的 Starlette 应用（测试里也直接用它，不用起 uvicorn）。"""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    transport = SseServerTransport(MESSAGE_ENDPOINT, security_settings=build_security_settings(host))
    return Starlette(
        routes=[
            Route(SSE_ENDPOINT, endpoint=_SseEndpoint(transport), methods=["GET"]),
            Mount(MESSAGE_ENDPOINT, app=transport.handle_post_message),
        ]
    )


def serve_sse(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """起 uvicorn 提供 SSE 传输，阻塞直到 Ctrl+C。"""
    import uvicorn

    app = build_asgi_app(host)
    logger.info(
        "MCP server %s v%s 已启动（SSE），http://%s:%s%s",
        SERVER_NAME, SERVER_VERSION, host, port, SSE_ENDPOINT,
    )
    uvicorn.run(app, host=host, port=port, log_level="info", log_config=None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 agent_engine 的工具暴露成 MCP 服务")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio", help="传输方式：stdio（默认，由客户端拉起）或 sse（常驻 HTTP 服务）")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"SSE 监听地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"SSE 监听端口（默认 {DEFAULT_PORT}）")
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.transport == "stdio":
        logger.info("MCP server %s v%s 已启动（stdio），cwd=%s", SERVER_NAME, SERVER_VERSION, os.getcwd())
        asyncio.run(_serve_stdio())
    else:
        serve_sse(args.host, args.port)


if __name__ == "__main__":
    main()
