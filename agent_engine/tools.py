"""工具注册机制 + 示例工具。

新增工具只需要三步：
1. 写一个普通函数，参数加类型注解，docstring 写清楚功能
2. 函数上方加 @tool 装饰器
3. 完成（工具名、描述、参数 schema 全部自动生成，前端自动支持）
"""
import ast
import inspect
import json
import operator
from datetime import datetime
from pathlib import Path

# 工具注册表：工具名 -> 函数
TOOL_REGISTRY: dict[str, callable] = {}

# 类型注解 -> OpenAI JSON Schema 类型
_TYPE_MAP = {str: "string", int: "integer", float: "number", bool: "boolean"}


def tool(func):
    """装饰器：把函数注册为工具（函数名 = 工具名）。"""
    TOOL_REGISTRY[func.__name__] = func
    return func


def build_tools_schema(registry: dict | None = None) -> list[dict]:
    """从函数签名自动生成 OpenAI 要求的 tools schema（LLM 靠它知道有哪些工具可用）。

    registry 默认是主 agent 的全局注册表；本地 MCP 服务各自传自己的字典，
    这样同一套 schema 生成逻辑不用复制多份。
    """
    registry = TOOL_REGISTRY if registry is None else registry
    schema = []
    for name, func in registry.items():
        properties, required = {}, []
        for param_name, param in inspect.signature(func).parameters.items():
            properties[param_name] = {
                "type": _TYPE_MAP.get(param.annotation, "string"),
            }
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
            else:
                properties[param_name]["default"] = param.default
        schema.append({
            "type": "function",
            "function": {
                "name": name,
                "description": inspect.getdoc(func) or "",
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        })
    return schema


def execute_tool_result(name: str, arguments: dict, registry: dict | None = None) -> tuple[str, bool]:
    """执行工具，返回 (结果文本, 是否失败)。

    聊天循环只要文本；MCP 服务端还要知道成败（对应 CallToolResult.isError）。
    任何异常都转成错误文本、绝不向外抛——模型会看到并自行纠正。
    """
    registry = TOOL_REGISTRY if registry is None else registry
    func = registry.get(name)
    if func is None:
        return f"错误：找不到工具 {name}", True
    try:
        return str(func(**arguments)), False
    except Exception as e:
        return f"工具执行出错：{type(e).__name__}: {e}", True


def execute_tool(name: str, arguments_json: str) -> str:
    """执行工具并返回结果字符串（聊天循环入口，不区分成败）。"""
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except Exception as e:  # 参数不是合法 JSON，同样按工具错误处理
        return f"工具执行出错：{type(e).__name__}: {e}"
    return execute_tool_result(name, args)[0]


# ---------------------------------------------------------------------------
# 示例工具 1：安全计算器（用 ast 解析 + 运算符白名单，杜绝任意代码执行）
# ---------------------------------------------------------------------------
_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"不支持的表达式元素：{type(node).__name__}")


@tool
def calculate(expression: str) -> float:
    """计算数学表达式，例如 "1+2*3" 或 "(3+5)*7"。只支持 + - * / // % ** 和括号。"""
    return _safe_eval(ast.parse(expression, mode="eval"))


# ---------------------------------------------------------------------------
# 示例工具 2：获取当前时间
# ---------------------------------------------------------------------------
@tool
def get_current_time() -> str:
    """获取当前本地时间，格式：年-月-日 时:分:秒。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 示例工具 3：写文件（沙箱限制在项目 workspace/ 目录内，防止越权写任意路径）
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = PROJECT_ROOT / "workspace"   # agent 只能在这里读写文件


def _resolve_safe_path(path: str) -> Path:
    """相对路径默认相对 workspace/，解析后必须仍在沙箱内，否则抛错。"""
    p = (WORKSPACE / path).resolve()
    if not p.is_relative_to(WORKSPACE):
        raise ValueError(f"路径超出沙箱目录（仅允许 workspace/ 内）：{path}")
    return p


@tool
def write_file(path: str, content: str) -> str:
    """把内容写入工作区文件（仅限 workspace/ 目录内，使用相对路径，如 "result.txt"），返回写入的文件路径。"""
    p = _resolve_safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 workspace/{p.relative_to(WORKSPACE)}"


@tool
def read_file(path: str) -> str:
    """读取工作区内文本文件的内容（相对 workspace/ 的路径，如 "notes.txt"）。"""
    return _resolve_safe_path(path).read_text(encoding="utf-8")
