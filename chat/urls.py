from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("chat/sessions", views.sessions, name="sessions"),
    path("chat/sessions/create", views.session_create, name="session_create"),
    path("chat/sessions/rename", views.session_rename, name="session_rename"),
    path("chat/sessions/delete", views.session_delete, name="session_delete"),
    path("chat/history", views.history, name="history"),
    path("chat/tools", views.tools, name="tools"),
    path("chat/config", views.current_config, name="current_config"),
    path("chat/config-save", views.config_save, name="config_save"),
    path("chat/stream", views.chat_stream, name="chat_stream"),
    path("chat/mcp", views.mcp_list, name="mcp_list"),
    path("chat/mcp/save", views.mcp_save, name="mcp_save"),
    path("chat/mcp/delete", views.mcp_delete, name="mcp_delete"),
    path("chat/mcp/test", views.mcp_test, name="mcp_test"),
]
