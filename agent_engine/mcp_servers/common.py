"""本地 MCP 服务的共用小工具。"""
from pathlib import Path

# agent_engine/mcp_servers/common.py -> 项目根目录（agent_ybh/）
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def make_toolbox():
    """返回 (注册表, 装饰器)。

    每个本地 MCP 服务各自维护一份注册表，避免和主 agent 的全局工具混在一起。
    """
    registry: dict[str, callable] = {}

    def tool(func):
        registry[func.__name__] = func
        return func

    return registry, tool
