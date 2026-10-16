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
