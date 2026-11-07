"""Agent Skill：按需加载的"使用说明"，实现渐进式披露（progressive disclosure）。

核心思想（这是它区别于 system prompt 的地方）：
- system prompt 每轮都占 token，且卸载不掉
- Skill 平时只暴露 name + description（几十字节），模型判断相关才读全文

于是上下文里能放下 20 个技能，而不会被 20 份长文挤爆。

目录约定（与 Claude Agent Skills 的形态一致）：

    skills/
    └── sqlite-query/
        ├── SKILL.md        # 必需：frontmatter + 正文
        └── reference.md    # 可选：更细的参考，正文里可提示去读

SKILL.md 形如：

    ---
    name: sqlite-query
    description: 查本地 SQLite 时用；涉及聚合/多表关联/日期字段的注意事项
    when: 用户问数据、统计、报表，或需要先弄清表结构
    ---
    # 查数据库
    ...

本模块只负责"发现 + 读全文"，不涉及执行脚本——Skill 是纯文档，
不引入代码执行面（见 README 的说明）。
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = PROJECT_ROOT / "skills"

MAX_BODY_CHARS = 8000          # 单次加载上限，避免把上下文塞爆
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


@dataclass
class Skill:
    slug: str                  # 目录名，稳定标识
    name: str = ""
    description: str = ""
    when: str = ""
    body: str = ""
    path: Path | None = None
    extra_files: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        """注入 system prompt 的一行摘要：只说"什么时候用"，不泄露正文。"""
        when = f"（适用：{self.when}）" if self.when else ""
        return f"- {self.name}: {self.description}{when}"

    def full_text(self) -> str:
        """加载全文；超过上限就截断并说明，避免一个技能吃掉整个上下文。"""
        text = self.body.strip()
        if len(text) > MAX_BODY_CHARS:
            text = text[:MAX_BODY_CHARS] + f"\n\n…（已截断，原文 {len(self.body)} 字符）"
        tail = ""
        if self.extra_files:
            tail = "\n\n---\n同目录下的参考文件（需要时用 read_file 读取）：\n" + "\n".join(
                f"- {name}" for name in self.extra_files)
        return f"# {self.name}\n\n{text}{tail}"


def _parse(path: Path) -> Skill | None:
    """解析单个 SKILL.md；格式不对只记日志、跳过，不让一个坏文件拖垮整个列表。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("读取 skill 失败 %s：%s", path, exc)
        return None

    match = _FRONTMATTER.match(raw)
    if not match:
        logger.warning("skill 缺少 frontmatter，已跳过：%s", path)
        return None

    meta_raw, body = match.group(1), match.group(2)
    meta: dict[str, str] = {}
    for line in meta_raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip().lower()] = value.strip()

    directory = path.parent
    slug = directory.name
    name = meta.get("name") or slug
    description = meta.get("description", "").strip()
    if not description:
        logger.warning("skill 缺少 description，模型将无法判断何时使用：%s", path)
        description = name

    extra = sorted(
        p.name for p in directory.iterdir()
        if p.is_file() and p.name != "SKILL.md" and p.suffix in {".md", ".txt", ".json", ".csv"}
    )

    return Skill(
        slug=slug,
        name=name,
        description=description,
        when=meta.get("when", ""),
        body=body,
        path=path,
        extra_files=extra,
    )


class SkillLibrary:
    """按目录扫描技能。每次调用都重新扫描：改完文档立刻生效，不用重启。"""

    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory or SKILLS_DIR)

    def load_all(self) -> list[Skill]:
        if not self.directory.is_dir():
            return []
        skills = []
        for path in sorted(self.directory.glob("*/SKILL.md")):
            skill = _parse(path)
            if skill is not None:
                skills.append(skill)
        return skills

    def get(self, name_or_slug: str) -> Skill | None:
        key = (name_or_slug or "").strip().lower()
        if not key:
            return None
        for skill in self.load_all():
            if key in (skill.slug.lower(), skill.name.lower()):
                return skill
        return None

    def prompt_block(self) -> str:
        """注入 system prompt 的清单。没有技能时返回空串，行为与从前完全一致。"""
        skills = self.load_all()
        if not skills:
            return ""
        lines = [
            "## 可用技能（Skill）",
            "下面是本环境内置的操作手册索引。它们**不会自动生效**：",
            "当任务与某条描述相关时，先调用 `load_skill` 读取全文，再按其中的步骤作答。",
            "不相关就不要加载，避免浪费上下文。",
            "",
        ]
        lines.extend(skill.summary_line() for skill in skills)
        return "\n".join(lines)


def load_skill_tool(library: SkillLibrary):
    """把"读全文"包装成一个工具，交给模型自主决定何时调用。

    刻意做成普通工具而不是特殊机制：这样它自然复用了已有的 tool call 循环、
    事件流与前端展示，不必新增协议。
    """
    from .tools import tool as register_tool

    @register_tool
    def load_skill(name: str) -> str:
        """按名称加载一个技能的使用说明全文。任务与某个技能相关时先调用它，再按说明作答。"""
        skill = library.get(name)
        if skill is None:
            available = ", ".join(s.name for s in library.load_all()) or "（无）"
            return f"没有名为 {name} 的技能。可用技能：{available}"
        return skill.full_text()

    return load_skill
