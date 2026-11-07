"""A2A 任务执行器：把 run_agent 的事件流映射成 Task 的状态与产物。

并发模型说明（这是本文件最需要注意的地方）：
`run_agent` 是同步生成器，一次任务可能要跑十几秒。若在请求线程里直接跑，
`message/send` 与 `tasks/get` 会互相阻塞（尤其 Django 的 dev server 是多线程，
但客户端可能只开一条连接）。

所以这里给每个任务起一个**后台线程**跑 run_agent，主线程只负责：
- 建 Task、写入内存存储
- 轮询/等待状态变化
- 把事件写进任务的 history 与 artifacts

取消不是优先事项，但 `tasks/cancel` 必须诚实：只置标记，由执行循环在
下一轮迭代边界退出（不强杀线程），状态写成 canceled。
"""
import logging
import threading
import time
from collections import OrderedDict

import agent_engine.a2a_protocol as proto
from agent_engine.agent import run_agent

logger = logging.getLogger(__name__)

MAX_TASKS = 200          # 内存里最多保留多少个任务
TASK_TTL_SECONDS = 6 * 3600


class TaskStore:
    """内存任务存储。

    A2A 的 Task 语义上是服务端状态，重启丢失是可接受的（本项目是本地演示服务）。
    真正需要跨重启的话，应该把 Task 落到数据库，和 ChatSession 一起管。
    """

    def __init__(self, max_tasks: int = MAX_TASKS):
        self._tasks: OrderedDict[str, proto.A2ATask] = OrderedDict()
        self._lock = threading.RLock()
        self._max_tasks = max_tasks

    def create(self, context_id: str, message: dict) -> proto.A2ATask:
        task = proto.A2ATask(
            task_id=proto.new_id("task"),
            context_id=context_id,
            history=[message],
        )
        with self._lock:
            self._tasks[task.task_id] = task
            self._evict()
        return task

    def get(self, task_id: str) -> proto.A2ATask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, context_id: str | None = None) -> list[proto.A2ATask]:
        with self._lock:
            tasks = list(self._tasks.values())
        if context_id:
            tasks = [t for t in tasks if t.context_id == context_id]
        return sorted(tasks, key=lambda t: t.updated_at, reverse=True)

    def _evict(self) -> None:
        """优先淘汰已终结且最久未更新的任务，避免内存无限增长。"""
        if len(self._tasks) <= self._max_tasks:
            return
        now = time.time()
        for task_id in list(self._tasks):
            task = self._tasks[task_id]
            expired = (now - task.updated_at) > TASK_TTL_SECONDS
            if expired and task.terminal:
                del self._tasks[task_id]
        while len(self._tasks) > self._max_tasks:
            oldest = next(iter(self._tasks))
            del self._tasks[oldest]

    def clear(self) -> None:
        with self._lock:
            self._tasks.clear()


class TaskRunner:
    """在后台线程里跑 run_agent，把事件写进 Task。

    on_persist：可选回调，用来把对话写进 ChatSession（复用聊天历史）。
    """

    def __init__(self, store: TaskStore, llm_config: dict | None = None,
                 mcp_hub_factory=None, on_persist=None):
        self.store = store
        self.llm_config = llm_config
        self._mcp_hub_factory = mcp_hub_factory
        self._on_persist = on_persist
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    # ---------- 启动 ----------

    def start(self, task: proto.A2ATask, user_text: str, history: list[dict] | None = None) -> threading.Thread:
        thread = threading.Thread(
            target=self._run, args=(task, user_text, history or []),
            daemon=True, name=f"a2a-{task.task_id}",
        )
        with self._lock:
            self._threads[task.task_id] = thread
        thread.start()
        return thread

    # ---------- 执行 ----------

    def _run(self, task: proto.A2ATask, user_text: str, history: list[dict]) -> None:
        hub = None
        try:
            task.set_state(proto.WORKING)
            messages = list(history) + [{"role": "user", "content": user_text}]
            self._persist({"role": "user", "content": user_text})

            if self._mcp_hub_factory is not None:
                hub = self._mcp_hub_factory()

            answer_parts: list[str] = []
            for event in run_agent(messages, self.llm_config, mcp_hub=hub):
                if task.cancel_requested:
                    task.set_state(proto.CANCELED, proto.agent_message(
                        "任务已按请求取消。", task_id=task.task_id, context_id=task.context_id))
                    return

                kind = event.get("type")
                if kind == "text_delta":
                    answer_parts.append(event.get("content", ""))
                elif kind == "tool_start":
                    # 工具调用也进 history，客户端能看见 agent 在做什么
                    task.history.append(proto.agent_message(
                        f"调用工具 {event.get('name')}（{event.get('source', 'local')}）",
                        task_id=task.task_id, context_id=task.context_id))
                    task.updated_at = time.time()
                elif kind == "tool_result":
                    task.history.append(proto.agent_message(
                        f"工具返回：{str(event.get('result', ''))[:500]}",
                        task_id=task.task_id, context_id=task.context_id))
                    task.updated_at = time.time()
                elif kind == "message":
                    self._persist(event.get("message") or {})
                elif kind == "error":
                    message = event.get("message") or "执行出错"
                    task.error = message
                    task.history.append(proto.agent_message(
                        message, task_id=task.task_id, context_id=task.context_id))
                    task.set_state(proto.FAILED, proto.agent_message(
                        message, task_id=task.task_id, context_id=task.context_id))
                    return
                elif kind == "done":
                    answer = event.get("content") or "".join(answer_parts)
                    self._persist({"role": "assistant", "content": answer})
                    if answer:
                        task.artifacts.append(proto.artifact_from_text(answer))
                    task.history.append(proto.agent_message(
                        answer or "（无输出）", task_id=task.task_id, context_id=task.context_id))
                    task.set_state(proto.COMPLETED)
                    return

            # 循环正常结束但没收到 done（理论上不该发生）
            task.set_state(proto.COMPLETED)
        except Exception as exc:  # 兜底：绝不让任务永远停在 working
            logger.exception("A2A 任务 %s 执行失败", task.task_id)
            task.error = f"{type(exc).__name__}: {exc}"
            task.set_state(proto.FAILED, proto.agent_message(
                f"执行失败：{task.error}", task_id=task.task_id, context_id=task.context_id))
        finally:
            if hub is not None:
                try:
                    hub.close()
                except Exception:
                    pass
            with self._lock:
                self._threads.pop(task.task_id, None)

    def _persist(self, message: dict) -> None:
        if self._on_persist is None or not message:
            return
        try:
            self._on_persist(message)
        except Exception:  # 落库失败不该影响任务执行
            logger.exception("A2A 落库失败")

    # ---------- 等待 ----------

    def wait(self, task: proto.A2ATask, timeout: float = 120.0, poll: float = 0.1) -> proto.A2ATask:
        """阻塞直到任务终结或中断，用于 message/send（非流式）语义。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if task.terminal or task.state in proto.INTERRUPTED_STATES:
                return task
            time.sleep(poll)
        return task
