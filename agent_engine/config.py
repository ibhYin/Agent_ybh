"""读取 .env 中的 LLM API 配置。

支持任何 OpenAI 兼容协议的服务（DeepSeek、Kimi、通义千问、硅基流动、vLLM、Ollama 等），
只需在 .env 里修改 BASE_URL / API_KEY / MODEL 即可切换。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def normalize_base_url(url: str) -> str:
    """有的服务要求填 https://api.xxx.com/v1，有的只填 https://api.xxx.com。

    统一补上 /v1 后缀，避免 404。
    """
    url = url.strip().rstrip("/")
    if url and not url.endswith("/v1"):
        url += "/v1"
    return url


def get_config(overrides: dict | None = None) -> dict:
    """读取 .env 配置，overrides（前端传入的配置）优先；留空的项回退到 .env。"""
    overrides = overrides or {}
    cfg = {
        "base_url": overrides.get("base_url")
        or os.getenv("BASE_URL", "").strip()
        or "https://api.openai.com/v1",
        "api_key": overrides.get("api_key") or os.getenv("API_KEY", "").strip(),
        "model": overrides.get("model") or os.getenv("MODEL", "").strip(),
    }
    cfg["base_url"] = normalize_base_url(cfg["base_url"])
    return cfg
