"""MCP 客户端与配置合并的测试：不联网、不需要 API Key。

覆盖的都是实现过程中真实踩到的坑：
- 连接关闭时的 cancel scope 报错（进/出必须同一个任务）
- SSE 分支写 `Client(url)` 会走 Streamable HTTP 探测并失败
- 连接失败时错误信息不能是空的、也不能是 ExceptionGroup 的默认说明
- 服务名/工具名里的特殊字符要清洗成合法的 function name

跑法：
    python -m unittest agent_engine.tests_mcp_client -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

from agent_engine.mcp_client import MCPClient, MCPServerSpec, _describe_error, mcp_tool_name
from agent_engine.mcp_config import (
    MCPHub,
    load_file_specs,
    load_specs,
    merge_specs,
)


def stdio_self_spec(name: str = "self") -> MCPServerSpec:
    """用本项目自己的 MCP 服务端当被测对象：零外部依赖。"""
    return MCPServerSpec(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=["-m", "agent_engine.mcp_server"],
        cwd=str(Path(__file__).resolve().parent.parent),
    )


class ToolNameTest(unittest.TestCase):
    def test_double_underscore_prefix(self):
        self.assertEqual(mcp_tool_name("fs", "read"), "fs__read")

    def test_special_chars_are_sanitized(self):
        # OpenAI function name 只允许 [a-zA-Z0-9_-]
        self.assertEqual(mcp_tool_name("my.server", "a b/c"), "my_server__a_b_c")

    def test_length_is_capped(self):
        self.assertEqual(len(mcp_tool_name("a" * 50, "b" * 50)), 64)


class SpecValidationTest(unittest.TestCase):
    def test_rejects_empty_name(self):
        self.assertIn("服务名", MCPServerSpec(name="").validate())

    def test_rejects_illegal_name(self):
        self.assertIn("服务名", MCPServerSpec(name="非法 名字").validate())

    def test_rejects_stdio_without_command(self):
        self.assertIn("启动命令", MCPServerSpec(name="x", transport="stdio").validate())

    def test_rejects_sse_without_url(self):
        self.assertIn("URL", MCPServerSpec(name="x", transport="sse").validate())

    def test_rejects_unknown_transport(self):
        self.assertIn("传输方式", MCPServerSpec(name="x", transport="http").validate())

    def test_accepts_valid_specs(self):
        self.assertEqual(MCPServerSpec(name="ok", transport="stdio", command="ls").validate(), "")
        self.assertEqual(MCPServerSpec(name="ok", transport="sse", url="http://x/sse").validate(), "")


class DescribeErrorTest(unittest.TestCase):
    def test_plain_exception(self):
        self.assertEqual(_describe_error(ValueError("boom")), "ValueError: boom")

    def test_exception_group_is_unwrapped(self):
        """anyio 的 ExceptionGroup 默认文字没有信息量，必须挖到最里层。

        ExceptionGroup 是 3.11 才内置的：3.10 要装 exceptiongroup 包，
        3.11+ 直接用内置的。CI 跑 3.13，写死 import exceptiongroup 会直接报错。
        """
        import builtins

        Group = getattr(builtins, "ExceptionGroup", None)
        if Group is None:  # Python 3.10：用 exceptiongroup 兼容包
            from exceptiongroup import ExceptionGroup as Group

        group = Group("unhandled errors in a TaskGroup", [FileNotFoundError("no such cmd")])
        self.assertEqual(_describe_error(group), "FileNotFoundError: no such cmd")

    def test_empty_message_still_names_the_type(self):
        self.assertEqual(_describe_error(TimeoutError()), "TimeoutError")


class ClientLifecycleTest(unittest.TestCase):
    """真实起一个 MCP 服务子进程，验证连接/调用/关闭整条路径。"""

    def setUp(self):
        self.client = MCPClient(stdio_self_spec("lifecycle"), timeout=40)

    def tearDown(self):
        self.client.close()

    def test_connect_lists_tools(self):
        tools = self.client.connect()
        names = {t["function"]["name"] for t in tools}
        self.assertIn("lifecycle__calculate", names)
        self.assertIn("lifecycle__read_file", names)

    def test_tool_schema_is_reused(self):
        tools = {t["function"]["name"]: t for t in self.client.connect()}
        self.assertEqual(tools["lifecycle__calculate"]["function"]["parameters"]["required"], ["expression"])
        # 内部字段不喂给 LLM，但路由信息要留着
        self.assertEqual(tools["lifecycle__calculate"]["_mcp"], {"server": "lifecycle", "tool": "calculate"})

    def test_call_returns_text(self):
        self.client.connect()
        text, failed = self.client.call("calculate", {"expression": "(3+5)*7"})
        self.assertFalse(failed)
        self.assertEqual(text, "56")

    def test_tool_error_is_text_not_exception(self):
        self.client.connect()
        text, failed = self.client.call("calculate", {"expression": "1/0"})
        self.assertTrue(failed)
        self.assertIn("ZeroDivisionError", text)

    def test_close_twice_is_safe(self):
        self.client.connect()
        self.client.close()
        self.client.close()  # 不该抛

    def test_call_after_close_is_graceful(self):
        self.client.connect()
        self.client.close()
        text, failed = self.client.call("calculate", {"expression": "1+1"})
        self.assertTrue(failed)
        self.assertIn("未连接", text)


class ConnectFailureTest(unittest.TestCase):
    def test_missing_command_reports_reason(self):
        client = MCPClient(MCPServerSpec(name="missing", transport="stdio", command="/no/such/binary"), timeout=15)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                client.connect()
            msg = str(ctx.exception)
            self.assertIn("连接失败", msg)
            # 关键：不能是空消息，也不能只有 ExceptionGroup 的套话
            self.assertNotIn("unhandled errors in a TaskGroup", msg)
            self.assertGreater(len(msg), len("MCP 服务 missing 连接失败："))
        finally:
            client.close()

    def test_unreachable_sse_reports_reason(self):
        client = MCPClient(
            MCPServerSpec(name="ssebad", transport="sse", url="http://127.0.0.1:1/sse"), timeout=15
        )
        try:
            with self.assertRaises(RuntimeError) as ctx:
                client.connect()
            self.assertIn("连接失败", str(ctx.exception))
        finally:
            client.close()


class HubTest(unittest.TestCase):
    def test_hub_merges_and_routes(self):
        hub = MCPHub([stdio_self_spec("hub")], timeout=40)
        try:
            tools = hub.connect_all()
            self.assertTrue(tools)
            self.assertTrue(hub.has("hub__calculate"))
            self.assertFalse(hub.has("nope__x"))

            text, failed = hub.call("hub__calculate", {"expression": "6*6"})
            self.assertFalse(failed)
            self.assertEqual(text, "36")

            text, failed = hub.call("nope__x", {})
            self.assertTrue(failed)
            self.assertIn("找不到 MCP 工具", text)
        finally:
            hub.close()

    def test_hub_records_failure_without_raising(self):
        """单个服务连不上，不能让整个 hub 抛异常——外部依赖失败不该让 agent 不可用。"""
        bad = MCPServerSpec(name="bad", transport="stdio", command="/no/such/binary")
        hub = MCPHub([bad], timeout=10)
        try:
            tools = hub.connect_all()
            self.assertEqual(tools, [])
            self.assertIn("bad", hub.errors)
        finally:
            hub.close()

    def test_hub_context_manager_closes(self):
        with MCPHub([stdio_self_spec("ctx")], timeout=40) as hub:
            self.assertTrue(hub.clients)
        self.assertEqual(hub.clients, {})


class ConfigFileTest(unittest.TestCase):
    def test_missing_file_yields_nothing(self):
        self.assertEqual(load_file_specs(Path("/nonexistent/mcp.json")), [])

    def test_parses_claude_desktop_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp.json"
            path.write_text(
                '{"mcpServers": {'
                '"remote": {"url": "http://127.0.0.1:8022/sse"},'
                '"local": {"command": "npx", "args": ["-y", "pkg"]}'
                "}}",
                encoding="utf-8",
            )
            specs = {s.name: s for s in load_file_specs(path)}
        self.assertEqual(specs["remote"].transport, "sse")  # 有 url 自动判为 sse
        self.assertEqual(specs["local"].transport, "stdio")  # 有 command 判为 stdio
        self.assertEqual(specs["local"].args, ["-y", "pkg"])

    def test_invalid_json_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_file_specs(path), [])

    def test_invalid_spec_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp.json"
            path.write_text('{"mcpServers": {"broken": {"transport": "stdio"}}}', encoding="utf-8")
            self.assertEqual(load_file_specs(path), [])  # 缺 command，被跳过


class MergeTest(unittest.TestCase):
    def test_db_overrides_file_on_same_name(self):
        file_spec = MCPServerSpec(name="dup", transport="stdio", command="from-file")
        db_spec = MCPServerSpec(name="dup", transport="stdio", command="from-db")
        merged = merge_specs([file_spec], [db_spec])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].command, "from-db")

    def test_file_order_is_preserved(self):
        a = MCPServerSpec(name="a", transport="stdio", command="x")
        b = MCPServerSpec(name="b", transport="stdio", command="x")
        c = MCPServerSpec(name="c", transport="stdio", command="x")
        merged = merge_specs([a, b], [c])
        self.assertEqual([s.name for s in merged], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()


class BaseUrlNormalizeTest(unittest.TestCase):
    """normalize_base_url 的回归测试。

    真实踩过：.env 写 .../api/coding/v3，旧实现无条件补 /v1，
    变成 .../v3/v1 导致 404，而模型侧只看到"接口地址错误"，极难定位。
    """

    def test_appends_v1_when_no_version_segment(self):
        from agent_engine.config import normalize_base_url

        self.assertEqual(normalize_base_url("https://api.deepseek.com"), "https://api.deepseek.com/v1")
        self.assertEqual(normalize_base_url("http://localhost:11434"), "http://localhost:11434/v1")

    def test_keeps_existing_version_segment(self):
        from agent_engine.config import normalize_base_url

        self.assertEqual(normalize_base_url("https://api.deepseek.com/v1"), "https://api.deepseek.com/v1")
        # 这条就是修掉的那个 bug
        self.assertEqual(
            normalize_base_url("https://ark.cn-beijing.volces.com/api/coding/v3"),
            "https://ark.cn-beijing.volces.com/api/coding/v3",
        )

    def test_handles_trailing_slash_and_empty(self):
        from agent_engine.config import normalize_base_url

        self.assertEqual(normalize_base_url("https://x.com/v1/"), "https://x.com/v1")
        self.assertEqual(normalize_base_url(""), "")
