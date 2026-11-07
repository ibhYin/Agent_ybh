"""本地 MCP 服务：文件系统（只在一个沙箱根目录内操作）。

和主 agent 的 workspace/ 沙箱是同一套边界：所有路径都相对根目录解析，
绝对路径与 `../` 越界一律拒绝。默认根是项目 workspace/，
可用环境变量 MCP_FS_ROOT 指向别的目录（启动这个服务的进程必须能读到）。

运行：
    python -m agent_engine.mcp_servers.fs              # stdio
    python -m agent_engine.mcp_servers.fs --transport sse --port 8031
"""
import os
from pathlib import Path

from agent_engine.mcp_server import run_cli

from .common import PROJECT_ROOT, make_toolbox

SERVER_NAME = "local-filesystem"
SERVER_VERSION = "1.0.0"
SERVER_INSTRUCTIONS = (
    "只读/写一个受限根目录内的文件。路径必须相对根目录，越界会被拒绝。"
    "适合让 agent 查看项目资料、写结果文件。"
)

ROOT = Path(os.environ.get("MCP_FS_ROOT") or (PROJECT_ROOT / "workspace")).resolve()

REGISTRY, tool = make_toolbox()

MAX_LIST = 300
MAX_READ_CHARS = 20000
MAX_FILE_BYTES = 2 * 1024 * 1024


def _resolve(path: str) -> Path:
    """相对路径 -> 根目录内的绝对路径；越界抛 ValueError。"""
    raw = str(path or "").strip()
    p = Path(raw) if raw else Path(".")
    if p.is_absolute():
        raise ValueError(f"只接受相对路径（相对 {ROOT}），收到：{raw}")
    target = (ROOT / p).resolve()
    if not target.is_relative_to(ROOT):
        raise ValueError(f"路径超出沙箱目录（仅允许 {ROOT} 内）：{raw}")
    return target


def _rel(p: Path) -> str:
    rel = p.relative_to(ROOT).as_posix()
    return rel or "."


@tool
def list_files(path: str = "", recursive: bool = False) -> str:
    """列出沙箱目录下的文件与子目录。path 相对根目录；recursive=true 时递归。"""
    target = _resolve(path)
    if not target.exists():
        raise FileNotFoundError(f"目录不存在：{path}")
    if not target.is_dir():
        raise NotADirectoryError(f"不是目录：{path}")

    entries: list[str] = []
    iterator = target.rglob("*") if recursive else target.iterdir()
    for entry in sorted(iterator, key=lambda x: x.as_posix()):
        try:
            relative = entry.relative_to(ROOT).as_posix()
        except ValueError:
            continue
        if entry.is_dir():
            entries.append(relative + "/")
        elif entry.is_file():
            entries.append(relative)
        if len(entries) >= MAX_LIST:
            entries.append(f"…（超过 {MAX_LIST} 条，已截断）")
            break

    return "\n".join(entries) if entries else "（空目录）"


@tool
def read_text(path: str, max_chars: int = 8000) -> str:
    """读取沙箱内文本文件。path 相对根目录；max_chars 限制返回长度。"""
    target = _resolve(path)
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"文件超过 {MAX_FILE_BYTES} 字节，拒绝整文件读取：{path}")
    text = target.read_text(encoding="utf-8")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n…（已截断，共 {len(text)} 字符）"
    return text


@tool
def write_text(path: str, content: str) -> str:
    """把文本写入沙箱内文件（不存在会创建父目录）。path 相对根目录。"""
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"已写入 {_rel(target)}（{len(content)} 字符）"


@tool
def search_text(query: str, path: str = "", max_results: int = 30) -> str:
    """在沙箱内按内容搜索文本文件，返回 路径:行号:内容。query 为空则不搜索。"""
    query = str(query or "")
    if not query:
        return "query 不能为空"

    root = _resolve(path)
    hits: list[str] = []
    files = [root] if root.is_file() else sorted(root.rglob("*"))

    for file in files:
        if len(hits) >= max_results:
            break
        if not file.is_file() or file.suffix in {".pyc", ".png", ".jpg", ".zip", ".db"}:
            continue
        try:
            if file.stat().st_size > MAX_FILE_BYTES:
                continue
            for lineno, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                if query in line:
                    hits.append(f"{_rel(file)}:{lineno}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        break
        except (UnicodeDecodeError, OSError):
            continue  # 二进制/无权限文件直接跳过

    return "\n".join(hits) if hits else f"没有找到包含「{query}」的内容"


def main() -> None:
    run_cli(
        SERVER_NAME,
        SERVER_VERSION,
        SERVER_INSTRUCTIONS,
        REGISTRY,
        description="文件系统 MCP 服务（受限根目录）",
    )


if __name__ == "__main__":
    main()
