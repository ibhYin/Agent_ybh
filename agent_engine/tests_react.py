"""ReAct 思考（thought）提取与事件流测试。

验证三种思考来源：
1. 原生 reasoning_content（推理模型）
2. "Thought:" 文本前缀（普通模型英文）
3. "思考：" 文本前缀（中文）

以及配套的：思考要从正文剥离、私有字段发送前要剥掉、thought 事件出现在
tool_start 之前。
"""
import unittest
from unittest.mock import patch

import agent_engine.agent as agent_mod


class _Delta:
    def __init__(self, content=None, tool_calls=None, reasoning_content=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _Chunk:
    def __init__(self, delta):
        self.choices = [type("Choice", (), {"delta": delta})()]


class _ToolCall:
    def __init__(self, index, call_id, name, arguments):
        self.index = index
        self.id = call_id
        self.function = type("Fn", (), {"name": name, "arguments": arguments})()


def _client(rounds):
    """可控多轮假客户端。"""
    state = {"i": 0}

    class Completions:
        def create(self, **kwargs):
            data = rounds[min(state["i"], len(rounds) - 1)]
            state["i"] += 1
            return iter(data)

    class Client:
        def __init__(self, *a, **k):
            self.chat = type("Chat", (), {"completions": Completions()})()

    return Client


CFG = {"base_url": "http://x", "api_key": "k", "model": "m"}


class ThoughtHelperTest(unittest.TestCase):
    def test_native_reasoning_wins(self):
        self.assertEqual(agent_mod._extract_thought("正文", "原生思考"), "原生思考")

    def test_english_thought_prefix(self):
        content = "Thought: 先看有哪些表。\n\n我来查"
        self.assertEqual(agent_mod._extract_thought(content, ""), "先看有哪些表。")

    def test_chinese_thought_prefix(self):
        content = "思考：需要确认单位。\n然后查"
        self.assertEqual(agent_mod._extract_thought(content, ""), "需要确认单位。")

    def test_strip_removes_thought_from_content(self):
        content = "Thought: 先看表。\n\n我来查"
        stripped = agent_mod._strip_thought_prefix(content)
        self.assertNotIn("先看表", stripped)
        self.assertIn("我来查", stripped)

    def test_no_thought_returns_empty(self):
        self.assertEqual(agent_mod._extract_thought("直接回答", ""), "")
        self.assertEqual(agent_mod._strip_thought_prefix("直接回答"), "直接回答")

    def test_sanitize_removes_private_field(self):
        msg = {"role": "assistant", "content": "x", "tool_calls": [], "reasoning": "思考"}
        out = agent_mod._sanitize_for_llm(msg)
        self.assertNotIn("reasoning", out)
        self.assertIn("content", out)
        self.assertIn("tool_calls", out)


class ThoughtEventTest(unittest.TestCase):
    def _run(self, rounds):
        with patch.object(agent_mod, "OpenAI", _client(rounds)):
            return list(agent_mod.run_agent(
                [{"role": "user", "content": "hi"}], CFG))

    def test_reasoning_content_emits_thought_before_tool(self):
        rounds = [
            [_Chunk(_Delta(reasoning_content="我先查表结构",
                           tool_calls=[_ToolCall(0, "c1", "calculate", '{"expression":"1+1"}')]))],
            [_Chunk(_Delta(content="结果是 2"))],
        ]
        events = self._run(rounds)
        types = [e["type"] for e in events]
        self.assertIn("thought", types)
        # thought 必须出现在 tool_start 之前
        thought_idx = types.index("thought")
        tool_idx = types.index("tool_start")
        self.assertLess(thought_idx, tool_idx)
        thought = next(e for e in events if e["type"] == "thought")
        self.assertEqual(thought["content"], "我先查表结构")

    def test_thought_prefix_emits_thought_and_strips_content(self):
        rounds = [
            [_Chunk(_Delta(content="Thought: 先算一下。\n",
                           tool_calls=[_ToolCall(0, "c1", "calculate", '{"expression":"1+1"}')]))],
            [_Chunk(_Delta(content="结果是 2"))],
        ]
        events = self._run(rounds)
        thought = next(e for e in events if e["type"] == "thought")
        self.assertEqual(thought["content"], "先算一下。")

        # 落库的 assistant 消息：reasoning 字段有值，content 已剥离 Thought
        assistant = next(e for e in events if e["type"] == "message" and e["message"]["role"] == "assistant")
        self.assertEqual(assistant["message"]["reasoning"], "先算一下。")
        self.assertNotIn("先算一下", assistant["message"]["content"] or "")

    def test_no_tools_plain_answer_has_no_thought(self):
        rounds = [[_Chunk(_Delta(content="你好"))]]
        events = self._run(rounds)
        self.assertNotIn("thought", [e["type"] for e in events])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["content"], "你好")


if __name__ == "__main__":
    unittest.main()
