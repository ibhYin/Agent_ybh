"""端点：
- GET  /                       聊天页面
- GET  /chat/sessions          会话列表（左栏用）
- POST /chat/sessions/create   新建空会话
- POST /chat/sessions/rename   重命名
- POST /chat/sessions/delete   删除会话及其消息
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
import uuid

from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from agent_engine import architecture, config
from agent_engine.agent import run_agent
from agent_engine.mcp_client import MCPClient, MCPServerSpec
from agent_engine.mcp_config import MCPHub, load_file_specs, load_specs
from agent_engine.mcp_servers import builtin_specs
from agent_engine.tools import build_tools_schema

from .crypto import decrypt_secret, encrypt_secret
from .models import ChatMessage, ChatSession, LLMConfig, MCPServerConfig


def index(request):
    return render(request, "chat/index.html")


def mcp_page(request):
    """独立的 MCP 管理页：比设置里的表单更适合看状态与工具清单。"""
    return render(request, "chat/mcp.html")


def architecture_page(request):
    """功能架构图：数据来自 agent_engine/architecture.py，页面只负责渲染。"""
    return render(request, "chat/architecture.html")


def architecture_data(request):
    return JsonResponse(architecture.as_dict(), json_dumps_params={"ensure_ascii": False})


def history(request):
    session_id = request.GET.get("session_id", "")
    messages = [m.payload for m in ChatMessage.objects.filter(session_id=session_id)]
    return JsonResponse({"messages": messages})


# ---------------------------------------------------------------------------
# 会话管理（左栏会话列表）
# ---------------------------------------------------------------------------

def _session_dict(session: ChatSession) -> dict:
    return {
        "session_id": session.session_id,
        "title": session.title or session.first_user_text()[:80] or "新对话",
        "message_count": session.message_count(),
        # 一轮 = 一条用户消息（内部还有 assistant/tool 消息，直接数会显得很多）
        "turn_count": ChatMessage.objects.filter(session_id=session.session_id, role="user").count(),
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
    }


def _json_body(request):
    """解析 JSON body；空 body 视为 {}，方便"新建会话"这类无参数请求。"""
    if not request.body:
        return {}
    return json.loads(request.body)


def sessions(request):
    """会话列表，最近活跃的在前。"""
    rows = ChatSession.objects.all()
    return JsonResponse({"sessions": [_session_dict(s) for s in rows]})


# 本地学习项目用 @csrf_exempt 简化；生产环境应改为 token 校验。
@csrf_exempt
def session_create(request):
    if request.method != "POST":
        return JsonResponse({"error": "仅支持 POST"}, status=405)
    try:
        data = _json_body(request)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体不是合法 JSON"}, status=400)

    title = str(data.get("title", "")).strip()[:80]
    session = ChatSession.objects.create(session_id=uuid.uuid4().hex, title=title)
    return JsonResponse({"ok": True, "session": _session_dict(session)})


@csrf_exempt
def session_rename(request):
    if request.method != "POST":
        return JsonResponse({"error": "仅支持 POST"}, status=405)
    try:
        data = _json_body(request)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体不是合法 JSON"}, status=400)

    session_id = str(data.get("session_id", "")).strip()
    title = str(data.get("title", "")).strip()[:80]
    if not session_id:
        return JsonResponse({"error": "缺少 session_id"}, status=400)

    updated = ChatSession.objects.filter(session_id=session_id).update(title=title)
    if not updated:
        return JsonResponse({"error": "会话不存在"}, status=404)
    return JsonResponse({"ok": True})


@csrf_exempt
def session_delete(request):
    if request.method != "POST":
        return JsonResponse({"error": "仅支持 POST"}, status=405)
    try:
        data = _json_body(request)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体不是合法 JSON"}, status=400)

    session_id = str(data.get("session_id", "")).strip()
    if not session_id:
        return JsonResponse({"error": "缺少 session_id"}, status=400)

    ChatSession.objects.filter(session_id=session_id).delete()
    ChatMessage.objects.filter(session_id=session_id).delete()
    return JsonResponse({"ok": True})


def tools(request):
    """工具列表由装饰器注册表自动生成，新增 @tool 后前端无需改动。

    ?with_mcp=1 时把外部 MCP 服务的工具也连上列出来（设置面板用），
    默认不连——列工具不该触发建立外部连接。
    """
    local = build_tools_schema()
    if request.GET.get("with_mcp") != "1":
        return JsonResponse({"tools": local, "local_count": len(local), "mcp_count": 0, "mcp_errors": {}})

    hub = MCPHub(timeout=15.0)
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

    session_id = str(data.get("session_id", ""))[:64].strip()
    user_text = str(data.get("message", "")).strip()
    if not user_text:
        return JsonResponse({"error": "message 不能为空"}, status=400)
    if not session_id:
        session_id = uuid.uuid4().hex

    # 会话不存在就建一个（兼容旧前端直接带 session_id 过来的情况），
    # 第一条用户消息自动成为标题
    session, created = ChatSession.objects.get_or_create(session_id=session_id)
    if not session.title:
        session.title = user_text[:80]
    session.save()  # 触发 updated_at，让会话排到左栏最前面

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
                # 最终回答只以 "done" 事件出现，之前没落库，导致刷新后答案消失、
                # 下一轮对话也看不到上一句回答。这里补存一条 assistant 消息。
                elif event["type"] == "done" and event.get("content"):
                    payload = {"role": "assistant", "content": event["content"]}
                    ChatMessage.objects.create(session_id=session_id, role="assistant", payload=payload)
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

    existing = MCPServerConfig.objects.filter(name=spec.name).first()
    # 面板回显不回传 env 值（可能含密钥）；表单留空表示"保持原样"，不是清空
    env = data.get("env") if "env" in data else (existing.env if existing else {})
    # 同理：编辑已停用的服务不应悄悄把它重新启用
    enabled = data.get("enabled", existing.enabled if existing else True)

    row, created = MCPServerConfig.objects.update_or_create(
        name=spec.name,
        defaults={
            "transport": spec.transport,
            "command": spec.command,
            "args": spec.args,
            "env": env if isinstance(env, dict) else {},
            "cwd": spec.cwd or "",
            "url": spec.url,
            "enabled": bool(enabled),
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


# ---------------------------------------------------------------------------
# MCP 管理页专用接口
# ---------------------------------------------------------------------------

def mcp_builtin(request):
    """内置本地 MCP 目录（文件系统 / SQLite），并标注是否已经添加过。"""
    installed = set(MCPServerConfig.objects.values_list("name", flat=True))
    installed |= {s.name for s in load_file_specs()}
    items = []
    for spec in builtin_specs():
        spec["installed"] = spec["name"] in installed
        items.append(spec)
    return JsonResponse({"builtins": items})


@csrf_exempt
def mcp_toggle(request):
    """启用 / 停用一个数据库里的服务；mcp.json 的只读，不能从页面停用。"""
    if request.method != "POST":
        return JsonResponse({"error": "仅支持 POST"}, status=405)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体不是合法 JSON"}, status=400)

    name = str(data.get("name", "")).strip()
    enabled = bool(data.get("enabled", True))
    row = MCPServerConfig.objects.filter(name=name).first()
    if row is None:
        return JsonResponse({"error": f"数据库里没有服务 {name}（mcp.json 里的服务请直接改文件）"}, status=404)

    row.enabled = enabled
    row.save(update_fields=["enabled"])
    return JsonResponse({"ok": True, "name": name, "enabled": enabled})


@csrf_exempt
def mcp_test_stored(request):
    """按名字测试一个已保存的服务（会带上数据库里的 env，不回传 env 本身）。"""
    if request.method != "POST":
        return JsonResponse({"error": "仅支持 POST"}, status=405)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体不是合法 JSON"}, status=400)

    name = str(data.get("name", "")).strip()
    spec = next((s for s in load_specs() if s.name == name), None)
    if spec is None:
        return JsonResponse({"ok": False, "error": "服务不存在或已停用"}, status=404)

    client = MCPClient(spec, timeout=15.0)
    try:
        remote = client.connect()
        return JsonResponse({
            "ok": True,
            "tools": [
                {
                    "name": t["_mcp"]["tool"],
                    "description": t["function"]["description"],
                    "parameters": t["function"]["parameters"],
                }
                for t in remote
            ],
        })
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)})
    finally:
        client.close()


def mcp_status(request):
    """逐个连接已启用的服务，返回状态、耗时与工具清单（管理页初次加载用）。

    串行连接：本地 2–3 个服务，每个约 1–2 秒；并发化会引入线程池与调试成本，
    暂时不值得。单个服务失败只记录，不影响其他服务。
    """
    import time

    db_names = set(MCPServerConfig.objects.values_list("name", flat=True))
    file_names = {s.name for s in load_file_specs()}

    servers = []
    for spec in load_specs():
        started = time.perf_counter()
        client = MCPClient(spec, timeout=15.0)
        item = {
            "name": spec.name,
            "transport": spec.transport,
            "command": spec.command,
            "args": spec.args,
            "url": spec.url,
            "cwd": spec.cwd or "",
            "source": "database" if spec.name in db_names else "file" if spec.name in file_names else "builtin",
            "enabled": True,
            "ok": False,
            "tools": [],
            "error": "",
        }
        try:
            tools = client.connect()
            item["ok"] = True
            item["tools"] = [
                {
                    "name": t["_mcp"]["tool"],
                    "description": t["function"]["description"],
                    "parameters": t["function"]["parameters"],
                }
                for t in tools
            ]
        except Exception as e:
            item["error"] = str(e)
        finally:
            client.close()
            item["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        servers.append(item)

    return JsonResponse({"servers": servers})
