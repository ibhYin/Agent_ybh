"""本地 MCP 服务（文件系统 / SQLite）的端到端测试。

真起子进程：验证的是使用者实际会走的那条路径（客户端 spawn 服务端）。
重点不是"能调用"，而是"越权会被拒绝"：
- 文件服务：绝对路径、`../` 逃逸必须失败
- SQLite 服务：DELETE / ATTACH / PRAGMA writable_schema 必须失败

跑法：
    python -m unittest agent_engine.tests_mcp_servers -v
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from agent_engine.mcp_client import MCPClient, MCPServerSpec

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _stdio(module: str, env: dict) -> MCPServerSpec:
    return MCPServerSpec(
        name="test",
        transport="stdio",
        command=sys.executable,
        args=["-m", module],
        env=env,
        cwd=str(PROJECT_ROOT),
    )


class FilesystemServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        (cls.root / "notes").mkdir()
        (cls.root / "notes" / "a.txt").write_text("hello world\nsecond line\n", encoding="utf-8")
        cls.client = MCPClient(_stdio("agent_engine.mcp_servers.fs", {"MCP_FS_ROOT": str(cls.root)}), timeout=40)
        cls.tools = cls.client.connect()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def test_exposes_four_tools(self):
        names = {t["function"]["name"] for t in self.tools}
        self.assertEqual(names, {"test__list_files", "test__read_text", "test__write_text", "test__search_text"})

    def test_list_recursive(self):
        text, failed = self.client.call("list_files", {"recursive": True})
        self.assertFalse(failed)
        self.assertIn("notes/a.txt", text)

    def test_read_text(self):
        text, failed = self.client.call("read_text", {"path": "notes/a.txt"})
        self.assertFalse(failed)
        self.assertIn("second line", text)

    def test_search_text(self):
        text, failed = self.client.call("search_text", {"query": "second"})
        self.assertFalse(failed)
        self.assertIn("notes/a.txt:2", text)

    def test_write_then_read(self):
        self.client.call("write_text", {"path": "out/x.txt", "content": "ok"})
        text, failed = self.client.call("read_text", {"path": "out/x.txt"})
        self.assertFalse(failed)
        self.assertEqual(text, "ok")

    def test_absolute_path_is_rejected(self):
        text, failed = self.client.call("read_text", {"path": "/etc/passwd"})
        self.assertTrue(failed)
        self.assertIn("相对路径", text)

    def test_parent_escape_is_rejected(self):
        text, failed = self.client.call("read_text", {"path": "../../../etc/passwd"})
        self.assertTrue(failed)
        self.assertIn("超出沙箱目录", text)


class SqliteServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "shop.db"
        conn = sqlite3.connect(cls.db)
        conn.executescript(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, amount REAL);"
            "INSERT INTO orders (id, status, amount) VALUES (1, 'paid', 12.5), (2, 'refunded', 3.0);"
        )
        conn.commit()
        conn.close()

        cls.client = MCPClient(
            _stdio("agent_engine.mcp_servers.sqlite", {"MCP_SQLITE_DB": str(cls.db)}),
            timeout=40,
        )
        cls.tools = cls.client.connect()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def test_exposes_three_tools(self):
        names = {t["function"]["name"] for t in self.tools}
        self.assertEqual(names, {"test__list_tables", "test__describe_table", "test__query"})

    def test_list_tables(self):
        text, failed = self.client.call("list_tables", {})
        self.assertFalse(failed)
        self.assertIn("orders", text)

    def test_describe_table(self):
        text, failed = self.client.call("describe_table", {"table": "orders"})
        self.assertFalse(failed)
        self.assertIn("CREATE TABLE orders", text)
        self.assertIn("amount", text)

    def test_select(self):
        text, failed = self.client.call("query", {"sql": "SELECT id, status FROM orders ORDER BY id"})
        self.assertFalse(failed)
        self.assertIn("paid", text)
        self.assertIn("refunded", text)

    def test_delete_is_denied(self):
        text, failed = self.client.call("query", {"sql": "DELETE FROM orders"})
        self.assertTrue(failed)
        self.assertIn("not authorized", text)
        # 数据仍在
        text, _ = self.client.call("query", {"sql": "SELECT COUNT(*) AS n FROM orders"})
        self.assertIn("2", text)

    def test_attach_is_denied(self):
        text, failed = self.client.call("query", {"sql": "ATTACH DATABASE '/tmp/x.db' AS x"})
        self.assertTrue(failed)
        self.assertIn("not authorized", text)

    def test_writable_schema_pragma_is_denied(self):
        text, failed = self.client.call("query", {"sql": "PRAGMA writable_schema=ON"})
        self.assertTrue(failed)
        self.assertIn("not authorized", text)

    def test_missing_db_reports_config(self):
        client = MCPClient(
            _stdio("agent_engine.mcp_servers.sqlite", {"MCP_SQLITE_DB": "/no/such/file.db"}),
            timeout=30,
        )
        try:
            client.connect()
            text, failed = client.call("list_tables", {})
            self.assertTrue(failed)
            self.assertIn("不存在", text)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
