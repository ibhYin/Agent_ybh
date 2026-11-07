"""MCP SSE 传输测试：起真实 HTTP 服务，走真实网络，不用 mock。

为什么必须真起 uvicorn：SSE 是长连接 + 二次 POST 的往返，
在进程内伪造 streams 验证不到「客户端拿到 endpoint 事件后往 /messages/ POST」
这条真实路径，而那正是 SSE 传输最容易写错的地方（session_id 拼错、
Host 校验没收、连接结束后没回 Response）。

跑法：
    python -m unittest agent_engine.tests_mcp_sse -v

不联网、不需要 API Key；服务只监听 127.0.0.1 的临时端口。
"""
import socket
import threading
import time
import unittest

# 用 httpx2 而不是 httpx：mcp 2.x 依赖的就是 httpx2，用它可以少一个直接依赖。
# （首次在干净 venv 里跑 CI 时才暴露出来：本地 conda 恰好装了 httpx，掩盖了这个遗漏。）
import httpx2 as httpx
import uvicorn
from mcp import Client
from mcp.client.sse import sse_client
from mcp.types import TextContent

from agent_engine.mcp_server import build_asgi_app
from agent_engine.tools import TOOL_REGISTRY


def _free_port() -> int:
    """让内核分配一个空闲端口，避免和本机其他服务（8000/8020/8022）撞车。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def text_of(result) -> str:
    return "".join(block.text for block in result.content if isinstance(block, TextContent))


class SseServerTest(unittest.IsolatedAsyncioTestCase):
    """整个类共用一台服务：起停一次，避免每条用例都等 uvicorn 启动。"""

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        config = uvicorn.Config(
            build_asgi_app("127.0.0.1"),
            host="127.0.0.1",
            port=cls.port,
            log_level="warning",
            log_config=None,
        )
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 15
        while not cls.server.started:
            if time.time() > deadline:
                raise RuntimeError("SSE 服务在 15 秒内没有启动")
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)

    def sse_url(self) -> str:
        return f"{self.base_url}/sse"

    # ---------- 协议与工具语义 ----------

    async def test_lists_every_registered_tool(self):
        async with Client(sse_client(self.sse_url()), read_timeout_seconds=20) as client:
            result = await client.list_tools()
        self.assertEqual({tool.name for tool in result.tools}, set(TOOL_REGISTRY))

    async def test_calls_tool_over_sse(self):
        async with Client(sse_client(self.sse_url()), read_timeout_seconds=20) as client:
            result = await client.call_tool("calculate", {"expression": "(3+5)*7"})
        self.assertFalse(result.is_error)
        self.assertEqual(text_of(result), "56")

    async def test_tool_error_is_content_not_protocol_error(self):
        async with Client(sse_client(self.sse_url()), read_timeout_seconds=20) as client:
            result = await client.call_tool("calculate", {"expression": "1/0"})
        self.assertTrue(result.is_error)
        self.assertIn("ZeroDivisionError", text_of(result))

    async def test_sandbox_applies_to_sse_clients_too(self):
        """SSE 是对外暴露的入口，沙箱约束必须和 stdio 一样生效。"""
        async with Client(sse_client(self.sse_url()), read_timeout_seconds=20) as client:
            result = await client.call_tool("read_file", {"path": "../../etc/passwd"})
        self.assertTrue(result.is_error)
        self.assertIn("超出沙箱目录", text_of(result))

    async def test_write_then_read_roundtrip(self):
        try:
            async with Client(sse_client(self.sse_url()), read_timeout_seconds=20) as client:
                await client.call_tool("write_file", {"path": "sse_test_tmp.txt", "content": "hello sse"})
                result = await client.call_tool("read_file", {"path": "sse_test_tmp.txt"})
            self.assertEqual(text_of(result), "hello sse")
        finally:
            from agent_engine.tools import WORKSPACE

            (WORKSPACE / "sse_test_tmp.txt").unlink(missing_ok=True)

    # ---------- 多连接 ----------

    async def test_multiple_concurrent_clients(self):
        """一次起服务、多客户端各占一条长连接 —— 这是 SSE 相对 stdio 的核心价值。"""

        async def one(name: str) -> str:
            async with Client(sse_client(self.sse_url()), read_timeout_seconds=20) as client:
                result = await client.call_tool("calculate", {"expression": name})
                return text_of(result)

        import asyncio

        a, b = await asyncio.gather(one("1+1"), one("2*3"))
        self.assertEqual((a, b), ("2", "6"))

    async def test_reconnect_after_disconnect(self):
        async with Client(sse_client(self.sse_url()), read_timeout_seconds=20) as client:
            await client.list_tools()
        async with Client(sse_client(self.sse_url()), read_timeout_seconds=20) as client:
            result = await client.call_tool("calculate", {"expression": "10-4"})
        self.assertEqual(text_of(result), "6")

    # ---------- 传输层的安全与错误分支 ----------

    async def test_rejects_forged_host_header(self):
        """DNS rebinding 防护：Host 不是白名单内的值必须被拒。"""
        async with httpx.AsyncClient(trust_env=False) as http:
            resp = await http.get(self.sse_url(), headers={"Host": "evil.example.com"})
        self.assertEqual(resp.status_code, 421)

    async def test_rejects_forged_origin_header(self):
        async with httpx.AsyncClient(trust_env=False) as http:
            resp = await http.get(self.sse_url(), headers={"Origin": "http://evil.example.com"})
        self.assertEqual(resp.status_code, 403)

    async def test_message_post_without_session_is_rejected(self):
        async with httpx.AsyncClient(trust_env=False) as http:
            resp = await http.post(
                f"{self.base_url}/messages/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("session_id", resp.text)

    async def test_message_post_with_unknown_session_is_404(self):
        async with httpx.AsyncClient(trust_env=False) as http:
            resp = await http.post(
                f"{self.base_url}/messages/?session_id=00000000000000000000000000000000",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        self.assertEqual(resp.status_code, 404)

    async def test_message_endpoint_rejects_get(self):
        async with httpx.AsyncClient(trust_env=False) as http:
            resp = await http.get(f"{self.base_url}/messages/")
        self.assertEqual(resp.status_code, 405)


class SseTransportFlagTest(unittest.TestCase):
    """不起服务，只验证命令行参数接线：--transport sse 真的走到 serve_sse。"""

    def test_default_transport_is_stdio(self):
        from agent_engine.mcp_server import _build_parser

        args = _build_parser().parse_args([])
        self.assertEqual(args.transport, "stdio")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8022)

    def test_sse_transport_parses_host_and_port(self):
        from agent_engine.mcp_server import _build_parser

        args = _build_parser().parse_args(["--transport", "sse", "--port", "9999"])
        self.assertEqual(args.transport, "sse")
        self.assertEqual(args.port, 9999)


if __name__ == "__main__":
    unittest.main()
