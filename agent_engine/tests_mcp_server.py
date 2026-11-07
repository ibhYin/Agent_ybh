"""MCP 服务端测试：不联网、不需要 API Key。

两层验证：
1. in-process 连接（Client(build_server())）——快，验证协议与工具语义
2. 真实 stdio 子进程（Client(StdioServerParameters)）——慢一点，验证"客户端拉起进程"这条真实路径
   这一步能抓到 stdout 被日志污染、模块导入失败这类只有真进程才暴露的问题

跑法：
    python -m unittest agent_engine.tests_mcp_server -v

故意不 import Django：这个入口要能在没有 Django 的环境里独立运行。
"""
import subprocess
import sys
import unittest
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import TextContent

from agent_engine.mcp_server import build_server
from agent_engine.tools import TOOL_REGISTRY

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 工具沙箱目录（.gitignore 已忽略）；测试写入后自己清理
WORKSPACE = PROJECT_ROOT / "workspace"


def text_of(result) -> str:
    """把 CallToolResult 的 content 块拼成纯文本。"""
    return "".join(block.text for block in result.content if isinstance(block, TextContent))


class InProcessServerTest(unittest.IsolatedAsyncioTestCase):
    """in-process：客户端和服务端在同一个事件循环里，不 spawn 子进程。"""

    async def test_lists_every_registered_tool(self):
        async with Client(build_server()) as client:
            result = await client.list_tools()
        self.assertEqual({tool.name for tool in result.tools}, set(TOOL_REGISTRY))

    async def test_schema_is_reused_not_rewritten(self):
        async with Client(build_server()) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        self.assertEqual(tools["calculate"].input_schema["required"], ["expression"])
        self.assertIn("计算数学表达式", tools["calculate"].description or "")

    async def test_calculate_returns_text_result(self):
        async with Client(build_server()) as client:
            result = await client.call_tool("calculate", {"expression": "(3+5)*7"})
        self.assertFalse(result.is_error)
        self.assertEqual(text_of(result), "56")

    async def test_tool_error_is_content_not_protocol_error(self):
        async with Client(build_server()) as client:
            result = await client.call_tool("calculate", {"expression": "1/0"})
        self.assertTrue(result.is_error)
        self.assertIn("ZeroDivisionError", text_of(result))

    async def test_write_then_read_roundtrip(self):
        try:
            async with Client(build_server()) as client:
                await client.call_tool("write_file", {"path": "mcp_test_tmp.txt", "content": "hello mcp"})
                result = await client.call_tool("read_file", {"path": "mcp_test_tmp.txt"})
            self.assertFalse(result.is_error)
            self.assertEqual(text_of(result), "hello mcp")
        finally:
            (WORKSPACE / "mcp_test_tmp.txt").unlink(missing_ok=True)

    async def test_path_escape_is_rejected(self):
        async with Client(build_server()) as client:
            result = await client.call_tool("read_file", {"path": "../../etc/passwd"})
        self.assertTrue(result.is_error)
        self.assertIn("超出沙箱目录", text_of(result))


class StdioTransportTest(unittest.IsolatedAsyncioTestCase):
    """真实路径：客户端 spawn 一个 `python -m agent_engine.mcp_server` 子进程。"""

    async def test_stdio_subprocess_serves_tools(self):
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "agent_engine.mcp_server"],
            cwd=str(PROJECT_ROOT),
        )
        async with Client(params, read_timeout_seconds=20) as client:
            tools = await client.list_tools()
            self.assertIn("get_current_time", {tool.name for tool in tools.tools})

            result = await client.call_tool("get_current_time", {})
            self.assertFalse(result.is_error)
            self.assertRegex(text_of(result), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class NoDjangoDependencyTest(unittest.TestCase):
    def test_imports_without_django_settings(self):
        code = (
            "import sys; import agent_engine.mcp_server; "
            "assert 'django' not in sys.modules, '意外 import 了 Django'; "
            "print('ok')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)


if __name__ == "__main__":
    unittest.main()
