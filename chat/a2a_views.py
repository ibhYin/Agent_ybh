"""A2A（Agent2Agent）服务端接入：让别的 agent 能把本项目当 agent 调用。

和 MCP 的区别（这是本文件存在的理由）：
- MCP：对外提供**工具**，调用是离散、无状态的
- A2A：对外提供**agent**，任务是长生命周期的，可多轮、可取消、有产物

端点：
- GET  /agent-card.json     AgentCard，供发现（规范建议放在 .well-known 下）
- POST /a2a                 JSON-RPC 端点：message/send、message/stream、tasks/get 等
- GET  /a2a                 A2A v0.2.5 的流式 GET 入口（保留，返回 405 说明用法）

会话复用：A2A 的 contextId 直接映射到本项目的 ChatSession，
所以外部 agent 的对话会出现在左栏，能在页面上接着聊。
"""
import json
import logging
import time

from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

import agent_engine.a2a_protocol as proto
from agent_engine.mcp_config import MCPHub

from .crypto import decrypt_secret
from .models import ChatMessage, ChatSession, LLMConfig

logger = logging.getLogger(__name__)

# 进程内共享：所有请求复用同一份任务表
from agent_engine.a2a_server import TaskRunner, TaskStore  # noqa: E402

STORE = TaskStore()
_RUNNER: TaskRunner | None = None


def _llm_config() -> dict:
    row = LLMConfig.singleton()
    return {
        "base_url": row.base_url,
        "api_key": decrypt_secret(row.api_key_encrypted),
        "model": row.model,
    }


def _persist_factory(session_id: str):
    """把 A2A 任务的对话写进 ChatSession，这样页面上也能看到并接着聊。

    两个必须处理的细节：
    1. 后台线程用完连接要主动关。Django 的连接是线程局部的，
       线程结束后不关会一直占着 sqlite 文件锁。
    2. sqlite 在并发写时容易 database table is locked，所以加重试。
    """
    def persist(message: dict) -> None:
        if not message or not message.get("role"):
            return
        try:
            from django.db import connection, OperationalError

            for attempt in range(3):
                try:
                    ChatMessage.objects.create(
                        session_id=session_id, role=message["role"], payload=message)
                    ChatSession.objects.filter(session_id=session_id).update(
                        updated_at=timezone.now())
                    return
                except OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 2:
                        raise
                    time.sleep(0.15 * (attempt + 1))
        finally:
            from django.db import connection
            connection.close()
    return persist


_RUNNERS: dict[str, TaskRunner] = {}


def _runner_for(session_id: str) -> TaskRunner:
    """每个会话一个 runner。

    之前用一个全局 runner + "当前会话"全局变量，并发请求时会把 A 的对话
    写进 B 的会话（串话）。改成按 session_id 缓存 runner：落库回调绑定在
    runner 上，任务归属就固定了。
    """
    runner = _RUNNERS.get(session_id)
    if runner is None:
        runner = TaskRunner(
            STORE,
            llm_config=_llm_config(),
            mcp_hub_factory=lambda: MCPHub(timeout=15.0),
            on_persist=_persist_factory(session_id),
        )
        _RUNNERS[session_id] = runner
    return runner


def agent_card(request):
    """AgentCard：给外部 agent 做能力发现。"""
    row = LLMConfig.singleton()
    model = row.model or ""

    from agent_engine.tools import build_tools_schema

    tool_names = [t["function"]["name"] for t in build_tools_schema()]

    # MCP 技能无法在 AgentCard 里"连上再列工具"：拉卡片是发现阶段的轻请求，
    # 每次都 spawn 子进程既慢又吵。这里按已配置的服务名查内置目录拿到**真实工具名**，
    # 让 build_agent_card 的技能推导能匹配上（占位名匹配不到任何规则）。
    try:
        from agent_engine.mcp_config import load_specs
        from agent_engine.mcp_servers import builtin_specs

        catalog = {item["name"]: item.get("tools", []) for item in builtin_specs()}
        for spec in load_specs():
            for tool in catalog.get(spec.name, []):
                tool_names.append(f"{spec.name}__{tool}")
    except Exception as exc:  # 卡片生成不该因为配置读不出来而失败
        logger.warning("AgentCard 读取 MCP 配置失败：%s", exc)

    card = proto.build_agent_card(
        base_url=request.build_absolute_uri("/"),
        model_name=model,
        tool_names=tool_names,
    )
    return JsonResponse(card, json_dumps_params={"ensure_ascii": False})


@csrf_exempt
def a2a_endpoint(request):
    """JSON-RPC 2.0 端点。"""
    if request.method != "POST":
        return JsonResponse(
            {"error": "A2A 端点只接受 POST（JSON-RPC 2.0）；发现请访问 /agent-card.json"},
            status=405,
        )

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse(proto.rpc_error(None, proto.ERROR_PARSE, "请求体不是合法 JSON"))

    request_id = payload.get("id")
    method = payload.get("method", "")
    params = payload.get("params") or {}

    canonical = proto.METHOD_ALIASES.get(method)
    if canonical is None:
        return JsonResponse(proto.rpc_error(
            request_id, proto.ERROR_METHOD_NOT_FOUND, f"不支持的方法：{method}"))

    try:
        if canonical == "message/send":
            return _handle_send(request_id, params, streaming=False)
        if canonical == "message/stream":
            return _handle_send(request_id, params, streaming=True)
        if canonical == "tasks/get":
            return _handle_task_get(request_id, params)
        if canonical == "tasks/list":
            return _handle_task_list(request_id, params)
        if canonical == "tasks/cancel":
            return _handle_task_cancel(request_id, params)
    except Exception as exc:
        logger.exception("A2A 处理 %s 失败", method)
        return JsonResponse(proto.rpc_error(
            request_id, proto.ERROR_INTERNAL, f"{type(exc).__name__}: {exc}"))

    return JsonResponse(proto.rpc_error(
        request_id, proto.ERROR_METHOD_NOT_FOUND, f"不支持的方法：{method}"))


def _prepare_task(params: dict):
    """解析 message、确定 session（= contextId），必要时建档。"""
    message = params.get("message") or {}
    user_text = proto.extract_text(message)
    if not user_text:
        return None, None, "message 里没有可处理的文本内容"

    context_id = params.get("contextId") or message.get("contextId") or proto.new_id("ctx")

    # contextId → ChatSession：让外部 agent 的对话也能在页面上接着聊
    session, created = ChatSession.objects.get_or_create(session_id=context_id[:64])
    if created or not session.title:
        session.title = user_text[:80]
    session.save()

    # 历史：该会话已有消息，作为多轮上下文
    history = [m.payload for m in ChatMessage.objects.filter(session_id=session.session_id)]

    task = STORE.create(context_id=session.session_id, message={
        "kind": "message",
        "messageId": message.get("messageId") or proto.new_id("msg"),
        "role": "user",
        "parts": message.get("parts") or [proto.text_part(user_text)],
        "contextId": session.session_id,
    })
    return task, (user_text, history), None


def _handle_send(request_id, params: dict, streaming: bool):
    task, prepared, error = _prepare_task(params)
    if error:
        return JsonResponse(proto.rpc_error(request_id, proto.ERROR_INVALID_PARAMS, error))

    user_text, history = prepared
    runner = _runner_for(task.context_id)

    history_length = (params.get("configuration") or {}).get("historyLength")
    return_immediately = bool((params.get("configuration") or {}).get("returnImmediately"))

    if not streaming:
        runner.start(task, user_text, history)
        if return_immediately:
            return JsonResponse(proto.rpc_result(request_id, {"kind": "task", "task": task.to_dict(history_length)}))
        runner.wait(task, timeout=180)
        return JsonResponse(proto.rpc_result(request_id, {"kind": "task", "task": task.to_dict(history_length)}))

    # 流式：SSE 推送 statusUpdate / artifactUpdate，最后一条 final
    def stream():
        runner.start(task, user_text, history)
        yield f"data: {json.dumps(proto.event_task(task, history_length), ensure_ascii=False)}\n\n"
        last_state = None
        last_history_len = len(task.history)
        deadline_after = 300
        started = 0.0
        import time
        started = time.time()
        while time.time() - started < deadline_after:
            if task.cancel_requested and not task.terminal:
                task.set_state(proto.CANCELED)

            if task.state != last_state:
                last_state = task.state
                yield f"data: {json.dumps(proto.event_status(task, final=task.state in proto.TERMINAL_STATES), ensure_ascii=False)}\n\n"

            # 工具调用会追加 history，作为过程消息推给客户端
            while last_history_len < len(task.history):
                yield f"data: {json.dumps(proto.event_message(task.history[last_history_len]), ensure_ascii=False)}\n\n"
                last_history_len += 1

            if task.artifacts:
                for artifact in task.artifacts:
                    yield f"data: {json.dumps(proto.event_artifact(task, artifact), ensure_ascii=False)}\n\n"
                task.artifacts = []
                if task.terminal:
                    break

            if task.terminal:
                break
            time.sleep(0.15)

        # 收尾：确保客户端一定收到终态
        if not task.terminal:
            task.set_state(proto.FAILED, proto.agent_message(
                "任务超时未完成", task_id=task.task_id, context_id=task.context_id))
            yield f"data: {json.dumps(proto.event_status(task, final=True), ensure_ascii=False)}\n\n"

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _handle_task_get(request_id, params: dict):
    task_id = params.get("id") or params.get("taskId")
    task = STORE.get(task_id) if task_id else None
    if task is None:
        return JsonResponse(proto.rpc_error(
            request_id, proto.ERROR_TASK_NOT_FOUND, f"任务不存在：{task_id}"))
    history_length = params.get("historyLength")
    return JsonResponse(proto.rpc_result(request_id, {"kind": "task", "task": task.to_dict(history_length)}))


def _handle_task_list(request_id, params: dict):
    context_id = params.get("contextId")
    tasks = STORE.list(context_id)
    return JsonResponse(proto.rpc_result(request_id, {
        "tasks": [{"kind": "task", "task": t.to_dict()} for t in tasks],
        "count": len(tasks),
    }))


def _handle_task_cancel(request_id, params: dict):
    task_id = params.get("id") or params.get("taskId")
    task = STORE.get(task_id) if task_id else None
    if task is None:
        return JsonResponse(proto.rpc_error(
            request_id, proto.ERROR_TASK_NOT_FOUND, f"任务不存在：{task_id}"))
    if task.terminal:
        return JsonResponse(proto.rpc_error(
            request_id, proto.ERROR_TASK_NOT_CANCELABLE, f"任务已结束（{task.state}），无法取消"))

    task.cancel_requested = True
    task.set_state(proto.CANCELED, proto.agent_message(
        "任务已按请求取消。", task_id=task.task_id, context_id=task.context_id))
    return JsonResponse(proto.rpc_result(request_id, {"kind": "task", "task": task.to_dict()}))


def a2a_well_known(request):
    """规范推荐位置：/.well-known/agent-card.json，重定向到正式地址。"""
    from django.http import HttpResponseRedirect
    return HttpResponseRedirect("/agent-card.json")
