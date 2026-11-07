"""会话与聊天流的 Django 层测试。

重点验证多会话改造没有破坏"消息按 session 隔离"这条老约定，
以及新加的空会话、重命名、删除、自动标题这些行为。
不联网：run_agent 与 MCPHub 都被替换，只测持久化与 HTTP 层。
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from chat.models import ChatMessage, ChatSession, MCPServerConfig


class SessionEndpointTest(TestCase):
    def test_list_is_empty_initially(self):
        resp = self.client.get("/chat/sessions")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["sessions"], [])

    def test_create_empty_session(self):
        resp = self.client.post(
            "/chat/sessions/create",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["session"]["title"], "新对话")
        self.assertEqual(data["session"]["message_count"], 0)
        self.assertEqual(ChatSession.objects.count(), 1)

    def test_create_with_title(self):
        resp = self.client.post(
            "/chat/sessions/create",
            data=json.dumps({"title": "我的会话"}),
            content_type="application/json",
        )
        self.assertEqual(resp.json()["session"]["title"], "我的会话")

    def test_rename(self):
        ChatSession.objects.create(session_id="s1", title="旧标题")
        resp = self.client.post(
            "/chat/sessions/rename",
            data=json.dumps({"session_id": "s1", "title": "新标题"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ChatSession.objects.get(session_id="s1").title, "新标题")

    def test_rename_missing_session_is_404(self):
        resp = self.client.post(
            "/chat/sessions/rename",
            data=json.dumps({"session_id": "nope", "title": "x"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_delete_removes_session_and_messages(self):
        ChatSession.objects.create(session_id="s1", title="t")
        ChatMessage.objects.create(session_id="s1", role="user", payload={"role": "user", "content": "hi"})
        ChatMessage.objects.create(session_id="s2", role="user", payload={"role": "user", "content": "other"})

        resp = self.client.post(
            "/chat/sessions/delete",
            data=json.dumps({"session_id": "s1"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ChatSession.objects.filter(session_id="s1").exists())
        self.assertFalse(ChatMessage.objects.filter(session_id="s1").exists())
        # 别的会话不受影响——这是最容易写错的地方
        self.assertTrue(ChatMessage.objects.filter(session_id="s2").exists())

    def test_empty_title_falls_back_to_first_user_text(self):
        ChatSession.objects.create(session_id="s1", title="")
        ChatMessage.objects.create(
            session_id="s1", role="user", payload={"role": "user", "content": "第一句话"}
        )
        resp = self.client.get("/chat/sessions")
        row = resp.json()["sessions"][0]
        self.assertEqual(row["title"], "第一句话")


class ChatStreamSessionTest(TestCase):
    """chat_stream 首次收到消息时应自动建会话，并用这条消息做标题。"""

    def _post_stream(self, payload):
        with patch("chat.views.run_agent") as fake_agent, patch("chat.views.MCPHub") as fake_hub:
            fake_agent.return_value = iter([{"type": "done", "content": "ok"}])
            fake_hub.return_value = fake_hub
            resp = self.client.post(
                "/chat/stream",
                data=json.dumps(payload),
                content_type="application/json",
            )
            # 消费流，确保生成器跑完
            b"".join(resp.streaming_content)
        return resp

    def test_creates_session_with_title_from_first_message(self):
        self._post_stream({"session_id": "abc", "message": "帮我算一下"})
        session = ChatSession.objects.get(session_id="abc")
        self.assertEqual(session.title, "帮我算一下")
        self.assertEqual(ChatMessage.objects.filter(session_id="abc", role="user").count(), 1)

    def test_keeps_auto_title_on_second_message(self):
        self._post_stream({"session_id": "abc", "message": "第一句"})
        self._post_stream({"session_id": "abc", "message": "第二句"})
        self.assertEqual(ChatSession.objects.get(session_id="abc").title, "第一句")

    def test_generates_id_when_missing(self):
        self._post_stream({"message": "没有 session_id"})
        self.assertEqual(ChatSession.objects.count(), 1)
        self.assertNotEqual(ChatSession.objects.first().session_id, "")

    def test_final_answer_is_persisted(self):
        """最终回答只出现在 done 事件里，必须落库，否则刷新后答案消失。"""
        self._post_stream({"session_id": "abc", "message": "嗨"})
        rows = list(ChatMessage.objects.filter(session_id="abc").order_by("created_at"))
        self.assertEqual([r.role for r in rows], ["user", "assistant"])
        self.assertEqual(rows[-1].payload["content"], "ok")

    def test_messages_are_isolated_between_sessions(self):
        self._post_stream({"session_id": "a", "message": "会话 A"})
        self._post_stream({"session_id": "b", "message": "会话 B"})

        resp_a = self.client.get("/chat/history", {"session_id": "a"})
        resp_b = self.client.get("/chat/history", {"session_id": "b"})
        users_a = [m["content"] for m in resp_a.json()["messages"] if m["role"] == "user"]
        users_b = [m["content"] for m in resp_b.json()["messages"] if m["role"] == "user"]
        self.assertEqual(users_a, ["会话 A"])
        self.assertEqual(users_b, ["会话 B"])


class SessionMigrationBackfillTest(TestCase):
    """回填逻辑本身在迁移里跑过一次，这里只验证「无会话但有消息」时列表不会丢数据。"""

    def test_orphan_messages_still_have_a_session_row_created_on_stream(self):
        # 模拟旧版本遗留：只有消息，没有 ChatSession
        ChatMessage.objects.create(
            session_id="legacy", role="user", payload={"role": "user", "content": "旧消息"}
        )
        with patch("chat.views.run_agent") as fake_agent, patch("chat.views.MCPHub"):
            fake_agent.return_value = iter([{"type": "done", "content": "ok"}])
            resp = self.client.post(
                "/chat/stream",
                data=json.dumps({"session_id": "legacy", "message": "新消息"}),
                content_type="application/json",
            )
            b"".join(resp.streaming_content)

        session = ChatSession.objects.get(session_id="legacy")
        self.assertEqual(session.first_user_text(), "旧消息")  # 标题取最早的一条，而不是"新消息"



class MCPPanelApiTest(TestCase):
    """管理页 /mcp 依赖的几个接口：内置目录、启停、状态探测、按名测试。

    文件配置固定为"不存在"，避免测试时连到项目根目录的真实 mcp.json。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fs_root = Path(self.tmp.name)
        (self.fs_root / "a.txt").write_text("hello", encoding="utf-8")

        patcher = patch("agent_engine.mcp_config.CONFIG_PATH", Path(self.tmp.name) / "none.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _add_filesystem(self, name="filesystem", with_env=True):
        payload = {
            "name": name,
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "agent_engine.mcp_servers.fs"],
            "cwd": str(Path(__file__).resolve().parent.parent),
        }
        if with_env:
            payload["env"] = {"MCP_FS_ROOT": str(self.fs_root)}
        resp = self.client.post(
            "/chat/mcp/save",
            data=json.dumps(payload),
            content_type="application/json",
        )
        return resp

    def test_builtin_catalog(self):
        resp = self.client.get("/chat/mcp/builtin")
        self.assertEqual(resp.status_code, 200)
        items = {b["name"]: b for b in resp.json()["builtins"]}
        self.assertEqual(set(items), {"filesystem", "sqlite"})
        self.assertFalse(items["filesystem"]["installed"])
        # 默认库只在容器里有 askdb 样例库时自动填入；干净克隆里可能是空 dict，
        # 所以这里断言工具清单与 env 的类型，而不是假设某个路径一定存在
        self.assertEqual(set(items["sqlite"]["tools"]), {"list_tables", "describe_table", "query"})
        self.assertIsInstance(items["sqlite"]["env"], dict)

    def test_builtin_marks_installed_after_add(self):
        self._add_filesystem()
        resp = self.client.get("/chat/mcp/builtin")
        items = {b["name"]: b for b in resp.json()["builtins"]}
        self.assertTrue(items["filesystem"]["installed"])

    def test_save_preserves_env_when_omitted(self):
        self._add_filesystem()
        # 编辑时回显不含 env，留空应当保留原环境变量而不是清空
        self._add_filesystem(with_env=False)
        row = MCPServerConfig.objects.get(name="filesystem")
        self.assertEqual(row.env, {"MCP_FS_ROOT": str(self.fs_root)})

    def test_toggle_disables_and_enables(self):
        self._add_filesystem()
        resp = self.client.post(
            "/chat/mcp/toggle",
            data=json.dumps({"name": "filesystem", "enabled": False}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(MCPServerConfig.objects.get(name="filesystem").enabled)

        # 停用后不再出现在状态探测里
        status = self.client.get("/chat/mcp/status").json()["servers"]
        self.assertEqual(status, [])

        self.client.post(
            "/chat/mcp/toggle",
            data=json.dumps({"name": "filesystem", "enabled": True}),
            content_type="application/json",
        )
        self.assertTrue(MCPServerConfig.objects.get(name="filesystem").enabled)

    def test_toggle_missing_is_404(self):
        resp = self.client.post(
            "/chat/mcp/toggle",
            data=json.dumps({"name": "nope", "enabled": False}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_status_reports_tools(self):
        self._add_filesystem()
        resp = self.client.get("/chat/mcp/status")
        self.assertEqual(resp.status_code, 200)
        servers = resp.json()["servers"]
        self.assertEqual(len(servers), 1)
        item = servers[0]
        self.assertTrue(item["ok"], item.get("error"))
        self.assertEqual({t["name"] for t in item["tools"]}, {"list_files", "read_text", "write_text", "search_text"})
        self.assertGreaterEqual(item["elapsed_ms"], 0)

    def test_test_stored_uses_saved_env(self):
        self._add_filesystem()
        resp = self.client.post(
            "/chat/mcp/test-stored",
            data=json.dumps({"name": "filesystem"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"], data.get("error"))
        self.assertEqual(len(data["tools"]), 4)

    def test_status_reports_failure_without_raising(self):
        payload = {
            "name": "broken",
            "transport": "stdio",
            "command": "/no/such/binary",
        }
        self.client.post("/chat/mcp/save", data=json.dumps(payload), content_type="application/json")
        servers = self.client.get("/chat/mcp/status").json()["servers"]
        self.assertEqual(len(servers), 1)
        self.assertFalse(servers[0]["ok"])
        self.assertIn("连接失败", servers[0]["error"])

    def test_delete_file_server_is_404(self):
        resp = self.client.post(
            "/chat/mcp/delete",
            data=json.dumps({"name": "from-mcp-json"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)
