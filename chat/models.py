"""对话消息持久化（sqlite）。

payload 存完整 OpenAI 格式的消息 dict（user / assistant / tool 三种角色），
读出来可直接原样发给 LLM，不需要二次转换。
"""
from django.db import models


class ChatMessage(models.Model):
    session_id = models.CharField(max_length=64, db_index=True)
    role = models.CharField(max_length=16)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class LLMConfig(models.Model):
    """LLM 配置（单例表，只存一行）。api_key 加密存储，永不回传前端。"""

    base_url = models.CharField(max_length=255, blank=True)
    api_key_encrypted = models.TextField(blank=True)  # Fernet 密文
    model = models.CharField(max_length=128, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def singleton(cls):
        return cls.objects.first() or cls.objects.create()


class MCPServerConfig(models.Model):
    """外部 MCP 服务配置（页面设置面板维护）。

    与项目根目录的 mcp.json 是两条独立的来源，合并时同名以本表为准
    （见 agent_engine/mcp_config.py::merge_specs）。
    这里不加密：配置里是启动命令和 URL，不含密钥；需要密钥的项目
    应通过 env 字段传递，而 env 在页面里不展示也不回显。
    """

    TRANSPORT_CHOICES = [("stdio", "stdio（本地进程）"), ("sse", "SSE（HTTP 长连接）")]

    name = models.CharField(max_length=32, unique=True)  # 工具名前缀，需唯一
    transport = models.CharField(max_length=8, choices=TRANSPORT_CHOICES, default="stdio")
    command = models.CharField(max_length=255, blank=True)  # stdio：启动命令
    args = models.JSONField(default=list, blank=True)       # stdio：参数数组
    env = models.JSONField(default=dict, blank=True)        # stdio：环境变量
    cwd = models.CharField(max_length=255, blank=True)      # stdio：工作目录
    url = models.CharField(max_length=512, blank=True)      # sse：端点地址
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

