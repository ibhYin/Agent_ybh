"""Agent Skill 机制测试。

验证的是"渐进式披露"这个核心设计：
1. 只有 name + description 进 system prompt，正文不进
2. 模型调用 load_skill 后才拿到全文
3. 名字对不上给出可用列表，不静默失败
4. 缺 frontmatter / description 的坏文件只跳过，不拖垮整个列表
"""
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_engine.agent as agent_mod
from agent_engine.skills import Skill, SkillLibrary

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _make_library(tmp: Path) -> SkillLibrary:
    (tmp / "sqlite-query").mkdir(parents=True)
    (tmp / "sqlite-query" / "SKILL.md").write_text(
        "---\nname: sqlite-query\ndescription: 查数据库用\nwhen: 问数据时\n---\n"
        "# 正文\n这里是**不会**自动注入的详细步骤。\n",
        encoding="utf-8",
    )
    (tmp / "broken").mkdir()
    (tmp / "broken" / "SKILL.md").write_text("没有 frontmatter 的文件\n", encoding="utf-8")
    return SkillLibrary(tmp)


class SkillLibraryTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lib = _make_library(Path(self.tmp.name))

    def test_discovers_valid_skills_only(self):
        skills = self.lib.load_all()
        self.assertEqual([s.slug for s in skills], ["sqlite-query"])

    def test_summary_line_contains_description_not_body(self):
        skill = self.lib.load_all()[0]
        line = skill.summary_line()
        self.assertIn("查数据库用", line)          # description
        self.assertIn("问数据时", line)            # when
        self.assertNotIn("详细步骤", line)          # 正文绝不能进摘要

    def test_prompt_block_only_has_summaries(self):
        block = self.lib.prompt_block()
        self.assertIn("## 可用技能", block)
        self.assertIn("sqlite-query", block)
        self.assertNotIn("详细步骤", block)          # 渐进式披露：正文不进 system prompt
        self.assertIn("load_skill", block)          # 提示模型用工具读全文

    def test_get_by_name_and_slug(self):
        self.assertIsNotNone(self.lib.get("sqlite-query"))
        self.assertIsNotNone(self.lib.get("SQLite-Query"))  # 大小写不敏感

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.lib.get("不存在的技能"))

    def test_full_text_truncates_large_body(self):
        big = Path(self.tmp.name) / "big"
        big.mkdir()
        (big / "SKILL.md").write_text(
            "---\nname: big\ndescription: d\n---\n" + "字" * 20000, encoding="utf-8")
        skill = SkillLibrary(Path(self.tmp.name)).get("big")
        self.assertLess(len(skill.full_text()), 10000)  # 被截断
        self.assertIn("已截断", skill.full_text())


class SkillInjectionTest(unittest.TestCase):
    """load_skill 必须出现在模型可见的工具清单里（这是曾踩过的顺序 bug）。"""

    class _D:
        def __init__(self, c=None, tc=None): self.content = c; self.tool_calls = tc
    class _Chunk:
        def __init__(self, d): self.choices = [type("X", (), {"delta": d})()]
    class _TC:
        def __init__(self, i, cid, name, args):
            self.index = i; self.id = cid
            self.function = type("F", (), {"name": name, "arguments": args})()

    def test_load_skill_in_tools_schema(self):
        seen = {}

        class Comp:
            def create(self, **kw):
                seen["tools"] = [t["function"]["name"] for t in kw["tools"]]
                seen["system"] = kw["messages"][0]["content"]
                # 只给一轮文本回答，结束循环
                return iter([self._Chunk(self._D(c="好的"))])

        class FC:
            def __init__(self, *a, **k):
                self.chat = type("C", (), {"completions": Comp()})()

        with patch.object(agent_mod, "OpenAI", FC):
            list(agent_mod.run_agent([{"role": "user", "content": "hi"}],
                                     {"base_url": "http://x", "api_key": "k", "model": "m"}))

        self.assertIn("load_skill", seen["tools"])       # 工具可见
        self.assertIn("load_skill", seen["system"])      # 清单提示存在
        self.assertNotIn("常见陷阱", seen["system"])       # 正文不注入

    def test_load_skill_returns_full_text(self):
        from agent_engine.skills import load_skill_tool, SkillLibrary
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            lib = _make_library(Path(tmp))
            fn = load_skill_tool(lib)
            result = fn("sqlite-query")
            self.assertIn("详细步骤", result)            # 全文进来了
            self.assertIn("# sqlite-query", result)

        # 未知技能给可用列表
        with tempfile.TemporaryDirectory() as tmp:
            lib = _make_library(Path(tmp))
            fn = load_skill_tool(lib)
            self.assertIn("没有名为", fn("nope"))
            self.assertIn("sqlite-query", fn("nope"))


if __name__ == "__main__":
    unittest.main()
