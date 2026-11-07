"""端点：
- GET  /                       聊天页面
- GET  /chat/history?session_id=...   加载某会话的历史消息
- GET  /chat/tools             可用工具列表（本地 @tool + 外部 MCP，自动合并）
- GET  /chat/config            当前生效的 LLM 配置（不含 key 明文）
- POST /chat/config-save       保存 LLM 配置（key 加密存 DB，空值不覆盖）
- POST /chat/stream            发送一条消息，SSE 流式返回 agent 的全部过程
- GET  /chat/mcp               外部 MCP 服务配置（文件 + 数据库，env 不回显）
- POST /chat/mcp/save          新增或更新一个 MCP 服务
- POST /chat/mcp/delete        删除一个数据库里的 MCP 服务
- POST /chat/mcp/test          试连一个服务并列出它能提供的工具
"""
import json

from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from agent_engine import config
from agent_engine.agent import run_agent
from agent_engine.mcp_client import MCPClient, MCPServerSpec
from agent_engine.mcp_config import MCPHub, load_file_specs, load_specs
from agent_engine.tools import build_tools_schema

from .crypto import decrypt_secret, encrypt_secret
from .models import ChatMessage, LLMConfig, MCPServerConfig


def index(request):
    return render(request, "chat/index.html")


def history(request):
    session_id = request.GET.get("session_id", "")
    messages = [m.payload for m in ChatMessage.objects.filter(session_id=session_id)]
    return JsonResponse({"messages": messages})


def tools(request):
    """工具列表由装饰器注册表自动生成，新增 @tool 后前端无需改动。

    ?with_mcp=1 时把外部 MCP 服务的工具也连上列出来（设置面板用），
    默认不连——列工具不该触发建立外部连接。
    """
    local = build_tools_schema()
    if request.GET.get("with_mcp") != "1":
        return JsonResponse({"tools": local, "local_count": len(local), "mcp_count": 0, "mcp_errors": {}})

    hub = MCPHub(timeout=8.0)
    try:
        remote = hub.connect_all()
    finally:
        hub.close()
    return JsonResponse({
        "tools": local + remote,
        "local_count": len(local),
        "mcp_count": len(remote),
        "mcp_errors": hub.errors,
    })


def current_config(request):
    """返回当前生效配置（数据库优先，空值回退 .env）；api_key 只返回是否已配置，绝不回传明文。"""
    db_config = LLMConfig.singleton()
    env_cfg = config.get_config()
    has_db_key = bool(db_config.api_key_encrypted)
    return JsonResponse({
        "base_url": db_config.base_url or env_cfg["base_url"],
        "model": db_config.model or env_cfg["model"],
        "api_key_configured": has_db_key or bool(env_cfg["api_key"]),
        "source": "数据库" if (db_config.base_url or db_config.model or has_db_key) else ".env",
    })


# 本地学习项目用 @csrf_exempt 简化（前端无需处理 token）；
# 生产环境应删除该装饰器，并在前端通过 {% csrf_token %} + X-CSRFToken 头校验。
@csrf_exempt
def config_save(request):
    if request.method != "POST":
        return JsonResponse({"error": "仅支持 POST"}, status=405)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体不是合法 JSON"}, status=400)

    db_config = LLMConfig.singleton()
    for field in ("base_url", "model"):
        value = str(data.get(field, "")).strip()
        if value:
            setattr(db_config, field, value)
    api_key = str(data.get("api_key", "")).strip()
    if api_key:  # 留空 = 不修改已保存的 key
        db_config.api_key_encrypted = encrypt_secret(api_key)
    db_config.save()
    return JsonResponse({"ok": True})


# 本地学习项目用 @csrf_exempt 简化（前端无需处理 token）；
# 生产环境应删除该装饰器，并在前端通过 {% csrf_token %} + X-CSRFToken 头校验。
@csrf_exempt
def chat_stream(request):
    if request.method != "POST":
        return JsonResponse({"error": "仅支持 POST"}, status=405)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体不是合法 JSON"}, status=400)

    session_id = str(data.get("session_id", ""))[:64]
    user_text = str(data.get("message", "")).strip()
    if not user_text:
        return JsonResponse({"error": "message 不能为空"}, status=400)

    # LLM 配置存后端数据库（页面设置面板保存），空值项由 run_agent 回退 .env
    db_config = LLMConfig.singleton()
    llm_config = {
        "base_url": db_config.base_url,
        "api_key": decrypt_secret(db_config.api_key_encrypted),
        "model": db_config.model,
    }

    # 从数据库加载该会话完整历史（OpenAI 格式），再追加本次用户消息
    history_messages = [m.payload for m in ChatMessage.objects.filter(session_id=session_id)]
    user_message = {"role": "user", "content": user_text}
    history_messages.append(user_message)
    ChatMessage.objects.create(session_id=session_id, role="user", payload=user_message)

    def event_stream():
        # MCP 连接必须在生成器内部建、内部关：StreamingHttpResponse 是流式的，
        # 视图函数返回时连接还没开始用，提前建连会让连接一直悬着不释放。
        hub = MCPHub(timeout=15.0)
        try:
            for event in run_agent(history_messages, llm_config, mcp_hub=hub):
                # "message" 事件是已完成的 OpenAI 格式消息，落库持久化（前端会忽略它）
                if event["type"] == "message":
                    ChatMessage.objects.create(
                        session_id=session_id,
                        role=event["message"]["role"],
                        payload=event["message"],
                    )
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            # 客户端中途断开（关页面）时生成器被 close()，这里保证后台线程不泄漏
            hub.close()

    return StreamingHttpResponse(event_stream(), content_type="text/event-stream")


# ---------------------------------------------------------------------------
# 外部 MCP 服务管理
# ---------------------------------------------------------------------------

def _spec_to_dict(spec: MCPServerSpec, source: str) -> dict:
    return {
        "name": spec.name,
        "transport": spec.transport,
        "command": spec.command,
        "args": spec.args,
        "cwd": spec.cwd or "",
        "url": spec.url,
        "enabled": spec.enabled,
        "source": source,
        # env 可能含密钥，只说明有几个键，不回传值
        "env_keys": sorted(spec.env.keys()),
    }


def mcp_list(request):
    """列出所有服务：数据库的在前（可编辑），文件的在后（只读）。"""
    db_specs = []
    for row in MCPServerConfig.objects.all():
        db_specs.append(_spec_to_dict(MCPServerSpec.from_dict({
            "name": row.name, "transport": row.transport, "command": row.command,
            "args": row.args, "env": row.env, "cwd": row.cwd, "url": row.url,
            "enabled": row.enabled,
        }), "database"))
    file_specs = [_spec_to_dict(s, "file") for s in load_file_specs()]
    return JsonResponse({"servers": db_specs + file_specs})


# 本地学习项目用 @csrf_exempt 简化；生产环境应改为 token 校验。
@csrf_exempt
def mcp_save(request):
    if request.method != "POST":
        return JsonResponse({"error": "仅支持 POST"}, status=405)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体不是合法 JSON"}, status=400)

    spec = MCPServerSpec.from_dict(data)
    error = spec.validate()
    if error:
        return JsonResponse({"error": error}, status=400)

    row, created = MCPServerConfig.objects.update_or_create(
        name=spec.name,
        defaults={
            "transport": spec.transport,
            "command": spec.command,
            "args": spec.args,
            "env": spec.env,
            "cwd": spec.cwd or "",
            "url": spec.url,
            "enabled": spec.enabled,
        },
    )
    return JsonResponse({"ok": True, "created": created, "name": row.name})


@csrf_exempt
def mcp_delete(request):
    if request.method != "POST":
        return JsonResponse({"error": "仅支持 POST"}, status=405)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体不是合法 JSON"}, status=400)

    name = str(data.get("name", "")).strip()
    deleted, _ = MCPServerConfig.objects.filter(name=name).delete()
    if not deleted:
        # 明确区分：可能它只存在于 mcp.json（文件里的本页面删不掉）
        return JsonResponse({"error": f"数据库里没有服务 {name}（mcp.json 里的服务请直接改文件）"}, status=404)
    return JsonResponse({"ok": True})


@csrf_exempt
def mcp_test(request):
    """试连一个服务并列出工具，用于设置面板的"测试连接"。"""
    if request.method != "POST":
        return JsonResponse({"error": "仅支持 POST"}, status=405)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体不是合法 JSON"}, status=400)

    spec = MCPServerSpec.from_dict(data)
    error = spec.validate()
    if error:
        return JsonResponse({"ok": False, "error": error}, status=400)

    client = MCPClient(spec, timeout=15.0)
    try:
        remote = client.connect()
        return JsonResponse({
            "ok": True,
            "tools": [
                {"name": t["function"]["name"], "description": t["function"]["description"]}
                for t in remote
            ],
        })
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        client.close()
