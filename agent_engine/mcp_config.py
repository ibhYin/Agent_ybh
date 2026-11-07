"""外部 MCP 服务的来源与合并：配置文件 mcp.json + 数据库 MCPServerConfig。

两处都要，是因为它们的用处不同：
- **文件**（项目根目录 `mcp.json`）适合跟着仓库走、和 Claude Desktop 那类工具共用一份配置
- **数据库**适合在页面里即时增删调试，不用重启服务、不用碰文件

合并规则：**同名时数据库覆盖文件**。这样文件给默认值，页面上的改动立刻生效，
而清空页面配置也不会把文件里的服务弄丢。
"""
import json
import logging
import os
from pathlib import Path

from .mcp_client import MCPServerSpec, mcp_tool_name
from .mcp_client import MCPClient

logger = logging.getLogger(__name__)

# 配置文件位置：项目根目录（与 manage.py 同级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("AGENT_MCP_CONFIG") or (PROJECT_ROOT / "mcp.json"))


def load_file_specs(path: Path | None = None) -> list[MCPServerSpec]:
    """读 mcp.json。格式与 Claude Desktop 的 claude_desktop_config.json 兼容：

        {"mcpServers": {
            "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
            "remote": {"url": "http://127.0.0.1:8022/sse"}
        }}

    文件不存在或格式不对都只记一条日志，绝不抛——缺一个配置文件不该让页面打不开。
    """
    path = path or CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取 MCP 配置文件 %s 失败：%s", path, e)
        return []

    specs = []
    for name, conf in (raw.get("mcpServers") or {}).items():
        if not isinstance(conf, dict):
            continue
        # 有 url 就是 sse，有 command 就是 stdio —— 与 Claude Desktop 的约定一致
        transport = "sse" if conf.get("url") else "stdio"
        spec = MCPServerSpec.from_dict({**conf, "name": name, "transport": conf.get("transport", transport)})
        if spec.enabled and not spec.validate():
            specs.append(spec)
    return specs


def load_db_specs() -> list[MCPServerSpec]:
    """读数据库里的服务配置。只在 Django 已配置时可用（测试里可能没有）。"""
    try:
        from chat.models import MCPServerConfig
    except Exception:  # 没有 Django 或 app 未就绪
        return []
    try:
        rows = MCPServerConfig.objects.all()
    except Exception as e:  # 表还没迁移
        logger.warning("读取数据库 MCP 配置失败（可能尚未 migrate）：%s", e)
        return []

    specs = []
    for row in rows:
        spec = MCPServerSpec.from_dict({
            "name": row.name,
            "transport": row.transport,
            "command": row.command,
            "args": row.args,
            "env": row.env,
            "cwd": row.cwd,
            "url": row.url,
            "enabled": row.enabled,
        })
        if spec.enabled and not spec.validate():
            specs.append(spec)
    return specs


def merge_specs(file_specs: list[MCPServerSpec], db_specs: list[MCPServerSpec]) -> list[MCPServerSpec]:
    """同名时数据库优先；保持文件里的顺序在前。"""
    merged: dict[str, MCPServerSpec] = {s.name: s for s in file_specs}
    for spec in db_specs:
        merged[spec.name] = spec
    return list(merged.values())


def load_specs(path: Path | None = None) -> list[MCPServerSpec]:
    return merge_specs(load_file_specs(path), load_db_specs())


class MCPHub:
    """管理多个 MCP 服务连接，把它们的工具合并进本地工具表。

    刻意做成"显式建连、显式关闭"的对象，而不是全局单例：
    测试里需要能干净地建、用、拆，不留后台线程。
    """

    def __init__(self, specs: list[MCPServerSpec] | None = None, timeout: float = 15.0):
        self.specs = specs if specs is not None else load_specs()
        self.timeout = timeout
        self.clients: dict[str, MCPClient] = {}
        # 工具名 -> (spec.name, 远程真实工具名)
        self._route: dict[str, tuple[str, str]] = {}
        self.errors: dict[str, str] = {}
        self._tools: list[dict] = []

    def connect_all(self) -> list[dict]:
        """连接所有服务，返回合并后的远程工具 schema（OpenAI 格式）。

        单个服务连不上只记录下来、不影响其他服务——外部依赖失败不该让整个 agent 不可用。
        """
        tools: list[dict] = []
        for spec in self.specs:
            client = MCPClient(spec, timeout=self.timeout)
            try:
                remote_tools = client.connect()
            except Exception as e:
                # connect() 抛出的信息已经带了服务名与原因，这里直接记，不再加前缀避免重复
                self.errors[spec.name] = str(e)
                logger.warning("%s", e)
                client.close()
                continue

            self.clients[spec.name] = client
            for item in remote_tools:
                name = item["function"]["name"]
                self._route[name] = (spec.name, item["_mcp"]["tool"])
                # 干净的工具定义（去掉内部字段，LLM 不需要看到 _mcp）
                tools.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": item["function"]["description"],
                        "parameters": item["function"]["parameters"],
                    },
                })
        self._tools = tools
        return tools

    def has(self, tool_name: str) -> bool:
        return tool_name in self._route

    def call(self, tool_name: str, arguments: dict) -> tuple[str, bool]:
        entry = self._route.get(tool_name)
        if entry is None:
            return f"错误：找不到 MCP 工具 {tool_name}", True
        server_name, remote_name = entry
        client = self.clients.get(server_name)
        if client is None:
            return f"错误：MCP 服务 {server_name} 未连接", True
        return client.call(remote_name, arguments)

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        self._route.clear()
        self._tools = []

    def __enter__(self) -> "MCPHub":
        self.connect_all()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def tool_names_of(specs: list[MCPServerSpec]) -> dict[str, list[str]]:
    """给设置面板用：列出每个服务声明了哪些工具名（不实际连接）。"""
    return {}


# mcp_tool_name 在这里 re-export，方便调用方只 import 本模块
__all__ = [
    "CONFIG_PATH",
    "MCPHub",
    "MCPServerSpec",
    "load_db_specs",
    "load_file_specs",
    "load_specs",
    "merge_specs",
    "mcp_tool_name",
]
