"""把已有的 ChatMessage 会话回填成 ChatSession。

改造前会话只存在于 localStorage 里，数据库只有消息行。不补齐的话，
用户升级后左栏会空空如也——历史记录还在，却看不见入口。
"""
from django.db import migrations

TITLE_MAX = 80


def backfill(apps, schema_editor):
    ChatMessage = apps.get_model("chat", "ChatMessage")
    ChatSession = apps.get_model("chat", "ChatSession")

    seen: dict[str, str] = {}
    for msg in ChatMessage.objects.order_by("created_at").iterator():
        sid = msg.session_id
        if not sid:
            continue
        if sid not in seen:
            seen[sid] = ""
        # 用第一条用户消息当标题
        if not seen[sid] and msg.role == "user":
            payload = msg.payload or {}
            text = payload.get("content") if isinstance(payload, dict) else ""
            if text:
                seen[sid] = str(text)[:TITLE_MAX]

    for sid, title in seen.items():
        ChatSession.objects.get_or_create(session_id=sid, defaults={"title": title})


def unbackfill(apps, schema_editor):
    # 回滚：删掉没有消息支撑的会话，避免留下空壳
    ChatMessage = apps.get_model("chat", "ChatMessage")
    ChatSession = apps.get_model("chat", "ChatSession")
    live_ids = set(ChatMessage.objects.values_list("session_id", flat=True).distinct())
    ChatSession.objects.exclude(session_id__in=live_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0004_chatsession"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
