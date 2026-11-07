"""把工具注册表暴露成 MCP 服务（stdio / SSE 两种传输）。

同一套实现服务两类用法：
- 主 agent 的工具（calculate / 时间 / workspace 文件）：
    python -m agent_engine.mcp_server
- 独立的本地 MCP 服务（agent_engine/mcp_servers/*）：
    python -m agent_engine.mcp_servers.fs
    python -m agent_engine.mcp_servers.sqlite

调用方只需传「服务名 + 说明 + 自己的注册表」，schema 生成与协议处理都在这里。

硬约束：stdio 模式下 stdout 是协议通道，只能出现 JSON-RPC 帧。
任何 print / 日志写到 stdout 都会让客户端解析失败，所以日志一律走 stderr。
"""
import argparse
import asyncio
import logging
import os
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .tools import TOOL_REGISTRY, build_tools_schema, execute_tool_result

SERVER_NAME = "agent-ybh"
SERVER_VERSION = "1.0.0"
SERVER_INSTRUCTIONS = (
    "智能体 MVP 的本地工具：安全计算、取当前时间、读写 workspace/ 内的文件。"
    "文件工具被限制在项目 workspace/ 目录内，越界路径会返回错误文本而不是抛异常。"
)

logger = logging.getLogger(__name__)


def _make_handlers(registry: dict):
    """为某个注册表生成 tools/list 与 tools/call 处理函数。"""

    async def list_tools(ctx, params) -> types.ListToolsResult:
        tools = []
        for item in build_tools_schema(registry):
            fn = item["function"]
            tools.append(
                types.Tool(
                    name=fn["name"],
                    description=fn["description"] or None,
                    input_schema=fn["parameters"],
                )
            )
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx, params) -> types.CallToolResult:
        """按 MCP 规范：工具自身错误放内容里 + isError=True，协议错误只用于框架问题。"""
        text, failed = execute_tool_result(params.name, params.arguments or {}, registry)
        if failed:
            logger.info("tool %s failed: %s", params.name, text.replace("\n", " ")[:200])
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            is_error=failed,
        )

    return list_tools, call_tool


def build_server(
    name: str = SERVER_NAME,
    version: str = SERVER_VERSION,
    instructions: str = SERVER_INSTRUCTIONS,
    registry: dict | None = None,
) -> Server:
    """构造 MCP Server。测试里也直接用它做 in-process 连接，不用起子进程。"""
    registry = TOOL_REGISTRY if registry is None else registry
    list_tools, call_tool = _make_handlers(registry)
    return Server(
        name,
        version=version,
        instructions=instructions,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def _serve_stdio(server_factory=build_server) -> None:
    server = server_factory()
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

    刻意写成裸 ASGI 可调用对象：SDK 文档示例用了 request._send，
    但 starlette 1.6 起 Request 不再带这个属性，照抄会 AttributeError。

    每条连接 new 一个 Server：Server.run() 会进入 lifespan，
    共用实例意味着多连接同时进 lifespan，语义不清楚。
    """

    def __init__(self, transport, server_factory=build_server) -> None:
        self._transport = transport
        self._server_factory = server_factory

    async def __call__(self, scope, receive, send) -> None:
        from starlette.responses import Response  # 延迟 import：stdio 模式不需要 starlette

        if scope["type"] == "lifespan":
            return

        server = self._server_factory()
        try:
            async with self._transport.connect_sse(scope, receive, send) as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())
        except ValueError as exc:
            if "Request validation failed" not in str(exc):
                raise
            logger.warning("拒绝连接：Host/Origin 不在白名单内（scope client=%s）", scope.get("client"))
            return
        await Response()(scope, receive, send)


def build_asgi_app(host: str = DEFAULT_HOST, server_factory=build_server):
    """构造 SSE 用的 Starlette 应用（测试里也直接用它，不用起 uvicorn）。"""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    transport = SseServerTransport(MESSAGE_ENDPOINT, security_settings=build_security_settings(host))
    return Starlette(
        routes=[
            Route(SSE_ENDPOINT, endpoint=_SseEndpoint(transport, server_factory), methods=["GET"]),
            Mount(MESSAGE_ENDPOINT, app=transport.handle_post_message),
        ]
    )


def serve_sse(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    server_factory=build_server,
    server_name: str = SERVER_NAME,
    server_version: str = SERVER_VERSION,
) -> None:
    """起 uvicorn 提供 SSE 传输，阻塞直到 Ctrl+C。"""
    import uvicorn

    app = build_asgi_app(host, server_factory)
    logger.info(
        "MCP server %s v%s 已启动（SSE），http://%s:%s%s",
        server_name, server_version, host, port, SSE_ENDPOINT,
    )
    uvicorn.run(app, host=host, port=port, log_level="info", log_config=None)


def _build_parser(description: str = "把工具注册表暴露成 MCP 服务") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio",
                        help="传输方式：stdio（默认，由客户端拉起）或 sse（常驻 HTTP 服务）")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"SSE 监听地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"SSE 监听端口（默认 {DEFAULT_PORT}）")
    return parser


def run_cli(
    name: str,
    version: str,
    instructions: str,
    registry: dict,
    description: str = "把工具注册表暴露成 MCP 服务",
    argv: list[str] | None = None,
) -> None:
    """本地 MCP 服务统一入口：解析参数、配日志、按传输方式启动。"""
    args = _build_parser(description).parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    factory = lambda: build_server(name, version, instructions, registry)  # noqa: E731

    if args.transport == "stdio":
        logger.info("MCP server %s v%s 已启动（stdio），cwd=%s", name, version, os.getcwd())
        asyncio.run(_serve_stdio(factory))
    else:
        serve_sse(args.host, args.port, factory, name, version)


def main() -> None:
    run_cli(SERVER_NAME, SERVER_VERSION, SERVER_INSTRUCTIONS, TOOL_REGISTRY)


if __name__ == "__main__":
    main()
