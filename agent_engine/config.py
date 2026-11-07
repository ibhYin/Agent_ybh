"""读取 .env 中的 LLM API 配置。

支持任何 OpenAI 兼容协议的服务（DeepSeek、Kimi、通义千问、硅基流动、vLLM、Ollama 等），
只需在 .env 里修改 BASE_URL / API_KEY / MODEL 即可切换。
"""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def normalize_base_url(url: str) -> str:
    """补全 API 地址，避免 404。

    有的服务要求填 https://api.xxx.com/v1，有的只填 https://api.xxx.com，
    还有的用版本号结尾（如火山方舟的 .../api/coding/v3）。
    因此只有在**完全看不出路径版本**时才补 /v1，否则原样保留。

    反例（真实踩过的）：把 .../api/coding/v3 补成 .../api/coding/v3/v1 → 404。
    """
    url = url.strip().rstrip("/")
    if not url:
        return url

    # 已经有版本段（/v1、/v3、/v1beta …）就不要再补
    last_segment = url.rsplit("/", 1)[-1]
    if re.fullmatch(r"v\d+[\w.-]*", last_segment, flags=re.IGNORECASE):
        return url

    return url + "/v1"


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
