"""内置的本地 MCP 服务目录。

管理页（/mcp）据此显示"内置服务"卡片；点添加时把这里的 spec 写进数据库。
每个服务都是独立进程，用 `python -m agent_engine.mcp_servers.<module>` 启动。
"""
import sys
from pathlib import Path

from .common import PROJECT_ROOT


def _sqlite_default() -> str:
    """给 SQLite 服务找一个开箱可用的库；找不到就留空，由用户填 MCP_SQLITE_DB。"""
    candidates = [
        PROJECT_ROOT.parent / "askdb" / "data" / "sample" / "shop.db",
        PROJECT_ROOT / "data" / "sample" / "shop.db",
        PROJECT_ROOT / "sample.db",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return ""


BUILTINS = [
    {
        "name": "filesystem",
        "title": "文件系统",
        "module": "agent_engine.mcp_servers.fs",
        "description": "在沙箱根目录内列出、读取、写入、搜索文本文件；默认根是项目 workspace/。",
        "env": {"MCP_FS_ROOT": str(PROJECT_ROOT / "workspace")},
        "tools": ["list_files", "read_text", "write_text", "search_text"],
        "transport": "stdio",
    },
    {
        "name": "sqlite",
        "title": "SQLite 只读",
        "module": "agent_engine.mcp_servers.sqlite",
        "description": "只读查询本地 SQLite：列表、看结构、执行 SELECT；写操作会被拒绝。默认连 askdb 样例库，没有时请在添加后设置 MCP_SQLITE_DB。",
        "env": {"MCP_SQLITE_DB": _sqlite_default()},
        "tools": ["list_tables", "describe_table", "query"],
        "transport": "stdio",
    },
]


def builtin_specs() -> list[dict]:
    """转成与 MCPServerConfig 同构的配置（可直接 POST /chat/mcp/save）。"""
    specs = []
    for item in BUILTINS:
        specs.append({
            "name": item["name"],
            "title": item["title"],
            "description": item["description"],
            "transport": item["transport"],
            "command": sys.executable,
            "args": ["-m", item["module"]],
            "cwd": str(PROJECT_ROOT),
            "env": {k: v for k, v in item["env"].items() if v},
            "tools": item["tools"],
        })
    return specs
