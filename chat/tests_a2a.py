"""A2A（Agent2Agent）服务端测试。

分两层：
1. 协议层与任务状态机（不联网、不调 LLM，用假模型驱动 run_agent）
2. HTTP 层（agent-card 发现、JSON-RPC 方法、错误码、会话复用）

为什么不用真模型：CI 里没有 API Key，且 LLM 输出不确定；
这里验证的是「协议与状态机是否正确」，不是「模型答得对不对」。
"""
import json
import threading
import unittest
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

import agent_engine.a2a_protocol as proto
import agent_engine.agent as agent_mod
from agent_engine.a2a_server import TaskRunner, TaskStore
from chat.models import ChatMessage, ChatSession


# ---------------------------------------------------------------------------
# 假模型：可控地产生「先调工具、再回答」两轮
# ---------------------------------------------------------------------------

class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, delta):
        self.choices = [type("Choice", (), {"delta": delta})()]


class _ToolCall:
    def __init__(self, index, call_id, name, arguments):
        self.index = index
        self.id = call_id
        self.function = type("Fn", (), {"name": name, "arguments": arguments})()


def fake_openai(rounds):
    """返回一个可 patch 到 agent_engine.agent.OpenAI 的假客户端。"""
    state = {"i": 0}

    class _Completions:
        def create(self, **kwargs):
            round_data = rounds[min(state["i"], len(rounds) - 1)]
            state["i"] += 1
            return iter(round_data)

    class _Client:
        def __init__(self, *args, **kwargs):
            self.chat = type("Chat", (), {"completions": _Completions()})()

    return _Client


TOOL_THEN_ANSWER = [
    [_Chunk(_Delta(tool_calls=[_ToolCall(0, "call_1", "calculate", '{"expression": "(3+5)*7"}')]))],
    [_Chunk(_Delta(content="结果是 56"))],
]


class ProtocolTest(unittest.TestCase):
    def test_text_part_and_extract(self):
        message = {"role": "user", "parts": [proto.text_part("你好")]}
        self.assertEqual(proto.extract_text(message), "你好")

    def test_extract_joins_multiple_parts(self):
        message = {"parts": [proto.text_part("第一段"), proto.text_part("第二段")]}
        self.assertEqual(proto.extract_text(message), "第一段\n第二段")

    def test_extract_data_part(self):
        message = {"parts": [{"kind": "data", "data": {"q": 1}}]}
        self.assertIn('"q": 1', proto.extract_text(message))

    def test_extract_ignores_empty(self):
        self.assertEqual(proto.extract_text({"parts": []}), "")

    def test_agent_message_shape(self):
        msg = proto.agent_message("hi", task_id="t1", context_id="c1")
        self.assertEqual(msg["role"], "agent")
        self.assertEqual(msg["taskId"], "t1")
        self.assertEqual(msg["contextId"], "c1")
        self.assertTrue(msg["messageId"].startswith("msg-"))

    def test_method_aliases(self):
        # 两代客户端的方法名都要认
        self.assertEqual(proto.METHOD_ALIASES["message/send"], "message/send")
        self.assertEqual(proto.METHOD_ALIASES["SendMessage"], "message/send")
        self.assertEqual(proto.METHOD_ALIASES["GetTask"], "tasks/get")

    def test_task_dict_is_camel_case(self):
        task = proto.A2ATask(task_id="t1", context_id="c1")
        data = task.to_dict()
        for key in ("id", "contextId", "status", "artifacts", "history"):
            self.assertIn(key, data)
        self.assertEqual(data["status"]["state"], proto.SUBMITTED)

    def test_history_length_limits(self):
        task = proto.A2ATask(task_id="t1", context_id="c1")
        task.history = [proto.agent_message(f"m{i}", task_id="t1", context_id="c1") for i in range(5)]
        self.assertEqual(len(task.to_dict(history_length=2)["history"]), 2)
        self.assertEqual(len(task.to_dict(history_length=0)["history"]), 0)
        self.assertEqual(len(task.to_dict()["history"]), 5)

    def test_agent_card_has_required_fields(self):
        card = proto.build_agent_card("http://127.0.0.1:3000/", "deepseek",
                                      ["calculate", "sqlite__query"])
        for key in ("protocolVersion", "name", "description", "url", "version",
                    "capabilities", "defaultInputModes", "defaultOutputModes", "skills"):
            self.assertIn(key, card)
        self.assertTrue(card["capabilities"]["streaming"])
        self.assertTrue(card["url"].endswith("/a2a"))
        skill_ids = {s["id"] for s in card["skills"]}
        self.assertIn("agentic-loop", skill_ids)
        self.assertIn("sqlite-query", skill_ids)  # 有 sqlite 工具才出现

    def test_agent_card_omits_sqlite_skill_without_tools(self):
        card = proto.build_agent_card("http://x/", "", ["calculate"])
        self.assertNotIn("sqlite-query", {s["id"] for s in card["skills"]})


class TaskRunnerTest(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore()

    def _task(self, text="算一下"):
        return self.store.create("ctx-1", {
            "kind": "message", "messageId": "m1", "role": "user",
            "parts": [proto.text_part(text)],
        })

    def test_completes_and_produces_artifact(self):
        task = self._task()
        runner = TaskRunner(self.store, llm_config={"base_url": "http://x", "api_key": "k", "model": "m"})
        with patch.object(agent_mod, "OpenAI", fake_openai(TOOL_THEN_ANSWER)):
            runner.start(task, "算一下", [])
            runner.wait(task, timeout=30)

        self.assertEqual(task.state, proto.COMPLETED)
        self.assertTrue(task.terminal)
        self.assertEqual(task.artifacts[0]["parts"][0]["text"], "结果是 56")
        texts = [h["parts"][0]["text"] for h in task.history]
        self.assertTrue(any("调用工具 calculate" in t for t in texts))
        self.assertTrue(any("工具返回：56" in t for t in texts))

    def test_llm_error_marks_task_failed(self):
        task = self._task()

        def boom(**kwargs):
            raise RuntimeError("429 quota exceeded")

        class _Client:
            def __init__(self, *a, **k):
                self.chat = type("C", (), {"completions": type("D", (), {"create": staticmethod(boom)})()})()

        runner = TaskRunner(self.store, llm_config={"base_url": "http://x", "api_key": "k", "model": "m"})
        with patch.object(agent_mod, "OpenAI", _Client):
            runner.start(task, "算一下", [])
            runner.wait(task, timeout=30)

        self.assertEqual(task.state, proto.FAILED)
        self.assertIn("429", task.error)
        # 失败原因必须出现在 history 里，客户端才看得到
        texts = [h["parts"][0]["text"] for h in task.history]
        self.assertTrue(any("429" in t for t in texts))

    def test_cancel_is_honored(self):
        task = self._task()
        slow_rounds = [[_Chunk(_Delta(content="思考中…"))]] * 50
        runner = TaskRunner(self.store, llm_config={"base_url": "http://x", "api_key": "k", "model": "m"})
        with patch.object(agent_mod, "OpenAI", fake_openai(slow_rounds)):
            task.cancel_requested = True  # 立刻取消
            runner.start(task, "算一下", [])
            runner.wait(task, timeout=30)

        self.assertEqual(task.state, proto.CANCELED)

    def test_store_lists_and_filters_by_context(self):
        t1 = self._task()
        t2 = self.store.create("ctx-2", {"kind": "message", "messageId": "m2",
                                         "role": "user", "parts": [proto.text_part("x")]})
        self.assertEqual(len(self.store.list()), 2)
        self.assertEqual(len(self.store.list("ctx-1")), 1)
        self.assertEqual(self.store.list("ctx-1")[0].task_id, t1.task_id)
        self.assertIsNotNone(self.store.get(t2.task_id))

    def test_store_caps_size(self):
        store = TaskStore(max_tasks=3)
        for i in range(6):
            store.create(f"c{i}", {"kind": "message", "messageId": f"m{i}",
                                   "role": "user", "parts": [proto.text_part("x")]})
        self.assertLessEqual(len(store.list()), 3)


FAKE_LLM_CONFIG = {"base_url": "http://fake.local/v1", "api_key": "test-key", "model": "fake-model"}


class A2AHttpTest(TransactionTestCase):
    """/agent-card.json 与 /a2a 的 HTTP 行为。

    用 TransactionTestCase 而不是 TestCase：A2A 的执行在**后台线程**里，
    而 TestCase 把每个用例包在一个未提交的事务里，后台线程用的是另一条连接，
    看不到这些数据，也写不进去（表现为 "database table is locked" 与查不到记录）。
    """

    def setUp(self):
        # A2A 的模型配置来自数据库；测试环境没有 .env，必须显式给一份，
        # 否则任务会在"未配置 API_KEY"处失败，掩盖真正要验证的东西。
        from chat import a2a_views

        self._patcher = patch.object(a2a_views, "_llm_config", return_value=FAKE_LLM_CONFIG)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

        # runner 按会话缓存并按需构造，清空以免复用带了旧配置的实例
        a2a_views._RUNNERS.clear()
        self.addCleanup(a2a_views._RUNNERS.clear)
        self.addCleanup(a2a_views.STORE.clear)

    def _rpc(self, method, params=None, request_id=1):
        return self.client.post(
            "/a2a",
            data=json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}),
            content_type="application/json",
        )

    def test_agent_card_endpoint(self):
        resp = self.client.get("/agent-card.json")
        self.assertEqual(resp.status_code, 200)
        card = resp.json()
        self.assertEqual(card["protocolVersion"], proto.PROTOCOL_VERSION)
        self.assertTrue(card["capabilities"]["streaming"])
        self.assertTrue(card["skills"])

    def test_well_known_redirects(self):
        resp = self.client.get("/.well-known/agent-card.json")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/agent-card.json", resp["Location"])

    def test_get_on_endpoint_is_405(self):
        resp = self.client.get("/a2a")
        self.assertEqual(resp.status_code, 405)
        self.assertIn("POST", resp.json()["error"])

    def test_unknown_method_is_rpc_error(self):
        resp = self._rpc("tasks/nope")
        body = resp.json()
        self.assertEqual(body["error"]["code"], proto.ERROR_METHOD_NOT_FOUND)

    def test_empty_message_is_invalid_params(self):
        resp = self._rpc("message/send", {"message": {"role": "user", "parts": []}})
        body = resp.json()
        self.assertEqual(body["error"]["code"], proto.ERROR_INVALID_PARAMS)

    def test_bad_json_is_parse_error(self):
        resp = self.client.post("/a2a", data="{not json", content_type="application/json")
        self.assertEqual(resp.json()["error"]["code"], proto.ERROR_PARSE)

    def test_task_get_missing_is_error(self):
        resp = self._rpc("tasks/get", {"id": "nope"})
        self.assertEqual(resp.json()["error"]["code"], proto.ERROR_TASK_NOT_FOUND)

    def test_message_send_completes_and_reuses_session(self):
        """contextId 应当映射成一个 ChatSession，对话要落库。"""
        with patch.object(agent_mod, "OpenAI", fake_openai(TOOL_THEN_ANSWER)):
            resp = self._rpc("message/send", {
                "contextId": "ctx-demo",
                "message": {"messageId": "m1", "role": "user",
                            "parts": [proto.text_part("算 (3+5)*7")]},
            })
        body = resp.json()
        self.assertIn("result", body)
        task = body["result"]["task"]
        self.assertEqual(task["status"]["state"], proto.COMPLETED)
        self.assertEqual(task["artifacts"][0]["parts"][0]["text"], "结果是 56")

        # 会话复用：A2A 的对话能在页面上看到
        session = ChatSession.objects.get(session_id="ctx-demo")
        self.assertTrue(session.title)
        roles = list(ChatMessage.objects.filter(session_id="ctx-demo").values_list("role", flat=True))
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    def test_tasks_get_after_send(self):
        with patch.object(agent_mod, "OpenAI", fake_openai(TOOL_THEN_ANSWER)):
            created = self._rpc("message/send", {
                "contextId": "ctx-get",
                "message": {"messageId": "m1", "role": "user", "parts": [proto.text_part("算")]},
            }).json()["result"]["task"]

        resp = self._rpc("tasks/get", {"id": created["id"]})
        self.assertEqual(resp.json()["result"]["task"]["status"]["state"], proto.COMPLETED)

    def test_tasks_list_filters(self):
        with patch.object(agent_mod, "OpenAI", fake_openai(TOOL_THEN_ANSWER)):
            self._rpc("message/send", {"contextId": "ctx-a",
                                       "message": {"messageId": "m1", "role": "user",
                                                   "parts": [proto.text_part("算")]}})
        body = self._rpc("tasks/list", {"contextId": "ctx-a"}).json()
        self.assertEqual(body["result"]["count"], 1)
        self.assertEqual(self._rpc("tasks/list", {"contextId": "ctx-b"}).json()["result"]["count"], 0)

    def test_cancel_unknown_task_is_error(self):
        resp = self._rpc("tasks/cancel", {"id": "nope"})
        self.assertEqual(resp.json()["error"]["code"], proto.ERROR_TASK_NOT_FOUND)

    def test_stream_emits_sse_events(self):
        with patch.object(agent_mod, "OpenAI", fake_openai(TOOL_THEN_ANSWER)):
            resp = self.client.post(
                "/a2a",
                data=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "message/stream", "params": {
                    "contextId": "ctx-stream",
                    "message": {"messageId": "m1", "role": "user", "parts": [proto.text_part("算")]},
                }}),
                content_type="application/json",
            )
            self.assertEqual(resp["Content-Type"], "text/event-stream")
            chunks = [c.decode() if isinstance(c, bytes) else c for c in resp.streaming_content]

        raw = "".join(chunks)
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
        kinds = [e.get("kind") for e in events]
        self.assertIn("task", kinds)
        self.assertIn("status-update", kinds)
        # 终态必须是 completed 且带 final
        final = [e for e in events if e.get("kind") == "status-update" and e.get("final")]
        self.assertTrue(final)
        self.assertEqual(final[-1]["status"]["state"], proto.COMPLETED)
