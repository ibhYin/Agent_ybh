"""端点：
- GET  /                       聊天页面
- GET  /chat/history?session_id=...   加载某会话的历史消息
- GET  /chat/tools             可用工具列表（从注册表自动生成）
- GET  /chat/config            当前生效的 LLM 配置（不含 key 明文）
- POST /chat/config-save       保存 LLM 配置（key 加密存 DB，空值不覆盖）
- POST /chat/stream            发送一条消息，SSE 流式返回 agent 的全部过程
"""
import json

from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from agent_engine import config
from agent_engine.agent import run_agent
from agent_engine.tools import build_tools_schema

from .crypto import decrypt_secret, encrypt_secret
from .models import ChatMessage, LLMConfig


def index(request):
    return render(request, "chat/index.html")


def history(request):
    session_id = request.GET.get("session_id", "")
    messages = [m.payload for m in ChatMessage.objects.filter(session_id=session_id)]
    return JsonResponse({"messages": messages})


def tools(request):
    """工具列表由装饰器注册表自动生成，新增 @tool 后前端无需改动。"""
    return JsonResponse({"tools": build_tools_schema()})


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
        for event in run_agent(history_messages, llm_config):
            # "message" 事件是已完成的 OpenAI 格式消息，落库持久化（前端会忽略它）
            if event["type"] == "message":
                ChatMessage.objects.create(
                    session_id=session_id,
                    role=event["message"]["role"],
                    payload=event["message"],
                )
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingHttpResponse(event_stream(), content_type="text/event-stream")
