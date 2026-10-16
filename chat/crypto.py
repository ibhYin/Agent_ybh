"""API Key 等敏感字段的对称加密（Fernet）。

密钥从 Django SECRET_KEY 派生，零额外配置。本机单用户场景下这层加密是
纵深防御：主要目标是 key 不落浏览器、不进网络请求、不出现明文日志。
"""
import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet() -> Fernet:
    digest = hmac.new(
        settings.SECRET_KEY.encode(),
        b"agent-ybh-llm-key",
        hashlib.sha256,
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(cipher: str) -> str:
    try:
        return _fernet().decrypt(cipher.encode()).decode()
    except InvalidToken:
        return ""
