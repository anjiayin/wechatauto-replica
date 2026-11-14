# -*- coding: utf-8 -*-
"""密码哈希、会话令牌、CSRF、Cookie 与客户端 IP。

不引入 passlib / bcrypt / python-jose：
    * 密码：stdlib ``hashlib.pbkdf2_hmac``（PBKDF2-HMAC-SHA256，260000 轮）；
    * 令牌：stdlib ``secrets`` 随机 + 数据库存 sha256（库泄露也无法冒充）；
    * CSRF：双提交令牌（随机 32 hex，存会话行，写请求走请求头）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from typing import Optional, Tuple

from fastapi import Request, Response

from app.config import get_config
from app.errors import ApiError

logger = logging.getLogger(__name__)

#: 哈希串前缀（格式：``pbkdf2_sha256$迭代次数$base64(salt)$base64(dk)``）
_HASH_PREFIX = "pbkdf2_sha256"
_SALT_BYTES = 16
_DK_LEN = 32
#: 密码策略：8–64 位，不含空格
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 64


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------
def hash_password(password: str, iterations: Optional[int] = None) -> str:
    """生成密码哈希串（salt 随机 → 同一密码两次结果不同）。"""
    cfg = get_config()
    rounds = int(iterations or cfg.pbkdf2_iterations)
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"),
                             salt, rounds, dklen=_DK_LEN)
    return "$".join([
        _HASH_PREFIX,
        str(rounds),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    ])


def _split_hash(stored: str) -> Optional[Tuple[int, bytes, bytes]]:
    """拆解哈希串为 ``(迭代次数, salt, dk)``；格式非法返回 None。"""
    parts = str(stored or "").split("$")
    if len(parts) != 4 or parts[0] != _HASH_PREFIX:
        return None
    try:
        rounds = int(parts[1])
        salt = base64.b64decode(parts[2])
        dk = base64.b64decode(parts[3])
    except (ValueError, TypeError):
        return None
    if rounds <= 0 or not salt or not dk:
        return None
    return rounds, salt, dk


def verify_password(password: str, stored: str) -> bool:
    """校验密码（``hmac.compare_digest`` 防时序侧信道）。"""
    parsed = _split_hash(stored)
    if parsed is None:
        return False
    rounds, salt, expected = parsed
    try:
        dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"),
                                 salt, rounds, dklen=len(expected))
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(dk, expected)


def needs_rehash(stored: str) -> bool:
    """是否需要按当前配置提升迭代次数（在线平滑升级）。"""
    parsed = _split_hash(stored)
    if parsed is None:
        return True
    return parsed[0] < get_config().pbkdf2_iterations


def dummy_verify(password: str) -> None:
    """用户名不存在时也跑一次哈希，避免用响应时间枚举账号。"""
    try:
        hash_password(str(password or "x") + "|dummy")
    except Exception:                              # 极端情况下也不影响主流程
        pass


def validate_password_policy(password: str) -> str:
    """校验密码策略并返回明文；不合规抛 400 ``invalid_param``。"""
    text = str(password or "")
    if len(text) < PASSWORD_MIN_LEN or len(text) > PASSWORD_MAX_LEN:
        raise ApiError("invalid_param",
                       f"密码长度必须为 {PASSWORD_MIN_LEN}–{PASSWORD_MAX_LEN} 位",
                       400)
    if any(ch.isspace() for ch in text):
        raise ApiError("invalid_param", "密码不能包含空格", 400)
    return text


# ---------------------------------------------------------------------------
# 会话令牌 / CSRF
# ---------------------------------------------------------------------------
def new_session_token() -> str:
    """会话令牌（43 字符 url-safe 随机串，只下发客户端，库里只存 sha256）。"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """令牌的 sha256 十六进制（``admin_session.token_hash``）。"""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    """CSRF 令牌（32 hex，双提交用）。"""
    return secrets.token_hex(16)


def compare_token(expected: str, actual: str) -> bool:
    """常量时间比较两个令牌（非 ASCII 直接判失败，避免 ``TypeError``）。"""
    left = str(expected or "")
    right = str(actual or "")
    if not left or not right:
        return False
    try:
        return hmac.compare_digest(left, right)
    except TypeError:
        return False


# ---------------------------------------------------------------------------
# Cookie
# ---------------------------------------------------------------------------
def set_session_cookie(response: Response, token: str) -> None:
    """写入会话 Cookie（HttpOnly + SameSite=Lax，Secure 由配置控制）。"""
    cfg = get_config()
    response.set_cookie(
        key=cfg.cookie_name,
        value=token,
        max_age=cfg.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=cfg.cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """清除会话 Cookie。"""
    cfg = get_config()
    response.delete_cookie(key=cfg.cookie_name, path="/",
                           httponly=True, samesite="lax",
                           secure=cfg.cookie_secure)


# ---------------------------------------------------------------------------
# 客户端 IP
# ---------------------------------------------------------------------------
def get_client_ip(request: Request) -> str:
    """取客户端 IP（``LS_TRUST_PROXY=1`` 时优先代理头）。"""
    cfg = get_config()
    if cfg.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for") or ""
        for piece in forwarded.split(","):
            candidate = piece.strip()
            if candidate:
                return candidate[:45]
        real_ip = (request.headers.get("x-real-ip") or "").strip()
        if real_ip:
            return real_ip[:45]
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") if client else ""
    return str(host or "")[:45]


def get_user_agent(request: Request) -> str:
    """取 User-Agent（截断到列宽，避免非严格模式静默截断）。"""
    return (request.headers.get("user-agent") or "")[:255]
